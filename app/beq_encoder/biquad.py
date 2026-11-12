"""RBJ cookbook biquads, matching the BEQ catalogue / BEQDesigner implementation."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .workers import POOL

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


# A filter's state below this is zeroed. One 24-bit step is 1.2e-7 of full scale, so
# nothing this small reaches a sample; float64's own rounding of the audio is far larger.
QUIET_STATE = 1e-30
# The state is looked at this often (frames). No BEQ filter decays from QUIET_STATE to the
# subnormal range (278 decades) in a third of a second.
SETTLE_FRAMES = 1 << 14


def _filter(sosfilt, sos, x, state):
    """sosfilt along axis 0 from `state`, a piece at a time, with what is left of the
    state zeroed once it has decayed below QUIET_STATE.

    When a channel falls silent the state decays towards zero but never gets there: it
    reaches the subnormal numbers and stays, and a CPU works on those many times slower
    than on ordinary ones (measured: a channel with silent stretches filtered 20 to 50
    times slower than one without). An Atmos object is silent for much of a film."""
    out = np.empty(x.shape)
    for i in range(0, len(x), SETTLE_FRAMES):
        out[i:i + SETTLE_FRAMES], state = sosfilt(sos, x[i:i + SETTLE_FRAMES], axis=0,
                                                  zi=state)
        state[np.abs(state) < QUIET_STATE] = 0.0
    return out, state


def sosfilt_channels(sos, x, zi):
    """scipy's sosfilt along axis 0 of (frames, channels) with state `zi`
    (sections, 2, channels), each channel in its own thread: sosfilt releases the GIL
    and channels are independent, so the threads change nothing but the time taken.
    A state that has decayed to nothing is zeroed (`_filter`), which no sample shows."""
    from scipy.signal import sosfilt  # imported on use: it adds ~0.7 s to start-up

    ch = x.shape[1] if x.ndim == 2 else 1
    if ch < 2 or len(x) < 4096:
        return _filter(sosfilt, sos, x, zi)
    out = np.empty(x.shape)

    def one(c):  # the channel is taken out of the block and put back in its own thread too
        out[:, c], state = _filter(sosfilt, sos, np.ascontiguousarray(x[:, c]), zi[:, :, c])
        return state

    return out, np.stack(list(POOL.map(one, range(ch))), axis=2)
