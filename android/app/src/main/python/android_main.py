"""Entry point the Android app calls through Chaquopy.

The Kotlin side calls start(files_dir) once. Everything after that is the same
worker + server that runs under Termux, on a background thread, bound to
loopback. The WebView then points at http://127.0.0.1:8080.
"""

from __future__ import annotations

import os
import threading

from flightpath import gopro
from flightpath.server import serve
from flightpath.worker import Settings, Worker

_state: dict = {}


def start(files_dir: str, port: int = 8080) -> str:
    """Idempotent. Returns the URL the WebView should load."""
    if _state.get("httpd"):
        return f"http://127.0.0.1:{_state['port']}/"

    os.makedirs(files_dir, exist_ok=True)
    settings = Settings(
        clip_dir=os.path.join(files_dir, "clips"),
        session_path=os.path.join(files_dir, "session.json"),
        config_path=os.path.join(files_dir, "config.json"),
    )
    worker = Worker(settings, gopro.GoProClient())
    worker.start()

    httpd = serve(worker, "127.0.0.1", port)
    t = threading.Thread(target=httpd.serve_forever, name="flightpath-http", daemon=True)
    t.start()

    _state.update(httpd=httpd, worker=worker, port=port, thread=t)
    return f"http://127.0.0.1:{port}/"


def stop() -> None:
    httpd = _state.pop("httpd", None)
    worker = _state.pop("worker", None)
    if httpd:
        httpd.shutdown()
    if worker:
        worker.stop()


def camera_ok() -> bool:
    w = _state.get("worker")
    return bool(w and w.camera_ok)


def reconnect() -> None:
    """Called by Kotlin right after the WiFi binding lands, so the user does not
    wait for the next background tick."""
    w = _state.get("worker")
    if w:
        threading.Thread(target=w.connect_now, daemon=True).start()
