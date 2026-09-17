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

from . import calibrate, cameras, detect, gopro, lens
from .session import Session, Shot

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
            for _ in range(6):
                try:
                    self.client.stop_recording()
                except Exception:                          # noqa: BLE001
                    pass
                time.sleep(0.7)
                try:
                    if self.client.is_recording() is False:
                        return "recorded"
                except Exception:                          # noqa: BLE001
                    pass                                   # state still recovering
            # Never saw a clean idle across ~4 s of retries. Either the camera
            # is genuinely stuck recording or this firmware never reports idle.
            # Say so without crashing the capture.
            msg = ("sent, but could not confirm the camera stopped. "
                   "If its red light is on, press the shutter button.")
            with self._lock:
                self.last_error = f"trigger: {msg}"
            return msg
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
            if result != "recorded":
                # The shutter did not happen. Do not go looking for a clip.
                stage("failed", f"shutter: {result}")
                return

            stage("fetching")
            item = None
            for _ in range(15):
                time.sleep(1.0)
                fresh = [i for i in self.client.media_list()
                         if i.is_main_video and i.path not in before]
                if fresh:
                    item = sorted(fresh, key=lambda i: i.mtime)[-1]
                    break
            if item is None:
                stage("failed", "no new clip appeared. Make sure the camera is on "
                                "its shooting screen (press Mode), not a menu, then "
                                "try again.")
                return

            path = self.client.download(item, self.settings.clip_dir)
            self.seen.add(item.path)

            cap = cv2.VideoCapture(path)
            ok, frame = cap.read()
            cap.release()
            if not ok:
                stage("failed", "could not decode the clip")
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
                # MPEG-TS packets are 188 bytes starting with 0x47. Media3's
                # TsExtractor needs that; report whether the stream provides it.
                if data[:1] == b"\x47" or data[188:189] == b"\x47":
                    ts_sync += 1
        finally:
            s.close()
        note = f"first bytes {first.hex(' ')}"
        if n:
            note += f"; {ts_sync}/{n} look like MPEG-TS (0x47 sync)"
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

            # 2. Shutter, watched through the camera's state, not its reply.
            if not self.begin_trigger():
                self._note("shutter: busy (a shot or capture is in progress), skipped")
            else:
                try:
                    try:
                        self.stop_preview()
                    except Exception:                      # noqa: BLE001
                        pass
                    before: set[str] = set()
                    try:
                        before = {i.path for i in self.client.media_list()}
                    except Exception as exc:               # noqa: BLE001
                        self._note(f"media list before: {type(exc).__name__}: {exc}")
                    try:
                        self.client.start_recording()
                        self._note("shutter start: answered")
                    except Exception as exc:               # noqa: BLE001
                        self._note(f"shutter start: {exc}")
                    rec = None
                    for i in range(6):
                        time.sleep(0.5)
                        rec = self.client.is_recording()
                        self._note(f"  {0.5 * (i + 1):.1f}s encoding = {rec}")
                        if rec is True:
                            break
                    try:
                        self.client.stop_recording()
                        self._note("shutter stop: answered")
                    except Exception as exc:               # noqa: BLE001
                        self._note(f"shutter stop: {exc}")
                    idle = None
                    for _ in range(10):
                        time.sleep(0.5)
                        idle = self.client.is_recording()
                        if idle is not True:
                            break
                    self._note(f"after stop: encoding = {idle}"
                               + ("  STILL RECORDING, press the camera's button" if idle is True else ""))
                    time.sleep(1.0)
                    try:
                        after = {i.path for i in self.client.media_list()}
                        new = sorted(after - before)
                        self._note("new clip: " + (", ".join(new) if new else "none"))
                    except Exception as exc:               # noqa: BLE001
                        self._note(f"media list after: {type(exc).__name__}: {exc}")
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
                elif n == 0:
                    self._note(f"udp {gopro.PREVIEW_UDP_PORT}: nothing arrived in 5 s. "
                               "Camera on a menu screen? Not emitting the Open GoPro stream?")
                else:
                    self._note(f"udp {gopro.PREVIEW_UDP_PORT}: {n} datagrams, {nbytes} bytes; {note}")
                self.stop_preview()
            self._note("camera test finished")
        except Exception as exc:                           # noqa: BLE001
            self._note(f"camera test crashed: {type(exc).__name__}: {exc}")
        finally:
            self._diag_running = False

    def configure_camera(self) -> dict:
        """Put the camera into 1080p240 Linear so the user does not have to."""
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

    def _tick(self) -> None:
        if not self.camera_ok:
            self._connect()
            return

        # Heartbeat first, on the cheap status endpoint. This alone decides
        # whether the camera is still there. It has to be the same kind of
        # check _connect() uses to declare it reachable, or the two disagree
        # and the status oscillates: probe says up, the poll says down, forever.
        try:
            self.client.state()
            self._misses = 0
        except Exception as exc:                           # noqa: BLE001
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
