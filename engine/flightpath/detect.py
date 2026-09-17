"""Ball detection and track fitting from a high-speed clip.

Geometry assumed: camera side-on, sensor plane roughly parallel to the
ball's flight plane. That is the geometry where speed and launch angle fall
straight out of the image with no 3D inference and no model.

Pipeline, mirroring OpenFlight's shape (trigger -> buffer -> extract):
  1. Build a static background from the quiet frames before impact.
  2. Difference every frame against it to find moving blobs.
  3. Filter blobs to ball-like candidates (area, circularity, brightness).
  4. RANSAC a physically consistent track through the candidates.
  5. Report velocity in px/s, which calibrate.py turns into m/s.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import nativecap


def to_gray(frame: np.ndarray) -> np.ndarray:
    """Single channel view of a decoded frame.

    The native Android decoder hands back the luma plane already, so a frame
    can arrive with two dimensions or three depending on the platform.
    """
    if frame.ndim == 2:
        return frame
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


@dataclass
class Detection:
    frame_index: int
    x: float          # px, image coords
    y: float          # px, image coords (down is positive)
    radius: float     # px
    area: float


@dataclass
class Track:
    detections: list[Detection]
    vx_px_s: float
    vy_px_s: float          # image convention: positive is downward
    x0: float
    y0: float
    t0_frame: int
    residual_px: float
    fps: float
    readout_s: float = 0.0
    frame_height: int = 0

    @property
    def speed_px_s(self) -> float:
        return math.hypot(self.vx_px_s, self.vy_px_s)

    @property
    def launch_angle_deg(self) -> float:
        """Positive is upward, converting out of image coords."""
        return math.degrees(math.atan2(-self.vy_px_s, abs(self.vx_px_s)))

    @property
    def n_frames(self) -> int:
        return len(self.detections)

    @property
    def confidence(self) -> float:
        """0-1. More frames and tighter residual both help.

        This mirrors OpenFlight's per-metric confidence score. It is a data
        quality indicator, not an accuracy guarantee.
        """
        frame_score = min(1.0, (self.n_frames - 2) / 6.0) if self.n_frames > 2 else 0.0
        fit_score = 1.0 / (1.0 + self.residual_px / 2.0)
        return max(0.0, min(1.0, 0.5 * frame_score + 0.5 * fit_score))


@dataclass
class DetectorConfig:
    min_area_px: float = 6.0
    max_area_px: float = 4000.0
    # A ball at driver speed smears into a long capsule, whose circularity can
    # fall to 0.2 or below. Keep this loose and let RANSAC do the real
    # rejection: clutter does not lie on a straight, fast, consistent track.
    min_circularity: float = 0.15
    diff_threshold: int = 28
    background_frames: int = 20
    max_track_frames: int = 24
    ransac_iterations: int = 400
    ransac_tolerance_px: float = 3.0
    min_track_points: int = 4
    blur_kernel: int = 3
    # "pre" builds the background from the quiet frames before impact, which is
    # right for a struck ball. "global" takes the median of the whole clip,
    # which is right for a drop: the ball never rests, but it occupies any one
    # pixel for only a few frames out of hundreds, so the median is clean.
    background_mode: str = "pre"
    roi: tuple[int, int, int, int] | None = None   # x, y, w, h
    debug_frames: list = field(default_factory=list)


MAX_DECODE_BYTES = 512 * 1024 * 1024


def load_frames(path: str, max_frames: int = 400,
                max_bytes: int = MAX_DECODE_BYTES,
                window: bool = False, pre_frames: int = 24,
                post_frames: int = 60, max_scan_frames: int = 6000,
                ) -> tuple[list[np.ndarray], float]:
    """Read a clip into memory. Returns (frames_gray, container_fps).

    Capped by BYTES, not just frame count. 400 frames of grayscale 4K is about
    3.3 GB, which is an instant out-of-memory kill on a phone. A frame count
    alone does not bound memory; frame size varies by 16x across the modes
    these cameras offer.

    window=False keeps the head of the clip up to the cap. Right for a drop
    test, where the ball is moving from frame one, and for short clips.

    window=True is for a struck shot. At 1080p the cap is 258 frames, which
    is 1.07 s at 240 fps, and a golfer cannot be made to strike inside the
    first second of a clip. So instead of keeping the head, scan the clip
    frame by frame with the same rule find_impact_frame uses, hold a ring of
    `pre_frames` quiet frames, and stop `post_frames` after the first hard
    change. Memory is bounded by pre + post rather than by clip length: a 6 s
    clip with the strike at 4 s comes back as an 85 frame window around the
    strike. If no change is found and the clip fit inside the cap anyway, fall
    back to the head so short clips behave exactly as before.
    """
    cap = nativecap.open_capture(path)
    if not cap.isOpened():
        raise FileNotFoundError(f"could not open video: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if w > 0 and h > 0:
        max_frames = max(8, min(max_frames, max_bytes // max(w * h, 1)))

    if not window:
        frames = _read_head(cap, max_frames)
        cap.release()
        if not frames:
            raise ValueError(f"no frames decoded from {path}")
        return frames, fps

    pre = max(1, min(pre_frames, max_frames // 3))
    post = max(1, min(post_frames, max_frames - pre))
    ring: list[np.ndarray] = []
    energies: list[float] = []
    kept: list[np.ndarray] | None = None
    prev = None
    scanned = 0
    while scanned < max_scan_frames:
        ok, frame = cap.read()
        if not ok:
            break
        g = to_gray(frame)
        scanned += 1
        if kept is not None:
            kept.append(g)
            if len(kept) >= pre + 1 + post:
                break
            continue
        if prev is not None:
            e = float(cv2.absdiff(g, prev).mean())
            if len(energies) >= 3:
                arr = np.asarray(energies)
                floor = float(np.median(arr))
                spread = float(np.median(np.abs(arr - floor))) or 1e-6
                if e > floor + max(6.0 * spread, 0.5):
                    # Same rule as find_impact_frame, applied as we go. The
                    # ring holds the quiet frames before this one, so the
                    # caller's find_impact_frame lands here and steps back one.
                    kept = list(ring) + [g]
                    continue
            energies.append(e)
        ring.append(g)
        if len(ring) > pre:
            ring.pop(0)
        prev = g
    cap.release()

    if kept is not None:
        return kept, fps
    if scanned <= max_frames:
        # Short clip with no clear change: exactly what window=False returns.
        cap = nativecap.open_capture(path)
        frames = _read_head(cap, max_frames)
        cap.release()
        if frames:
            return frames, fps
    if not ring:
        raise ValueError(f"no frames decoded from {path}")
    return ring, fps


def _read_head(cap, max_frames: int) -> list[np.ndarray]:
    frames: list[np.ndarray] = []
    while len(frames) < max_frames:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(to_gray(frame))
    return frames


def find_impact_frame(frames: list[np.ndarray], search_from: int = 0) -> int:
    """Frame where the scene first changes hard. Stand-in for OpenFlight's
    SEN-14262 sound trigger: same job, different sensor.

    Takes the FIRST frame whose difference breaks well clear of the resting
    noise floor, not the largest. The largest jump usually lands a few frames
    into flight, and those early frames are the ones worth keeping.
    """
    energies = []
    for i in range(max(1, search_from), len(frames)):
        d = cv2.absdiff(frames[i], frames[i - 1])
        energies.append(float(d.mean()))
    if not energies:
        return 0

    arr = np.array(energies)
    floor = float(np.median(arr))
    spread = float(np.median(np.abs(arr - floor))) or 1e-6
    trigger = floor + max(6.0 * spread, 0.5)

    hits = np.flatnonzero(arr > trigger)
    if hits.size == 0:
        return int(np.argmax(arr)) + max(1, search_from)
    # Step back one frame so the ball is caught leaving the tee.
    return max(0, int(hits[0]) + max(1, search_from) - 1)


def _background(frames: list[np.ndarray], upto: int, count: int) -> np.ndarray:
    lo = max(0, upto - count)
    sample = frames[lo:upto] if upto > lo else frames[:1]
    if not sample:
        sample = frames[:1]
    return np.median(np.stack(sample), axis=0).astype(np.uint8)


def detect_candidates(
    frames: list[np.ndarray],
    start_frame: int,
    cfg: DetectorConfig,
) -> list[Detection]:
    """Blob candidates in each frame after impact."""
    if cfg.background_mode == "global":
        step = max(1, len(frames) // 40)
        bg = np.median(np.stack(frames[::step]), axis=0).astype(np.uint8)
    else:
        bg = _background(frames, start_frame, cfg.background_frames)
    out: list[Detection] = []

    end = min(len(frames), start_frame + cfg.max_track_frames)
    for idx in range(start_frame, end):
        frame = frames[idx]
        diff = cv2.absdiff(frame, bg)
        if cfg.blur_kernel > 1:
            k = cfg.blur_kernel | 1
            diff = cv2.GaussianBlur(diff, (k, k), 0)
        _, mask = cv2.threshold(diff, cfg.diff_threshold, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

        if cfg.roi:
            rx, ry, rw, rh = cfg.roi
            keep = np.zeros_like(mask)
            keep[ry:ry + rh, rx:rx + rw] = 255
            mask = cv2.bitwise_and(mask, keep)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area = cv2.contourArea(c)
            if area < cfg.min_area_px or area > cfg.max_area_px:
                continue
            perim = cv2.arcLength(c, True)
            if perim <= 0:
                continue
            circularity = 4.0 * math.pi * area / (perim * perim)
            # A fast ball smears into a streak, so circularity is a weak filter.
            # It is here to drop legs, shafts and long grass edges.
            if circularity < cfg.min_circularity:
                continue
            (cx, cy), radius = cv2.minEnclosingCircle(c)
            out.append(Detection(idx, float(cx), float(cy), float(radius), float(area)))

    return out


def fit_track(
    candidates: list[Detection],
    fps: float,
    cfg: DetectorConfig,
    rng_seed: int = 0,
    frame_height: int = 0,
    readout_s: float = 0.0,
) -> Track | None:
    """RANSAC a straight-line track through candidates.

    Over the first ~10 frames of flight gravity moves the ball well under a
    pixel, so a straight line is the right model and a parabola would just
    fit noise.
    """
    by_frame: dict[int, list[Detection]] = {}
    for d in candidates:
        by_frame.setdefault(d.frame_index, []).append(d)
    if len(by_frame) < cfg.min_track_points:
        return None

    rng = np.random.default_rng(rng_seed)
    frames_sorted = sorted(by_frame)
    best: tuple[int, float, list[Detection]] | None = None

    flat = candidates
    if len(flat) < 2:
        return None

    for _ in range(cfg.ransac_iterations):
        i, j = rng.choice(len(flat), size=2, replace=False)
        a, b = flat[i], flat[j]
        dt = b.frame_index - a.frame_index
        if dt == 0 or abs(dt) > cfg.max_track_frames:
            continue
        vx = (b.x - a.x) / dt
        vy = (b.y - a.y) / dt
        if math.hypot(vx, vy) < 1.0:      # a ball in flight moves
            continue

        inliers: list[Detection] = []
        for f in frames_sorted:
            # At most one detection per frame: the closest to the prediction.
            px = a.x + vx * (f - a.frame_index)
            py = a.y + vy * (f - a.frame_index)
            best_d, best_r = None, cfg.ransac_tolerance_px
            for d in by_frame[f]:
                r = math.hypot(d.x - px, d.y - py)
                if r <= best_r:
                    best_d, best_r = d, r
            if best_d is not None:
                inliers.append(best_d)

        if len(inliers) < cfg.min_track_points:
            continue
        resid = _line_residual(inliers)
        score = (len(inliers), -resid)
        if best is None or score > (best[0], -best[1]):
            best = (len(inliers), resid, inliers)

    if best is None:
        return None

    _, resid, inliers = best
    inliers.sort(key=lambda d: d.frame_index)

    # Least-squares refit on the inlier set, in real seconds.
    #
    # Rolling shutter: a CMOS sensor reads out row by row, so a detection is
    # not captured at the frame's nominal time but at
    #     t = frame/fps + (y / height) * readout_time
    # The ball climbs through the frame as it flies, so successive detections
    # carry successive timing offsets and the apparent time span is wrong.
    # Correcting the time basis is the whole fix.
    t = _capture_times(inliers, fps, frame_height, readout_s)
    xs = np.array([d.x for d in inliers], dtype=float)
    ys = np.array([d.y for d in inliers], dtype=float)
    ax, bx = np.polyfit(t, xs, 1)      # ax is already px/s
    ay, by = np.polyfit(t, ys, 1)

    return Track(
        detections=inliers,
        vx_px_s=float(ax),
        vy_px_s=float(ay),
        x0=float(bx + ax * t[0]),
        y0=float(by + ay * t[0]),
        t0_frame=int(inliers[0].frame_index),
        residual_px=float(resid),
        fps=fps,
        readout_s=readout_s,
        frame_height=frame_height,
    )


def _capture_times(
    dets: list[Detection], fps: float, frame_height: int, readout_s: float
) -> np.ndarray:
    """Real capture time of each detection, rolling shutter included."""
    base = np.array([d.frame_index / fps for d in dets], dtype=float)
    if readout_s <= 0 or frame_height <= 0:
        return base
    rows = np.array([d.y for d in dets], dtype=float)
    return base + (rows / float(frame_height)) * readout_s


def rolling_shutter_span(
    track: "Track", readout_s: float
) -> float:
    """Speed this track would show for a different sensor readout time.

    Lets you bound the answer when the readout time is unknown, instead of
    quietly picking one and hoping.
    """
    t = _capture_times(track.detections, track.fps, track.frame_height, readout_s)
    xs = np.array([d.x for d in track.detections], dtype=float)
    ys = np.array([d.y for d in track.detections], dtype=float)
    ax = np.polyfit(t, xs, 1)[0]
    ay = np.polyfit(t, ys, 1)[0]
    return float(math.hypot(ax, ay))


def _line_residual(dets: list[Detection]) -> float:
    if len(dets) < 3:
        return 0.0
    t = np.array([d.frame_index for d in dets], dtype=float)
    xs = np.array([d.x for d in dets], dtype=float)
    ys = np.array([d.y for d in dets], dtype=float)
    ax, bx = np.polyfit(t, xs, 1)
    ay, by = np.polyfit(t, ys, 1)
    rx = xs - (ax * t + bx)
    ry = ys - (ay * t + by)
    return float(np.sqrt(np.mean(rx ** 2 + ry ** 2)))
