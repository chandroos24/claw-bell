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
