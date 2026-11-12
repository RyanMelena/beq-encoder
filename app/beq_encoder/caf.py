"""Minimal Core Audio Format (CAF) LPCM reader/writer.

truehdd writes the DAMF `.atmos.audio` essence as a CAF file holding
interleaved integer LPCM (24-bit in practice). We only need to locate the
audio bytes and convert blocks to/from float so they can be filtered.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

import numpy as np

from .workers import over_rows

_FLAG_FLOAT = 1
_FLAG_LITTLE_ENDIAN = 2


@dataclass(slots=True)
class CafInfo:
    path: str
    sample_rate: float
    channels: int
    bits: int
    is_float: bool
    little_endian: bool
    data_offset: int  # absolute offset of the first audio byte
    data_bytes: int  # number of audio bytes

    @property
    def bytes_per_sample(self) -> int:
        return self.bits // 8

    @property
    def frame_bytes(self) -> int:
        return self.bytes_per_sample * self.channels

    @property
    def frames(self) -> int:
        return self.data_bytes // self.frame_bytes

    @property
    def full_scale(self) -> float:
        return 1.0 if self.is_float else float(2 ** (self.bits - 1))


def read_info(path: str) -> CafInfo:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(8)
        if len(head) < 8 or head[:4] != b"caff":
            raise ValueError(f"{path} is not a CAF file")
        desc = None
        pos = 8
        while pos + 12 <= size:
            fh.seek(pos)
            ctype, csize = struct.unpack(">4sq", fh.read(12))
            body = pos + 12
            if ctype == b"desc":
                desc = struct.unpack(">d4sIIIII", fh.read(32))
            elif ctype == b"data":
                if desc is None:
                    raise ValueError("CAF data chunk precedes desc chunk")
                sr, fmt, flags, _bpp, _fpp, ch, bits = desc
                if fmt != b"lpcm":
                    raise ValueError(f"Unsupported CAF format {fmt!r}")
                # data chunk starts with a 4-byte edit count
                start = body + 4
                end = size if csize == -1 else min(size, body + csize)
                info = CafInfo(
                    path=path,
                    sample_rate=sr,
                    channels=ch,
                    bits=bits,
                    is_float=bool(flags & _FLAG_FLOAT),
                    little_endian=bool(flags & _FLAG_LITTLE_ENDIAN),
                    data_offset=start,
                    data_bytes=0,
                )
                info.data_bytes = ((end - start) // info.frame_bytes) * info.frame_bytes
                if info.is_float and bits not in (32, 64):
                    raise ValueError(f"Unsupported float bit depth {bits}")
                if not info.is_float and bits not in (16, 24, 32):
                    raise ValueError(f"Unsupported integer bit depth {bits}")
                return info
            if csize == -1:
                break
            pos = body + csize
    raise ValueError(f"{path}: no audio data chunk found")


def _decode_part(info: CafInfo, raw: np.ndarray, out: np.ndarray) -> None:
    """Whole samples as bytes (uint8) -> `out` (flat float64), full scale = 1.0."""
    e = "<" if info.little_endian else ">"
    if info.is_float:
        out[:] = raw.view(f"{e}f{info.bytes_per_sample}")
    elif info.bits == 24:
        b = raw.reshape(-1, 3)
        w = np.zeros((b.shape[0], 4), dtype=np.uint8)
        if info.little_endian:
            w[:, 1:] = b
            np.divide(w.view("<i4")[:, 0] >> 8, info.full_scale, out=out)
        else:
            w[:, :3] = b
            np.divide(w.view(">i4")[:, 0] >> 8, info.full_scale, out=out)
    else:
        np.divide(raw.view(f"{e}i{info.bytes_per_sample}"), info.full_scale, out=out)


def decode(info: CafInfo, raw: bytes) -> np.ndarray:
    """Raw interleaved bytes -> float64 array (frames, channels), full scale = 1.0."""
    fb = info.frame_bytes
    frames = len(raw) // fb
    if frames * fb != len(raw):
        raise ValueError(f"{len(raw)} bytes are not whole frames of {fb}")
    src = np.frombuffer(raw, dtype=np.uint8)
    out = np.empty((frames, info.channels))

    def part(a: int, b: int) -> None:
        _decode_part(info, src[a * fb: b * fb], out[a:b].reshape(-1))

    over_rows(part, frames)
    return out


def quantise(x: np.ndarray, full_scale: float) -> tuple[np.ndarray, int]:
    """Round to integers on a +/-`full_scale` grid. Returns (int32 samples, clipped count)."""
    q = np.rint(x * full_scale)
    lo, hi = -full_scale, full_scale - 1
    clipped = int(np.count_nonzero((q < lo) | (q > hi)))
    return np.clip(q, lo, hi).astype(np.int32), clipped


def _encode_part(info: CafInfo, flat: np.ndarray, out: np.ndarray) -> int:
    """Flat float64 samples -> `out` (uint8, bytes_per_sample each). Returns the count
    clipped."""
    e = "<" if info.little_endian else ">"
    if info.is_float:
        out.view(f"{e}f{info.bytes_per_sample}")[:] = flat
        return int(np.count_nonzero(np.abs(flat) > 1.0))
    q, clipped = quantise(flat, info.full_scale)
    if info.bits == 24:
        if info.little_endian:
            out.reshape(-1, 3)[:] = (q << 8).astype("<i4").view(np.uint8).reshape(-1, 4)[:, 1:]
        else:
            out.reshape(-1, 3)[:] = (q << 8).astype(">i4").view(np.uint8).reshape(-1, 4)[:, :3]
    else:
        out.view(f"{e}i{info.bytes_per_sample}")[:] = q
    return clipped


def encode(info: CafInfo, x: np.ndarray) -> tuple[bytes, int]:
    """float64 (frames, channels) -> raw bytes. Returns (bytes, clipped_sample_count).

    `x` must already include any dither; it is rounded and clipped here.
    """
    x = x.reshape(-1, info.channels)
    fb = info.frame_bytes
    out = np.empty(len(x) * fb, dtype=np.uint8)

    def part(a: int, b: int) -> int:
        return _encode_part(info, x[a:b].reshape(-1), out[a * fb: b * fb])

    clipped = sum(over_rows(part, len(x)))
    return out.tobytes(), clipped


class CafStream:
    """Read a CAF file's LPCM in frames, like pcmio.PCMStream: `read(n)` -> (≤n, ch)
    float64, full scale = 1.0 (24-bit integers are exact in float64)."""

    def __init__(self, path: str, what: str = "audio"):
        self.info = read_info(path)
        self.ch = self.info.channels
        self.what = what
        self.frames = 0
        self._fh = open(path, "rb")
        self._fh.seek(self.info.data_offset)
        self._left = self.info.frames

    def read(self, n: int) -> np.ndarray:
        k = min(int(n), self._left)
        if k <= 0:
            return np.zeros((0, self.ch))
        raw = self._fh.read(k * self.info.frame_bytes)
        k = len(raw) // self.info.frame_bytes
        self._left = 0 if k == 0 else self._left - k
        x = decode(self.info, raw[: k * self.info.frame_bytes])
        self.frames += len(x)
        return x

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        self.close()
