"""Lens distortion.

A GoPro on Wide bends straight lines. That corrupts two things at once: your
scale reference measures a curved path as if it were straight, and a ball
flying dead straight traces a curve the line fit cannot follow. Both push the
answer around by percent, not fractions of a percent.

Two ways out, in order of preference:

1. **Shoot in Linear.** The HERO9 corrects distortion in-camera, and Linear is
   available at 1080p240. This costs nothing and is the right answer. If you
   do this, you never need this module.

2. **Calibrate.** Print a checkerboard, film it from a dozen angles, and solve
   for the distortion coefficients. Use this only if you are stuck with Wide
   footage you cannot reshoot.
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, asdict

import cv2
import numpy as np


@dataclass
class LensModel:
    camera_matrix: list       # 3x3, row major
    dist_coeffs: list         # k1 k2 p1 p2 k3
    image_width: int
    image_height: int
    rms_error: float
    n_images: int

    def save(self, path: str) -> str:
        with open(path, "w") as fh:
            json.dump(asdict(self), fh, indent=2)
        return path

    @classmethod
    def load(cls, path: str) -> "LensModel":
        with open(path) as fh:
            return cls(**json.load(fh))

    def as_arrays(self):
        K = np.array(self.camera_matrix, dtype=np.float64).reshape(3, 3)
        D = np.array(self.dist_coeffs, dtype=np.float64).reshape(-1, 1)
        return K, D


def calibrate_from_images(
    pattern: str,
    board_cols: int = 9,
    board_rows: int = 6,
    square_size_mm: float = 25.0,
) -> LensModel:
    """Solve a lens model from checkerboard stills.

    board_cols/board_rows count INNER corners, not squares. A printed 10x7
    square board has 9x6 inner corners.
    """
    paths = sorted(glob.glob(pattern))
    if len(paths) < 5:
        raise ValueError(
            f"found {len(paths)} images matching {pattern}; want at least 10, "
            "shot from a spread of angles and distances"
        )

    objp = np.zeros((board_rows * board_cols, 3), np.float32)
    objp[:, :2] = np.mgrid[0:board_cols, 0:board_rows].T.reshape(-1, 2)
    objp *= square_size_mm

    obj_points, img_points = [], []
    size = None
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)

    for p in paths:
        img = cv2.imread(p)
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        size = gray.shape[::-1]
        found, corners = cv2.findChessboardCorners(
            gray, (board_cols, board_rows),
            cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE,
        )
        if not found:
            continue
        corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
        obj_points.append(objp)
        img_points.append(corners)

    if len(obj_points) < 5:
        raise ValueError(
            f"only found the board in {len(obj_points)} of {len(paths)} images. "
            "Check board_cols/board_rows are INNER corner counts."
        )

    rms, K, D, _, _ = cv2.calibrateCamera(obj_points, img_points, size, None, None)
    return LensModel(
        camera_matrix=K.flatten().tolist(),
        dist_coeffs=D.flatten().tolist(),
        image_width=size[0],
        image_height=size[1],
        rms_error=float(rms),
        n_images=len(obj_points),
    )


def build_maps(model: LensModel, width: int, height: int):
    """Precompute remap tables once, then reuse for every frame."""
    K, D = model.as_arrays()
    if (width, height) != (model.image_width, model.image_height):
        sx = width / model.image_width
        sy = height / model.image_height
        K = K.copy()
        K[0, :] *= sx
        K[1, :] *= sy
    newK, _ = cv2.getOptimalNewCameraMatrix(K, D, (width, height), 0, (width, height))
    return cv2.initUndistortRectifyMap(K, D, None, newK, (width, height), cv2.CV_16SC2)


def undistort_frames(frames: list[np.ndarray], model: LensModel) -> list[np.ndarray]:
    """Remap in place. Building a second full list would double peak memory at
    exactly the moment it is already highest."""
    if not frames:
        return frames
    h, w = frames[0].shape[:2]
    map1, map2 = build_maps(model, w, h)
    for i in range(len(frames)):
        frames[i] = cv2.remap(frames[i], map1, map2, cv2.INTER_LINEAR)
    return frames


def extract_calibration_stills(video: str, out_dir: str, every: int = 15,
                               limit: int = 40) -> int:
    """Pull stills out of a video of you waving a checkerboard around.

    Easier than shooting 20 photos: film 30 seconds of the board at varied
    angles and distances, then run this.
    """
    os.makedirs(out_dir, exist_ok=True)
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise FileNotFoundError(video)
    i = saved = 0
    while saved < limit:
        ok, frame = cap.read()
        if not ok:
            break
        if i % every == 0:
            cv2.imwrite(os.path.join(out_dir, f"calib_{saved:03d}.png"), frame)
            saved += 1
        i += 1
    cap.release()
    return saved
