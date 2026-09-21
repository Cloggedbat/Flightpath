"""What is actually inside an MP4, using nothing but the standard library.

This exists for one question: the camera keeps writing a 27,639 byte clip
for a 3 second shutter window, and the camera itself reports no card,
battery or temperature problem. A file that small either has no video
track, or a video track with no frames in it, and those two mean very
different things. The container knows, so ask it.

Deliberately not OpenCV: a decoder that refuses the file tells us nothing,
and on the phone the decoder is a separate problem again.
"""

from __future__ import annotations

import struct

# Boxes that hold other boxes rather than data.
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta"}
MAX_BOXES = 4000            # a stub is tiny; never walk a whole 50 MB clip


def _boxes(data: bytes, start: int, end: int, budget: list):
    """Yield (type, payload_start, payload_end) for boxes in [start, end)."""
    pos = start
    while pos + 8 <= end and budget[0] > 0:
        budget[0] -= 1
        size = int.from_bytes(data[pos:pos + 4], "big")
        typ = data[pos + 4:pos + 8]
        body = pos + 8
        if size == 1:                      # 64 bit size
            if body + 8 > end:
                return
            size = int.from_bytes(data[body:body + 8], "big")
            body += 8
        elif size == 0:                    # to end of file
            size = end - pos
        if size < 8 or pos + size > end:
            return
        yield typ, body, pos + size
        pos += size


def _find(data: bytes, start: int, end: int, path: tuple, budget: list):
    """Walk a box path such as (b"moov", b"mvhd"). Returns (s, e) or None."""
    if not path:
        return start, end
    for typ, bs, be in _boxes(data, start, end, budget):
        if typ == path[0]:
            found = _find(data, bs, be, path[1:], budget)
            if found:
                return found
    return None


def _version_flags(data: bytes, pos: int) -> int:
    return data[pos]


def probe(path: str, max_bytes: int = 8 * 1024 * 1024) -> dict:
    """Structure of an MP4. Never raises; returns {"error": ...} instead."""
    out: dict = {"tracks": []}
    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    out["size"] = len(data)
    if len(data) > max_bytes:
        return {"error": "file too large to probe", "size": len(data)}
    if len(data) < 8:
        return {"error": "file is empty or truncated", "size": len(data)}

    budget = [MAX_BOXES]
    out["top_level"] = [t.decode("latin1") for t, _, _ in _boxes(data, 0, len(data), budget)]
    if "moov" not in out["top_level"]:
        # No index at all: the camera never finalised the file.
        out["moov"] = False
        return out
    out["moov"] = True

    budget = [MAX_BOXES]
    mvhd = _find(data, 0, len(data), (b"moov", b"mvhd"), budget)
    if mvhd:
        s, _ = mvhd
        ver = _version_flags(data, s)
        try:
            if ver == 1:
                timescale, duration = struct.unpack(">IQ", data[s + 20:s + 32])
            else:
                timescale, duration = struct.unpack(">II", data[s + 12:s + 20])
            out["duration_s"] = round(duration / timescale, 3) if timescale else None
        except struct.error:
            pass

    # Each track: what kind, which codec, how many samples (frames).
    budget = [MAX_BOXES]
    moov = _find(data, 0, len(data), (b"moov",), budget)
    if not moov:
        return out
    ms, me = moov
    budget = [MAX_BOXES]
    for typ, ts, te in _boxes(data, ms, me, budget):
        if typ != b"trak":
            continue
        track: dict = {}
        b2 = [MAX_BOXES]
        hdlr = _find(data, ts, te, (b"mdia", b"hdlr"), b2)
        if hdlr:
            s, _ = hdlr
            track["type"] = data[s + 8:s + 12].decode("latin1", "replace")
        b2 = [MAX_BOXES]
        mdhd = _find(data, ts, te, (b"mdia", b"mdhd"), b2)
        if mdhd:
            s, _ = mdhd
            ver = _version_flags(data, s)
            try:
                if ver == 1:
                    timescale, duration = struct.unpack(">IQ", data[s + 20:s + 32])
                else:
                    timescale, duration = struct.unpack(">II", data[s + 12:s + 20])
                track["duration_s"] = round(duration / timescale, 3) if timescale else None
            except struct.error:
                pass
        b2 = [MAX_BOXES]
        stsd = _find(data, ts, te, (b"mdia", b"minf", b"stbl", b"stsd"), b2)
        if stsd:
            s, e = stsd
            if s + 16 <= e:
                track["codec"] = data[s + 12:s + 16].decode("latin1", "replace")
        b2 = [MAX_BOXES]
        stsz = _find(data, ts, te, (b"mdia", b"minf", b"stbl", b"stsz"), b2)
        if stsz:
            s, _ = stsz
            try:
                track["samples"] = struct.unpack(">I", data[s + 8:s + 12])[0]
            except struct.error:
                pass
        out["tracks"].append(track)
    return out


def summary(info: dict) -> str:
    """One line for the wizard's log."""
    if info.get("error"):
        return f"clip contents: {info['error']}"
    if not info.get("moov"):
        return ("clip contents: no moov index, so the camera never finalised "
                "the file (it was cut off mid-recording)")
    bits = []
    if info.get("duration_s") is not None:
        bits.append(f"duration {info['duration_s']} s")
    if not info.get("tracks"):
        bits.append("NO TRACKS AT ALL")
    for t in info["tracks"]:
        kind = t.get("type", "?")
        n = t.get("samples")
        codec = t.get("codec", "?")
        dur = t.get("duration_s")
        piece = f"{kind} track ({codec}): {n if n is not None else '?'} samples"
        if dur is not None:
            piece += f", {dur} s"
        if kind == "vide" and n == 0:
            piece += "  NO VIDEO FRAMES"
        bits.append(piece)
    return "clip contents: " + "; ".join(bits)
