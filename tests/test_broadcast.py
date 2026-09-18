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
