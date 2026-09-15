"""Known camera profiles.

The two numbers that matter per camera are the true capture frame rate and the
sensor readout time. Readout time drives the rolling shutter correction; where
it is not published, the bound below is wide on purpose and `analyze.py`
reports the resulting spread rather than pretending to a single answer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CameraProfile:
    name: str
    fps: float
    readout_s: float          # best estimate, seconds for a full frame readout
    readout_lo_s: float       # lower bound for the sensitivity report
    readout_hi_s: float       # upper bound
    notes: str
    recommended_mode: str


PROFILES: dict[str, CameraProfile] = {
    "hero9-1080p240": CameraProfile(
        name="GoPro HERO9 Black, 1080p240",
        fps=240.0,
        readout_s=0.0042,
        readout_lo_s=0.0,
        readout_hi_s=0.0042,
        notes=(
            "240 fps is only available at 1080p on the HERO9. Rolling shutter "
            "readout is not published; the bound spans instant readout to a "
            "full frame period."
        ),
        recommended_mode="1080p240, Linear lens, Hypersmooth OFF, shutter fixed if possible",
    ),
    "hero9-1080p120": CameraProfile(
        name="GoPro HERO9 Black, 1080p120",
        fps=120.0,
        readout_s=0.0042,
        readout_lo_s=0.0,
        readout_hi_s=0.0083,
        notes="Half the frames, twice the travel between them. Use 240 if you can.",
        recommended_mode="1080p120, Linear lens, Hypersmooth OFF",
    ),
    "hero9-2p7k120": CameraProfile(
        name="GoPro HERO9 Black, 2.7K120",
        fps=120.0,
        readout_s=0.0060,
        readout_lo_s=0.0,
        readout_hi_s=0.0083,
        notes=(
            "More pixels per metre than 1080p240, which helps scale accuracy, "
            "but half the temporal samples. Worth testing against 1080p240."
        ),
        recommended_mode="2.7K120, Linear lens, Hypersmooth OFF",
    ),
    "x5-single-1080p120": CameraProfile(
        name="Insta360 X5, single lens 1080p120",
        fps=120.0,
        readout_s=0.0042,
        readout_lo_s=0.0,
        readout_hi_s=0.0083,
        notes=(
            "The X5's fastest single-lens mode. Half the HERO9's frame rate, so "
            "a 120 mph ball travels about 18 in between frames. Usable, but the "
            "HERO9 is the better primary."
        ),
        recommended_mode="Single-lens 1080p120, locked exposure",
    ),
    "x5-360-4k120": CameraProfile(
        name="Insta360 X5, 360 mode 4K120",
        fps=120.0,
        readout_s=0.0042,
        readout_lo_s=0.0,
        readout_hi_s=0.0083,
        notes=(
            "3840x1920 equirectangular over the full sphere is about 10.7 px "
            "per degree, so a ball at 10 ft is roughly 9 px across. Aiming is "
            "free because it sees everything, but you must reproject a flat "
            "view out of the sphere before the scale reference means anything, "
            "and the stitch seam is a trap. Good for launch DIRECTION, not speed."
        ),
        recommended_mode="360 4K120, reproject a flat view before analysis",
    ),
    "s22ultra-240": CameraProfile(
        name="Samsung Galaxy S22 Ultra, Slow motion",
        fps=240.0,
        readout_s=0.0042,
        readout_lo_s=0.0,
        readout_hi_s=0.0083,
        notes=(
            "Container reports ~30 fps because the file is already time "
            "stretched. Always pass the true rate."
        ),
        recommended_mode="Camera > More > Slow motion, landscape, locked AE",
    ),
    "s22ultra-480": CameraProfile(
        name="Samsung Galaxy S22 Ultra, Super Slow-mo",
        fps=480.0,
        readout_s=0.0021,
        readout_lo_s=0.0,
        readout_hi_s=0.0042,
        notes="Only about one second of capture and awkward to trigger.",
        recommended_mode="Camera > More > Super Slow-mo, Manual trigger",
    ),
}


def get(name: str) -> CameraProfile:
    key = name.lower().strip()
    if key not in PROFILES:
        raise KeyError(
            f"unknown camera '{name}'. Known: {', '.join(sorted(PROFILES))}"
        )
    return PROFILES[key]


def listing() -> str:
    lines = []
    for key, p in sorted(PROFILES.items()):
        lines.append(f"  {key:<20} {p.name}")
        lines.append(f"  {'':<20} {p.recommended_mode}")
    return "\n".join(lines)
