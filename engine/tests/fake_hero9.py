"""A HERO9 Black, firmware 2.0, that misbehaves exactly like the real one.

Every quirk here was measured on AJ's camera on 2026-09-17 and is written
down in CLAUDE.md under Camera facts:

- Two HTTP servers. Open GoPro, media list and downloads on one port; the
  legacy gpControl commands on another (80 on the real camera).
- The Open GoPro shutter 404s. The legacy shutter never answers (the client
  times out) and does NOT record: it leaves a 27,639 byte stub and ties the
  camera up for a while. GoPro deprecated the WiFi control commands from
  this model on. Proven 2026-09-22: the same camera, same moment, produced
  a 79.8 MB clip the instant the shutter went over Bluetooth instead.
  wifi_shutter_records=True restores the old wrong belief for a camera that
  really does answer HTTP.
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
                 stub_clips: bool = False, wifi_shutter_records: bool = False,
                 write_speed_errors: int = 0,
                 sd_errors: int = 0, overheating: bool = False,
                 battery_pct: int = 82, sd_remaining_kb: int = 52_428_800,
                 preset_group: int = 1000, flatmode: int = 12,
                 firmware: str = "HD9.01.70.00",
                 open_gopro_camera_info: bool = False,
                 udp_datagrams: int = 500, udp_header: bytes = b""):
        self.write_speed_errors = write_speed_errors
        self.sd_errors = sd_errors
        self.overheating = overheating
        self.battery_pct = battery_pct
        self.sd_remaining_kb = sd_remaining_kb
        # 1000 Video, 1001 Photo, 1002 Timelapse. Set to 1002 to reproduce a
        # camera that writes a tiny MP4 because it is timelapsing.
        self.preset_group = preset_group
        self.flatmode = flatmode
        # GoPro's documented minimum for Open GoPro on a HERO9 is v01.70.00.
        self.firmware = firmware
        # The real camera 404s /gopro/camera/info and answers the legacy
        # /gp/gpControl/info, so that is the default here too.
        self.open_gopro_camera_info = open_gopro_camera_info
        with open(clip_path, "rb") as fh:
            self.clip_bytes = fh.read()
        # Measured 2026-09-18: the camera closed a 3 s recording at 27,639
        # bytes. With this on, every recording is that stub, however it was
        # started.
        self.stub_clips = stub_clips
        # Whether the deprecated WiFi shutter actually records. On the real
        # HERO9 it does not; it only ever leaves a stub.
        self.wifi_shutter_records = wifi_shutter_records
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

    def _camera_info(self):
        return self._json({"info": {"model_name": "HERO9 Black",
                                    "model_number": 55,
                                    "firmware_version": self.firmware,
                                    "serial_number": "C000000000000"}})

    def _state(self) -> dict:
        """Status ids as GoPro documents them: 8 busy, 10 encoding, 13 the
        duration so far, 111 card write speed errors, 6 overheating. Busy
        stays set through finalisation, which is the whole reason the stop
        loop waits on idle rather than on encoding."""
        now = time.monotonic()
        rec = self._recording()
        finalising = not rec and now < self.dead_until
        return {"status": {
            "1": 1,
            "2": 4,
            "6": 1 if self.overheating else 0,
            "8": 1 if (rec or finalising) else 0,
            "10": 1 if rec else 0,
            "13": int(now - self.recording_since) if rec else 0,
            "35": 1800,
            "54": self.sd_remaining_kb,
            "70": self.battery_pct,
            "89": self.flatmode,
            "93": 0,
            "96": self.preset_group,
            "97": 0,
            "111": self.write_speed_errors,
            "112": self.sd_errors,
            "117": 1,
        }, "settings": dict(self.settings)}

    def _listed_size(self, f: dict, now: float) -> int:
        """What the media list says, which lags the file: 0 while the camera
        is still finalising, a few KB for a while after the file is whole,
        then the truth."""
        if now < f["settle_at"] - self.size_settle_s:
            return 0
        if now < f["settle_at"] + self.list_lag_s:
            return min(f["size"], 30_000)
        return f["size"]

    def _served_size(self, f: dict, now: float) -> int:
        """What the download server can serve: the whole file once it is
        closed, a growing prefix while the camera is still writing it."""
        if now >= f["settle_at"] or self.size_settle_s <= 0:
            return f["size"]
        start = f["settle_at"] - self.size_settle_s
        frac = max(0.0, min(1.0, (now - start) / self.size_settle_s))
        return max(STUB_BYTES, int(f["size"] * frac))

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
                        self.streaming = False       # the shutter kills the stream
                        if self.wifi_shutter_records:
                            self.recording_since = now
                        else:
                            # Deprecated: a stub lands on the card and the
                            # camera is tied up for a while afterwards.
                            self._end_recording(now, stub=True)
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
            if path == "/gopro/camera/keep_alive":
                return self._json({})
            if path == "/gopro/version":
                # The API version, NOT the camera firmware. Kept separate on
                # purpose: conflating them hid the real firmware for a while.
                return self._json({"version": "2.0"})
            if path == "/gopro/camera/info":
                # This HERO9 404s the Open GoPro info endpoint, exactly as it
                # 404s the Open GoPro shutter. Only the legacy one answers.
                if not self.open_gopro_camera_info:
                    return self._json({}, 404)
                return self._camera_info()
            if path == "/gp/gpControl/info":
                return self._camera_info()
            if path in ("/gopro/media/list", "/gp/gpMediaList"):
                return self._json(self._media(now))
            if path.startswith("/videos/DCIM/100GOPRO/"):
                name = path.rsplit("/", 1)[-1]
                for f in self.files:
                    if f["n"] == name:
                        if f["n"].endswith(".MP4"):
                            # A stub is a real clip cut short, which is what
                            # a recording that aborts leaves behind.
                            want = min(self._served_size(f, now), len(self.clip_bytes))
                            body = self.clip_bytes[:want]
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

    def _end_recording(self, now: float, stub: bool | None = None) -> None:
        duration = now - (self.recording_since or now)
        self.recording_since = None
        self.dead_until = now + self.finalize_s
        name = f"GX01{self.next_no:04d}.MP4"
        self.next_no += 1
        if stub is None:
            stub = self.stub_clips
        size = 27_639 if stub else len(self.clip_bytes)
        clip = {"n": name, "size": size, "mod": int(time.time()),
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
