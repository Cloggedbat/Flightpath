"""GoPro HERO9 control over WiFi HTTP.

Why HTTP and not Bluetooth: BLE from Termux on Android is painful. Turning the
camera's WiFi on by hand from its own menu, then joining that network from the
phone, gets you the full control surface over plain HTTP with no BLE stack at
all. One extra button press per session buys a much simpler app.

Endpoint paths differ across firmware. Rather than hard-code a guess, `probe()`
tries the known candidates and reports which ones your camera actually answers,
so a firmware difference shows up as a clear message instead of a hang.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# The camera is an untrusted input source. Anyone who can answer at 10.5.5.9 —
# an evil twin of the camera's SSID, or another device on its network — chooses
# both the filenames and the file bytes we write to disk. Names are matched
# against this, never merely joined onto a directory: os.path.join with an
# absolute path silently discards the directory prefix.
SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
SAFE_FOLDER = re.compile(r"[A-Za-z0-9._-]{1,32}")
MAX_CLIP_BYTES = 512 * 1024 * 1024

DEFAULT_HOST = "10.5.5.9"
DEFAULT_PORT = 8080

# Candidates in preference order. Open GoPro paths first, legacy second.
ENDPOINTS = {
    "state": ["/gopro/camera/state", "/gp/gpControl/status"],
    "shutter_start": ["/gopro/camera/shutter/start", "/gp/gpControl/command/shutter?p=1"],
    "shutter_stop": ["/gopro/camera/shutter/stop", "/gp/gpControl/command/shutter?p=0"],
    "media_list": ["/gopro/media/list", "/gp/gpMediaList"],
    # Legacy has no keep-alive command; any GET on the control port does the
    # job. The old fallback here was system/sleep?p=0, which only ever failed
    # to reach the camera because it went to the wrong port. Now that the
    # port is right, it must never be sent.
    "keep_alive": ["/gopro/camera/keep_alive", "/gp/gpControl/status"],
    "version": ["/gopro/version", "/gp/gpControl/info"],
    # Live preview: the camera pushes an MPEG-TS/H.264 stream over UDP to
    # port 8554 of whichever client asked for it. Low resolution, aiming only.
    "stream_start": ["/gopro/camera/stream/start", "/gp/gpControl/execute?p1=gpStream&c1=restart"],
    "stream_stop": ["/gopro/camera/stream/stop", "/gp/gpControl/execute?p1=gpStream&c1=stop"],
}

PREVIEW_UDP_PORT = 8554

# Open GoPro setting ids and option values. These are read back from the
# camera after being set, so a wrong id shows up as "could not confirm"
# rather than as silently wrong footage.
SETTING_RESOLUTION = 2
SETTING_FPS = 3
SETTING_LENS = 121
SETTING_HYPERSMOOTH = 135

RESOLUTION_1080 = 9
FPS_240 = 0
FPS_120 = 1
LENS_LINEAR = 4
LENS_LINEAR_HORIZON = 8          # what the HERO9 offers at 1080p240
HYPERSMOOTH_OFF = 0

PRESET_GROUP_VIDEO = 1000

# Human names for what the camera reports, so the UI can say "1080p" not "9".
RES_NAMES = {1: "4K", 4: "2.7K", 6: "2.7K 4:3", 7: "1440p", 9: "1080p",
             18: "4K 4:3", 24: "5K", 25: "5K 4:3", 27: "5.3K"}
FPS_NAMES = {0: 240, 1: 120, 2: 100, 5: 60, 6: 50, 8: 30, 9: 25, 10: 24, 13: 200}
LENS_NAMES = {0: "Wide", 2: "Narrow", 3: "SuperView", 4: "Linear",
              7: "Max SuperView", 8: "Linear + Horizon", 9: "HyperView",
              10: "Linear + Horizon Lock"}
HS_NAMES = {0: "Off", 1: "Low", 2: "High", 3: "Boost", 4: "Auto Boost",
            100: "Standard"}


class GoProError(RuntimeError):
    pass


@dataclass
class MediaItem:
    folder: str
    name: str
    size: int
    mtime: int

    @property
    def path(self) -> str:
        return f"{self.folder}/{self.name}"

    @property
    def is_video(self) -> bool:
        return self.name.upper().endswith((".MP4", ".LRV"))

    @property
    def is_main_video(self) -> bool:
        """Skip the low-res proxy files GoPro writes alongside each clip."""
        return self.name.upper().endswith(".MP4")


@dataclass
class ProbeResult:
    reachable: bool
    host: str
    working: dict = field(default_factory=dict)
    failed: dict = field(default_factory=dict)
    firmware: str = ""

    def summary(self) -> str:
        if not self.reachable:
            return (
                f"No answer from {self.host}.\n"
                "  1. Turn the camera's WiFi on from its own menu "
                "(Preferences > Connections > Connect Device > GoPro App).\n"
                "  2. Join that network from the phone.\n"
                "  3. Android may refuse to use a network with no internet. "
                "Accept the 'stay connected' prompt when it appears."
            )
        lines = [f"Camera answering at {self.host}."]
        if self.firmware:
            lines.append(f"  firmware: {self.firmware}")
        for name, path in sorted(self.working.items()):
            lines.append(f"  ok      {name:<15} {path}")
        for name, why in sorted(self.failed.items()):
            lines.append(f"  FAILED  {name:<15} {why}")
        return "\n".join(lines)


class GoProClient:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 8.0, control_port: int = 80):
        self.host = host
        self.port = port
        self.control_port = control_port   # legacy gpControl server; 80 on a real camera
        self.timeout = timeout
        self._resolved: dict[str, str] = {}

    # ---------- plumbing ----------

    def _url(self, path: str) -> str:
        # Two servers live on the camera. Open GoPro (/gopro/...), the media
        # list (/gp/gpMediaList) and file downloads (/videos/DCIM/...) are on
        # 8080. Legacy control (/gp/gpControl/...) is on port 80 and nothing
        # else. Sending a gpControl command to 8080 does not 404, it hangs
        # until the timeout, which is what "shutter start timed out" was.
        port = self.control_port if path.startswith("/gp/gpControl") else self.port
        return f"http://{self.host}:{port}{path}"

    def _get(self, path: str, timeout: float | None = None) -> bytes:
        req = urllib.request.Request(self._url(path))
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return r.read()

    def _get_json(self, path: str) -> dict:
        raw = self._get(path)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8", "replace"))

    def _call(self, name: str, timeout: float | None = None) -> bytes:
        """Call a logical endpoint, resolving which concrete path works.

        The error names every URL tried, with its port, so a report from a
        phone with no logcat still says exactly which server did what.
        """
        if name in self._resolved:
            return self._get(self._resolved[name], timeout=timeout)
        tried = []
        for path in ENDPOINTS[name]:
            try:
                out = self._get(path, timeout=timeout)
                self._resolved[name] = path
                return out
            except Exception as exc:                       # noqa: BLE001
                tried.append(f"{self._url(path)}: {exc}")
        raise GoProError(f"no working path for '{name}': " + "; ".join(tried))

    # ---------- discovery ----------

    def probe(self, timeout: float = 4.0) -> ProbeResult:
        """Find out what this camera actually answers. Run this first.

        `timeout` is per attempt. A "Connect" button wants a quick answer, so
        it passes a short one; the background reconnect loop can afford more.
        """
        res = ProbeResult(reachable=False, host=f"{self.host}:{self.port}")
        for name, paths in ENDPOINTS.items():
            if name in ("shutter_start", "shutter_stop", "stream_start", "stream_stop"):
                continue           # never fire the shutter or toggle the stream to test
            ok = False
            why = ""
            for path in paths:
                try:
                    self._get(path, timeout=timeout)
                    res.working[name] = path
                    self._resolved[name] = path
                    res.reachable = True
                    ok = True
                    break
                except urllib.error.HTTPError as exc:
                    why = f"HTTP {exc.code}"
                except Exception as exc:                   # noqa: BLE001
                    why = type(exc).__name__
            if not ok:
                res.failed[name] = why or "no response"

        if res.reachable:
            # Shutter and stream paths are NOT pinned here. _call() resolves
            # each on first real use by trying both candidates in order, so a
            # camera that answers /gopro/camera/state but 404s on
            # /gopro/camera/shutter/start (a real HERO9 did exactly this)
            # falls through to the legacy shutter instead of failing every
            # capture. Probing them for real would fire the shutter or restart
            # the stream on every reconnect. Listed below for the summary only.
            family = 0 if res.working.get("state", "").startswith("/gopro") else 1
            for name in ("shutter_start", "shutter_stop", "stream_start", "stream_stop"):
                res.working[name] = ENDPOINTS[name][family] + "  (not probed; resolved on first use)"
            try:
                v = self._get_json(res.working["version"])
                res.firmware = str(v.get("version") or v.get("info", {}).get("firmware_version", ""))
            except Exception:                              # noqa: BLE001
                pass
        return res

    # ---------- control ----------

    def state(self) -> dict:
        return json.loads(self._call("state").decode("utf-8", "replace") or "{}")

    def keep_alive(self) -> None:
        try:
            self._call("keep_alive")
        except GoProError:
            pass

    def start_recording(self) -> None:
        # Short timeout, fire and forget. On the HERO9 the shutter exists only
        # on the legacy control server, which acts on the command but does not
        # answer while recording. The worker confirms via state() rather than
        # trusting a response that may never come.
        self._call("shutter_start", timeout=2.0)

    def stop_recording(self) -> None:
        self._call("shutter_stop", timeout=2.0)

    def start_preview(self) -> None:
        """Ask the camera to stream its live view to us on UDP 8554.

        The camera must be idle: starting the shutter kills the stream, so the
        worker stops it before every capture and the UI restarts it after.
        """
        try:
            self._call("stream_start")
        except urllib.error.HTTPError as exc:
            # 409 Conflict is the camera saying "busy": already streaming (a
            # stop that never reached it while it was unresponsive after a
            # shutter), or still recording. A stop and one more start clears
            # the first case. The second still fails, and the UI already tells
            # the user the camera must not be recording.
            if exc.code != 409:
                raise
            self.stop_preview()
            time.sleep(0.5)
            self._call("stream_start")

    def stop_preview(self) -> None:
        # Never fatal. A resolved path raises raw HTTPError/URLError rather
        # than GoProError, and stopping a stream that is not running is not
        # an error anyone needs to hear about.
        try:
            self._call("stream_stop")
        except Exception:                                  # noqa: BLE001
            pass

    def stream_keep_alive(self) -> None:
        """The legacy preview stream stops unless the camera keeps seeing HTTP
        traffic on its control port (documented as "GET every 25 s or so").
        Best effort, called every worker tick while a preview is on."""
        try:
            self._get("/gp/gpControl/status", timeout=2.0)
        except Exception:                                  # noqa: BLE001
            pass

    def is_recording(self) -> bool | None:
        """None when the state shape is not recognised, rather than a wrong False."""
        try:
            st = self.state()
        except Exception:                                  # noqa: BLE001
            return None
        status = st.get("status", {})
        for key in ("8", 8, "encoding"):
            if key in status:
                return bool(status[key])
        return None

    def set_setting(self, setting_id: int, option: int) -> None:
        self._get(f"/gopro/camera/setting?setting={setting_id}&option={option}")

    def set_video_group(self) -> None:
        self._get(f"/gopro/camera/presets/set_group?id={PRESET_GROUP_VIDEO}")

    def read_settings(self) -> dict:
        """Current camera settings in plain words. Empty dict if unreadable."""
        try:
            st = self.state()
        except Exception:                                  # noqa: BLE001
            return {}
        raw = st.get("settings", {})

        def get(sid):
            for k in (str(sid), sid):
                if k in raw:
                    try:
                        return int(raw[k])
                    except (TypeError, ValueError):
                        return None
            return None

        res, fps, lens, hs = (get(SETTING_RESOLUTION), get(SETTING_FPS),
                              get(SETTING_LENS), get(SETTING_HYPERSMOOTH))
        return {
            "resolution": RES_NAMES.get(res, f"code {res}") if res is not None else None,
            "fps": FPS_NAMES.get(fps, f"code {fps}") if fps is not None else None,
            "lens": LENS_NAMES.get(lens, f"code {lens}") if lens is not None else None,
            "hypersmooth": HS_NAMES.get(hs, f"code {hs}") if hs is not None else None,
            "raw": {"res": res, "fps": fps, "lens": lens, "hs": hs},
        }

    def configure_for_launch_monitor(self, fps: int = 240) -> dict:
        """Put the camera in 1080p at high frame rate, Linear, stabilisation off.

        Sets each value, then reads the camera's state back and reports what it
        actually says. Returns {"ok": bool, "settings": {...}, "problems": [...]}.
        Order matters: video group first, then resolution, then fps, then lens,
        because the camera rejects an fps its current resolution cannot do.
        """
        problems: list[str] = []

        def attempt(label, fn):
            try:
                fn()
                return True
            except Exception as exc:                       # noqa: BLE001
                problems.append(f"{label}: {type(exc).__name__}")
                return False

        attempt("video mode", self.set_video_group)
        time.sleep(0.4)
        attempt("resolution 1080p", lambda: self.set_setting(SETTING_RESOLUTION, RESOLUTION_1080))
        time.sleep(0.4)
        want_fps = FPS_240 if fps >= 240 else FPS_120
        attempt(f"fps {fps}", lambda: self.set_setting(SETTING_FPS, want_fps))
        time.sleep(0.4)
        # Plain Linear is not offered at 1080p240 on the HERO9; Linear + Horizon
        # Leveling is. Both are rectilinear, which is all that matters here.
        if not attempt("lens Linear", lambda: self.set_setting(SETTING_LENS, LENS_LINEAR)):
            problems.pop()
            attempt("lens Linear+Horizon",
                    lambda: self.set_setting(SETTING_LENS, LENS_LINEAR_HORIZON))
        time.sleep(0.4)
        attempt("HyperSmooth off", lambda: self.set_setting(SETTING_HYPERSMOOTH, HYPERSMOOTH_OFF))
        time.sleep(0.6)

        got = self.read_settings()
        raw = got.get("raw", {})
        checks = [
            ("resolution", raw.get("res") == RESOLUTION_1080, got.get("resolution")),
            ("fps", raw.get("fps") == want_fps, got.get("fps")),
            ("lens", raw.get("lens") in (LENS_LINEAR, LENS_LINEAR_HORIZON), got.get("lens")),
            ("hypersmooth", raw.get("hs") == HYPERSMOOTH_OFF, got.get("hypersmooth")),
        ]
        for name, ok, val in checks:
            if val is None:
                problems.append(f"{name}: could not read back")
            elif not ok:
                problems.append(f"{name}: camera reports {val}")

        return {"ok": not problems, "settings": got, "problems": problems}

    # ---------- media ----------

    def media_list(self) -> list[MediaItem]:
        raw = json.loads(self._call("media_list").decode("utf-8", "replace") or "{}")
        out: list[MediaItem] = []
        for folder in raw.get("media", []):
            fname = str(folder.get("d", ""))
            if not SAFE_FOLDER.fullmatch(fname):
                continue
            for f in folder.get("fs", []):
                name = str(f.get("n", ""))
                # Reject anything that is not a plain filename. This is the
                # single most important check in the file.
                if not SAFE_NAME.fullmatch(name) or name in (".", ".."):
                    continue
                if not name.upper().endswith((".MP4", ".LRV", ".JPG", ".THM")):
                    continue
                try:
                    out.append(MediaItem(
                        folder=fname,
                        name=name,
                        size=int(f.get("s", 0)),
                        mtime=int(f.get("mod", 0)),
                    ))
                except (TypeError, ValueError):
                    continue
        return out

    def clip_size(self, item: MediaItem) -> int | None:
        """The clip's size as the download server reports it, or None.

        The media list lags the file system for seconds after a recording:
        measured on the HERO9, a fresh clip was listed at 0 bytes one second
        after idle and at a few KB, stable across two reads, twenty seconds
        later. The download server serves the file itself, so a one byte
        ranged GET returns its true size in Content-Range without pulling
        the clip. Content-Length is used if the server ignores Range.
        """
        name = os.path.basename(item.name)
        if not SAFE_NAME.fullmatch(name) or name in (".", ".."):
            raise GoProError(f"refusing suspicious media name: {item.name!r}")
        url = self._url(f"/videos/DCIM/{item.folder}/{name}")
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                cr = r.headers.get("Content-Range", "")      # bytes 0-0/12345678
                if "/" in cr:
                    total = cr.rsplit("/", 1)[1].strip()
                    if total.isdigit():
                        return int(total)
                cl = r.headers.get("Content-Length", "")
                if r.status == 200 and cl.isdigit():
                    return int(cl)                           # Range ignored; body left unread
        except Exception:                                    # noqa: BLE001
            return None
        return None

    def download(self, item: MediaItem, dest_dir: str,
                 progress=None) -> str:
        """Stream a clip to disk. Returns the local path.

        Transfer is the slow step in the whole loop. A 3 second 1080p240 clip is
        roughly 30 MB, and HERO9 WiFi in practice runs a few MB/s, so expect
        about 10 seconds per shot. Keep clips short.
        """
        # Belt and braces: media_list already rejected unsafe names, but this
        # method is public and must not depend on that having run.
        name = os.path.basename(item.name)
        if not SAFE_NAME.fullmatch(name) or name in (".", ".."):
            raise GoProError(f"refusing suspicious media name: {item.name!r}")

        os.makedirs(dest_dir, exist_ok=True)
        root = os.path.realpath(dest_dir)
        dest = os.path.join(root, name)
        if os.path.dirname(os.path.realpath(dest)) != root:
            raise GoProError("download path escapes the clip directory")

        tmp = dest + ".part"
        url = self._url(f"/videos/DCIM/{item.folder}/{name}")

        # A hostile server can stream until storage is full. Cap it.
        limit = min(MAX_CLIP_BYTES, int(item.size * 1.1) + 65536) if item.size \
            else MAX_CLIP_BYTES

        started = time.time()
        got = 0
        try:
            with urllib.request.urlopen(url, timeout=max(self.timeout, 60)) as r, \
                    open(tmp, "wb") as fh:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > limit:
                        raise GoProError(
                            f"{name}: transfer exceeded {limit} bytes, aborting"
                        )
                    fh.write(chunk)
                    if progress:
                        progress(got, item.size, time.time() - started)
                # A dropped WiFi connection ends the read with no error, and a
                # half a clip decodes just far enough to give a wrong answer.
                # The media list already told us how big the file is.
                if item.size and got < item.size:
                    raise GoProError(
                        f"{name}: transfer ended early, {got} of {item.size} bytes. "
                        "Move the phone closer to the camera and try again."
                    )
            os.replace(tmp, dest)
        except BaseException:
            # Never leave a .part behind to accumulate on a phone.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return dest


def transfer_estimate_s(size_bytes: int, mb_per_s: float = 3.0) -> float:
    return size_bytes / (mb_per_s * 1024 * 1024)
