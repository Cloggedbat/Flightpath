"""The whole capture chain against a camera that behaves like the HERO9.

Runs the real Worker, poll loop and all, against fake_hero9: connect and
baseline, apply camera settings, a calibration capture through to a
reference frame, a shot through to a speed on the session, and the wizard's
camera test through to its verdict. A watcher thread checks the connection
never drops while any of that happens.

Run from engine/ with the venv:  .venv/Scripts/python.exe tests/test_capture_flow.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
sys.path.insert(0, ENGINE)
sys.path.insert(0, HERE)

from fake_hero9 import FakeHero9                                   # noqa: E402
from flightpath import gopro                                       # noqa: E402
from flightpath.worker import Settings, Worker                     # noqa: E402

fails: list[str] = []


def check(label: str, cond, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{(' :: ' + extra) if extra else ''}", flush=True)
    if not cond:
        fails.append(label)


def wait_for(pred, timeout_s: float, step: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="fp_flow_")
    clip = os.path.join(tmp, "shot120.mp4")
    subprocess.run([sys.executable, os.path.join(ENGINE, "make_test_clip.py"),
                    "--speed-mph", "120", "--angle", "19", "--fps", "240", "--out", clip],
                   check=True, capture_output=True)
    full = os.path.getsize(clip)

    cam = FakeHero9(clip).start()
    client = gopro.GoProClient(host="127.0.0.1", port=cam.port, control_port=cam.control_port)
    settings = Settings(clip_dir=os.path.join(tmp, "clips"),
                        session_path=os.path.join(tmp, "session.json"),
                        config_path=os.path.join(tmp, "config.json"),
                        poll_seconds=0.5)
    # The synthetic clip's 46 inch ruler sits at these pixels.
    settings.ref_px = (120.0, 980.0, 611.0, 980.0)
    settings.ref_inches = 46.0
    w = Worker(settings, client)

    # Watch the connection the whole time. Any drop is a flap.
    drops: list[str] = []
    stop_watch = threading.Event()
    phase = ["startup"]

    def watch():
        connected_once = False
        while not stop_watch.is_set():
            if w.camera_ok:
                connected_once = True
            elif connected_once:
                drops.append(f"{phase[0]}: {w.camera_note}")
                time.sleep(0.5)
            time.sleep(0.1)
    threading.Thread(target=watch, daemon=True).start()

    try:
        # --- connect and baseline ------------------------------------
        w.start()
        check("connects to the camera", wait_for(lambda: w.camera_ok, 15.0), w.camera_note)
        check("old clip on the card is baselined, not analysed",
              wait_for(lambda: "100GOPRO/GX010001.MP4" in w.seen, 5.0))

        # --- apply camera settings --------------------------------------
        phase[0] = "configure"
        r = w.configure_camera()
        cs = r.get("settings", {})
        check("configure reports ok", r.get("ok") is True, str(r.get("problems")))
        check("reads back 1080p", cs.get("resolution") == "1080p", str(cs.get("resolution")))
        check("reads back 240 fps", cs.get("fps") == 240, str(cs.get("fps")))
        check("Linear refused at 240, fell back to Linear + Horizon",
              cs.get("lens") == "Linear + Horizon", str(cs.get("lens")))
        check("HyperSmooth off", cs.get("hypersmooth") == "Off", str(cs.get("hypersmooth")))

        # --- calibration capture, end to end -------------------------------
        phase[0] = "calibration capture"
        t0 = time.monotonic()
        w.capture_reference_frame(seconds=0.5)
        dt = time.monotonic() - t0
        check("calibration stage is ready", w.calib_stage == "ready",
              f"{w.calib_stage}: {w.calib_message} ({dt:.1f} s)")
        check("reference frame is a JPEG",
              bool(w.ref_frame_jpeg) and w.ref_frame_jpeg[:2] == b"\xff\xd8")
        check("reference frame is 1920x1080", w.ref_frame_size == (1920, 1080), str(w.ref_frame_size))
        check("downloaded the full clip, not the 0 MB stub",
              bool(cam.served) and cam.served[-1][1] == full, str(cam.served))
        check("calibration clip marked seen so it is never a shot",
              any(p.endswith("GX010002.MP4") for p in w.seen))
        check("camera recorded about the asked window",
              cam.clips_recorded and cam.clips_recorded[-1]["duration_s"] < 3.0,
              str(cam.clips_recorded[-1]["duration_s"] if cam.clips_recorded else None))

        # --- a shot: /api/trigger fires in a thread, the poll loop does the rest
        phase[0] = "shot"
        check("shutter claimed for the shot", w.begin_trigger())
        threading.Thread(target=w.trigger, args=(0.5,), daemon=True).start()
        done = wait_for(lambda: w.snapshot()["shot_count"] == 1
                        or any(q["stage"] == "failed" for q in w.snapshot()["queue"]), 45.0)
        snap = w.snapshot()
        check("shot analysed by the poll loop", done and snap["shot_count"] == 1,
              str([(q["name"], q["stage"], q["message"]) for q in snap["queue"]]))
        if snap["shots"]:
            shot = snap["shots"][0]
            mph = shot.get("ball_speed_mph")
            # The worker applies the HERO9 profile's 4.2 ms rolling-shutter
            # readout, which a synthetic clip does not have, so the reading
            # sits at the top of the profile's band: 119.9 (no readout) to
            # 123.5. The tracker itself is proven to 0.1 mph by the selftest.
            check("ball speed is the synthetic 120 mph within the readout band",
                  mph is not None and 119.0 <= mph <= 124.5, str(mph))
            ang = shot.get("launch_angle_deg", shot.get("launch_deg"))
            check("launch angle is the synthetic 19 deg", ang is not None and abs(ang - 19.0) < 1.0, str(ang))
        check("no stub was ever downloaded",
              all(n == full for _, n in cam.served), str(cam.served))
        check("shot clip taken only once its size settled",
              wait_for(lambda: not w._pending_size, 3.0), str(w._pending_size))

        # --- the wizard's camera test -------------------------------------
        phase[0] = "camera test"
        # As /api/camera/test does: claim the test (which is what routes the
        # log lines to the panel), then run it.
        check("camera test claimed", w.begin_diagnostic())
        w.camera_diagnostic()
        check("camera test released", not w._diag_running)
        log = list(w.diag_log)
        verdict = [l for l in log if "verdict:" in l]
        check("camera test gives a verdict", bool(verdict), "\n".join(log[-6:]))
        if verdict:
            check("verdict: shutter ok with a clip size", "shutter ok" in verdict[0], verdict[0])
            check("verdict: stream ok, MPEG-TS found",
                  "stream ok" in verdict[0] and "MPEG-TS found" in verdict[0], verdict[0])
        check("camera test explains the shutter errors are normal",
              any("normal on this" in l for l in log))
        check("no poll failures logged anywhere", not any("camera poll" in l for l in log))
        check("camera test clip marked seen", any(p.endswith("GX010004.MP4") for p in w.seen))

        # --- and the connection ---------------------------------------------
        phase[0] = "end"
        time.sleep(1.5)
        check("connection never dropped, start to finish", not drops, "; ".join(drops))
        check("still connected at the end", w.camera_ok, w.camera_note)
        check("only the one shot on the session", w.snapshot()["shot_count"] == 1)
    finally:
        stop_watch.set()
        w.stop()
        cam.stop()

    stub_scenario(clip)
    health_scenario(clip)

    print()
    if fails:
        print(f"{len(fails)} FAILED: {fails}")
        return 1
    print("all checks passed")
    return 0


def health_scenario(clip: str) -> None:
    """When the camera counts its own SD card write speed errors, the app must
    name the card rather than offer the three-way guess."""
    print("--- camera health ---", flush=True)
    tmp = tempfile.mkdtemp(prefix="fp_health_")
    cam = FakeHero9(clip, stub_clips=True, list_lag_s=0.0,
                    write_speed_errors=7, battery_pct=64).start()
    client = gopro.GoProClient(host="127.0.0.1", port=cam.port, control_port=cam.control_port)
    w = Worker(Settings(clip_dir=os.path.join(tmp, "clips"),
                        session_path=os.path.join(tmp, "session.json"),
                        config_path=os.path.join(tmp, "config.json"),
                        poll_seconds=0.5), client)
    w.CLIP_SETTLE_S = 5.0
    try:
        w._connect()
        h = client.health()
        check("health: reads the write speed error count", h["sd_write_speed_error"] == 7, str(h))
        check("health: reads the battery", h["battery_pct"] == 64, str(h["battery_pct"]))
        check("health: card free space in MB", h["sd_remaining_mb"] == 51200, str(h["sd_remaining_mb"]))
        line = gopro.GoProClient.health_line(h)
        check("health line warns about the card", "SD CARD TOO SLOW" in line, line)

        w.capture_reference_frame(seconds=0.5)
        check("health: capture blames the card, not a guess",
              "cannot keep up with 1080p240" in w.calib_message, w.calib_message)
        check("health: capture suggests V30 or 1080p120",
              "V30" in w.calib_message and "1080p120" in w.calib_message, w.calib_message)

        # The other causes, on the reason builder itself.
        r = Worker._stub_reason(27_639, {"overheating": 1})
        check("health: overheating is named", "overheating" in r, r)
        r = Worker._stub_reason(27_639, {"sd_errors": 3})
        check("health: card errors suggest a format", "Format the card" in r, r)
        r = Worker._stub_reason(27_639, {"battery_pct": 9})
        check("health: a flat battery is named", "battery is at 9%" in r, r)
        r = Worker._stub_reason(27_639, {"sd_write_speed_error": 0, "battery_pct": 80})
        check("health: a healthy camera points at the app's shutter",
              "how this app fires the shutter" in r, r)
    finally:
        w.stop()
        cam.stop()


def stub_scenario(clip: str) -> None:
    """The camera closes every recording at 27 KB (measured 2026-09-18).
    The app must say so, in the capture and in the camera test, and must
    not call it a menu-screen problem."""
    print("--- stub clips ---", flush=True)
    tmp = tempfile.mkdtemp(prefix="fp_stub_")
    cam = FakeHero9(clip, stub_clips=True, list_lag_s=0.0).start()
    client = gopro.GoProClient(host="127.0.0.1", port=cam.port, control_port=cam.control_port)
    settings = Settings(clip_dir=os.path.join(tmp, "clips"),
                        session_path=os.path.join(tmp, "session.json"),
                        config_path=os.path.join(tmp, "config.json"),
                        poll_seconds=0.5)
    w = Worker(settings, client)
    w.CLIP_SETTLE_S = 5.0                    # the answer is the same after 5 s or 25
    w.STUB_AFTER_S = 3.0
    try:
        w._connect()
        check("stub: connected", w.camera_ok, w.camera_note)
        w.capture_reference_frame(seconds=0.5)
        check("stub: calibration capture fails", w.calib_stage == "failed", w.calib_stage)
        check("stub: says the camera stopped recording at once",
              "stopped recording almost at once" in w.calib_message, w.calib_message)
        check("stub: names the size", "26 KB" in w.calib_message, w.calib_message)
        check("stub: does not blame the menu screen", "press Mode" not in w.calib_message)
        check("stub: clip marked seen", any(p.endswith("GX010002.MP4") for p in w.seen))

        w.begin_diagnostic()
        w.camera_diagnostic()
        log = list(w.diag_log)
        check("stub: latest-clip line before the shutter",
              any("latest clip on the card before the shutter" in l for l in log))
        verdict = [l for l in log if "verdict:" in l]
        check("stub: verdict says STUB CLIP with the size",
              bool(verdict) and "STUB CLIP, 26 KB" in verdict[0], verdict[0] if verdict else "")
        check("stub: verdict does not say press Mode",
              bool(verdict) and "press Mode" not in verdict[0])

        # The poll loop, on its own, must also recognise a stub and drop it.
        w.begin_trigger()
        threading.Thread(target=w.trigger, args=(0.5,), daemon=True).start()
        w.start()
        check("stub: poll loop drops the stub and says why",
              wait_for(lambda: any(p.endswith("GX010004.MP4") for p in w.seen)
                       and "stopped recording almost at once" in w.last_error, 20.0),
              w.last_error)
        check("stub: nothing queued for analysis", not w.snapshot()["queue"], str(w.snapshot()["queue"]))
    finally:
        w.stop()
        cam.stop()


if __name__ == "__main__":
    raise SystemExit(main())
