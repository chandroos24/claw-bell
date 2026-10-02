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
