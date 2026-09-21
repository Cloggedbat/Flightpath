"""Background shot pipeline.

Two modes, switchable at runtime:

  queue  - keep hitting. New clips are pulled and processed in the background
           and results appear when they are ready. Never blocks you. This is
           OpenFlight's queue-and-sync pattern applied to file transfer.

  focus  - one shot at a time. The app waits for the clip, processes it, and
           shows the number before accepting the next. Paces your session, but
           you get the answer while the swing is still fresh.

The worker owns all camera and disk access. The web layer only reads snapshots
of its state, so nothing blocks the UI thread.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

import json

import cv2

from . import calibrate, cameras, detect, gopro, lens, nativecap
from .session import Session, Shot

# Below this a "clip" is a file the camera closed almost as soon as it opened
# it. A single second of 1080p240 HEVC is several MB. Measured 2026-09-18: the
# HERO9 wrote GX010551.MP4 at 27,639 bytes for a 3 s shutter window, and both
# the media list and the download server agreed on that size for 25 s.
MIN_CLIP_BYTES = 256 * 1024


CONFIG_KEYS = ("ref_px", "ref_inches", "club", "mode", "camera_profile")


@dataclass
class Settings:
    camera_profile: str = "hero9-1080p240"
    ref_px: tuple[float, float, float, float] | None = None
    ref_inches: float = 46.0
    lens_model_path: str | None = None
    club: str = "7i"
    mode: str = "queue"                  # queue | focus
    clip_dir: str = "clips"
    session_path: str = "session.json"
    poll_seconds: float = 3.0
    assumed_mb_per_s: float = 3.0
    config_path: str = "config.json"

    def load(self) -> None:
        """Pull saved calibration and preferences back in.

        Without this, calibration dies with the process and every restart
        means re-tapping the club. That is the kind of friction that stops a
        tool getting used.
        """
        try:
            with open(self.config_path) as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(raw.get("ref_px"), list) and len(raw["ref_px"]) == 4:
            try:
                self.ref_px = tuple(float(v) for v in raw["ref_px"])
            except (TypeError, ValueError):
                pass
        for key in ("ref_inches",):
            if isinstance(raw.get(key), (int, float)) and raw[key] > 0:
                setattr(self, key, float(raw[key]))
        for key in ("club", "mode", "camera_profile"):
            if isinstance(raw.get(key), str) and raw[key]:
                setattr(self, key, raw[key][:32])
        if self.mode not in ("queue", "focus"):
            self.mode = "queue"

    def save(self) -> None:
        data = {k: getattr(self, k) for k in CONFIG_KEYS}
        if data["ref_px"] is not None:
            data["ref_px"] = list(data["ref_px"])
        tmp = self.config_path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.config_path)
        except OSError:
            pass

    def scale(self):
        if not self.ref_px:
            return None
        return calibrate.scale_from_reference(*self.ref_px, self.ref_inches)


@dataclass
class QueueEntry:
    name: str
    size: int
    stage: str = "waiting"               # waiting | downloading | analysing | done | failed
    progress: float = 0.0
    eta_s: float = 0.0
    message: str = ""
    club: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class Worker:
    def __init__(self, settings: Settings, client: gopro.GoProClient | None = None):
        self.settings = settings
        self.settings.load()
        self.client = client or gopro.GoProClient()
        self.session = Session()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.queue: list[QueueEntry] = []
        self.seen: set[str] = set()
        self.camera_ok = False
        self.camera_note = "not connected"
        self.last_error = ""
        self.recording = False
        self._lens_model = None
        self._trigger_lock = threading.Lock()
        self.ref_frame_jpeg: bytes | None = None
        self.ref_frame_size: tuple[int, int] = (0, 0)
        self.calib_stage = "idle"      # idle | recording | fetching | ready | failed
        self.calib_message = ""
        self.camera_settings: dict = {}
        self.camera_configured = False
        self.preview_on = False
        self._misses = 0                 # consecutive failed polls
        # New clips whose size has not settled: path -> (size, monotonic when
        # that size was first seen).
        self._pending_size: dict[str, tuple[int, float]] = {}
        self._baselined = False          # seen holds everything already on the card
        self._calib_started = 0.0        # monotonic() when a calibration capture began
        self.diag_log: list[str] = []    # camera test output, one line per step, newest last
        self._diag_running = False
        self._ring: list[str] = []       # last 200 engine log lines, served at /api/log

        if os.path.exists(settings.session_path):
            try:
                self.session = Session.load(settings.session_path)
            except Exception:                              # noqa: BLE001
                pass

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    # ---------- public API used by the web layer ----------

    def snapshot(self) -> dict:
        with self._lock:
            shots = [asdict(s) for s in self.session.shots[-40:]]
            return {
                "camera_ok": self.camera_ok,
                "camera_note": self.camera_note,
                "recording": self.recording,
                "busy": self._busy_unlocked(),
                "mode": self.settings.mode,
                "club": self.settings.club,
                "calibrated": self.settings.ref_px is not None,
                "ref_inches": self.settings.ref_inches,
                "calib_stage": self.calib_stage,
                "calib_message": self.calib_message,
                "has_ref_frame": self.ref_frame_jpeg is not None,
                "camera_settings": {k: v for k, v in self.camera_settings.items() if k != "raw"},
                "camera_configured": self.camera_configured,
                "preview_on": self.preview_on,
                "preview_port": gopro.PREVIEW_UDP_PORT,
                "ref_frame_w": self.ref_frame_size[0],
                "ref_frame_h": self.ref_frame_size[1],
                "queue": [q.as_dict() for q in self.queue[-12:]],
                "queue_depth": sum(
                    1 for q in self.queue if q.stage in ("waiting", "downloading", "analysing")
                ),
                "shots": list(reversed(shots)),
                "shot_count": len(self.session.shots),
                "club_summary": self.session.club_summary(),
                "last_error": self.last_error,
                "diag": list(self.diag_log),
            }

    def set_mode(self, mode: str) -> None:
        if mode in ("queue", "focus"):
            with self._lock:
                self.settings.mode = mode
                self.settings.save()

    def set_club(self, club: str) -> None:
        with self._lock:
            self.settings.club = club.strip() or "unknown"
            self.settings.save()

    def set_reference(self, x1, y1, x2, y2, inches) -> None:
        with self._lock:
            self.settings.ref_px = (float(x1), float(y1), float(x2), float(y2))
            self.settings.ref_inches = float(inches)
            # Clear any stale "not calibrated" complaint so the UI does not keep
            # showing an error the user just fixed.
            if "not calibrated" in self.last_error:
                self.last_error = ""
            self.settings.save()

    def begin_trigger(self) -> bool:
        """Claim the shutter. False when one is already in flight.

        Without this, repeated triggers stack threads and race on the camera's
        shutter, which can leave it recording until the card fills.
        """
        if not self._trigger_lock.acquire(blocking=False):
            return False
        with self._lock:
            self.recording = True
        return True

    # How long to keep sending stop and waiting for a clean idle. Measured
    # 2026-09-17: after a 3 s clip the HERO9 refused every connection for
    # 19 s, then answered idle. Six fast refusals used to exhaust the old
    # try-count loop in 4 s and call a finished recording unconfirmed.
    STOP_CONFIRM_S = 30.0

    def _stop_and_confirm(self) -> tuple[bool, str]:
        """Send stop until state reports a clean idle or the deadline passes.

        is_recording() returns None while state is unreadable and a real
        False once the camera is idle; only that False is proof it stopped.
        Returns (confirmed, last stop error or "").
        """
        deadline = time.monotonic() + self.STOP_CONFIRM_S
        err = ""
        while time.monotonic() < deadline:
            try:
                self.client.stop_recording()
                err = ""
            except Exception as exc:                       # noqa: BLE001
                err = str(exc)
            time.sleep(0.7)
            try:
                # Idle, not "not encoding": this camera keeps the busy flag
                # set for the whole twenty seconds it spends closing a file.
                if self.client.is_idle() is True:
                    return True, err
            except Exception:                              # noqa: BLE001
                pass                                       # state still recovering
        return False, err

    # How long a new clip gets to reach a real, stable size.
    CLIP_SETTLE_S = 25.0
    # How long a tiny clip must sit unchanged before the poll loop calls it
    # a stub. The measured stub held its size for the full 25 s.
    STUB_AFTER_S = 20.0

    @staticmethod
    def _stub_reason(size_bytes: int, health: dict | None) -> str:
        """Why a recording aborted, preferring the camera's own counters over
        the three-way guess."""
        kb = size_bytes // 1024
        h = health or {}
        if h.get("sd_write_speed_error"):
            return (f"the camera stopped recording almost at once and wrote only {kb} KB, "
                    f"and it reports {h['sd_write_speed_error']} SD card write speed "
                    "errors. The card cannot keep up with 1080p240. Use a V30 card, or "
                    "drop to 1080p120 in the camera.")
        if h.get("sd_errors"):
            return (f"the camera stopped recording almost at once and wrote only {kb} KB, "
                    f"and it reports {h['sd_errors']} SD card errors. Format the card in "
                    "the camera (Preferences, Reset, Format SD Card) after copying "
                    "anything you want off it.")
        if h.get("overheating"):
            return (f"the camera stopped recording almost at once and wrote only {kb} KB, "
                    "and it reports that it is overheating. Let it cool down.")
        pct = h.get("battery_pct")
        if isinstance(pct, int) and pct <= 15:
            return (f"the camera stopped recording almost at once and wrote only {kb} KB, "
                    f"and its battery is at {pct}%. Charge it and try again.")
        return (f"the camera stopped recording almost at once and wrote only {kb} KB, "
                "and it reports no card, battery or temperature problem. Record a few "
                "seconds with the camera's own shutter button, then run Test camera: "
                "the 'latest clip' line says whether the camera records normally on "
                "its own, which would put the fault in how this app fires the shutter.")

    def _stub_after(self, before: set[str]):
        """A new main video that exists but never grew past MIN_CLIP_BYTES,
        with its size corrected from the download server, or None."""
        try:
            fresh = [i for i in self.client.media_list()
                     if i.is_main_video and i.path not in before]
        except Exception:                                  # noqa: BLE001
            return None
        if not fresh:
            return None
        item = sorted(fresh, key=lambda i: i.mtime)[-1]
        served = self.client.clip_size(item)
        if served is not None:
            item.size = served
        return item if 0 < item.size < MIN_CLIP_BYTES else None

    def _wait_for_new_clip(self, before: set[str], timeout_s: float = 25.0,
                           trace=None):
        """The newest main video not in `before`, once its size has settled.
        Returns the MediaItem with its size corrected, or None.

        Measured 2026-09-17: one second after the camera reports idle the
        media list names the new clip at 0 bytes; twenty seconds later it
        said a few KB, stable across two reads, for a clip that is tens of
        MB. The list lags the file system. So the size that counts is the
        one the download server reports for the file itself (clip_size), the
        list being the fallback, and it must clear MIN_CLIP_BYTES and be
        identical on two consecutive reads a second apart. A media list that
        errors is the camera still recovering, not a reason to give up.
        `trace`, if given, gets one line per observation.
        """
        deadline = time.monotonic() + timeout_s
        last: tuple[str, int] | None = None
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                fresh = [i for i in self.client.media_list()
                         if i.is_main_video and i.path not in before]
            except Exception:                              # noqa: BLE001
                continue
            if not fresh:
                continue
            item = sorted(fresh, key=lambda i: i.mtime)[-1]
            listed = item.size
            served = self.client.clip_size(item)
            size = served if served is not None else listed
            if trace:
                trace(f"  {item.name}: media list says {listed} B, download server says "
                      + (f"{served} B" if served is not None else "nothing"))
            if size >= MIN_CLIP_BYTES and last == (item.path, size):
                item.size = size
                return item
            last = (item.path, size)
        return None

    def trigger(self, seconds: float = 3.0) -> str:
        """Manual shutter. Call begin_trigger() first to claim it.

        Returns "recorded" once the record-then-stop cycle ran and the camera
        is idle again, otherwise a short reason. "recorded" does not promise a
        clip: whether one resulted is confirmed by the caller from the media
        list, which on this camera is the only reliable signal.

        Observed on a real HERO9 (firmware 2.0): the shutter command 404s on
        the Open GoPro path and errors, times out, or returns 500 on the
        legacy path, yet the recording still starts. Worse, the camera's whole
        HTTP stack goes unresponsive *while recording* (state returns 500, then
        times out, then drops the connection), so state cannot be read to
        confirm a recording is in progress. It becomes readable again the
        instant recording ends. So: fire start and ignore the response, record
        the full window without trusting state, then stop and retry stop until
        state reports a clean idle. That clean idle is both the proof we
        stopped and the guard against leaving the camera recording until the
        card fills.
        """
        seconds = max(0.2, min(float(seconds), 15.0))
        try:
            # Best effort. The camera kills the stream itself when the shutter
            # starts, so a failure stopping it must never cost the shot.
            try:
                self.stop_preview()
            except Exception as exc:                       # noqa: BLE001
                with self._lock:
                    self.last_error = f"live view stop: {exc}"

            # Fire start and move on. A 404, a 500, or a timeout here does not
            # mean it failed; on this camera the recording starts regardless.
            try:
                self.client.start_recording()
            except Exception:                              # noqa: BLE001
                pass

            # Record the full window. Do NOT poll state to abort early: state
            # is unreadable during recording, so an early read would be a false
            # negative on exactly the shots that are working.
            time.sleep(seconds)

            # Stop, and confirm. is_recording() returns None while state is
            # still unresponsive and a real False once recording has ended, so
            # a clean False is proof the camera stopped. Keep sending stop
            # until we see it.
            confirmed, _ = self._stop_and_confirm()
            if confirmed:
                return "recorded"
            # Never saw a clean idle inside the deadline. Either the camera is
            # genuinely stuck recording or it is still finalising the file.
            # The caller decides by the media list; say so without crashing.
            msg = (f"sent, but could not confirm the camera stopped within "
                   f"{self.STOP_CONFIRM_S:.0f} s. If its red light is on, press "
                   "the shutter button.")
            with self._lock:
                self.last_error = f"trigger: {msg}"
            return "unconfirmed"
        except Exception as exc:                           # noqa: BLE001
            with self._lock:
                self.last_error = f"trigger failed: {exc}"
            return str(exc)
        finally:
            with self._lock:
                self.recording = False
            try:
                self._trigger_lock.release()
            except RuntimeError:
                pass

    # ---------- calibration ----------

    def capture_reference_frame(self, seconds: float = 2.0) -> None:
        """Record a short clip and keep one frame for tap-to-calibrate.

        Typing pixel coordinates by hand is the worst part of the setup and the
        main reason this is not yet something you could hand to a stranger.
        Tapping two points on a picture is the same measurement without the
        arithmetic.
        """
        def stage(name, msg=""):
            with self._lock:
                self.calib_stage = name
                self.calib_message = msg

        try:
            self._calib_started = time.monotonic()
            stage("recording")
            before = {i.path for i in self.client.media_list()}
            if self.begin_trigger():
                result = self.trigger(seconds)
            else:
                stage("failed", "camera is busy")
                return
            if result not in ("recorded", "unconfirmed"):
                # The shutter did not happen. Do not go looking for a clip.
                stage("failed", f"shutter: {result}")
                return
            unconfirmed = result == "unconfirmed"
            if unconfirmed:
                # On this camera the clip is the only reliable signal, so an
                # unconfirmed stop is a reason to look harder, not to give up.
                self._note("shutter stop not confirmed; looking for the clip anyway")

            stage("fetching")
            item = self._wait_for_new_clip(before, timeout_s=self.CLIP_SETTLE_S)
            if item is None:
                stub = self._stub_after(before)
                if stub is not None:
                    # A file exists, so the shutter fired; the camera itself
                    # gave up on the recording. Not a menu-screen problem.
                    # Ask the camera why before blaming anything.
                    self.seen.add(stub.path)
                    stage("failed", f"{stub.name}: "
                          + self._stub_reason(stub.size, self.client.health()))
                elif unconfirmed:
                    stage("failed", "no new clip appeared and the camera never "
                                    "confirmed it stopped. If its red light is on, "
                                    "press the shutter button, then try again.")
                else:
                    stage("failed", "no new clip appeared. Make sure the camera is on "
                                    "its shooting screen (press Mode), not a menu, then "
                                    "try again.")
                return

            path = self.client.download(item, self.settings.clip_dir)
            self.seen.add(item.path)

            cap = nativecap.open_capture(path)
            opened = cap.isOpened()
            ok, frame = cap.read() if opened else (False, None)
            # Read the diagnostics before release() drops the decoder.
            detail = cap.info() if isinstance(cap, nativecap.NativeCapture) else {}
            cap.release()
            if not ok:
                # Decoding runs on the phone's own hardware decoder now, so a
                # failure here is no longer a codec question. Say which decoder
                # was used and what it said, and give the file size, because a
                # short file means the transfer was cut off, not the decode.
                size = os.path.getsize(path) if os.path.exists(path) else 0
                why = detail.get("error") or getattr(cap, "error", "") or "no frame returned"
                msg = (f"could not decode the clip ({item.name}, {size // 1024} KB, "
                       f"decoder {nativecap.probe()[1]}, opened={opened}): {why}")
                self._note(msg)
                stage("failed", msg)
                return

            if self.settings.lens_model_path:
                if self._lens_model is None:
                    self._lens_model = lens.LensModel.load(self.settings.lens_model_path)
                frame = lens.undistort_frames([frame], self._lens_model)[0]

            h, w = frame.shape[:2]
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
            if not ok:
                stage("failed", "could not encode the frame")
                return

            with self._lock:
                self.ref_frame_jpeg = buf.tobytes()
                self.ref_frame_size = (w, h)
                self.calib_stage = "ready"
                self.calib_message = f"{w}x{h}"
        except Exception as exc:                           # noqa: BLE001
            stage("failed", f"{type(exc).__name__}: {exc}")

    # ---------- live preview ----------

    def start_preview(self) -> dict:
        """Start the camera's UDP live view. Returns where to listen."""
        try:
            self.client.start_preview()
        except Exception as exc:                           # noqa: BLE001
            with self._lock:
                self.preview_on = False
                self.last_error = f"live view failed: {exc}"
            return {"ok": False, "error": str(exc)}
        with self._lock:
            self.preview_on = True
        return {"ok": True, "port": gopro.PREVIEW_UDP_PORT}

    def stop_preview(self) -> dict:
        with self._lock:
            was_on = self.preview_on
            self.preview_on = False
        if was_on:
            self.client.stop_preview()
        return {"ok": True}

    # ---------- the phone as its own test rig ----------

    def _note(self, msg: str) -> None:
        """One engine log line. Goes to the ring served at /api/log, and to the
        camera test panel while a test is running. No lock: list.append is
        atomic and callers may already hold self._lock."""
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self._ring.append(line)
        if len(self._ring) > 200:
            del self._ring[:-200]
        if self._diag_running:
            self.diag_log.append(line)
            if len(self.diag_log) > 200:
                del self.diag_log[:-200]

    def log_lines(self) -> list[str]:
        return list(self._ring)

    def begin_diagnostic(self) -> bool:
        """Claim the camera test. False when one is already running."""
        if self._diag_running:
            return False
        self._diag_running = True
        self.diag_log.clear()
        return True

    @staticmethod
    def _ts_offset(data: bytes) -> int | None:
        """Byte offset of the first MPEG-TS packet in a datagram, or None.

        A TS packet is 188 bytes starting with 0x47. The HERO9 wraps seven of
        them in a 12 byte header per datagram (1328 bytes, measured), so the
        sync byte sits at 12, 200, 388. Search the first 64 bytes for a 0x47
        that repeats 188 later, and again 376 later when the datagram is long
        enough to check.
        """
        n = len(data)
        for o in range(0, min(64, n - 188)):
            if data[o] != 0x47 or data[o + 188] != 0x47:
                continue
            if o + 376 < n and data[o + 376] != 0x47:
                continue
            return o
        return None

    @staticmethod
    def _udp_listen(port: int, seconds: int) -> tuple[int | None, int | None, str]:
        """Bind UDP `port` the way Media3 does (0.0.0.0, in this network-bound
        process) and count what arrives. Slots of one second, so the fake clock
        in tests cannot stall it; stops after `seconds` idle slots or 500
        datagrams. Returns (count, bytes, note) where note describes the first
        bytes and whether the payload looks like MPEG-TS, or (None, None,
        reason) if the port could not be bound."""
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        try:
            s.bind(("0.0.0.0", port))
        except OSError as exc:
            s.close()
            return None, None, f"could not bind port {port}: {exc}"
        n = total = ts_sync = 0
        first = b""
        offset: int | None = None
        per = 0
        idle = 0
        try:
            while idle < max(1, int(seconds)) and n < 500:
                try:
                    data, _ = s.recvfrom(65536)
                except socket.timeout:
                    idle += 1
                    continue
                except OSError as exc:
                    return n, total, f"receive failed after {n}: {exc}"
                n += 1
                total += len(data)
                if not first:
                    first = data[:8]
                    offset = Worker._ts_offset(data)
                    per = (len(data) - offset) // 188 if offset is not None else 0
                # MPEG-TS packets are 188 bytes starting with 0x47. The HERO9
                # wraps seven of them in a 12 byte header per datagram, so
                # check at the offset the first datagram showed, not at byte 0.
                o = offset or 0
                if data[o:o + 1] == b"\x47" and data[o + 188:o + 189] == b"\x47":
                    ts_sync += 1
        finally:
            s.close()
        note = f"first bytes {first.hex(' ')}"
        if n and offset is None:
            note += f"; no 188 byte 0x47 pattern in the first datagram, {ts_sync}/{n} sync at byte 0"
        elif n:
            note += (f"; MPEG-TS at byte {offset}, {per} packets per datagram, "
                     f"{ts_sync}/{n} datagrams sync there")
        return n, total, note

    def camera_diagnostic(self) -> None:
        """Run the experiments a developer would run with curl, against the real
        camera, and write one line per step into the wizard. This exists
        because the phone has no logcat we can reach and every guess used to
        cost a build."""
        try:
            self._note("camera test started")

            # 1. What answers, on which port, and the firmware.
            try:
                res = self.client.probe(timeout=3.0)
                for line in res.summary().splitlines():
                    self._note(line)
            except Exception as exc:                       # noqa: BLE001
                self._note(f"probe failed: {type(exc).__name__}: {exc}")

            verdict_shutter = "not run"
            verdict_stream = "not run"

            # 2. Shutter, watched through the camera's state, not its reply.
            if not self.begin_trigger():
                self._note("shutter: busy (a shot or capture is in progress), skipped")
                verdict_shutter = "skipped, camera busy"
            else:
                try:
                    try:
                        self.stop_preview()
                    except Exception:                      # noqa: BLE001
                        pass
                    before: set[str] = set()
                    try:
                        items = self.client.media_list()
                        before = {i.path for i in items}
                        # The newest clip already on the card, with its size.
                        # Record one with the camera's own button first and
                        # this line says whether the camera and card can
                        # record at all, independent of how the app fires it.
                        vids = sorted((i for i in items if i.is_main_video),
                                      key=lambda i: i.mtime)
                        if vids:
                            last = vids[-1]
                            served = self.client.clip_size(last)
                            size = served if served is not None else last.size
                            self._note(f"latest clip on the card before the shutter: "
                                       f"{last.name} ({size / 1048576:.1f} MB)")
                        # What the camera says about itself, before it is
                        # asked to do anything. A recording that aborts
                        # leaves no explanation anywhere else, and the
                        # camera counts its own card write speed errors.
                        self._note(gopro.GoProClient.health_line(self.client.health()))
                    except Exception as exc:               # noqa: BLE001
                        self._note(f"media list before: {type(exc).__name__}: {exc}")
                    try:
                        self.client.start_recording()
                        self._note("shutter start: answered")
                    except Exception as exc:               # noqa: BLE001
                        self._note("shutter start: no usable reply, which is normal on this "
                                   f"HERO9 (it records anyway; the clip line is the proof): {exc}")
                    # Record a fixed 3 s of wall time, then stop. State is
                    # unreadable while the camera records, so it is not polled
                    # during the window: each read blocks to its timeout, and
                    # six of them once turned this 3 s test into a 32 s clip.
                    time.sleep(3.0)
                    self._note("  recorded 3.0 s (state is unreadable while recording, not polled)")
                    # One health read the instant the window ends. If the
                    # camera aborted the recording, its own counters say why,
                    # and status 13 says how many seconds it actually got.
                    after_health = self.client.health()
                    self._note("after the window, " +
                               gopro.GoProClient.health_line(after_health))
                    # Stop, and keep sending stop until state reports a clean
                    # idle, exactly as trigger() does. None means state is
                    # still recovering; only a real False proves it stopped.
                    t0 = time.monotonic()
                    confirmed, stop_err = self._stop_and_confirm()
                    if stop_err:
                        self._note("shutter stop: camera refused or ignored the command while "
                                   f"closing the file, which is normal: {stop_err}")
                    self._note(f"after stop: {'idle confirmed' if confirmed else 'NOT confirmed'}"
                               f" after {time.monotonic() - t0:.0f} s"
                               + ("" if confirmed else
                                  "; if the red light is on, press the camera's button"))
                    # Same wait the calibration capture uses: the clip shows
                    # up at 0 MB while the camera is still writing it. Its
                    # settled size says how long the camera really recorded;
                    # 1080p240 HEVC is roughly 8 to 10 MB per second.
                    t1 = time.monotonic()
                    clip = self._wait_for_new_clip(before, timeout_s=self.CLIP_SETTLE_S,
                                                   trace=self._note)
                    if clip is not None:
                        # A still camera, not a shot. Mark it seen or the
                        # poll loop analyses it as one.
                        self.seen.add(clip.path)
                        self._note(f"new clip: {clip.path} ({clip.size / 1048576:.1f} MB, "
                                   f"size settled after {time.monotonic() - t1:.0f} s)"
                                   "  (not analysed as a shot)")
                        verdict_shutter = f"ok, {clip.size / 1048576:.1f} MB clip"
                    else:
                        stub = self._stub_after(before)
                        if stub is not None:
                            self.seen.add(stub.path)
                            self._note(f"new clip: {stub.path} is only {stub.size // 1024} KB "
                                       f"after {self.CLIP_SETTLE_S:.0f} s")
                            verdict_shutter = (f"STUB CLIP, {stub.size // 1024} KB: "
                                               + self._stub_reason(stub.size, after_health))
                        else:
                            self._note(f"new clip: none within {self.CLIP_SETTLE_S:.0f} s")
                            verdict_shutter = "NO CLIP (camera on a menu screen? press Mode)"
                finally:
                    with self._lock:
                        self.recording = False
                    try:
                        self._trigger_lock.release()
                    except RuntimeError:
                        pass

            # 3. Stream: does anything reach this process on UDP 8554?
            if self.preview_on:
                self.stop_preview()
                self._note("live view was on, stopped it to free port 8554")
            r = self.start_preview()
            self._note("stream start: " + ("ok" if r.get("ok") else f"failed: {r.get('error')}"))
            if r.get("ok"):
                n, nbytes, note = self._udp_listen(gopro.PREVIEW_UDP_PORT, 5)
                if n is None:
                    self._note(f"udp {gopro.PREVIEW_UDP_PORT}: {note}")
                    verdict_stream = "port 8554 could not be opened"
                elif n == 0:
                    self._note(f"udp {gopro.PREVIEW_UDP_PORT}: nothing arrived in 5 s. "
                               "Camera on a menu screen? Not emitting the Open GoPro stream?")
                    verdict_stream = "NOTHING ARRIVED"
                else:
                    self._note(f"udp {gopro.PREVIEW_UDP_PORT}: {n} datagrams, {nbytes} bytes; {note}")
                    verdict_stream = (f"ok, {n} datagrams"
                                      + (", MPEG-TS found" if "MPEG-TS at byte" in note
                                         else ", no MPEG-TS pattern"))
                self.stop_preview()
            else:
                verdict_stream = f"start failed: {r.get('error')}"
            # The lines above print the camera's raw errors, and on this
            # HERO9 a working shutter produces several. Say plainly how it
            # went, so a passing test does not read as a failing one.
            self._note(f"verdict: shutter {verdict_shutter}; stream {verdict_stream}. "
                       "Lines above saying 404, timed out or refused during the "
                       "recording are this camera's normal behaviour.")
            self._note("camera test finished")
        except Exception as exc:                           # noqa: BLE001
            self._note(f"camera test crashed: {type(exc).__name__}: {exc}")
        finally:
            self._diag_running = False

    def configure_camera(self) -> dict:
        """Put the camera into 1080p240 Linear so the user does not have to."""
        if self._camera_busy():
            # Every read-back fails while the camera recovers from a shutter,
            # and "could not read back" four times over is not an answer.
            result = {"ok": False, "settings": {}, "problems": [
                "the camera is still busy with a recording or a test. "
                "Wait a few seconds and tap Apply again."]}
            with self._lock:
                self.camera_configured = False
            return result
        try:
            result = self.client.configure_for_launch_monitor(
                fps=int(cameras.get(self.settings.camera_profile).fps)
            )
        except Exception as exc:                           # noqa: BLE001
            result = {"ok": False, "settings": {},
                      "problems": [f"{type(exc).__name__}: {exc}"]}
        with self._lock:
            self.camera_settings = result.get("settings", {})
            self.camera_configured = bool(result.get("ok"))
        return result

    def refresh_camera_settings(self) -> dict:
        got = self.client.read_settings()
        with self._lock:
            self.camera_settings = got
            raw = got.get("raw", {})
            self.camera_configured = (
                raw.get("res") == gopro.RESOLUTION_1080
                and raw.get("fps") in (gopro.FPS_240, gopro.FPS_120)
                and raw.get("lens") in (gopro.LENS_LINEAR, gopro.LENS_LINEAR_HORIZON)
            )
        return got

    def clear_session(self) -> None:
        with self._lock:
            self.session = Session()
            self._save()

    # ---------- the loop ----------

    def _run(self) -> None:
        self._connect()
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:                       # noqa: BLE001
                with self._lock:
                    self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(self.settings.poll_seconds)

    def connect_now(self) -> dict:
        """User pressed Connect. Probe quickly and report, do not make them wait
        for the background loop's next tick."""
        self._connect(timeout=2.0)
        with self._lock:
            return {
                "camera_ok": self.camera_ok,
                "camera_note": self.camera_note,
                "camera_settings": {k: v for k, v in self.camera_settings.items() if k != "raw"},
            }

    def _connect(self, timeout: float = 4.0) -> None:
        res = self.client.probe(timeout=timeout)
        # Reachable has to mean the endpoint the heartbeat uses answered, not
        # just any endpoint. Otherwise probe says up on /version while _tick
        # says down on /state and the status oscillates on a 12 s period.
        ok = res.reachable and "state" in res.working
        with self._lock:
            self.camera_ok = ok
            if ok:
                # A fresh connection gets a fresh 3-strike budget. Without this
                # the counter stays at 3 forever after the first drop, so every
                # later failed poll re-trips it and the status square-waves.
                self._misses = 0
            self.camera_note = (
                f"connected ({res.firmware})" if ok and res.firmware
                else "connected" if ok
                else "camera answers but its status endpoint does not" if res.reachable
                else "no camera at 10.5.5.9"
            )
        if ok:
            try:
                # Baseline: everything already on the card is old news.
                for item in self.client.media_list():
                    self.seen.add(item.path)
                self._baselined = True
            except Exception as exc:                       # noqa: BLE001
                # Not silent any more. _baselined is left as it was: False on
                # a cold start, so _tick treats its first good media list as
                # the baseline instead of queueing the whole SD card; True on
                # a reconnect, where seen already holds the history and clips
                # recorded since are picked up normally.
                with self._lock:
                    self.last_error = f"media list baseline: {type(exc).__name__}: {exc}"
                    if not self._baselined:
                        self.camera_note = "connected, but no media list (SD card in?)"
            try:
                self.refresh_camera_settings()
            except Exception:                              # noqa: BLE001
                pass

    def _busy_unlocked(self) -> bool:
        """True while something this worker started has the camera tied up:
        a shot trigger, a camera test, or a calibration capture. Its HTTP
        stack is unresponsive while it records, by design, so during these a
        failed poll says nothing about whether the camera is still there."""
        calibrating = (self.calib_stage in ("recording", "fetching")
                       and time.monotonic() - self._calib_started < 60.0)
        return bool(self.recording or self._diag_running or calibrating)

    def _camera_busy(self) -> bool:
        with self._lock:
            return self._busy_unlocked()

    def _tick(self) -> None:
        if not self.camera_ok:
            self._connect()
            return

        if self._camera_busy():
            # Polling now would only count strikes against a camera that is
            # doing exactly what it was told, and the connection would drop
            # and reconnect around every capture. Do not touch the miss
            # counter, do not list media (the capture owns whatever clip
            # appears), come back next tick.
            self._misses = 0
            return

        # Heartbeat first, on the cheap status endpoint. This alone decides
        # whether the camera is still there. It has to be the same kind of
        # check _connect() uses to declare it reachable, or the two disagree
        # and the status oscillates: probe says up, the poll says down, forever.
        try:
            self.client.state()
            self._misses = 0
        except Exception as exc:                           # noqa: BLE001
            if self._camera_busy():
                # A capture or test started while this poll was in flight
                # (seen on the phone: a poll sent just before Test camera
                # came back 500 nine seconds later, from a camera that was by
                # then recording on purpose). Not a strike.
                self._misses = 0
                return
            # One failed poll is not a lost camera. The HERO9 answers slowly or
            # with an error while it is busy (just after a recording, or when
            # the GoPro Quik app is also talking to it). Three in a row is.
            self._misses += 1
            self._note(f"camera poll {self._misses}/3 failed: {type(exc).__name__}: {exc}")
            with self._lock:
                self.last_error = (
                    f"camera poll {self._misses}/3 failed: {type(exc).__name__}: {exc}"
                )
                if self._misses >= 3:
                    self.camera_ok = False
                    self.camera_note = f"lost camera: {type(exc).__name__}"
                else:
                    self.camera_note = f"connected, camera busy ({self._misses}/3)"
            return

        with self._lock:
            if self.last_error.startswith("camera poll"):
                self.last_error = ""
            if self.camera_note.startswith("connected, camera busy"):
                self.camera_note = "connected"
            calibrating = (self.calib_stage in ("recording", "fetching")
                           and time.monotonic() - self._calib_started < 60.0)
            previewing = self.preview_on
        if previewing:
            # The legacy preview stream dies without traffic on the camera's
            # control port. Cheap and best effort.
            self.client.stream_keep_alive()
        if calibrating:
            # A calibration capture is in flight on another thread and owns
            # whatever clip appears next; it adds it to seen once downloaded.
            # Listing here would race it for the SD card and then analyse a
            # 2 s clip of a still club as a golf shot. The 60 s cap means a
            # capture that hangs cannot stall shot processing for good.
            return

        # Work. The media list walks the whole SD card and the HERO9 stalls or
        # errors on it while busy. That is a queue hiccup, never a lost camera,
        # so it must not touch camera_ok or the miss counter.
        try:
            self.client.keep_alive()        # best effort; state() already proved liveness
        except Exception:                                  # noqa: BLE001
            pass
        try:
            items = self.client.media_list()
        except Exception as exc:                           # noqa: BLE001
            self._note(f"media list: {type(exc).__name__}: {exc}")
            with self._lock:
                self.last_error = f"media list: {type(exc).__name__}: {exc}"
                if not self._baselined:
                    # It has never answered since we connected. That is not a
                    # hiccup, it is a camera with no SD card or a card that has
                    # never been recorded on. Say so where the user is looking.
                    self.camera_note = "connected, but no media list (SD card in?)"
            return
        with self._lock:
            if self.last_error.startswith("media list"):
                self.last_error = ""
            if self.camera_note.startswith("connected, but"):
                self.camera_note = "connected"
        if not self._baselined:
            # The connect-time baseline failed on a cold start. This list is
            # the baseline: everything on the card is old news, none of it work.
            for i in items:
                self.seen.add(i.path)
            self._baselined = True
            return
        fresh = [i for i in items if i.is_main_video and i.path not in self.seen]
        fresh.sort(key=lambda i: i.mtime)
        # A clip shows up in the list at size 0 while the camera is still
        # writing it (measured: 0 MB one second after idle). Take it only
        # once its size is non-zero and unchanged since the previous tick,
        # the polling twin of _wait_for_new_clip. It is not in seen yet, so
        # it comes round again next tick.
        now = time.monotonic()
        settled = []
        for i in fresh:
            # Ask the download server, not the list: the list lags the file
            # by seconds and can say a few KB for a clip that is tens of MB.
            served = self.client.clip_size(i)
            size = served if served is not None else i.size
            prev = self._pending_size.get(i.path)
            if prev is None or prev[0] != size:
                self._pending_size[i.path] = (size, now)
                continue
            if size >= MIN_CLIP_BYTES:
                i.size = size
                settled.append(i)
                self._pending_size.pop(i.path, None)
            elif size > 0 and now - prev[1] >= self.STUB_AFTER_S:
                # Tiny and unchanged for a long time: the camera gave up on
                # this recording. Say so once and stop asking about it. Two
                # equal reads are not enough; a file still being written can
                # show the same size twice.
                self.seen.add(i.path)
                self._pending_size.pop(i.path, None)
                why = f"{i.name}: " + self._stub_reason(size, self.client.health())
                self._note(why)
                with self._lock:
                    self.last_error = why
        fresh = settled

        if self.settings.mode == "focus":
            fresh = fresh[:1]

        for item in fresh:
            if self._stop.is_set():
                return
            self.seen.add(item.path)
            self._process(item)

    def _process(self, item: gopro.MediaItem) -> None:
        entry = QueueEntry(
            name=item.name,
            size=item.size,
            eta_s=gopro.transfer_estimate_s(item.size, self.settings.assumed_mb_per_s),
            club=self.settings.club,
        )
        with self._lock:
            self.queue.append(entry)

        def on_progress(got, total, elapsed):
            with self._lock:
                entry.progress = got / total if total else 0.0
                if elapsed > 1 and got:
                    rate = got / elapsed
                    entry.eta_s = max(0.0, (total - got) / rate)

        try:
            with self._lock:
                entry.stage = "downloading"
            path = self.client.download(item, self.settings.clip_dir, on_progress)
        except Exception as exc:                           # noqa: BLE001
            with self._lock:
                entry.stage = "failed"
                entry.message = f"download failed: {type(exc).__name__}"
                self.last_error = entry.message
            return

        with self._lock:
            entry.stage = "analysing"
            entry.progress = 1.0
            entry.eta_s = 0.0

        try:
            shot = self._analyse(path, entry.club)
        except Exception as exc:                           # noqa: BLE001
            with self._lock:
                entry.stage = "failed"
                entry.message = f"{type(exc).__name__}: {exc}"
                self.last_error = entry.message + "\n" + traceback.format_exc(limit=2)
            return

        with self._lock:
            if shot is None:
                entry.stage = "failed"
                entry.message = "no ball track found"
            else:
                entry.stage = "done"
                entry.message = f"{shot.ball_speed_mph:.1f} mph"
                self.session.add(shot)
                self._save()

    def _analyse(self, path: str, club: str) -> Shot | None:
        scale = self.settings.scale()
        if scale is None:
            raise RuntimeError(
                "not calibrated: set the reference object before hitting"
            )
        profile = cameras.get(self.settings.camera_profile)

        frames, container_fps = detect.load_frames(path, window=True)
        if self.settings.lens_model_path:
            if self._lens_model is None:
                self._lens_model = lens.LensModel.load(self.settings.lens_model_path)
            frames = lens.undistort_frames(frames, self._lens_model)

        fps, _ = calibrate.effective_fps(container_fps, profile.fps)
        cfg = detect.DetectorConfig()
        start = detect.find_impact_frame(frames)
        candidates = detect.detect_candidates(frames, start, cfg)
        track = detect.fit_track(
            candidates, fps, cfg,
            frame_height=frames[0].shape[0],
            readout_s=profile.readout_s,
        )
        if track is None:
            return None
        return Shot.from_track(track, scale, club=club,
                               source_clip=os.path.basename(path))

    def _save(self) -> None:
        try:
            self.session.save(self.settings.session_path)
        except Exception:                                  # noqa: BLE001
            pass
