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


def decode(info: CafInfo, raw: bytes) -> np.ndarray:
    """Raw interleaved bytes -> float64 array (frames, channels), full scale = 1.0."""
    e = "<" if info.little_endian else ">"
    if info.is_float:
        x = np.frombuffer(raw, dtype=f"{e}f{info.bytes_per_sample}").astype(np.float64)
    elif info.bits == 24:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        w = np.zeros((b.shape[0], 4), dtype=np.uint8)
        if info.little_endian:
            w[:, 1:] = b
            x = (w.view("<i4")[:, 0] >> 8).astype(np.float64)
        else:
            w[:, :3] = b
            x = (w.view(">i4")[:, 0] >> 8).astype(np.float64)
        x /= info.full_scale
    else:
        x = np.frombuffer(raw, dtype=f"{e}i{info.bytes_per_sample}").astype(np.float64)
        x /= info.full_scale
    return x.reshape(-1, info.channels)


def quantise(x: np.ndarray, full_scale: float) -> tuple[np.ndarray, int]:
    """Round to integers on a +/-`full_scale` grid. Returns (int32 samples, clipped count)."""
    q = np.rint(x * full_scale)
    lo, hi = -full_scale, full_scale - 1
    clipped = int(np.count_nonzero((q < lo) | (q > hi)))
    return np.clip(q, lo, hi).astype(np.int32), clipped


def encode(info: CafInfo, x: np.ndarray) -> tuple[bytes, int]:
    """float64 (frames, channels) -> raw bytes. Returns (bytes, clipped_sample_count).

    `x` must already include any dither; it is rounded and clipped here.
    """
    e = "<" if info.little_endian else ">"
    flat = x.reshape(-1)
    if info.is_float:
        clipped = int(np.count_nonzero(np.abs(flat) > 1.0))
        return flat.astype(f"{e}f{info.bytes_per_sample}").tobytes(), clipped
    q, clipped = quantise(flat, info.full_scale)
    if info.bits == 24:
        if info.little_endian:
            b = (q << 8).astype("<i4").view(np.uint8).reshape(-1, 4)[:, 1:]
        else:
            b = (q << 8).astype(">i4").view(np.uint8).reshape(-1, 4)[:, :3]
        return np.ascontiguousarray(b).tobytes(), clipped
    return q.astype(f"{e}i{info.bytes_per_sample}").tobytes(), clipped


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
