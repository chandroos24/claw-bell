#!/usr/bin/env python3
"""claw-bell chime listener.

Plays the notification chime on this machine on behalf of a Claude Code
session running on a remote host. The remote hook writes one line to a
loopback port that an ssh RemoteForward tunnels back here; this process
resolves the line to local WAV files and plays them.

Wire format, newline-terminated, one per connection:

    event|theme|label        e.g.  stop|dnd|s1

Binds to loopback only. Every field is validated before it reaches the
filesystem: the theme is whitelisted against a real directory listing rather
than sanitised, so it cannot escape the sounds tree.

See docs/architecture.md.
"""

import argparse
import json
import os
import queue
import random
import re
import socketserver
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

VALID_EVENTS = ("stop", "notification")
LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_LINE = 256
QUEUE_DEPTH = 4
DEFAULT_PORT = 8127


class ChimeError(ValueError):
    """A message was malformed or referenced something that does not exist."""


def plugin_root():
    return Path(__file__).resolve().parent.parent


def load_config(root, override_path=None):
    """Plugin config.json, with ~/.claude/claw-bell.json layered on top.

    The override lives outside the plugin directory so per-host settings
    survive `claude plugin update`.
    """
    config = {}
    for path in (root / "config.json", override_path):
        if path is None:
            continue
        try:
            with open(path) as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                config.update(loaded)
        except (OSError, ValueError):
            continue
    return config


def list_themes(sounds_dir):
    """Theme directories under sounds/, excluding the speech tree."""
    try:
        return {
            entry.name
            for entry in Path(sounds_dir).iterdir()
            if entry.is_dir() and entry.name != "speech"
        }
    except OSError:
        return set()


def parse_message(line, valid_themes):
    """Parse and validate one wire line into (event, theme, label)."""
    if len(line) > MAX_LINE:
        raise ChimeError("line too long")

    text = line.strip()
    if not text:
        raise ChimeError("empty line")

    parts = text.split("|")
    if len(parts) != 3:
        raise ChimeError(f"expected 3 fields, got {len(parts)}")

    event, theme, label = parts

    if event not in VALID_EVENTS:
        raise ChimeError(f"unknown event {event!r}")
    if theme not in valid_themes:
        raise ChimeError(f"unknown theme {theme!r}")
    if not LABEL_RE.match(label):
        raise ChimeError(f"bad label {label!r}")

    return event, theme, label


def pick_melody(sounds_dir, theme, rng=random):
    wavs = sorted(Path(sounds_dir, theme).glob("*.wav"))
    return rng.choice(wavs) if wavs else None


def pick_speech(sounds_dir, event, accent, gender, rng=random):
    speech_dir = Path(sounds_dir, "speech", accent, gender)
    wavs = sorted(speech_dir.glob(f"{event}_[0-9]*.wav"))
    return rng.choice(wavs) if wavs else None


def resolve(sounds_dir, event, theme, config, rng=random):
    """Which files to play, honouring the configured mode."""
    mode = config.get("mode", "sound_and_voice")
    accent = config.get("accent", "us")
    gender = config.get("gender", "male")

    tracks = []
    if mode != "voice_only":
        melody = pick_melody(sounds_dir, theme, rng)
        if melody:
            tracks.append(melody)
    if mode != "sound_only":
        speech = pick_speech(sounds_dir, event, accent, gender, rng)
        if speech:
            tracks.append(speech)
    return tracks


class Player:
    """Serializes playback so two hosts chiming at once queue, not collide."""

    def __init__(self, command="afplay", depth=QUEUE_DEPTH, log=None):
        self.command = command
        self.log = log or (lambda message: None)
        self.queue = queue.Queue(maxsize=depth)
        self.played = []
        self.dropped = 0
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def submit(self, tracks, label):
        try:
            self.queue.put_nowait((tracks, label))
            return True
        except queue.Full:
            self.dropped += 1
            self.log(f"queue full, dropped chime from {label}")
            return False

    def _run(self):
        while True:
            tracks, label = self.queue.get()
            for track in tracks:
                self.played.append(track)
                try:
                    subprocess.run(
                        [self.command, str(track)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                except OSError as exc:
                    self.log(f"playback failed for {track}: {exc}")
            self.queue.task_done()


def make_handler(sounds_dir, config, player, log):
    class Handler(socketserver.StreamRequestHandler):
        timeout = 5

        def handle(self):
            try:
                raw = self.rfile.readline(MAX_LINE + 1)
            except OSError:
                return
            line = raw.decode("ascii", errors="replace")

            # The reply is what makes the send reliable: the sender blocks
            # until it arrives, so an ssh session that exits right after
            # chiming cannot tear the tunnel down before the line flushes.
            # Every path answers, so the sender never waits out its timeout.
            def ack(word):
                try:
                    self.wfile.write(word + b"\n")
                    self.wfile.flush()
                except OSError:
                    pass

            # Health check used by install-listener.sh. Answered without
            # playing anything, so verifying the agent is up is silent.
            if line.strip() == "ping":
                ack(b"pong")
                return

            try:
                event, theme, label = parse_message(line, list_themes(sounds_dir))
            except ChimeError as exc:
                log(f"rejected {line.strip()!r}: {exc}")
                ack(b"err")
                return

            tracks = resolve(sounds_dir, event, theme, config)
            if not tracks:
                log(f"{label}: {event}/{theme} resolved to nothing to play")
                ack(b"err")
                return

            log(f"{label}: {event} -> {', '.join(t.name for t in tracks)}")
            player.submit(tracks, label)
            ack(b"ok")

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description="claw-bell chime listener")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--sounds-dir", default=None)
    parser.add_argument("--player", default=os.environ.get("CLAW_BELL_PLAYER", "afplay"))
    parser.add_argument("--log-file", default=None)
    args = parser.parse_args(argv)

    root = plugin_root()
    override = Path.home() / ".claude" / "claw-bell.json"
    config = load_config(root, override)

    sounds_dir = Path(args.sounds_dir or root / "sounds")
    port = args.port or int(config.get("chime_port", DEFAULT_PORT))

    if args.log_file:
        handle = open(args.log_file, "a", buffering=1)
    else:
        handle = sys.stderr

    def log(message):
        stamp = datetime.now().isoformat(timespec="seconds")
        print(f"{stamp} {message}", file=handle, flush=True)

    player = Player(command=args.player, log=log)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server(("127.0.0.1", port), make_handler(sounds_dir, config, player, log))
    log(
        f"listening on 127.0.0.1:{port}, sounds={sounds_dir}, "
        f"themes={sorted(list_themes(sounds_dir))}"
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
