"""HTTP server: serves the dashboard page and streams live data over Server-Sent Events."""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .model import MetricsEngine, Snapshot
from .sources import DataSource

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class Poller(threading.Thread):
    """Reads the data source on a fixed interval and feeds the metrics engine."""

    def __init__(self, source: DataSource, engine: MetricsEngine, interval: float) -> None:
        super().__init__(daemon=True, name="robot-poller")
        self.source, self.engine, self.interval = source, engine, interval
        self.lock = threading.Lock()
        self.updated = threading.Condition(self.lock)
        self.version = 0
        self.last_error: str | None = None
        self._stop = threading.Event()

    def run(self) -> None:
        backoff = 1.0
        connected = False
        while not self._stop.is_set():
            started = time.time()
            try:
                if not connected:
                    self.source.connect()
                    connected = True
                    log.info("connected to data source")
                snap = self.source.read()
                self.last_error = None
                backoff = 1.0
            except Exception as exc:  # communication failures must not kill the poller
                if connected or self.last_error != str(exc):
                    log.warning("data source error: %s", exc)
                self.last_error = str(exc)
                connected = False
                try:
                    self.source.close()
                except Exception:
                    pass
                snap = Snapshot(connected=False)
            with self.lock:
                self.engine.ingest(snap)
                self.version += 1
                self.updated.notify_all()
            wait = self.interval if connected else min(backoff, 10.0)
            if not connected:
                backoff *= 2
            self._stop.wait(max(0.0, wait - (time.time() - started)))

    def stop(self) -> None:
        self._stop.set()

    def payload(self, include_history: bool) -> dict:
        with self.lock:
            data = self.engine.summary()
            if include_history:
                data["history"] = list(self.engine.history)
            else:
                data["point"] = self.engine.history[-1] if self.engine.history else None
        data["source_error"] = self.last_error
        return data


def make_handler(poller: Poller, ui_config: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WeldDashboard/1.0"

        def log_message(self, fmt, *args):  # route access logs through logging
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/snapshot":
                return self._json(poller.payload(include_history=True))
            if path == "/api/config":
                return self._json(ui_config)
            if path == "/api/stream":
                return self._stream()
            if path == "/":
                path = "/index.html"
            file = (STATIC_DIR / path.lstrip("/")).resolve()
            if STATIC_DIR.resolve() not in file.parents or not file.is_file():
                return self._json({"error": "not found"}, 404)
            body = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(file.name)[0] or "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            seen = -1
            try:
                first = True
                while True:
                    with poller.lock:
                        poller.updated.wait_for(lambda: poller.version != seen, timeout=15)
                        changed = poller.version != seen
                        seen = poller.version
                    if changed:
                        data = json.dumps(poller.payload(include_history=first))
                        first = False
                        self.wfile.write(f"data: {data}\n\n".encode())
                    else:
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    return Handler


def serve(poller: Poller, host: str, port: int, ui_config: dict) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(poller, ui_config))
    httpd.daemon_threads = True
    return httpd
