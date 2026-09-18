# Presence-Routed Chime Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route each chime to whichever device the user is near — the Mac when they are at it, an Android phone over WireGuard when they are not.

**Architecture:** The existing TCP listener gains a presence probe and a broadcaster. Presence (screen unlocked + HID idle) gates local `afplay` and is stamped onto every event. A separate HTTP server fans those events out over Server-Sent Events to a browser page on the phone, which applies its own three-way toggle. The listener never learns about HTTP; the web server never learns about TCP.

**Tech Stack:** Python 3 standard library only (`socketserver`, `http.server`, `queue`, `threading`, `subprocess`), vanilla HTML/CSS/JS with `EventSource` and Web Audio. No new dependencies — the plugin currently has none and must keep it that way.

**Spec:** `docs/superpowers/specs/2026-09-17-presence-routed-chime-design.md`

## Global Constraints

- **No third-party dependencies.** Python standard library only. The plugin installs on three machines with no pip step.
- **Tests run as plain scripts:** `python3 tests/test_x.py`, using `unittest`. Match the existing pattern in `tests/test_chime_listener.py`.
- **Never bind `0.0.0.0`.** Bind exactly one configured address. Default `127.0.0.1`; `10.66.66.2` reaches the phone.
- **Everything defaults off.** `web_enabled` and `presence_enabled` default `false`. Updating the plugin must change no existing behaviour.
- **Presence fails open.** Any probe error returns `True` so the Mac plays, preserving today's behaviour rather than going silently quiet.
- **No SSE replay.** `Last-Event-ID` is ignored. A phone waking from Doze must not fire a burst of stale chimes.
- **Mute outranks everything.** Enforced on the sending host; no change needed here, but no task may add a bypass.
- Sound files are 16-bit PCM mono WAV: melodies 44.1 kHz (~100–145 KB), speech 16 kHz (~60 KB).
- Existing module is `hooks/chime-listener.py` — a hyphen, so it is imported in tests via `importlib.util.spec_from_file_location`, not `import`.

---

### Task 1: Presence detection

**Files:**
- Create: `hooks/presence.py`
- Test: `tests/test_presence.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `parse_idle_seconds(ioreg_text: str) -> float | None`
  - `parse_screen_locked(ioreg_text: str) -> bool`
  - `probe_macos() -> dict` with keys `idle` (float|None), `locked` (bool), `user` (str)
  - `mac_active(idle_threshold: int = 300, probe=probe_macos) -> bool`
  - `PresenceCache(ttl: float = 2.0, idle_threshold: int = 300, probe=probe_macos)` with method `active() -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_presence.py`:

```python
#!/usr/bin/env python3
"""Tests for macOS presence detection. Run: python3 tests/test_presence.py"""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("presence", ROOT / "hooks" / "presence.py")
presence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(presence)

# Captured from `ioreg -c IOHIDSystem` on macOS 25.3.
IOREG_IDLE_10S = '''
    | |   "HIDIdleTime" = 10163847000
    | |   "HIDPointerAcceleration" = 786432
'''
IOREG_IDLE_600S = '''
    | |   "HIDIdleTime" = 600000000000
'''
IOREG_LOCKED = '''
    | | "CGSSessionScreenIsLocked" = Yes
    | | "kCGSSessionOnConsoleKey" = Yes
'''
IOREG_UNLOCKED = '''
    | | "kCGSSessionOnConsoleKey" = Yes
'''


def fake_probe(idle=10.0, locked=False, user="dattavani"):
    return lambda: {"idle": idle, "locked": locked, "user": user}


class ParseIdle(unittest.TestCase):
    def test_reads_seconds_from_nanoseconds(self):
        self.assertAlmostEqual(presence.parse_idle_seconds(IOREG_IDLE_10S), 10.163847, places=3)

    def test_reads_a_long_idle(self):
        self.assertAlmostEqual(presence.parse_idle_seconds(IOREG_IDLE_600S), 600.0, places=3)

    def test_missing_key_returns_none(self):
        self.assertIsNone(presence.parse_idle_seconds("no such key here"))

    def test_garbage_returns_none(self):
        self.assertIsNone(presence.parse_idle_seconds('"HIDIdleTime" = banana'))


class ParseLocked(unittest.TestCase):
    def test_locked_when_key_present_and_yes(self):
        self.assertTrue(presence.parse_screen_locked(IOREG_LOCKED))

    def test_unlocked_when_key_absent(self):
        self.assertFalse(presence.parse_screen_locked(IOREG_UNLOCKED))

    def test_unlocked_on_empty_output(self):
        self.assertFalse(presence.parse_screen_locked(""))


class MacActive(unittest.TestCase):
    def test_active_when_unlocked_and_recently_used(self):
        self.assertTrue(presence.mac_active(300, fake_probe(idle=10.0)))

    def test_inactive_when_idle_beyond_threshold(self):
        self.assertFalse(presence.mac_active(300, fake_probe(idle=600.0)))

    def test_inactive_when_locked(self):
        self.assertFalse(presence.mac_active(300, fake_probe(idle=1.0, locked=True)))

    def test_inactive_when_no_console_user(self):
        self.assertFalse(presence.mac_active(300, fake_probe(user="root")))

    def test_boundary_at_threshold_is_inactive(self):
        self.assertFalse(presence.mac_active(300, fake_probe(idle=300.0)))

    def test_fails_open_when_probe_raises(self):
        def boom():
            raise OSError("ioreg missing")
        self.assertTrue(presence.mac_active(300, boom))

    def test_fails_open_when_idle_unreadable(self):
        self.assertTrue(presence.mac_active(300, fake_probe(idle=None)))


