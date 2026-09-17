#!/usr/bin/env python3
"""FlightPath V0 command line.

  # 1. pull a frame so you can measure your reference object
  python analyze.py frame clip.mp4 --index 0 --out ref.png

  # 2. analyse a shot
  python analyze.py shot clip.mp4 \
      --fps 480 --ref 310,880,1180,880 --ref-inches 46 --club 7i

  # 3. roll a whole folder of clips into a session
  python analyze.py session ./clips --fps 480 \
      --ref 310,880,1180,880 --ref-inches 46 --out session.json
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

from flightpath import calibrate, cameras, detect, drop, lens, physics
from flightpath.session import Session, Shot


def _build_scale(args) -> calibrate.Scale:
    if not args.ref:
        raise SystemExit(
            "need --ref x1,y1,x2,y2 with --ref-inches. Run the 'frame' command "
            "first to export a still and read the coordinates off it."
        )
    try:
        x1, y1, x2, y2 = (float(v) for v in args.ref.split(","))
    except ValueError:
        raise SystemExit("--ref must be four comma-separated numbers: x1,y1,x2,y2")
    return calibrate.scale_from_reference(x1, y1, x2, y2, args.ref_inches)


def _resolve_camera(args):
    """Camera profile supplies fps and readout time; explicit flags win."""
    profile = cameras.get(args.camera) if args.camera else None
    fps = args.fps or (profile.fps if profile else None)
    if args.readout_ms is not None:
        readout = args.readout_ms / 1000.0
        lo = hi = readout
    elif profile:
        readout, lo, hi = profile.readout_s, profile.readout_lo_s, profile.readout_hi_s
    else:
        readout = lo = hi = 0.0
    return profile, fps, readout, lo, hi


def _analyse_clip(path: str, scale, args):
    frames, container_fps = detect.load_frames(path, window=True)
    profile, fps_arg, readout, r_lo, r_hi = _resolve_camera(args)
    fps, fps_note = calibrate.effective_fps(container_fps, fps_arg)

    if args.lens:
        model = lens.LensModel.load(args.lens)
        frames = lens.undistort_frames(frames, model)

    frame_height = frames[0].shape[0]

    cfg = detect.DetectorConfig(
        diff_threshold=args.threshold,
        min_area_px=args.min_area,
        max_area_px=args.max_area,
        min_circularity=args.min_circularity,
        max_track_frames=args.max_frames,
        ransac_tolerance_px=args.tolerance,
        min_track_points=args.min_points,
    )

    start = args.start if args.start is not None else detect.find_impact_frame(frames)
    candidates = detect.detect_candidates(frames, start, cfg)
    track = detect.fit_track(candidates, fps, cfg,
                             frame_height=frame_height, readout_s=readout)

    meta = {
        "profile": profile,
        "fps": fps,
        "fps_note": fps_note,
        "readout_s": readout,
        "readout_lo_s": r_lo,
        "readout_hi_s": r_hi,
        "start": start,
        "n_candidates": len(candidates),
        "n_frames": len(frames),
        "frame_height": frame_height,
        "undistorted": bool(args.lens),
    }
    return track, meta


def cmd_frame(args) -> int:
    out = calibrate.export_frame(args.video, args.index, args.out)
    print(f"wrote {out}")
    print("Open it, find both ends of your reference object, note the pixel")
    print("coordinates, then pass them as --ref x1,y1,x2,y2")
    return 0


def cmd_shot(args) -> int:
    scale = _build_scale(args)
    track, m = _analyse_clip(args.video, scale, args)
    fps = m["fps"]

    print(f"clip           {os.path.basename(args.video)}")
    if m["profile"]:
        print(f"camera         {m['profile'].name}")
    print(f"frames read    {m['n_frames']}")
    print(f"fps used       {fps:g}  ({m['fps_note']})")
    print(f"impact frame   {m['start']}")
    print(f"candidates     {m['n_candidates']}")
    print(f"scale          {scale.px_per_m:.1f} px/m  ({scale.source})")
    print(f"lens           {'undistorted from ' + args.lens if m['undistorted'] else 'as shot (use Linear FOV)'}")
    print(f"rolling shutter {m['readout_s'] * 1000:.2f} ms readout")
    print()

    if track is None:
        print("NO TRACK FOUND.")
        print()
        print("Things to try, in order of how often they are the problem:")
        print("  1. --fps is wrong. Samsung writes slow-mo as 30 fps files.")
        print("  2. --threshold too high. Try 15 for a dull or overcast clip.")
        print("  3. --min-circularity 0.2, a fast ball smears into a streak.")
        print("  4. --start N to point it at the real impact frame.")
        print("  5. --min-points 3 if the ball leaves frame very fast.")
        return 1

    shot = Shot.from_track(
        track, scale,
        club=args.club,
        source_clip=os.path.basename(args.video),
        backspin_rpm=args.spin,
    )

    bar = "#" * int(round(shot.confidence * 20))
    print(f"  ball speed     {shot.ball_speed_mph:>7.1f} mph")
    print(f"  launch angle   {shot.launch_angle_deg:>7.1f} deg")
    print(f"  carry          {shot.carry_yards:>7.1f} yds")
    print(f"  apex           {shot.apex_feet:>7.1f} ft")
    print(f"  hang time      {shot.hang_time_s:>7.2f} s")
    print(f"  descent        {shot.descent_angle_deg:>7.1f} deg")
    spin_tag = "estimated" if shot.spin_is_estimated else "supplied"
    print(f"  backspin       {shot.backspin_rpm:>7.0f} rpm  ({spin_tag})")
    print()
    print(f"  confidence     {shot.confidence:>7.2f}  [{bar:<20}]")
    print(f"  track          {shot.n_frames} frames, {shot.residual_px:.2f} px residual")

    if shot.confidence < 0.5:
        print()
        print("  Low confidence. Treat this number as a hint, not data.")

    # Rolling shutter sensitivity: how much is riding on an unknown.
    if m["readout_hi_s"] > m["readout_lo_s"]:
        lo_px = detect.rolling_shutter_span(track, m["readout_lo_s"])
        hi_px = detect.rolling_shutter_span(track, m["readout_hi_s"])
        lo = physics.ms_to_mph(scale.px_s_to_ms(lo_px))
        hi = physics.ms_to_mph(scale.px_s_to_ms(hi_px))
        spread = abs(hi - lo)
        pct = 100.0 * spread / max(shot.ball_speed_mph, 1e-6)
        print()
        print(f"  rolling shutter band  {min(lo, hi):.1f} to {max(lo, hi):.1f} mph"
              f"  ({pct:.1f}% of reading)")
        if pct > 2.0:
            print("  Wide band. Frame the shot so the ball climbs less of the")
            print("  image height, or measure your sensor readout time once.")

    if not 25.0 <= shot.ball_speed_mph <= 220.0:
        ratio = 120.0 / max(shot.ball_speed_mph, 0.1)
        print()
        print(f"  IMPLAUSIBLE SPEED ({shot.ball_speed_mph:.1f} mph).")
        print(f"  Almost always --fps. You passed {fps:g}; if the real capture")
        print(f"  rate were about {fps * ratio:.0f} this would land near 120 mph.")
        print("  Second most likely: --ref pixels or --ref-inches are wrong.")
    return 0


def cmd_dropcal(args) -> int:
    """Drop a ball. Gravity gives you the scale, and the readout time."""
    frames, container_fps = detect.load_frames(args.video)
    profile, fps_arg, _, _, _ = _resolve_camera(args)
    fps, fps_note = calibrate.effective_fps(container_fps, fps_arg)
    h = frames[0].shape[0]

    # A drop needs a LONG tracking window. At 240 fps a ball falls only 5 cm in
    # the 24 frames a struck shot uses, which is not enough travel to measure
    # anything. Half a second of fall is about 120 frames and 1.2 m.
    window = args.max_frames if args.max_frames != 24 else 400
    cfg = detect.DetectorConfig(
        diff_threshold=args.threshold,
        min_area_px=args.min_area,
        max_area_px=args.max_area,
        min_circularity=args.min_circularity,
        max_track_frames=window,
        background_mode="global",
    )
    # A drop has no impact to find. The ball is moving from the first frame, so
    # find_impact_frame locks onto the fastest motion at the END of the fall and
    # throws away everything before it. Start at the top instead.
    start = args.start if args.start is not None else 0
    cands = detect.detect_candidates(frames, start, cfg)

    # One detection per frame: the lowest plausible blob is the falling ball.
    by_frame = {}
    for d in cands:
        cur = by_frame.get(d.frame_index)
        if cur is None or d.y > cur.y:
            by_frame[d.frame_index] = d
    dets = [by_frame[k] for k in sorted(by_frame)]

    print(f"clip           {os.path.basename(args.video)}")
    print(f"fps used       {fps:g}  ({fps_note})")
    print(f"frame height   {h} px")
    print(f"points tracked {len(dets)}")
    print()

    if len(dets) < 4:
        print("NOT ENOUGH POINTS. Drop from higher, move closer, or lower --threshold.")
        return 1

    fit = drop.fit_drop(dets, fps, h, 0.0)
    if fit is None:
        print("Could not fit a falling parabola. Is the ball the only thing moving?")
        return 1

    print(f"  ball fell through {fit.drop_px:.0f} px "
          f"({100.0 * fit.drop_px / h:.0f}% of frame height)")
    print(f"  parabola residual {fit.g_residual_px:.2f} px  ({fit.quality})")
    print()

    if args.known_px_per_m:
        solved = drop.solve_readout(dets, fps, h, args.known_px_per_m)
        if solved:
            r, f2 = solved
            print(f"  YOUR SENSOR READOUT TIME: {r * 1000:.2f} ms")
            print(f"  (the value that makes gravity agree with your "
                  f"{args.known_px_per_m:.1f} px/m reference)")
            print()
            print(f"  Put this in flightpath/cameras.py for "
                  f"'{args.camera or 'your profile'}':")
            print(f"      readout_s={r:.5f}, readout_lo_s={r:.5f}, readout_hi_s={r:.5f}")
        else:
            print("  Could not solve for readout time.")
    else:
        print(f"  SCALE FROM GRAVITY: {fit.px_per_m:.1f} px/m "
              f"(assuming zero readout time)")
        print()
        print("  How much the unknown readout time is worth:")
        for ms, s_ in drop.sensitivity_table(dets, fps, h):
            print(f"    readout {ms:>4.1f} ms  ->  {s_:>7.1f} px/m")
        print()
        print("  Pass --known-px-per-m from your reference-object calibration")
        print("  and this will solve for your actual readout time.")
    return 0


def cmd_selftest(args) -> int:
    """Decode + track the bundled clips. Proves the stack on this machine."""
    import json as _json
    from flightpath import selftest
    r = selftest.run()
    print(_json.dumps(r, indent=2))
    return 0 if r.get("ok") else 1


def cmd_cameras(args) -> int:
    print("Known camera profiles (pass with --camera):")
    print()
    print(cameras.listing())
    return 0


def cmd_session(args) -> int:
    scale = _build_scale(args)
    clips = sorted(
        p for ext in ("mp4", "MP4", "mov", "MOV")
        for p in glob.glob(os.path.join(args.folder, f"*.{ext}"))
    )
    if not clips:
        raise SystemExit(f"no clips found in {args.folder}")

    sess = Session()
    failed = []
    for path in clips:
        try:
            track, _meta = _analyse_clip(path, scale, args)
        except Exception as exc:                       # noqa: BLE001
            failed.append((os.path.basename(path), str(exc)))
            continue
        if track is None:
            failed.append((os.path.basename(path), "no track"))
            continue
        club = args.club or _club_from_name(os.path.basename(path))
        sess.add(Shot.from_track(track, scale, club=club,
                                 source_clip=os.path.basename(path)))

    print(f"{len(sess.shots)} of {len(clips)} clips tracked")
    if failed:
        print(f"{len(failed)} failed:")
        for name, why in failed[:10]:
            print(f"  {name}: {why}")
    print()

    rows = sess.club_summary()
    if rows:
        print(f"{'club':<8}{'n':>4}{'carry':>9}{'sd':>7}{'min':>7}{'max':>7}{'ball':>8}")
        print("-" * 50)
        for r in rows:
            print(f"{r['club']:<8}{r['shots']:>4}{r['carry_avg']:>9.1f}"
                  f"{r['carry_sd']:>7.1f}{r['carry_min']:>7.1f}"
                  f"{r['carry_max']:>7.1f}{r['ball_speed_avg']:>8.1f}")
        print()

    sess.save(args.out)
    print(f"wrote {args.out}")
    return 0


def _club_from_name(name: str) -> str:
    """Name clips like '7i_003.mp4' or 'driver_001.mp4' and it sorts itself."""
    stem = os.path.splitext(name)[0]
    return stem.split("_")[0].lower() if "_" in stem else "unknown"


def _add_common(p):
    p.add_argument("--camera", type=str, default=None,
                   help="camera profile, e.g. hero9-1080p240. Supplies fps and "
                        "sensor readout time. Run 'cameras' to list them.")
    p.add_argument("--lens", type=str, default=None,
                   help="lens model JSON from the 'calibrate' command. Not "
                        "needed if you shot in Linear FOV.")
    p.add_argument("--readout-ms", type=float, default=None,
                   help="sensor rolling shutter readout time in ms; overrides "
                        "the camera profile")
    p.add_argument("--fps", type=float, default=None,
                   help="TRUE capture rate. S22 Ultra super slow-mo is 480. "
                        "Do not trust the container.")
    p.add_argument("--ref", type=str, default=None,
                   help="x1,y1,x2,y2 pixel endpoints of your reference object")
    p.add_argument("--ref-inches", type=float, default=46.0,
                   help="real length of that object in inches (default 46, a driver)")
    p.add_argument("--club", type=str, default=None)
    p.add_argument("--spin", type=float, default=None,
                   help="measured backspin rpm, if you have it from elsewhere")
    p.add_argument("--start", type=int, default=None, help="force the impact frame")
    p.add_argument("--threshold", type=int, default=28)
    p.add_argument("--min-area", type=float, default=6.0)
    p.add_argument("--max-area", type=float, default=4000.0)
    p.add_argument("--min-circularity", type=float, default=0.15)
    p.add_argument("--max-frames", type=int, default=24)
    p.add_argument("--tolerance", type=float, default=3.0)
    p.add_argument("--min-points", type=int, default=4)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="flightpath", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("frame", help="export a still for calibration")
    f.add_argument("video")
    f.add_argument("--index", type=int, default=0)
    f.add_argument("--out", default="ref.png")
    f.set_defaults(func=cmd_frame)

    s = sub.add_parser("shot", help="analyse one clip")
    s.add_argument("video")
    _add_common(s)
    s.set_defaults(func=cmd_shot)

    d = sub.add_parser("dropcal",
                       help="drop a ball: get scale and rolling shutter readout")
    d.add_argument("video")
    d.add_argument("--known-px-per-m", type=float, default=None,
                   help="scale from your reference object; supply it and this "
                        "solves for your sensor readout time")
    _add_common(d)
    d.set_defaults(func=cmd_dropcal)

    st = sub.add_parser("selftest", help="decode and track the bundled test clips")
    st.set_defaults(func=cmd_selftest)

    c = sub.add_parser("cameras", help="list known camera profiles")
    c.set_defaults(func=cmd_cameras)

    e = sub.add_parser("session", help="analyse a folder of clips")
    e.add_argument("folder")
    e.add_argument("--out", default="session.json")
    _add_common(e)
    e.set_defaults(func=cmd_session)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
