"""Video decoding that works on the phone.

The OpenCV inside the APK cannot read a frame on this hardware. It is
4.5.1.48, the only Android wheel Chaquopy offers, and it was built with an
empty Video I/O section apart from one backend, ANDROID_MEDIANDK. That
backend converts a decoded frame only when the decoder reports colour format
19 or 21; Qualcomm and Samsung decoders report a vendor format, so
VideoCapture.read() returns False on every frame. The file opens fine, which
is why the failure looked like a codec problem. It is not. H.264 and HEVC
fail identically, so changing the camera's Video Compression cannot fix it.

So on Android we decode through ClipDecoder.kt, which drives MediaCodec
directly and hands back the luma plane. Everything in the engine converts to
grayscale as its first act, so luma is the whole picture as far as the
tracker is concerned.

On a PC none of this applies and cv2.VideoCapture is used unchanged.
"""

from __future__ import annotations

import numpy as np

_CLASS = "dev.flightpath.app.ClipDecoder"
_probe: tuple[bool, str] | None = None


def _decoder_class():
    """The Kotlin class, or None when not running inside the app."""
    try:
        from java import jclass                    # Chaquopy only
    except ImportError:
        return None
    try:
        return jclass(_CLASS)
    except Exception:                              # noqa: BLE001
        return None


def available() -> bool:
    """True when the native decoder can be used."""
    return probe()[0]


def probe() -> tuple[bool, str]:
    """(usable, reason). Cached, because it costs a class load."""
    global _probe
    if _probe is None:
        cls = _decoder_class()
        if cls is None:
            _probe = (False, "not running under Chaquopy")
        else:
            _probe = (True, "MediaCodec")
    return _probe


class NativeCapture:
    """The slice of the cv2.VideoCapture surface this engine actually uses.

    read() returns a single channel uint8 frame, not BGR. Use
    detect.to_gray() on anything that comes out of open_capture().
    """

    def __init__(self, path: str):
        self._d = None
        self._ok = False
        self._err = ""
        cls = _decoder_class()
        if cls is None:
            self._err = "native decoder unavailable"
            return
        try:
            d = cls()
            if not d.open(path):
                self._err = str(d.getError() or "could not open the file")
                d.close()
                return
            self._d = d
            self._ok = True
        except Exception as exc:                   # noqa: BLE001
            self._err = f"{type(exc).__name__}: {exc}"

    def isOpened(self) -> bool:                    # noqa: N802  (cv2 name)
        return self._ok

    @property
    def error(self) -> str:
        return self._err

    def read(self):
        if not self._ok:
            return False, None
        try:
            buf = self._d.nextLuma()
        except Exception as exc:                   # noqa: BLE001
            self._err = f"{type(exc).__name__}: {exc}"
            return False, None
        if buf is None:
            err = str(self._d.getError() or "")
            if err:
                self._err = err
            return False, None
        h = int(self._d.getHeight())
        w = int(self._d.getWidth())
        # Chaquopy hands a Java byte[] back as Python bytes, but do not stake
        # the shot on it: anything with a buffer works, anything else is
        # coerced once.
        try:
            memoryview(buf)
        except TypeError:
            buf = bytes(bytearray(buf))
        if w <= 0 or h <= 0 or len(buf) < w * h:
            self._err = f"decoder returned {len(buf)} bytes for {w}x{h}"
            return False, None
        # Copy: frombuffer aliases the Java array's bytes read-only, and the
        # tracker is entitled to a frame it can write into.
        frame = np.frombuffer(buf, dtype=np.uint8, count=w * h).reshape(h, w).copy()
        return True, frame

    def get(self, prop: int) -> float:
        import cv2
        if not self._ok:
            return 0.0
        try:
            if prop == cv2.CAP_PROP_FRAME_WIDTH:
                return float(self._d.getWidth())
            if prop == cv2.CAP_PROP_FRAME_HEIGHT:
                return float(self._d.getHeight())
            if prop == cv2.CAP_PROP_FPS:
                # Timestamps beat the container: a GoPro writes 239.76 as
                # often as 240, and only the timestamps know which.
                measured = float(self._d.measuredFps())
                return measured or float(self._d.getFrameRate())
        except Exception:                          # noqa: BLE001
            return 0.0
        return 0.0

    def info(self) -> dict:
        """Diagnostics for the wizard's Engine row."""
        if not self._d:
            return {"error": self._err}
        try:
            return {
                "mime": str(self._d.getMime()),
                "decoder": str(self._d.getDecoderName()),
                "width": int(self._d.getWidth()),
                "height": int(self._d.getHeight()),
                "container_fps": float(self._d.getFrameRate()),
                "measured_fps": round(float(self._d.measuredFps()), 2),
                "frames": int(self._d.getFramesDecoded()),
                "error": str(self._d.getError() or ""),
            }
        except Exception as exc:                   # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    def release(self) -> None:
        if self._d is not None:
            try:
                self._d.close()
            except Exception:                      # noqa: BLE001
                pass
            self._d = None
        self._ok = False


def open_capture(path: str):
    """A capture for this platform. Native on the phone, cv2 everywhere else.

    Falls back to cv2 if the native decoder cannot open the file, so a bad
    build degrades to the old behaviour rather than to nothing.
    """
    if available():
        cap = NativeCapture(path)
        if cap.isOpened():
            return cap
    import cv2
    return cv2.VideoCapture(path)
