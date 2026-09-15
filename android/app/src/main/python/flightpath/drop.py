"""Free-fall calibration. A golf ball and a known nothing else.

Drop a ball in front of the camera and you get two things for free, because
gravity is a constant you already know:

**Scale.** Vertical position follows y(t) = y0 + v0*t + 0.5*g*t^2. Fit a
parabola to the ball in pixels and the quadratic term is 0.5 * g * (px per
metre). Since g is 9.80665, the scale falls out. No reference object, no
measuring tape, no typing.

**Rolling shutter readout time.** This is the real prize. The timing error from
row-by-row readout scales with how much of the frame height the ball crosses,
and a vertical drop crosses the most possible. That makes a drop maximally
sensitive to the readout time, which makes it the best way to measure it. If
you also know the scale from a reference object, solve for the readout time
that makes the two agree.

Neither needs a launch monitor, a range, or daylight. Both can be done on a
kitchen floor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .detect import Detection, Track, _capture_times

G = 9.80665


@dataclass
class DropFit:
    px_per_m: float
    readout_s: float
    g_residual_px: float
    n_points: int
    v0_px_s: float
    drop_px: float
    frame_height: int

    @property
    def quality(self) -> str:
        if self.n_points < 5:
            return "too few points, drop from higher or move closer"
        if self.g_residual_px > 3.0:
            return "poor parabola fit, check the ball is the only thing moving"
        return "good"


def fit_drop(dets: list[Detection], fps: float, frame_height: int,
             readout_s: float = 0.0) -> DropFit | None:
    """Fit a falling ball and read the scale off gravity."""
    if len(dets) < 4:
        return None
    dets = sorted(dets, key=lambda d: d.frame_index)

    t = _capture_times(dets, fps, frame_height, readout_s)
    y = np.array([d.y for d in dets], dtype=float)

    # y_px(t) = a t^2 + b t + c, with a = 0.5 * g * px_per_m
    a, b, c = np.polyfit(t, y, 2)
    if a <= 0:
        return None                       # not falling, or y axis flipped

    px_per_m = float(2.0 * a / G)
    resid = float(np.sqrt(np.mean((y - (a * t ** 2 + b * t + c)) ** 2)))

    return DropFit(
        px_per_m=px_per_m,
        readout_s=readout_s,
        g_residual_px=resid,
        n_points=len(dets),
        v0_px_s=float(b),
        drop_px=float(y[-1] - y[0]),
        frame_height=frame_height,
    )


def solve_readout(dets: list[Detection], fps: float, frame_height: int,
                  known_px_per_m: float,
                  lo_s: float = 0.0, hi_s: float = 0.020,
                  steps: int = 400) -> tuple[float, DropFit] | None:
    """Find the readout time that makes gravity agree with a known scale.

    Scans rather than solves analytically, because the readout time enters the
    timestamps non-linearly through the ball's own row position. The scan is
    cheap and there is exactly one crossing over any sane range.
    """
    best = None
    for i in range(steps + 1):
        r = lo_s + (hi_s - lo_s) * i / steps
        fit = fit_drop(dets, fps, frame_height, r)
        if fit is None:
            continue
        err = abs(fit.px_per_m - known_px_per_m)
        if best is None or err < best[0]:
            best = (err, r, fit)
    if best is None:
        return None
    return best[1], best[2]


def sensitivity_table(dets: list[Detection], fps: float, frame_height: int,
                      readouts_ms=(0.0, 2.0, 4.2, 6.0, 8.3)) -> list[tuple[float, float]]:
    """What scale each candidate readout time implies. Shows how much is riding
    on the unknown before you go and measure it."""
    out = []
    for ms in readouts_ms:
        fit = fit_drop(dets, fps, frame_height, ms / 1000.0)
        if fit:
            out.append((ms, fit.px_per_m))
    return out