class Cache(unittest.TestCase):
    def test_probes_once_within_ttl(self):
        calls = []

        def counting():
            calls.append(1)
            return {"idle": 10.0, "locked": False, "user": "dattavani"}

        cache = presence.PresenceCache(ttl=60.0, idle_threshold=300, probe=counting)
        cache.active()
        cache.active()
        cache.active()
        self.assertEqual(len(calls), 1)

    def test_reprobes_after_ttl_expires(self):
        calls = []
        clock = [0.0]

        def counting():
            calls.append(1)
            return {"idle": 10.0, "locked": False, "user": "dattavani"}

        cache = presence.PresenceCache(ttl=2.0, idle_threshold=300, probe=counting,
                                       clock=lambda: clock[0])
        cache.active()
        clock[0] = 5.0
        cache.active()
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_presence.py`
Expected: FAIL — `FileNotFoundError` / `No such file or directory: hooks/presence.py`

- [ ] **Step 3: Write minimal implementation**

Create `hooks/presence.py`:

```python
#!/usr/bin/env python3
"""Is the user actually at this Mac?

Used to decide whether a chime should play here or be handed to the phone.
Reads two IOKit values and the console user. Needs no special privileges and
works from a launchd GUI agent.

Every failure path returns True. Failing toward "play locally" preserves the
behaviour this plugin had before presence existed, rather than going silently
quiet because ioreg changed its output format.
"""

import re
import subprocess
import time

IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')
LOCKED_RE = re.compile(r'"CGSSessionScreenIsLocked"\s*=\s*(Yes|True|1)', re.IGNORECASE)
NOBODY = ("", "root", "_mbsetupuser", "loginwindow")


def parse_idle_seconds(ioreg_text):
    """HIDIdleTime is nanoseconds since the last input event."""
    match = IDLE_RE.search(ioreg_text or "")
    if not match:
        return None
    try:
        return int(match.group(1)) / 1_000_000_000
    except ValueError:
        return None


def parse_screen_locked(ioreg_text):
    """The key is absent entirely when the screen is unlocked."""
    return bool(LOCKED_RE.search(ioreg_text or ""))


def _run(args):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=5, check=False
    ).stdout


def probe_macos():
    return {
        "idle": parse_idle_seconds(_run(["ioreg", "-c", "IOHIDSystem"])),
        "locked": parse_screen_locked(
            _run(["ioreg", "-n", "Root", "-d1", "-r", "-k", "CGSSessionScreenIsLocked"])
        ),
        "user": _run(["stat", "-f%Su", "/dev/console"]).strip(),
    }


def mac_active(idle_threshold=300, probe=probe_macos):
    """True when someone is logged in, unlocked, and has touched the machine."""
    try:
        state = probe()
        idle = state.get("idle")
        if idle is None:
            return True
        if state.get("user", "").strip() in NOBODY:
            return False
        if state.get("locked"):
            return False
        return idle < idle_threshold
    except Exception:
        return True


class PresenceCache:
    """Caches the answer briefly so a burst of chimes is not a burst of ioregs."""

    def __init__(self, ttl=2.0, idle_threshold=300, probe=probe_macos, clock=time.monotonic):
        self.ttl = ttl
        self.idle_threshold = idle_threshold
        self.probe = probe
        self.clock = clock
        self._value = None
        self._at = None

    def active(self):
        now = self.clock()
        if self._at is None or (now - self._at) >= self.ttl:
            self._value = mac_active(self.idle_threshold, self.probe)
            self._at = now
        return self._value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_presence.py`
Expected: PASS — 16 tests OK

- [ ] **Step 5: Sanity-check against the real machine**

Run: `python3 -c "import importlib.util,pathlib; s=importlib.util.spec_from_file_location('p', 'hooks/presence.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.probe_macos()); print('active:', m.mac_active())"`
Expected: a dict with a small `idle`, `locked: False`, your username, and `active: True`.

- [ ] **Step 6: Commit**

```bash
git add hooks/presence.py tests/test_presence.py
git commit -m "Add macOS presence detection that fails open"
```

---

### Task 2: Event broadcaster

**Files:**
- Create: `hooks/broadcast.py`
- Test: `tests/test_broadcast.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Broadcaster(depth: int = 8)` with methods:
    - `subscribe() -> queue.Queue`
    - `unsubscribe(q: queue.Queue) -> None`
    - `publish(payload: dict) -> int` (number of subscribers delivered to)
    - `subscriber_count() -> int`
    - attribute `dropped: int`

- [ ] **Step 1: Write the failing test**

Create `tests/test_broadcast.py`:

```python
#!/usr/bin/env python3
"""Tests for the SSE fan-out. Run: python3 tests/test_broadcast.py"""

import importlib.util
import queue
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("broadcast", ROOT / "hooks" / "broadcast.py")
broadcast = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broadcast)


