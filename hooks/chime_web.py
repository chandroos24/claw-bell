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
