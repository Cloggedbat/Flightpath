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
    # The CAMERA's firmware, which is not the same thing as the API version
    # above. GoPro requires v01.70.00 or later on a HERO9 for Open GoPro to
    # work at all, so it is worth knowing which one is in front of us.
    "camera_info": ["/gopro/camera/info", "/gp/gpControl/info"],
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

# Camera status ids, taken from GoPro's own SDK (open_gopro/api/ble_statuses.py,
# whose doc links carry the id in the anchor). Worth having exactly right: the
# client read status 8 as "recording" for a long time, and 8 is BUSY. Encoding
# is 10. The two differ for the whole time the camera is closing a file.
STATUS_BATTERY_PRESENT = 1
STATUS_BATTERY_BARS = 2
STATUS_OVERHEATING = 6
STATUS_BUSY = 8
STATUS_ENCODING = 10
STATUS_ENCODING_DURATION = 13          # seconds of the clip being recorded
STATUS_REMAINING_VIDEO_S = 35
STATUS_SD_REMAINING_KB = 54
STATUS_BATTERY_PCT = 70
STATUS_SD_WRITE_SPEED_ERROR = 111      # card too slow for the chosen mode
STATUS_SD_ERRORS = 112
STATUS_SD_CAPACITY = 117
# Which mode the camera is actually IN, as opposed to what its video
# settings say. Reading 1080p240 back proves the stored values, not the
# mode: Time Lapse Video, Looping and TimeWarp all write a .MP4 too, and a
# three second press in any of them yields a handful of frames.
STATUS_FLATMODE = 89
STATUS_VIDEO_PRESET = 93
STATUS_PRESET_GROUP = 96
STATUS_PRESET = 97

PRESET_GROUP_NAMES = {1000: "Video", 1001: "Photo", 1002: "Timelapse"}

