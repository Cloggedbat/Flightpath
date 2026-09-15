#!/usr/bin/env python3
"""Generate a synthetic high-speed clip with a KNOWN ball speed.

This is the test rig. If the pipeline cannot recover a speed we planted
ourselves, it has no business being pointed at real footage.

  python make_test_clip.py --speed-mph 120 --angle 14 --out test.mp4
"""

from __future__ import annotations

import argparse
import math

import cv2
import numpy as np

INCHES_PER_M = 39.3700787


def make_clip(out_path, speed_mph, angle_deg, fps, px_per_m, width, height,
              n_frames, noise, motion_blur):
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(out_path, fourcc, fps, (width, height))

    rng = np.random.default_rng(7)

    # Static scene: sky band, turf, a mat, an alignment stick of known length.
    base = np.zeros((height, width, 3), np.uint8)
    base[:, :] = (150, 130, 95)                        # dull sky
    horizon = int(height * 0.55)
    base[horizon:, :] = (60, 110, 55)                  # turf
    cv2.rectangle(base, (60, height - 150), (520, height - 60), (75, 125, 70), -1)

    # Reference: a 46 in object lying flat, drawn at exactly px_per_m scale.
    ref_len_px = int(round((46.0 / INCHES_PER_M) * px_per_m))
    ref_y = height - 100
    ref_x0 = 120
    cv2.line(base, (ref_x0, ref_y), (ref_x0 + ref_len_px, ref_y), (235, 235, 235), 5)

    # Clutter that should NOT be tracked: a swaying stalk of grass.
    speed_ms = speed_mph / 2.2369362920544
    theta = math.radians(angle_deg)
    vx = speed_ms * math.cos(theta) * px_per_m / fps       # px per frame
    vy = -speed_ms * math.sin(theta) * px_per_m / fps      # image y is down

    ball_r = max(3.0, (1.68 / INCHES_PER_M) * px_per_m / 2.0)
    x0, y0 = 240.0, float(height - 190)

    pre_roll = 8
    for i in range(n_frames):
        frame = base.copy()

        # Moving clutter, so the detector has something to reject.
        sway = int(6 * math.sin(i * 0.4))
        cv2.line(frame, (width - 180, height - 40),
                 (width - 180 + sway, height - 190), (70, 130, 60), 3)

        if i >= pre_roll:
            t = i - pre_roll
            cx = x0 + vx * t
            cy = y0 + vy * t
            if motion_blur > 0:
                # Smear the ball along its path, as a real short exposure does.
                steps = max(2, int(motion_blur))
                layer = np.zeros_like(frame)
                for k in range(steps):
                    f = k / steps
                    cv2.circle(layer,
                               (int(round(cx + vx * f)), int(round(cy + vy * f))),
                               int(round(ball_r)), (250, 250, 250), -1)
                frame = cv2.addWeighted(frame, 1.0, layer, 0.9, 0)
            else:
                cv2.circle(frame, (int(round(cx)), int(round(cy))),
                           int(round(ball_r)), (250, 250, 250), -1)
        else:
            cv2.circle(frame, (int(round(x0)), int(round(y0))),
                       int(round(ball_r)), (250, 250, 250), -1)

        if noise > 0:
            frame = np.clip(
                frame.astype(np.int16) + rng.normal(0, noise, frame.shape), 0, 255
            ).astype(np.uint8)

        writer.write(frame)

    writer.release()

    return {
        "true_speed_mph": speed_mph,
        "true_angle_deg": angle_deg,
        "fps": fps,
        "px_per_m": px_per_m,
        "ref_px": (ref_x0, ref_y, ref_x0 + ref_len_px, ref_y),
        "ref_inches": 46.0,
        "impact_frame": pre_roll,
    }


def make_drop(out_path, drop_from_top_frac, fps, px_per_m, width, height,
              n_frames, noise, readout_s):
    """A ball falling under real gravity, optionally with rolling shutter."""
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(out_path, fourcc, fps, (width, height))
    rng = np.random.default_rng(11)

    base = np.zeros((height, width, 3), np.uint8)
    base[:, :] = (205, 200, 195)                       # a pale wall
    cv2.rectangle(base, (0, height - 60), (width, height), (120, 115, 110), -1)

    g_px = 9.80665 * px_per_m
    ball_r = max(4.0, (1.68 / INCHES_PER_M) * px_per_m / 2.0)
    x0 = width * 0.5
    y0 = height * drop_from_top_frac
    pre = 4

    for i in range(n_frames):
        frame = base.copy()
        if i >= pre:
            t_nominal = (i - pre) / fps
            tt = t_nominal
            for _ in range(8):                          # rolling shutter is implicit
                yy = y0 + 0.5 * g_px * tt * tt
                tt = t_nominal + (yy / height) * readout_s
            cy = y0 + 0.5 * g_px * tt * tt
            if cy > height:
                cy = None
            if cy is not None:
                cv2.circle(frame, (int(round(x0)), int(round(cy))),
                           int(round(ball_r)), (250, 250, 250), -1)
        else:
            cv2.circle(frame, (int(round(x0)), int(round(y0))),
                       int(round(ball_r)), (250, 250, 250), -1)
        if noise > 0:
            frame = np.clip(frame.astype(np.int16) + rng.normal(0, noise, frame.shape),
                            0, 255).astype(np.uint8)
        writer.write(frame)
    writer.release()
    return {"px_per_m": px_per_m, "fps": fps, "readout_ms": readout_s * 1000,
            "impact_frame": pre}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", action="store_true",
                    help="render a falling ball instead of a struck one")
    ap.add_argument("--drop-from", type=float, default=0.06,
                    help="start height as a fraction of frame height")
    ap.add_argument("--readout-ms", type=float, default=0.0,
                    help="simulate this rolling shutter readout time")
    ap.add_argument("--out", default="test.mp4")
    ap.add_argument("--speed-mph", type=float, default=120.0)
    ap.add_argument("--angle", type=float, default=14.0)
    ap.add_argument("--fps", type=float, default=480.0)
    ap.add_argument("--px-per-m", type=float, default=420.0)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--noise", type=float, default=3.0)
    ap.add_argument("--motion-blur", type=int, default=4)
    args = ap.parse_args()

    if args.drop:
        meta = make_drop(args.out, args.drop_from, args.fps, args.px_per_m,
                         args.width, args.height, args.frames, args.noise,
                         args.readout_ms / 1000.0)
        print(f"wrote {args.out}")
        for k, v in meta.items():
            print(f"  {k}: {v}")
        print()
        print("verify with:")
        print(f"  python analyze.py dropcal {args.out} --fps {args.fps:g}")
        return

    meta = make_clip(args.out, args.speed_mph, args.angle, args.fps,
                     args.px_per_m, args.width, args.height, args.frames,
                     args.noise, args.motion_blur)
    print(f"wrote {args.out}")
    for k, v in meta.items():
        print(f"  {k}: {v}")
    rx = meta["ref_px"]
    print()
    print("verify with:")
    print(f"  python analyze.py shot {args.out} --fps {args.fps:g} "
          f"--ref {rx[0]},{rx[1]},{rx[2]},{rx[3]} --ref-inches 46")


if __name__ == "__main__":
    main()
