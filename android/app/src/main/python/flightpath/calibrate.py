"""Turning pixels into real units.

One number matters: pixels per metre in the ball's flight plane. Get it by
putting an object of known length in frame, at roughly the same distance from
the camera as the ball will be, and measuring it in pixels.

An alignment stick is the natural reference. A 46 in / 1.168 m driver laid on
the mat works too. Anything is fine as long as it lies in the flight plane.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2

INCHES_PER_M = 39.3700787


@dataclass
class Scale:
    px_per_m: float
    source: str = "manual"

    def px_to_m(self, px: float) -> float:
        return px / self.px_per_m

    def px_s_to_ms(self, px_s: float) -> float:
        return px_s / self.px_per_m


def scale_from_reference(
    x1: float, y1: float, x2: float, y2: float, length_inches: float
) -> Scale:
    """Two clicked points on a reference object of known length."""
    px = math.hypot(x2 - x1, y2 - y1)
    if px <= 0:
        raise ValueError("reference points are identical")
    metres = length_inches / INCHES_PER_M
    return Scale(px_per_m=px / metres, source=f"{length_inches}in reference")


def scale_from_ball_radius(radius_px: float) -> Scale:
    """Fallback: the ball itself is a known 1.68 in diameter.

    Only trustworthy on a sharp, stationary ball before impact. A ball smeared
    across a frame in flight measures far too big and will read slow.
    """
    if radius_px <= 0:
        raise ValueError("radius must be positive")
    diameter_m = 1.68 / INCHES_PER_M
    return Scale(px_per_m=(radius_px * 2.0) / diameter_m, source="ball diameter")


def export_frame(video_path: str, frame_index: int, out_path: str) -> str:
    """Dump one frame to disk so you can open it and read off pixel coords.

    Any image viewer that shows cursor position works. So does opening it in
    a browser with the devtools inspector.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise FileNotFoundError(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise ValueError(f"could not read frame {frame_index} of {video_path}")
    cv2.imwrite(out_path, frame)
    return out_path


def effective_fps(container_fps: float, override: float | None) -> tuple[float, str]:
    """Samsung's slow-motion files are the single biggest footgun here.

    The S22 Ultra records the slow section at 480 fps but writes the file with
    a 30 fps playback rate and the frames already time-stretched. Trusting the
    container gives you a speed that is wrong by a factor of 16.

    Always pass the true capture rate explicitly.
    """
    if override:
        return override, "override"
    if container_fps >= 100:
        return container_fps, "container"
    return container_fps, "container (SUSPECT: under 100 fps)"
