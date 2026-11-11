"""RBJ cookbook biquads, matching the BEQ catalogue / BEQDesigner implementation."""

from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

SUPPORTED_TYPES = ("PeakingEQ", "LowShelf", "HighShelf")


@dataclass(frozen=True, slots=True)
class FilterSpec:
    type: str
    freq: float
    gain: float
    q: float
    count: int = 1

    @classmethod
    def from_catalogue(cls, f: dict) -> FilterSpec:
        if f["type"] not in SUPPORTED_TYPES:
            raise ValueError(f"Unsupported filter type {f['type']!r}")
        spec = cls(
            type=f["type"],
            freq=float(f["freq"]),
            gain=float(f["gain"]),
            q=float(f["q"]),
            count=int(f.get("count") or 1),
        )
        if not (0 < spec.freq < math.inf and 0 < spec.q < math.inf
                and math.isfinite(spec.gain) and spec.count > 0):
            raise ValueError(f"Invalid filter {f}")
        return spec

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "freq": self.freq,
            "gain": self.gain,
            "q": self.q,
            "count": self.count,
        }


def rbj(spec: FilterSpec, fs: float) -> tuple[list[float], list[float]]:
    """Return normalised (b, a) for one biquad, a[0] == 1."""
    A = 10 ** (spec.gain / 40)
    w = 2 * math.pi * spec.freq / fs
    c, s = math.cos(w), math.sin(w)
    alpha = s / (2 * spec.q)
    if spec.type == "PeakingEQ":
        b = [1 + alpha * A, -2 * c, 1 - alpha * A]
        a = [1 + alpha / A, -2 * c, 1 - alpha / A]
    elif spec.type == "LowShelf":
        sq = 2 * math.sqrt(A) * alpha
        b = [
            A * ((A + 1) - (A - 1) * c + sq),
            2 * A * ((A - 1) - (A + 1) * c),
            A * ((A + 1) - (A - 1) * c - sq),
        ]
        a = [
            (A + 1) + (A - 1) * c + sq,
            -2 * ((A - 1) + (A + 1) * c),
            (A + 1) + (A - 1) * c - sq,
        ]
    elif spec.type == "HighShelf":
        sq = 2 * math.sqrt(A) * alpha
        b = [
            A * ((A + 1) + (A - 1) * c + sq),
            -2 * A * ((A - 1) + (A + 1) * c),
            A * ((A + 1) + (A - 1) * c - sq),
        ]
        a = [
            (A + 1) - (A - 1) * c + sq,
            2 * ((A - 1) - (A + 1) * c),
            (A + 1) - (A - 1) * c - sq,
        ]
    else:
        raise ValueError(f"Unsupported filter type {spec.type!r}")
    a0 = a[0]
    return [x / a0 for x in b], [x / a0 for x in a]


def design_sos(filters: list[FilterSpec], fs: float) -> np.ndarray:
    """Cascade every filter (expanding `count`) into an (n, 6) SOS array."""
    rows = []
    for f in filters:
        if f.gain == 0:
            continue
        b, a = rbj(f, fs)
        rows.extend([[*b, *a]] * f.count)
    if not rows:
        return np.array([[1.0, 0, 0, 1.0, 0, 0]])
    return np.asarray(rows, dtype=np.float64)


def response_db(filters: list[FilterSpec], freqs: np.ndarray, fs: float = 48000) -> np.ndarray:
    """Magnitude response in dB of the full cascade at the given frequencies."""
    sos = design_sos(filters, fs)
    z = np.exp(-1j * 2 * np.pi * freqs / fs)
    h = np.ones_like(z)
    for b0, b1, b2, a0, a1, a2 in sos:
        h *= (b0 + b1 * z + b2 * z * z) / (a0 + a1 * z + a2 * z * z)
    return 20 * np.log10(np.maximum(np.abs(h), 1e-12))


# threads start on first use, so building it here costs nothing at import
_POOL = ThreadPoolExecutor(max(1, min(16, os.cpu_count() or 4)), thread_name_prefix="sosfilt")


def sosfilt_channels(sos, x, zi):
    """scipy's sosfilt along axis 0 of (frames, channels) with state `zi`
    (sections, 2, channels), each channel in its own thread: sosfilt releases the GIL
    and channels are independent, so the result is identical, only faster."""
    from scipy.signal import sosfilt  # imported on use: it adds ~0.7 s to start-up

    ch = x.shape[1] if x.ndim == 2 else 1
    if ch < 2 or len(x) < 4096:
        return sosfilt(sos, x, axis=0, zi=zi)
    cols = np.ascontiguousarray(x.T)

    def one(c):
        return sosfilt(sos, cols[c], zi=zi[:, :, c])

    res = list(_POOL.map(one, range(ch)))
    return (np.stack([r[0] for r in res], axis=1),
            np.stack([r[1] for r in res], axis=2))