class Fanout(unittest.TestCase):
    def setUp(self):
        self.b = broadcast.Broadcaster()

    def test_no_subscribers_is_not_an_error(self):
        self.assertEqual(self.b.publish({"a": 1}), 0)

    def test_one_subscriber_receives(self):
        q = self.b.subscribe()
        self.b.publish({"label": "s1"})
        self.assertEqual(q.get_nowait(), {"label": "s1"})

    def test_every_subscriber_receives_the_same_event(self):
        a, c = self.b.subscribe(), self.b.subscribe()
        self.assertEqual(self.b.publish({"label": "s2"}), 2)
        self.assertEqual(a.get_nowait()["label"], "s2")
        self.assertEqual(c.get_nowait()["label"], "s2")

    def test_unsubscribe_stops_delivery(self):
        q = self.b.subscribe()
        self.b.unsubscribe(q)
        self.assertEqual(self.b.publish({"label": "s1"}), 0)

    def test_unsubscribing_twice_is_harmless(self):
        q = self.b.subscribe()
        self.b.unsubscribe(q)
        self.b.unsubscribe(q)
        self.assertEqual(self.b.subscriber_count(), 0)

    def test_slow_subscriber_drops_instead_of_blocking(self):
        b = broadcast.Broadcaster(depth=2)
        b.subscribe()
        for i in range(10):
            b.publish({"n": i})
        self.assertEqual(b.dropped, 8)

    def test_a_slow_subscriber_does_not_starve_a_fast_one(self):
        b = broadcast.Broadcaster(depth=1)
        slow = b.subscribe()
        fast = b.subscribe()
        b.publish({"n": 1})
        fast.get_nowait()          # fast one drains
        b.publish({"n": 2})        # slow one is full, fast one is not
        self.assertEqual(fast.get_nowait()["n"], 2)
        self.assertEqual(slow.get_nowait()["n"], 1)

    def test_concurrent_publish_is_safe(self):
        q = self.b.subscribe()
        threads = [threading.Thread(target=self.b.publish, args=({"n": i},)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        received = 0
        while True:
            try:
                q.get_nowait()
                received += 1
            except queue.Empty:
                break
        self.assertEqual(received + self.b.dropped, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_broadcast.py`
Expected: FAIL — `hooks/broadcast.py` does not exist

- [ ] **Step 3: Write minimal implementation**

Create `hooks/broadcast.py`:

```python
#!/usr/bin/env python3
"""Fan one chime out to every connected browser.

Each subscriber gets its own bounded queue. A phone that has wandered out of
WireGuard range must never stall the Mac's afplay, so publish drops rather than
blocks when a queue is full.
"""

import queue
import threading


class Broadcaster:
    def __init__(self, depth=8):
        self.depth = depth
        self.dropped = 0
        self._subscribers = []
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=self.depth)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)

    def publish(self, payload):
        with self._lock:
            targets = list(self._subscribers)
        delivered = 0
        for q in targets:
            try:
                q.put_nowait(payload)
                delivered += 1
            except queue.Full:
                with self._lock:
                    self.dropped += 1
        return delivered
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_broadcast.py`
Expected: PASS — 8 tests OK

- [ ] **Step 5: Commit**

```bash
git add hooks/broadcast.py tests/test_broadcast.py
git commit -m "Add bounded-queue broadcaster for chime fan-out"
```

---

### Task 3: SSE and sound-serving HTTP server

**Files:**
- Create: `hooks/chime_web.py`
- Test: `tests/test_chime_web.py`

**Interfaces:**
- Consumes: `Broadcaster` from Task 2 (`subscribe`, `unsubscribe`, `publish`).
- Produces:
  - `sse_frame(payload: dict) -> bytes`
  - `safe_sound_path(sounds_dir: Path, rel: str) -> Path | None`
  - `make_handler(sounds_dir, broadcaster, page_path, log, heartbeat=20.0) -> class`
  - `serve(bind, port, sounds_dir, broadcaster, page_path, log) -> http.server.ThreadingHTTPServer` (already started on a daemon thread)

- [ ] **Step 1: Write the failing test**

Create `tests/test_chime_web.py`:

```python
#!/usr/bin/env python3
"""Tests for the SSE web server. Run: python3 tests/test_chime_web.py"""

import importlib.util
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "hooks" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


broadcast = load("broadcast", "broadcast.py")
web = load("chime_web", "chime_web.py")


def build_sounds(base):
    base = Path(base)
    for theme in ("dnd", "classical"):
        (base / theme).mkdir(parents=True)
        (base / theme / f"{theme}_one.wav").write_bytes(b"RIFF----WAVEfmt ")
    speech = base / "speech" / "us" / "male"
    speech.mkdir(parents=True)
    (speech / "stop_0.wav").write_bytes(b"RIFF----WAVEfmt ")
    return base


class Framing(unittest.TestCase):
    def test_frame_is_data_line_then_blank_line(self):
        frame = web.sse_frame({"label": "s1"})
        self.assertTrue(frame.startswith(b"data: "))
        self.assertTrue(frame.endswith(b"\n\n"))

    def test_frame_round_trips_as_json(self):
        payload = {"label": "s1", "theme": "dnd", "mac_active": False}
        line = web.sse_frame(payload).decode().split("data: ", 1)[1].strip()
        self.assertEqual(json.loads(line), payload)

    def test_newlines_in_values_cannot_break_the_frame(self):
        frame = web.sse_frame({"label": "a\nb"})
        self.assertEqual(frame.count(b"\n\n"), 1)


class SafePath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sounds = build_sounds(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolves_a_real_file(self):
        self.assertIsNotNone(web.safe_sound_path(self.sounds, "dnd/dnd_one.wav"))

    def test_resolves_a_nested_speech_file(self):
        self.assertIsNotNone(web.safe_sound_path(self.sounds, "speech/us/male/stop_0.wav"))

    def test_rejects_parent_traversal(self):
        self.assertIsNone(web.safe_sound_path(self.sounds, "../../etc/passwd"))

    def test_rejects_encoded_traversal(self):
        self.assertIsNone(web.safe_sound_path(self.sounds, "dnd/../../../etc/passwd"))

    def test_rejects_absolute_path(self):
        self.assertIsNone(web.safe_sound_path(self.sounds, "/etc/passwd"))

    def test_rejects_non_wav(self):
        (self.sounds / "secret.txt").write_text("nope")
        self.assertIsNone(web.safe_sound_path(self.sounds, "secret.txt"))

    def test_rejects_missing_file(self):
        self.assertIsNone(web.safe_sound_path(self.sounds, "dnd/absent.wav"))


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sounds = build_sounds(self.tmp.name)
        self.page = Path(self.tmp.name) / "index.html"
        self.page.write_text("<!doctype html><title>claw-bell</title>")
        self.logs = []
        self.b = broadcast.Broadcaster()
        self.server = web.serve("127.0.0.1", 0, self.sounds, self.b,
                                self.page, self.logs.append)
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_serves_the_page(self):
        with urllib.request.urlopen(self.url("/"), timeout=5) as r:
            self.assertIn(b"claw-bell", r.read())

    def test_serves_a_wav_with_the_right_type(self):
        with urllib.request.urlopen(self.url("/sounds/dnd/dnd_one.wav"), timeout=5) as r:
            self.assertEqual(r.headers["Content-Type"], "audio/wav")
            self.assertTrue(r.read().startswith(b"RIFF"))

    def test_traversal_over_http_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.url("/sounds/../../etc/passwd"), timeout=5)
        self.assertEqual(cm.exception.code, 404)

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.url("/admin"), timeout=5)
        self.assertEqual(cm.exception.code, 404)

    def test_events_stream_delivers_a_published_chime(self):
        received = []

        def reader():
            req = urllib.request.Request(self.url("/events"))
            with urllib.request.urlopen(req, timeout=10) as r:
                for raw in r:
                    if raw.startswith(b"data: "):
                        received.append(json.loads(raw[6:].decode()))
                        return

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.5)                      # let the subscription land
        self.b.publish({"label": "s1", "theme": "dnd"})
        t.join(timeout=5)
        self.assertEqual(received, [{"label": "s1", "theme": "dnd"}])

    def test_events_sets_stream_headers(self):
        req = urllib.request.Request(self.url("/events"))
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.headers["Content-Type"], "text/event-stream")
            self.assertEqual(r.headers["Cache-Control"], "no-cache")
            self.assertEqual(r.headers["X-Accel-Buffering"], "no")

    def test_last_event_id_produces_no_replay(self):
        self.b.publish({"label": "old", "theme": "dnd"})   # before anyone connects
        received = []

        def reader():
            req = urllib.request.Request(self.url("/events"), headers={"Last-Event-ID": "1"})
            with urllib.request.urlopen(req, timeout=4) as r:
                for raw in r:
                    if raw.startswith(b"data: "):
                        received.append(json.loads(raw[6:].decode()))
                        return

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(1.0)
        t.join(timeout=2)
        self.assertEqual(received, [])      # nothing stale replayed

    def test_heartbeat_is_a_named_event_the_page_can_see(self):
        """A bare ': hb' comment is invisible to EventSource, so it must be named."""
        server = web.serve("127.0.0.1", 0, self.sounds, self.b, self.page,
                           self.logs.append, heartbeat=0.3)
        port = server.server_address[1]
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/events")
            with urllib.request.urlopen(req, timeout=5) as r:
                lines = [r.readline() for _ in range(2)]
            self.assertEqual(lines[0], b"event: hb\n")
            self.assertEqual(lines[1], b"data: {}\n")
        finally:
            server.shutdown()
            server.server_close()

    def test_disconnect_unsubscribes(self):
        def reader():
            req = urllib.request.Request(self.url("/events"))
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read(1)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.5)
        self.b.publish({"label": "s1"})     # unblocks the read, client closes
        t.join(timeout=5)
        deadline = time.time() + 5
        while time.time() < deadline and self.b.subscriber_count() > 0:
            self.b.publish({"label": "probe"})
            time.sleep(0.2)
        self.assertEqual(self.b.subscriber_count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_chime_web.py`
Expected: FAIL — `hooks/chime_web.py` does not exist

- [ ] **Step 3: Write minimal implementation**

Create `hooks/chime_web.py`:

```python
#!/usr/bin/env python3
"""Serve the chime stream to browsers over Server-Sent Events.

Knows nothing about the TCP protocol or about playback. It takes a Broadcaster
and turns it into three HTTP routes:

    GET /                        the page
    GET /events                  SSE stream
    GET /sounds/<theme>/<f>.wav  the audio

SSE rather than WebSocket: the traffic is one-way, EventSource reconnects by
itself when a phone sleeps and wakes, and it needs no dependency and no
protocol upgrade through a proxy.

Last-Event-ID is deliberately ignored. A phone waking from Doze must rejoin a
live stream, not replay a burst of stale chimes.
"""

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HEARTBEAT_SECONDS = 20.0


def sse_frame(payload):
    """One SSE event. json.dumps escapes newlines, so values cannot break framing."""
    return b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"


def safe_sound_path(sounds_dir, rel):
    """Resolve a request path inside sounds/, or None if it escapes or is missing."""
    sounds_dir = Path(sounds_dir).resolve()
    candidate = (sounds_dir / unquote(rel).lstrip("/")).resolve()
    if not candidate.is_relative_to(sounds_dir):
        return None
    if candidate.suffix.lower() != ".wav":
        return None
    if not candidate.is_file():
        return None
    return candidate


def make_handler(sounds_dir, broadcaster, page_path, log, heartbeat=HEARTBEAT_SECONDS):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass                      # keep the listener log readable

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                self._page()
            elif path == "/events":
                self._events()
            elif path.startswith("/sounds/"):
                self._sound(path[len("/sounds/"):])
            else:
                self.send_error(404)

        def _page(self):
            try:
                body = Path(page_path).read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _sound(self, rel):
            resolved = safe_sound_path(sounds_dir, rel)
            if resolved is None:
                self.send_error(404)
                return
            body = resolved.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")   # never let a proxy buffer us
            self.end_headers()

            q = broadcaster.subscribe()
            peer = self.client_address[0]
            log(f"sse: {peer} connected ({broadcaster.subscriber_count()} total)")
            try:
                while True:
                    try:
                        payload = q.get(timeout=heartbeat)
                        self.wfile.write(sse_frame(payload))
                    except queue.Empty:
                        # A named event, not a bare ": hb" comment. EventSource
                        # never surfaces comments to JavaScript, so the page
                        # could not use one to tell idle from stalled.
                        self.wfile.write(b"event: hb\ndata: {}\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                pass
            finally:
                broadcaster.unsubscribe(q)
                log(f"sse: {peer} disconnected ({broadcaster.subscriber_count()} left)")

    return Handler


def serve(bind, port, sounds_dir, broadcaster, page_path, log, heartbeat=HEARTBEAT_SECONDS):
    """Start the server on a daemon thread and return it."""
    class Server(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server((bind, port),
                    make_handler(sounds_dir, broadcaster, page_path, log, heartbeat))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"web: serving on http://{bind}:{server.server_address[1]}")
    return server
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_chime_web.py`
Expected: PASS — 17 tests OK

Note: `Path.is_relative_to` requires Python 3.9+. Both servers and the Mac run 3.9 or newer; verify with `python3 -V` if a test errors on that line.

- [ ] **Step 5: Commit**

```bash
git add hooks/chime_web.py tests/test_chime_web.py
git commit -m "Add SSE server for browser chime delivery"
```

---

### Task 4: The browser page

**Files:**
- Create: `web/index.html`
- Test: manual (browser), plus Task 3's `test_serves_the_page`

**Interfaces:**
- Consumes: `GET /events` (SSE), `GET /sounds/...` from Task 3. Event fields: `id`, `ts`, `event`, `theme`, `label`, `melody`, `speech`, `mac_active`.
- Produces: nothing other tasks import.

Consider loading the `frontend-design` skill before this task — it is the only user-facing surface in the plan.

- [ ] **Step 1: Write the page**

Create `web/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>claw-bell</title>
<style>
  :root {
    --bg: #14161a; --fg: #e8eaed; --dim: #9aa0a6;
    --ok: #34a853; --warn: #fbbc04; --bad: #ea4335; --line: #2a2e35;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
    font: 16px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
    -webkit-user-select: none; user-select: none;
  }
  header { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; letter-spacing: -0.01em; }
  #dot { width: 10px; height: 10px; border-radius: 50%; background: var(--dim); flex: none; }
  #dot.ok { background: var(--ok); } #dot.warn { background: var(--warn); }
  #dot.bad { background: var(--bad); }
  #status { color: var(--dim); font-size: 13px; margin-left: auto; }
  button {
    font: inherit; color: var(--fg); background: #22262e; border: 1px solid var(--line);
    border-radius: 10px; padding: 12px 16px; cursor: pointer;
  }
  #arm { width: 100%; padding: 18px; font-size: 17px; font-weight: 600; margin-bottom: 16px; }
  #arm.armed { display: none; }
  fieldset { border: 1px solid var(--line); border-radius: 10px; margin: 0 0 16px; padding: 12px; }
  legend { color: var(--dim); font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }
  .modes { display: flex; gap: 8px; }
  .modes button { flex: 1; }
  .modes button[aria-pressed="true"] { background: #2f6fed; border-color: #2f6fed; }
  label.keep { display: flex; align-items: center; gap: 8px; color: var(--dim); font-size: 14px; margin-top: 12px; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { display: flex; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--line); }
  .host { font-weight: 600; min-width: 4em; }
  .meta { color: var(--dim); font-size: 13px; }
  .time { margin-left: auto; color: var(--dim); font-size: 13px; font-variant-numeric: tabular-nums; }
  .empty { color: var(--dim); padding: 24px 0; text-align: center; }
</style>
</head>
<body>
<header>
  <span id="dot"></span>
  <h1>claw-bell</h1>
  <span id="status">connecting…</span>
</header>

<button id="arm">Tap to enable sound</button>

<fieldset>
  <legend>Chime on this device</legend>
  <div class="modes">
    <button data-mode="always" aria-pressed="false">Always</button>
    <button data-mode="away"   aria-pressed="true">When away from Mac</button>
    <button data-mode="off"    aria-pressed="false">Off</button>
  </div>
  <label class="keep">
    <input type="checkbox" id="keepalive">
    Keep playing with the screen off (Android; shows a media notification)
  </label>
</fieldset>

<ul id="log"><li class="empty">No chimes yet</li></ul>

<script>
(() => {
  "use strict";
  const dot = document.getElementById("dot");
  const statusEl = document.getElementById("status");
  const armBtn = document.getElementById("arm");
  const logEl = document.getElementById("log");
  const keepBox = document.getElementById("keepalive");

  let ctx = null, armed = false, lastBeat = 0, keeper = null;
  let mode = load("mode", "away");
  const cache = new Map();

  function load(k, d) { try { return localStorage.getItem("clawbell." + k) ?? d; } catch { return d; } }
  function save(k, v) { try { localStorage.setItem("clawbell." + k, v); } catch {} }

  // --- mode toggle ---
  document.querySelectorAll(".modes button").forEach(b => {
    b.addEventListener("click", () => { mode = b.dataset.mode; save("mode", mode); paintModes(); });
  });
  function paintModes() {
    document.querySelectorAll(".modes button").forEach(b =>
      b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
  }
  paintModes();

  // --- arming: browsers block audio until a gesture ---
  armBtn.addEventListener("click", async () => {
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    await ctx.resume();
    armed = true;
    armBtn.classList.add("armed");
    if (keepBox.checked) startKeepAlive();
  });

  // --- Android: a silent loop + MediaSession keeps the tab alive screen-off ---
  keepBox.checked = load("keep", "0") === "1";
  keepBox.addEventListener("change", () => {
    save("keep", keepBox.checked ? "1" : "0");
    keepBox.checked && armed ? startKeepAlive() : stopKeepAlive();
  });
  function startKeepAlive() {
    if (keeper) return;
    keeper = new Audio("data:audio/wav;base64,UklGRigAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQQAAAAAAAAA");
    keeper.loop = true; keeper.volume = 0.0001;
    keeper.play().catch(() => {});
    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: "claw-bell", artist: "listening" });
      navigator.mediaSession.playbackState = "playing";
    }
  }
  function stopKeepAlive() {
    if (!keeper) return;
    keeper.pause(); keeper = null;
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "none";
  }

  // --- audio ---
  async function buffer(url) {
    if (cache.has(url)) return cache.get(url);
    const p = fetch(url).then(r => r.arrayBuffer()).then(b => ctx.decodeAudioData(b));
    cache.set(url, p);
    return p;
  }
  async function play(urls) {
    let at = ctx.currentTime;
    for (const url of urls.filter(Boolean)) {
      try {
        const buf = await buffer(url);
        const src = ctx.createBufferSource();
        src.buffer = buf;
        src.connect(ctx.destination);       // ducks over other audio rather than suppressing
        src.start(at);
        at += buf.duration;
      } catch { /* a missing sound must not break the stream */ }
    }
  }

  function shouldPlay(ev) {
    if (!armed || mode === "off") return false;
    if (mode === "always") return true;
    return ev.mac_active === false;         // "away": only when the Mac is idle
  }

  // --- event list ---
  function add(ev) {
    const empty = logEl.querySelector(".empty");
    if (empty) empty.remove();
    const li = document.createElement("li");
    const t = new Date(ev.ts || Date.now());
    li.innerHTML =
      `<span class="host"></span><span class="meta"></span><span class="time"></span>`;
    li.querySelector(".host").textContent = ev.label || "?";
    li.querySelector(".meta").textContent =
      `${ev.event === "stop" ? "finished" : "needs input"} · ${ev.theme || ""}`;
    li.querySelector(".time").textContent = t.toLocaleTimeString();
    logEl.prepend(li);
    while (logEl.children.length > 50) logEl.lastElementChild.remove();
  }

  // --- connection + staleness ---
  function setDot(cls, text) { dot.className = cls; statusEl.textContent = text; }

  const es = new EventSource("/events");
  es.onopen = () => { lastBeat = Date.now(); setDot("ok", "connected"); };
  es.onerror = () => setDot("bad", "disconnected — retrying");
  es.onmessage = (m) => {
    lastBeat = Date.now();
    setDot("ok", "connected");
    let ev; try { ev = JSON.parse(m.data); } catch { return; }
    add(ev);
    if (shouldPlay(ev)) play([ev.melody, ev.speech]);
  };
  // A comment-only heartbeat does not fire onmessage, so watch the clock instead.
  setInterval(() => {
    if (!lastBeat) return;
    const age = Date.now() - lastBeat;
    if (es.readyState === 2) setDot("bad", "disconnected — retrying");
    else if (age > 30000) setDot("warn", "no heartbeat — may be stale");
  }, 5000);
  // Any byte, including a heartbeat comment, resets the clock via the raw stream.
  es.addEventListener("hb", () => { lastBeat = Date.now(); });
})();
</script>
</body>
</html>
```

- [ ] **Step 2: Verify it is served and parses**

Run:
```bash
python3 - <<'PY'
import importlib.util, pathlib, tempfile, threading, time, urllib.request
ROOT = pathlib.Path(".")
def load(n, f):
    s = importlib.util.spec_from_file_location(n, ROOT / "hooks" / f)
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
b = load("broadcast", "broadcast.py").Broadcaster()
web = load("chime_web", "chime_web.py")
srv = web.serve("127.0.0.1", 0, ROOT / "sounds", b, ROOT / "web" / "index.html", print)
port = srv.server_address[1]
body = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
assert "claw-bell" in body and "EventSource" in body, "page not served correctly"
print("page OK,", len(body), "bytes")
srv.shutdown()
PY
```
Expected: `page OK, <n> bytes`

- [ ] **Step 3: Commit**

```bash
git add web/index.html
git commit -m "Add browser page with arming, routing toggle and staleness indicator"
```

---

### Task 5: Wire presence and broadcasting into the listener

**Files:**
- Modify: `hooks/chime-listener.py` — imports at top (after line 37), handler at lines 207-215, `main()` at lines 220-258
- Test: `tests/test_chime_listener.py` (extend)

**Interfaces:**
- Consumes: `presence.PresenceCache` (Task 1), `broadcast.Broadcaster` (Task 2), `chime_web.serve` (Task 3).
- Produces:
  - `build_event(event, theme, label, tracks, sounds_dir, mac_active, seq, now) -> dict`
  - `make_handler(sounds_dir, config, player, log, broadcaster=None, presence=None)` — two new optional keyword arguments; existing callers keep working.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_chime_listener.py`, before the `if __name__` block:

```python
class BuildEvent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sounds = build_sounds(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_maps_tracks_to_sound_urls(self):
        tracks = chime.resolve(self.sounds, "stop", "dnd", {})
        ev = chime.build_event("stop", "dnd", "s1", tracks, self.sounds, True, 7, "2026-09-17T14:00:00Z")
        self.assertEqual(ev["melody"], "/sounds/dnd/dnd_one.wav")
        self.assertEqual(ev["speech"], "/sounds/speech/us/male/stop_0.wav")

    def test_carries_identity_and_presence(self):
        tracks = chime.resolve(self.sounds, "stop", "dnd", {})
        ev = chime.build_event("stop", "dnd", "s1", tracks, self.sounds, False, 7, "2026-09-17T14:00:00Z")
        self.assertEqual(ev["label"], "s1")
        self.assertEqual(ev["event"], "stop")
        self.assertEqual(ev["theme"], "dnd")
        self.assertEqual(ev["mac_active"], False)
        self.assertEqual(ev["id"], 7)
        self.assertEqual(ev["ts"], "2026-09-17T14:00:00Z")

    def test_speech_is_none_when_only_a_melody_played(self):
        tracks = chime.resolve(self.sounds, "stop", "dnd", {"mode": "sound_only"})
        ev = chime.build_event("stop", "dnd", "s1", tracks, self.sounds, True, 1, "t")
        self.assertIsNone(ev["speech"])


class PresenceRouting(unittest.TestCase):
    """The Mac plays only when someone is at it; the event always goes out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sounds = build_sounds(self.tmp.name)
        self.logs = []
        self.player = chime.Player(command="/usr/bin/true", log=self.logs.append)
        self.published = []

        class FakeBroadcaster:
            def __init__(self, sink):
                self.sink = sink
            def publish(self, payload):
                self.sink.append(payload)
                return 1

        self.broadcaster = FakeBroadcaster(self.published)

    def tearDown(self):
        self.tmp.cleanup()

    def serve_with(self, present):
        class FakePresence:
            def active(self_inner):
                return present

        handler = chime.make_handler(self.sounds, {}, self.player, self.logs.append,
                                     broadcaster=self.broadcaster, presence=FakePresence())

        class Server(chime.socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_address[1]

    def send(self, port, line):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(line.encode())
            s.recv(16)
        time.sleep(0.3)

    def test_plays_locally_when_present(self):
        port = self.serve_with(True)
        self.send(port, "stop|dnd|s1\n")
        self.assertEqual(len(self.player.played), 2)
        self.server.shutdown()

    def test_stays_silent_locally_when_absent(self):
        port = self.serve_with(False)
        self.send(port, "stop|dnd|s1\n")
        self.assertEqual(self.player.played, [])
        self.server.shutdown()

    def test_broadcasts_regardless_of_presence(self):
        port = self.serve_with(False)
        self.send(port, "stop|dnd|s1\n")
        self.assertEqual(len(self.published), 1)
        self.assertEqual(self.published[0]["label"], "s1")
        self.assertEqual(self.published[0]["mac_active"], False)
        self.server.shutdown()

    def test_rejected_chime_is_not_broadcast(self):
        port = self.serve_with(True)
        self.send(port, "stop|../../etc|s1\n")
        self.assertEqual(self.published, [])
        self.server.shutdown()

    def test_event_ids_increment(self):
        port = self.serve_with(True)
        self.send(port, "stop|dnd|s1\n")
        self.send(port, "notification|classical|s2\n")
        self.assertEqual([e["id"] for e in self.published], [1, 2])
        self.server.shutdown()

    def test_without_presence_it_always_plays(self):
        """Backwards compatibility: no presence object means today's behaviour."""
        handler = chime.make_handler(self.sounds, {}, self.player, self.logs.append)

        class Server(chime.socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = Server(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.send(server.server_address[1], "stop|dnd|s1\n")
        self.assertEqual(len(self.player.played), 2)
        server.shutdown()
```

Also add `import threading` to the imports at the top of that test file if absent.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_chime_listener.py -k BuildEvent`
Expected: FAIL — `module 'chime_listener' has no attribute 'build_event'`

- [ ] **Step 3: Add `build_event` to `hooks/chime-listener.py`**

Insert after `resolve()` (after line 131):

```python
def build_event(event, theme, label, tracks, sounds_dir, mac_active, seq, now):
    """The JSON a browser receives. Names the exact files the Mac chose, so the
    phone plays the same sound rather than a generic beep."""
    sounds_dir = Path(sounds_dir)
    urls = []
    for track in tracks:
        try:
            urls.append("/sounds/" + Path(track).relative_to(sounds_dir).as_posix())
        except ValueError:
            urls.append(None)
    return {
        "id": seq,
        "ts": now,
        "event": event,
        "theme": theme,
        "label": label,
        "melody": urls[0] if len(urls) > 0 else None,
        "speech": urls[1] if len(urls) > 1 else None,
        "mac_active": mac_active,
    }
```

- [ ] **Step 4: Gate playback and publish, in `make_handler`**

Change the signature (line 172) from:

```python
def make_handler(sounds_dir, config, player, log):
```

to:

```python
def make_handler(sounds_dir, config, player, log, broadcaster=None, presence=None):
    seq = itertools.count(1)
```

Then replace the final two lines of `handle()` (lines 214-215):

```python
            log(f"{label}: {event} -> {', '.join(t.name for t in tracks)}")
            player.submit(tracks, label)
            ack(b"ok")
```

with:

```python
            here = presence.active() if presence is not None else True
            where = "here" if here else "away"
            log(f"{label}: {event} ({where}) -> {', '.join(t.name for t in tracks)}")

            if here:
                player.submit(tracks, label)

            if broadcaster is not None:
                broadcaster.publish(build_event(
                    event, theme, label, tracks, sounds_dir, here, next(seq),
                    datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                ))

            ack(b"ok")
```

Update the imports at the top: add `import itertools` and change
`from datetime import datetime` to `from datetime import datetime, timezone`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 tests/test_chime_listener.py`
Expected: PASS — all previous tests plus the 9 new ones

- [ ] **Step 6: Start presence and the web server in `main()`**

In `main()`, after `player = Player(...)` (around line 246), insert:

```python
    presence_cache = None
    if str(config.get("presence_enabled", False)).lower() == "true":
        presence_cache = presence.PresenceCache(
            idle_threshold=int(config.get("idle_threshold", 300))
        )
        log(f"presence gating on (idle threshold {config.get('idle_threshold', 300)}s)")

    broadcaster = None
    if str(config.get("web_enabled", False)).lower() == "true":
        broadcaster = broadcast.Broadcaster()
        chime_web.serve(
            config.get("web_bind", "127.0.0.1"),
            int(config.get("web_port", 8128)),
            sounds_dir,
            broadcaster,
            root / "web" / "index.html",
            log,
        )
```

Change the `server = Server(...)` line (line 250) to pass them through:

```python
    server = Server(
        ("127.0.0.1", port),
        make_handler(sounds_dir, config, player, log, broadcaster, presence_cache),
    )
```

Add sibling-module loading near the top of the file, after the imports:

```python
def _sibling(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


presence = _sibling("presence", "presence.py")
broadcast = _sibling("broadcast", "broadcast.py")
chime_web = _sibling("chime_web", "chime_web.py")
```

(The main module's filename has a hyphen, so siblings are loaded the same way
the tests load it, keeping one consistent mechanism.)
Add `import importlib.util` to the imports.

- [ ] **Step 7: Verify the whole suite still passes**

Run:
```bash
python3 tests/test_presence.py && python3 tests/test_broadcast.py && \
python3 tests/test_chime_web.py && python3 tests/test_chime_listener.py && \
bash tests/test_notify_ssh.sh
```
Expected: all five suites OK. The 44 pre-existing assertions must still pass — this task must not change behaviour when the new config keys are absent.

- [ ] **Step 8: Commit**

```bash
git add hooks/chime-listener.py tests/test_chime_listener.py
git commit -m "Gate local playback on presence and broadcast every chime"
```

---

### Task 6: Enable, document, verify on the phone

**Files:**
- Modify: `hooks/install-listener.sh` (report the web URL when enabled)
- Modify: `README.md`, `docs/architecture.md`
- Machine-local: `~/.claude/claw-bell.json`

- [ ] **Step 1: Report the web URL from the installer**

In `hooks/install-listener.sh`, after the `echo "  log:   $LOG"` line, add:

```bash
WEB=$(python3 -c "
import json, os
cfg = {}
for p in ('$PLUGIN_ROOT/config.json', os.path.expanduser('~/.claude/claw-bell.json')):
    try:
        d = json.load(open(p))
        if isinstance(d, dict):
            cfg.update(d)
    except Exception:
        pass
if str(cfg.get('web_enabled', False)).lower() == 'true':
    print('http://%s:%s' % (cfg.get('web_bind', '127.0.0.1'), cfg.get('web_port', 8128)))
")
[ -n "$WEB" ] && echo "  web:   $WEB"
```

- [ ] **Step 2: Turn it on for this Mac**

Write `~/.claude/claw-bell.json`:

```json
{
  "theme": "videogame",
  "chime_label": "mac",
  "chime_port": 8127,
  "web_enabled": true,
  "web_bind": "10.66.66.2",
  "web_port": 8128,
  "presence_enabled": true,
  "idle_threshold": 300
}
```

- [ ] **Step 3: Reinstall and confirm both servers are up**

Run:
```bash
cd /Users/dattavani/bells-and-whistles && claude plugin marketplace update claw-bell && \
claude plugin update bells-and-whistles@claw-bell && \
bash "$(claude plugin list --json | python3 -c 'import json,sys; print(next(p["installPath"] for p in json.load(sys.stdin) if p["id"].startswith("bells-and-whistles@")))')/hooks/install-listener.sh"
```
Expected: the installer prints `web: http://10.66.66.2:8128` alongside the plist and log lines.

- [ ] **Step 4: Verify the stream end to end from s1**

Run (on the Mac, in one terminal):
```bash
curl -N --max-time 30 http://10.66.66.2:8128/events
```
Then from another terminal:
```bash
ssh s1 'bash -c "printf \"stop|dnd|s1\n\" > /dev/tcp/127.0.0.1/8127"'
```
Expected: a `data: {...}` line appears in the curl output with `"label": "s1"`, `"theme": "dnd"`, and a `mac_active` field. Heartbeat `: hb` lines appear every 20s.

- [ ] **Step 5: Verify presence flips the routing**

Lock the screen (Ctrl-Cmd-Q), chime from s1, then unlock and check the log:

Run: `tail -5 ~/Library/Logs/claw-bell.log`
Expected: the locked-screen chime is logged `(away)` and no sound played on the Mac; an unlocked one is logged `(here)`.

- [ ] **Step 6: Verify on the Samsung S24**

With WireGuard connected on the phone, open `http://10.66.66.2:8128/` in Chrome. Tap "Tap to enable sound". Set the toggle to **Always**. Chime from s1 and confirm the D&D melody plays on the phone. Then set it to **When away from Mac**, chime while sitting at the Mac, and confirm the phone stays silent.

If the page does not load, check the phone's WireGuard tunnel is connected and that Chrome has battery set to Unrestricted in One UI.

- [ ] **Step 7: Document it**

Add a "Presence routing and the phone" section to `README.md` covering: the five new config keys with defaults, the `web_bind` warning (never `0.0.0.0`), the arming tap, the three-way toggle, the Android keepalive trade-off, and the Samsung battery settings. Add the routing truth table from the spec.

In `docs/architecture.md`, extend the mermaid diagram with the presence branch and the SSE path, and add the new failure modes to the existing table.

- [ ] **Step 8: Commit**

```bash
git add hooks/install-listener.sh README.md docs/architecture.md
git commit -m "Document presence routing and report the web URL on install"
```

---

## Self-Review

**Spec coverage:** Goal → Tasks 1–5. Presence detection (idle + lock + console user, fail-open, 300s) → Task 1. Broadcaster with bounded queues → Task 2. `/`, `/events`, `/sounds/` with whitelist, no replay, heartbeat, no `0.0.0.0` → Task 3. Arm button, three-way toggle, event list, staleness indicator, keepalive, ducking, preload/cache → Task 4. Routing truth table and the `mac_active` stamp → Task 5. Five config keys defaulting off → Tasks 5–6. Manual S24 verification → Task 6. Mute precedence needs no code (enforced on the sending host) and is asserted by the existing `tests/test_notify_ssh.sh` "muted: sends nothing" case, re-run in Task 5 Step 7.

**Placeholder scan:** No TBD/TODO. Every code step carries real code. Task 6 Step 7 describes documentation content rather than final prose, which is appropriate for prose but is the one step an executor must compose themselves.

**Type consistency:** `mac_active` is a bool everywhere — `presence.mac_active()` returns bool, `PresenceCache.active()` returns bool, `build_event(..., mac_active, ...)` stores it, the page reads `ev.mac_active === false`. `Broadcaster.publish(dict) -> int` matches the fake in Task 5. `serve(bind, port, sounds_dir, broadcaster, page_path, log)` matches its call in Task 5 Step 6. Sound URLs are `/sounds/<relpath>` in `build_event` and resolved by `safe_sound_path` stripping the same prefix in Task 3.

**One issue found and fixed inline:** the page listened for an `hb` event while the server sent a bare `: hb` comment, which `EventSource` never surfaces to JavaScript. A healthy but quiet connection would have gone amber after 30s and stayed there — the staleness indicator, the design's main defence against silent Android failure, would itself have been the thing crying wolf. Task 3 now emits `event: hb\ndata: {}\n\n` as a named event, `serve()` takes a `heartbeat` argument so it is testable in under a second, and `test_heartbeat_is_a_named_event_the_page_can_see` asserts the exact bytes.

**Test counts after this plan:** 16 presence + 8 broadcast + 18 web + ~43 listener + 10 shell.
