"""A HERO9 Black, firmware 2.0, that misbehaves exactly like the real one.

Every quirk here was measured on AJ's camera on 2026-09-17 and is written
down in CLAUDE.md under Camera facts:

- Two HTTP servers. Open GoPro, media list and downloads on one port; the
  legacy gpControl commands on another (80 on the real camera).
- The Open GoPro shutter 404s. The legacy shutter starts the recording and
  never answers (the client times out).
- While recording, every request on both ports is dropped without a reply.
  After stop, every request gets 500 for a finalisation period (19 to 20 s
  on the real camera), then the camera is idle.
- The new clip appears in the media list at size 0 while the file is still
  being written, and downloading it then returns a stub.
- The Open GoPro stream endpoint exists and answers 409 when a stream is
  already running. Starting it sends UDP datagrams to port 8554 of the
  requester, either raw MPEG-TS or TS behind a 12 byte header.
- Linear lens (option 4) is refused at 240 fps; Linear + Horizon (8) works.

Timings are parameters so a test can run in seconds. Shapes are not.
Stdlib only, like the engine.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STUB_BYTES = 1024
UDP_PORT = 8554


class FakeHero9:
    def __init__(self, clip_path: str, *, start_delay_s: float = 2.5,
                 finalize_s: float = 2.0, size_settle_s: float = 1.5,
                 list_lag_s: float = 4.0, range_support: bool = True,
                 udp_datagrams: int = 500, udp_header: bytes = b""):
        with open(clip_path, "rb") as fh:
            self.clip_bytes = fh.read()
        self.start_delay_s = start_delay_s
        self.finalize_s = finalize_s
        # File written (and served whole) size_settle_s after finalisation.
        self.size_settle_s = size_settle_s
        # The media list keeps saying a few KB for this long after that.
        self.list_lag_s = list_lag_s
        # Whether the download server honours Range (Content-Range total).
        self.range_support = range_support
        self.udp_datagrams = udp_datagrams
        self.udp_header = udp_header

        self.lock = threading.Lock()
        # Deliberately wrong to begin with: 4K, 30 fps, Wide, HyperSmooth on.
        self.settings = {"2": 1, "3": 8, "121": 0, "135": 1}
        self.group = None
        # One old clip and its proxy already on the card.
        self.files: list[dict] = [
            {"n": "GX010001.MP4", "size": 1000, "mod": 1700000000, "settle_at": 0.0},
            {"n": "GX010001.LRV", "size": 100, "mod": 1700000000, "settle_at": 0.0},
        ]
        self.next_no = 2
        self.recording_since: float | None = None
        self.dead_until = 0.0
        self.streaming = False
        self.requests: list[str] = []
        self.served: list[tuple[str, int]] = []      # (name, bytes) per download
        self.clips_recorded: list[dict] = []

        self._http = self._make_server()
        self._ctl = self._make_server()
        self.port = self._http.server_address[1]
        self.control_port = self._ctl.server_address[1]

    # ---------- lifecycle ----------

    def _make_server(self) -> ThreadingHTTPServer:
        cam = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):                 # noqa: D102
                pass

            def do_GET(self):                             # noqa: N802
                res = cam.handle(self.path, self.client_address[0],
                                 self.headers.get("Range"))
                if res is None:
                    # Drop the connection without a reply, as the real camera
                    # does while recording.
                    self.close_connection = True
                    return
                status, body, ctype, extra = res
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.daemon_threads = True
        return srv

    def start(self) -> "FakeHero9":
        for srv in (self._http, self._ctl):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        for srv in (self._http, self._ctl):
            srv.shutdown()
            srv.server_close()

    # ---------- the state machine ----------

    def _recording(self) -> bool:
        return self.recording_since is not None

    def _dead(self, now: float) -> bool:
        return self._recording() or now < self.dead_until

    def _json(self, obj, status: int = 200):
        return status, json.dumps(obj).encode(), "application/json", {}

    def _state(self) -> dict:
        return {"status": {"8": 1 if self._recording() else 0, "10": 0},
                "settings": dict(self.settings)}

    def _listed_size(self, f: dict, now: float) -> int:
        """What the media list says, which lags the file: 0 while the camera
        is still finalising, a few KB for a while after the file is whole,
        then the truth."""
        if now < f["settle_at"] - self.size_settle_s:
            return 0
        if now < f["settle_at"] + self.list_lag_s:
            return min(f["size"], 30_000)
        return f["size"]

    def _media(self, now: float) -> dict:
        fs = []
        for f in self.files:
            fs.append({"n": f["n"], "s": str(self._listed_size(f, now)), "mod": str(f["mod"])})
        return {"media": [{"d": "100GOPRO", "fs": fs}]}

    def handle(self, raw_path: str, client_ip: str, range_header: str | None = None):
        now = time.monotonic()
        url = urlparse(raw_path)
        path, q = url.path, parse_qs(url.query)
        self.requests.append(raw_path)

        # Legacy shutter: acts, never answers.
        if path == "/gp/gpControl/command/shutter":
            p = q.get("p", [""])[0]
            if p == "1":
                with self.lock:
                    if not self._dead(now):
                        self.recording_since = now
                        self.streaming = False       # the shutter kills the stream
                time.sleep(self.start_delay_s)       # the client times out first
                return None
            with self.lock:
                if p == "0":
                    if self._recording():
                        self._end_recording(now)
                        return None                  # dropped while closing the file
                    if now < self.dead_until:
                        return self._json({}, 500)
                    return self._json({})
            return self._json({}, 400)

        with self.lock:
            if self._recording():
                return None                           # whole HTTP stack is gone
            if now < self.dead_until:
                return self._json({"error": "busy"}, 500)

            if path in ("/gopro/camera/shutter/start", "/gopro/camera/shutter/stop"):
                return self._json({}, 404)            # HERO10 and later only
            if path in ("/gopro/camera/state", "/gp/gpControl/status"):
                return self._json(self._state())
            if path in ("/gopro/camera/keep_alive", "/gp/gpControl/info"):
                return self._json({})
            if path == "/gopro/version":
                return self._json({"version": "2.0",
                                   "info": {"model_name": "HERO9 Black",
                                            "firmware_version": "HD9.01.02.00.00"}})
            if path in ("/gopro/media/list", "/gp/gpMediaList"):
                return self._json(self._media(now))
            if path.startswith("/videos/DCIM/100GOPRO/"):
                name = path.rsplit("/", 1)[-1]
                for f in self.files:
                    if f["n"] == name:
                        if f["n"].endswith(".MP4") and f["size"] == len(self.clip_bytes):
                            body = self.clip_bytes if now >= f["settle_at"] else self.clip_bytes[:STUB_BYTES]
                        else:
                            body = b"\x00" * f["size"]
                        if range_header and self.range_support:
                            # A one byte probe. The file system knows the true
                            # size before the media list does.
                            return (206, body[:1], "application/octet-stream",
                                    {"Content-Range": f"bytes 0-0/{len(body)}"})
                        self.served.append((name, len(body)))
                        return 200, body, "application/octet-stream", {}
                return self._json({}, 404)
            if path == "/gopro/camera/setting":
                sid = q.get("setting", [""])[0]
                opt = q.get("option", [""])[0]
                if sid == "121" and opt == "4" and self.settings.get("3") == 0:
                    return self._json({}, 403)        # Linear not offered at 240 fps
                if sid in self.settings:
                    self.settings[sid] = int(opt)
                    return self._json({})
                return self._json({}, 400)
            if path == "/gopro/camera/presets/set_group":
                self.group = q.get("id", [""])[0]
                return self._json({})
            if path == "/gopro/camera/stream/start" or \
                    (path == "/gp/gpControl/execute" and q.get("c1") == ["restart"]):
                if self.streaming and path.startswith("/gopro"):
                    return self._json({}, 409)
                self.streaming = True
                threading.Thread(target=self._send_udp, args=(client_ip,), daemon=True).start()
                return self._json({})
            if path == "/gopro/camera/stream/stop" or \
                    (path == "/gp/gpControl/execute" and q.get("c1") == ["stop"]):
                self.streaming = False
                return self._json({})
        return self._json({}, 404)

    def _end_recording(self, now: float) -> None:
        duration = now - (self.recording_since or now)
        self.recording_since = None
        self.dead_until = now + self.finalize_s
        name = f"GX01{self.next_no:04d}.MP4"
        self.next_no += 1
        clip = {"n": name, "size": len(self.clip_bytes), "mod": int(time.time()),
                "settle_at": now + self.finalize_s + self.size_settle_s,
                "duration_s": round(duration, 2)}
        self.files.append(clip)
        self.files.append({"n": name[:-4] + ".LRV", "size": 5000, "mod": clip["mod"],
                           "settle_at": clip["settle_at"]})
        self.clips_recorded.append(clip)

    def _send_udp(self, ip: str) -> None:
        """What the camera pushes to port 8554: 7 TS packets per datagram,
        behind whatever header this instance was told to use."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ts = b"".join(b"\x47" + bytes([i % 251]) * 187 for i in range(7))
        try:
            for _ in range(self.udp_datagrams):
                if not self.streaming:
                    break
                s.sendto(self.udp_header + ts, (ip, UDP_PORT))
                time.sleep(0.001)
        except OSError:
            pass
        finally:
            s.close()
