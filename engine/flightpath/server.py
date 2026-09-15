"""Local web server. Stdlib only, so it runs under Termux with no extra deps.

Same shape as OpenFlight: a small backend on the device, a browser UI on
localhost:8080. Open http://localhost:8080 on the phone.

Security posture, because this listens on a socket on a phone:

- Binds 127.0.0.1 by default. Nothing off the device can reach it. Opening it
  to the network is an explicit flag that prints a warning and mints a token.
- POSTs require `Content-Type: application/json`. A hostile web page the user
  happens to be browsing cannot send that cross-origin without a preflight, so
  the no-cors CSRF path that would otherwise let any site wipe their session or
  fire the shutter is closed.
- The Host header must be localhost or a private IP literal. DNS rebinding
  needs a hostname that resolves to the device, so rejecting hostnames closes
  it.
- Bodies are capped, numbers are clamped, sockets time out.
"""

from __future__ import annotations

import ipaddress
import json
import os
import pkgutil
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# The UI is looked for in two places so the same code runs everywhere:
#   1. <repo>/ui/            the Termux / desktop layout
#   2. flightpath/ui/        packaged inside the module, which is how it ships
#                            in the Android APK (read via pkgutil, not the disk)
UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


def _ui_bytes(name: str) -> bytes | None:
    name = os.path.basename(name)
    full = os.path.join(UI_DIR, name)
    if os.path.isfile(full):
        with open(full, "rb") as fh:
            return fh.read()
    try:
        return pkgutil.get_data(__package__ or "flightpath", f"ui/{name}")
    except (OSError, ValueError, FileNotFoundError):
        return None

MAX_BODY = 64 * 1024
LOCAL_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]", ""}


def _host_allowed(host_header: str) -> bool:
    """Allow localhost and private IP literals. Reject every hostname.

    DNS rebinding always arrives with an attacker-controlled hostname in Host,
    so an IP-literal-only rule shuts it out without a config file.
    """
    host = (host_header or "").strip()
    if host.startswith("["):                       # [::1]:8080
        host = host.split("]")[0] + "]"
    elif host.count(":") == 1:
        host = host.split(":")[0]
    if host.lower() in LOCAL_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False                               # a hostname: refuse
    return ip.is_loopback or ip.is_private or ip.is_link_local


class _Handler(BaseHTTPRequestHandler):
    worker = None                      # injected by serve()
    token = ""                         # empty means no token required
    protocol_version = "HTTP/1.1"
    timeout = 15                       # no idle connections parked forever

    def log_message(self, fmt, *args):
        pass                           # keep the Termux console readable

    # ---------- helpers ----------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _guard(self, writing: bool) -> bool:
        """Common checks. Returns False once a response has been sent."""
        if not _host_allowed(self.headers.get("Host", "")):
            self._json({"error": "bad host"}, 421)
            return False
        if self.token:
            supplied = self.headers.get("X-FlightPath-Token", "")
            if not secrets.compare_digest(supplied, self.token):
                self._json({"error": "bad or missing token"}, 401)
                return False
        if writing:
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype.lower() != "application/json":
                # Blocks the simple cross-origin POST that needs no preflight.
                self._json({"error": "Content-Type must be application/json"}, 415)
                return False
        return True

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n <= 0 or n > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    @staticmethod
    def _num(body: dict, key: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(body.get(key, default))
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number")
        if not (lo <= v <= hi):
            raise ValueError(f"{key} must be between {lo} and {hi}")
        return v

    # ---------- routes ----------

    def do_GET(self):                                      # noqa: N802
        if not self._guard(writing=False):
            return
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(self.worker.snapshot())
        if path == "/api/session":
            return self._json(json.loads(self.worker.session.to_json()))
        if path == "/api/calibrate/frame":
            jpeg = self.worker.ref_frame_jpeg
            if not jpeg:
                return self._json({"error": "no reference frame captured yet"}, 404)
            return self._send(200, jpeg, "image/jpeg")
        if path == "/api/selftest":
            from . import selftest
            return self._json(selftest.run())
        if path == "/api/probe":
            return self._json({"summary": self.worker.client.probe().summary()})
        if path.startswith("/ui/"):
            name = os.path.basename(path)
            ctype = "text/css" if name.endswith(".css") else "application/javascript"
            return self._file(name, ctype)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):                                     # noqa: N802
        if not self._guard(writing=True):
            return
        path = urlparse(self.path).path
        body = self._body()

        if path == "/api/mode":
            mode = str(body.get("mode", "queue"))
            if mode not in ("queue", "focus"):
                return self._json({"error": "mode must be queue or focus"}, 400)
            self.worker.set_mode(mode)
            return self._json({"ok": True, "mode": self.worker.settings.mode})

        if path == "/api/club":
            club = str(body.get("club", ""))[:12]
            self.worker.set_club(club)
            return self._json({"ok": True, "club": self.worker.settings.club})

        if path == "/api/reference":
            try:
                coords = [self._num(body, k, None, -20000, 20000)
                          for k in ("x1", "y1", "x2", "y2")]
                inches = self._num(body, "inches", 46.0, 0.5, 600.0)
            except (ValueError, TypeError) as exc:
                return self._json({"error": f"bad reference: {exc}"}, 400)
            self.worker.set_reference(*coords, inches)
            return self._json({"ok": True})

        if path == "/api/trigger":
            try:
                secs = self._num(body, "seconds", 3.0, 0.2, 15.0)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            if not self.worker.begin_trigger():
                return self._json({"error": "already recording"}, 409)
            threading.Thread(
                target=self.worker.trigger, args=(secs,), daemon=True
            ).start()
            return self._json({"ok": True})

        if path == "/api/calibrate/capture":
            try:
                secs = self._num(body, "seconds", 2.0, 0.5, 10.0)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            threading.Thread(
                target=self.worker.capture_reference_frame, args=(secs,), daemon=True
            ).start()
            return self._json({"ok": True})

        if path == "/api/camera/configure":
            return self._json(self.worker.configure_camera())

        if path == "/api/camera/connect":
            return self._json(self.worker.connect_now())

        if path == "/api/camera/refresh":
            return self._json(self.worker.refresh_camera_settings())

        if path == "/api/preview/start":
            return self._json(self.worker.start_preview())

        if path == "/api/preview/stop":
            return self._json(self.worker.stop_preview())

        if path == "/api/clear":
            self.worker.clear_session()
            return self._json({"ok": True})

        return self._json({"error": "not found"}, 404)

    def _file(self, name: str, ctype: str) -> None:
        data = _ui_bytes(name)
        if data is None:
            return self._json({"error": f"missing {name}"}, 404)
        self._send(200, data, ctype)


def serve(worker, host: str = "127.0.0.1", port: int = 8080, token: str = ""):
    handler = type("Handler", (_Handler,), {"worker": worker, "token": token})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
