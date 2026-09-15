"""On-device self-test. Answers "can this phone decode video and track a ball?"
without a camera, a range, or a golf ball.

Two tiny clips ship inside the package: one H.264, one HEVC (what the HERO9
records at 1080p240). Each is a synthetic 100 mph ball. The test decodes both
and runs the real tracker. On Android this is the moment of truth for the
OpenCV build, which has no FFmpeg and relies on the phone's hardware decoder.
"""

from __future__ import annotations

import os
import pkgutil
import tempfile

import cv2

from . import calibrate, detect

TRUE_MPH = 100.0
PX_PER_M = 150.0
FPS = 240.0


def _clip_path(name: str) -> str | None:
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest", f"{name}.mp4")
    if os.path.isfile(here):
        return here
    data = pkgutil.get_data(__package__ or "flightpath", f"selftest/{name}.mp4")
    if not data:
        return None
    fd, path = tempfile.mkstemp(prefix=f"fp_selftest_{name}_", suffix=".mp4")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def run() -> dict:
    out = {"opencv": cv2.__version__, "clips": {}, "ok": False}
    any_ok = False
    for name in ("h264", "hevc"):
        r: dict = {"decoded_frames": 0, "opened": False, "tracked": False}
        try:
            path = _clip_path(name)
            if not path:
                r["error"] = "clip missing from package"
                out["clips"][name] = r
                continue
            cap = cv2.VideoCapture(path)
            r["opened"] = bool(cap.isOpened())
            r["backend"] = cap.getBackendName() if r["opened"] else None
            cap.release()
            if not r["opened"]:
                r["error"] = "VideoCapture could not open the file (no decoder for this codec)"
                out["clips"][name] = r
                continue
            frames, _ = detect.load_frames(path)
            r["decoded_frames"] = len(frames)
            if len(frames) < 8:
                r["error"] = f"only {len(frames)} frames decoded"
                out["clips"][name] = r
                continue
            cfg = detect.DetectorConfig()
            start = detect.find_impact_frame(frames)
            cands = detect.detect_candidates(frames, start, cfg)
            track = detect.fit_track(cands, FPS, cfg, frame_height=frames[0].shape[0])
            if track is None:
                r["error"] = "decoded fine but no ball track found"
                out["clips"][name] = r
                continue
            scale = calibrate.Scale(px_per_m=PX_PER_M, source="selftest")
            mph = scale.px_s_to_ms(track.speed_px_s) * 2.2369362920544
            r["tracked"] = True
            r["measured_mph"] = round(mph, 1)
            r["expected_mph"] = TRUE_MPH
            r["error_pct"] = round(100.0 * abs(mph - TRUE_MPH) / TRUE_MPH, 1)
            any_ok = True
        except Exception as exc:                           # noqa: BLE001
            r["error"] = f"{type(exc).__name__}: {exc}"
        out["clips"][name] = r
    out["ok"] = any_ok
    hevc = out["clips"].get("hevc", {})
    out["hevc_ok"] = bool(hevc.get("tracked"))
    if not out["ok"]:
        out["advice"] = ("This device cannot decode video with the bundled OpenCV. "
                         "The app will need a native decoder before it can measure shots.")
    elif not out["hevc_ok"]:
        out["advice"] = ("H.264 decodes but HEVC does not. Set the HERO9 to H.264 "
                         "(Preferences > General > Video Compression > H.264 + HEVC).")
    return out
