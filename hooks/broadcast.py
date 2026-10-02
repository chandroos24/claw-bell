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
