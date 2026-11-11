"""Compare the BEQ track against the original, inside the remuxed MKV.

ffmpeg decodes both tracks of the same file from its start, to the same channel
layout, high-passed at 300 Hz so the BEQ change in the bass
doesn't matter. Two checks come out of one decode:

* sync: cross-correlation of the mono sums; a positive lag means the BEQ track
  is late.
* channel order (in the track's real layout, see layouts.py): every active channel of the BEQ track must correlate best
  with the same channel of the original (catches L/R or side/rear swaps).
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import layouts, pcmio

# measurement only (never used for the encode, which keeps every decoded sample)
TIMELINE = "aresample=min_comp=0.002:min_hard_comp=0.002:first_pts=0"

SR = 8000  # plenty for a lag estimate; 0.125 ms resolution before interpolation
DECODE_TIMEOUT_S = 3600


@dataclass(slots=True)
class CheckResult:
    lag_ms: float
    confidence: float  # normalised correlation peak of the mono sums, 0..1
    at_s: float
    channels: int = 0
    mismatches: list[str] = field(default_factory=list)  # e.g. "Lss↔Lrs (0.91 vs 0.12)"
    diag: dict[str, float] = field(default_factory=dict)  # per-channel correlation

    @property
    def reliable(self) -> bool:
        return self.confidence >= 0.5


def _xcorr_lag(x: np.ndarray, y: np.ndarray, max_lag: int) -> tuple[float, float]:
    n = len(x)
    size = 1 << int(np.ceil(np.log2(2 * n)))
    xc = np.fft.irfft(np.fft.rfft(y, size) * np.conj(np.fft.rfft(x, size)), size)
    lags = np.concatenate([np.arange(0, max_lag + 1), np.arange(-max_lag, 0)])
    vals = np.concatenate([xc[: max_lag + 1], xc[-max_lag:]])
    i = int(np.argmax(vals))
    frac = 0.0
    if 0 < i < len(vals) - 1 and lags[i - 1] == lags[i] - 1 and lags[i + 1] == lags[i] + 1:
        y0, y1, y2 = vals[i - 1], vals[i], vals[i + 1]
        den = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / den if den else 0.0
    norm = float(np.sqrt(np.dot(x, x) * np.dot(y, y))) or 1.0
    return float(lags[i] + frac), float(vals[i]) / norm


def compare(
    ffmpeg: str, mkv: str, a_idx: int, b_idx: int, duration_s: float | None,
    channels: int, names: tuple[str, ...] | None = None, downmix: bool = False,
    window_s: float = 30.0, max_lag_ms: float = 500.0,
    popen: pcmio.Popen = subprocess.Popen,
) -> CheckResult:
    """`channels`/`names`: the layout both tracks are compared in. With `downmix`, ffmpeg
    downmixes both to `channels` first (tracks with different channel counts). The decode
    starts through `popen`, so a job can stop it."""
    # Window at 30% of the film (at most 15 min in). Decoding runs from the start
    # rather than seeking: a TrueHD decoder that lands between major syncs drops
    # frames, which would show up here as a bogus offset. For this measurement only,
    # each track is put on its own timeline (TIMELINE): padded from pts 0 and with
    # gaps over 2 ms filled or overlaps trimmed, as a player schedules it. The
    # threshold sits above the jitter of millisecond-rounded Matroska timestamps
    # (DTS 10.67 ms, TrueHD 0.83 ms frames) so normal streams are left as decoded.
    at = min(max(0.0, (duration_s or 600) * 0.3), 900.0)
    ch = max(1, int(channels))
    if names is None or len(names) != ch:
        names = layouts.names_or_generic(ch, None)
    ac = ["-ac", str(ch)] if downmix else []
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td) / "a.f32", Path(td) / "b.f32"
        af = (f"{TIMELINE},atrim=start={at:.3f}:duration={window_s},"
              f"highpass=f=300,aresample={SR}")
        proc = popen(
            [ffmpeg, "-v", "error", "-nostdin", *pcmio.DECODE_OPTS, "-i", mkv,
             "-map", f"0:a:{a_idx}", "-af", af, *ac, "-f", "f32le", str(a),
             "-map", f"0:a:{b_idx}", "-af", af, *ac, "-f", "f32le", str(b)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            _, err = proc.communicate(timeout=DECODE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed (code {proc.returncode}): "
                               f"{err.decode(errors='replace').strip()[-300:]}")
        x = np.fromfile(a, dtype="<f4").astype(np.float64).reshape(-1, ch)
        y = np.fromfile(b, dtype="<f4").astype(np.float64).reshape(-1, ch)
    n = min(len(x), len(y))
    if n < SR:
        return CheckResult(0.0, 0.0, at, ch)
    x, y = x[:n] - x[:n].mean(0), y[:n] - y[:n].mean(0)
    lag, conf = _xcorr_lag(x.sum(1), y.sum(1), int(SR * max_lag_ms / 1000))
    res = CheckResult(round(lag * 1000 / SR, 2), round(conf, 3), round(at, 1), ch)

    # channel order, after aligning by the measured lag
    k = int(round(lag))
    if k > 0:
        x, y = x[: n - k], y[k:]
    elif k < 0:
        x, y = x[-k:], y[: n + k]
    ex, ey = np.sqrt((x * x).sum(0)), np.sqrt((y * y).sum(0))
    loud = max(ex.max(), 1e-12)
    active = [i for i in range(ch) if ex[i] > loud * 0.01 and ey[i] > 1e-12]  # within -40 dB
    if not active:
        return res
    corr = (y[:, active].T @ x[:, active]) / np.outer(ey[active], ex[active])
    for r, i in enumerate(active):
        res.diag[names[i]] = round(float(corr[r, r]), 3)
        best = int(np.argmax(corr[r]))
        if best != r and corr[r, best] > corr[r, r] + 0.1:
            res.mismatches.append(
                f"{names[i]}↔{names[active[best]]} ({corr[r, best]:.2f} vs {corr[r, r]:.2f})"
            )
    return res