# A HERO9 firmware older than 01.70.00, which is GoPro's documented minimum
# for Open GoPro on this model. Matches HD9.01.00.xx to HD9.01.69.xx.
HERO9_FIRMWARE_LOW = re.compile(r"HD9\.01\.(0\d|[1-6]\d)\.", re.IGNORECASE)

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
    firmware: str = ""          # the CAMERA's firmware, e.g. HD9.01.01.72.00
    api_version: str = ""       # the Open GoPro API version, e.g. 2.0
    model: str = ""

    def summary(self) -> str:
        if not self.reachable:
            return (
                f"No answer from {self.host}.\n"
                "  1. Turn the camera's WiFi on. A HERO9 only broadcasts after an app "
                "asks over Bluetooth: open the Quik app until it shows the camera "
                "connected, force-stop Quik, then press Mode back to the shooting screen.\n"
                "  2. Join that network from the phone.\n"
                "  3. Android may refuse to use a network with no internet. "
                "Accept the 'stay connected' prompt when it appears."
            )
        lines = [f"Camera answering at {self.host}."]
        if self.model:
            lines.append(f"  model: {self.model}")
        if self.firmware:
            # GoPro requires v01.70.00 or later on a HERO9. The camera's own
            # string looks like HD9.01.70.00; older than that and parts of
            # the API simply are not there.
            lines.append(f"  camera firmware: {self.firmware}"
                         + ("  (HERO9 needs 01.70.00 or later for Open GoPro)"
                            if HERO9_FIRMWARE_LOW.search(self.firmware) else ""))
        if self.api_version:
            lines.append(f"  Open GoPro API version: {self.api_version}")
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
        # An open Bluetooth link to the camera, when the app has one. The
        # HERO9's WiFi shutter is deprecated and does not work; Bluetooth is
        # the supported path. Anything with shutterStart()/shutterStop()
        # returning "" for success will do, which keeps this testable.
        self.ble = None

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
                res.api_version = str(v.get("version") or "")
            except Exception:                              # noqa: BLE001
                pass
            try:
                # /gopro/version is the Open GoPro API version ("2.0"), NOT
                # the camera's firmware. Reporting the former as the latter
                # hid the real firmware for this whole project.
                info = self._get_json("/gopro/camera/info")
                inner = info.get("info", info)
                res.firmware = str(inner.get("firmware_version", "")
                                   or inner.get("firmware", ""))
                res.model = str(inner.get("model_name", "") or inner.get("model", ""))
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

    def _ble_shutter(self, start: bool) -> str | None:
        """Fire the shutter over Bluetooth. None when there is no link,
        "" on success, else the camera's reason."""
        ble = self.ble
        if ble is None:
            return None
        try:
            if not bool(ble.isReady()):
                return None
            return str(ble.shutterStart() if start else ble.shutterStop())
        except Exception as exc:                           # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    def start_recording(self) -> None:
        """Bluetooth first. This camera's WiFi shutter is deprecated: every
        attempt times out and leaves a 27,639 byte stub behind, the same size
        whatever the battery, card or mode. HTTP stays as a fallback for
        cameras that do answer it."""
        err = self._ble_shutter(True)
        if err == "":
            return
        if err:
            raise GoProError(f"bluetooth shutter start: {err}")
        # No Bluetooth link. Short timeout, fire and forget.
        self._call("shutter_start", timeout=2.0)

    def stop_recording(self) -> None:
        err = self._ble_shutter(False)
        if err == "":
            return
        if err:
            raise GoProError(f"bluetooth shutter stop: {err}")
        self._call("shutter_stop", timeout=2.0)

    def start_preview(self) -> None:
        """Ask the camera to stream its live view to us on UDP 8554.

        The camera must be idle: starting the shutter kills the stream, so the
        worker stops it before every capture and the UI restarts it after.
        """
        # GoPro's own PreviewStreamController stops any existing stream
        # before starting one, rather than starting and coping with the
        # refusal. Cheap, and it removes the usual cause of the 409.
        self.stop_preview()
        time.sleep(0.3)
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

    @staticmethod
    def _status(state: dict, sid: int):
        """One status value, or None. The camera keys these as strings over
        HTTP and as ints over BLE, so accept both."""
        status = state.get("status", {})
        for key in (str(sid), sid):
            if key in status:
                return status[key]
        return None

    def is_recording(self) -> bool | None:
        """Is the camera encoding right now? None when it cannot be read.

        Status 10, not 8. Status 8 is "busy", which stays set for the whole
        twenty seconds this camera spends closing a file after a recording.
        """
        try:
            st = self.state()
        except Exception:                                  # noqa: BLE001
            return None
        v = self._status(st, STATUS_ENCODING)
        if v is None:
            v = self._status(st, STATUS_BUSY)              # older shape
        return None if v is None else bool(v)

    def is_idle(self) -> bool | None:
        """Neither encoding nor busy: the camera has finished with the file.

        This, not is_recording(), is what proves a recording ended. The stop
        loop waits for it.
        """
        try:
            st = self.state()
        except Exception:                                  # noqa: BLE001
            return None
        enc = self._status(st, STATUS_ENCODING)
        busy = self._status(st, STATUS_BUSY)
        if enc is None and busy is None:
            return None
        return not bool(enc or 0) and not bool(busy or 0)

    def health(self) -> dict:
        """What the camera says about itself.

        This exists because a recording that aborts leaves a stub clip and no
        explanation anywhere else. The camera counts its own SD card write
        speed errors; asking beats guessing between card, battery and heat.
        """
        try:
            st = self.state()
        except Exception as exc:                           # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        g = lambda sid: self._status(st, sid)              # noqa: E731
        kb = g(STATUS_SD_REMAINING_KB)
        return {
            "battery_pct": g(STATUS_BATTERY_PCT),
            "battery_bars": g(STATUS_BATTERY_BARS),
            "overheating": g(STATUS_OVERHEATING),
            "busy": g(STATUS_BUSY),
            "encoding": g(STATUS_ENCODING),
            "encoding_s": g(STATUS_ENCODING_DURATION),
            "sd_write_speed_error": g(STATUS_SD_WRITE_SPEED_ERROR),
            "sd_errors": g(STATUS_SD_ERRORS),
            "sd_remaining_mb": None if kb is None else int(kb) // 1024,
            "remaining_video_s": g(STATUS_REMAINING_VIDEO_S),
            "preset_group": g(STATUS_PRESET_GROUP),
            "flatmode": g(STATUS_FLATMODE),
            "preset": g(STATUS_PRESET),
        }

    @staticmethod
    def in_video_mode(h: dict) -> bool | None:
        """True, False, or None when the camera did not say."""
        grp = h.get("preset_group")
        if grp is None:
            return None
        try:
            return int(grp) == PRESET_GROUP_VIDEO
        except (TypeError, ValueError):
            return None

    @staticmethod
    def health_line(h: dict) -> str:
        """One readable line for the wizard's log."""
        if h.get("error"):
            return f"camera health: unreadable ({h['error']})"
        bits = []
        if h.get("battery_pct") is not None:
            bits.append(f"battery {h['battery_pct']}%")
        elif h.get("battery_bars") is not None:
            bits.append(f"battery {h['battery_bars']}/4 bars")
        if h.get("sd_remaining_mb") is not None:
            bits.append(f"card {h['sd_remaining_mb']} MB free")
        if h.get("remaining_video_s") is not None:
            bits.append(f"{h['remaining_video_s']} s of video left")
        warn = []
        video = GoProClient.in_video_mode(h)
        grp = h.get("preset_group")
        if video is False:
            warn.append("CAMERA IS NOT IN VIDEO MODE (preset group "
                        f"{PRESET_GROUP_NAMES.get(int(grp), grp)})")
        elif video is True:
            bits.append("mode Video")
        if h.get("flatmode") is not None or h.get("preset") is not None:
            bits.append(f"flatmode {h.get('flatmode')}, preset {h.get('preset')}")
        if h.get("overheating"):
            warn.append("OVERHEATING")
        if h.get("sd_write_speed_error"):
            warn.append(f"SD CARD TOO SLOW (write speed errors: {h['sd_write_speed_error']})")
        if h.get("sd_errors"):
            warn.append(f"SD CARD ERRORS: {h['sd_errors']}")
        state = []
        if h.get("encoding"):
            secs = h.get("encoding_s")
            state.append("encoding" + (f" {secs} s" if secs else ""))
        if h.get("busy"):
            state.append("busy")
        line = "camera health: " + (", ".join(bits) if bits else "no readings")
        if state:
            line += "; " + ", ".join(state)
        if warn:
            line += "; " + "; ".join(warn)
        else:
            line += "; no card, battery or temperature warning"
        return line

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
