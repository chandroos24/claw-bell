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


class MultiBind(unittest.TestCase):
    """Binding several specific addresses, never 0.0.0.0."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sounds = build_sounds(self.tmp.name)
        self.page = Path(self.tmp.name) / "index.html"
        self.page.write_text("<!doctype html><title>claw-bell</title>")
        self.logs = []
        self.b = broadcast.Broadcaster()
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.tmp.cleanup()

    def test_unbindable_address_is_skipped_not_fatal(self):
        self.servers = web.serve_many(
            ["203.0.113.7", "127.0.0.1"], 0, self.sounds, self.b,
            self.page, self.logs.append)
        self.assertEqual(len(self.servers), 1)
        self.assertEqual(self.servers[0].server_address[0], "127.0.0.1")
        self.assertTrue(any("cannot bind 203.0.113.7" in m for m in self.logs))

    def test_all_addresses_unbindable_is_survivable(self):
        self.servers = web.serve_many(
            ["203.0.113.7"], 0, self.sounds, self.b, self.page, self.logs.append)
        self.assertEqual(self.servers, [])
        self.assertTrue(any("no address could be bound" in m for m in self.logs))

    def test_bound_server_actually_serves(self):
        self.servers = web.serve_many(
            ["127.0.0.1"], 0, self.sounds, self.b, self.page, self.logs.append)
        port = self.servers[0].server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            self.assertIn(b"claw-bell", r.read())


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
