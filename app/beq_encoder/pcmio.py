"""Bit-exact PCM streams from ffmpeg, and the full-length fidelity check.

Decoding is plain: no `aresample`, no `-ac`, no timestamp correction. ffmpeg's
`aresample=async=…` follows the container's millisecond-rounded timestamps by
stretching, padding or dropping samples, which changes the audio; here every
decoded sample is kept in order. Samples come out as float64, which holds every
integer (s16/s24/s32) and float decoder output exactly, including lossy
decoders' overs above full scale.

Where the stream starts on the film's timeline is the first decoded frame's pts
(`find_gaps()` measures it) and is applied as zero padding plus a whole-millisecond
mkvmerge `--sync`, see `lead()`. Where the decoder produced nothing for a stretch
of the timeline (frames it could not decode, e.g. TrueHD before a restart point,
or a gap between joined segments), `find_gaps()` finds it from the decoded frames'
timestamps and `GapFilled` puts the same length of digital silence there, as a
player would, so everything after it stays in sync. Nothing is ever trimmed.
"""

from __future__ import annotations

import math
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
from .biquad import sosfilt_channels

LSB24 = 1.0 / 2**23
# How far a faithful 24-bit sample can be from the signal it stands for: TPDF dither of
# +/-1 LSB, then rounding of 1/2 LSB. The margin is float rounding only.
FIDELITY_TOLERANCE_LSB = 1.5 + 1e-6
# The full dynamic range: ffmpeg's AC-3 / E-AC-3 decoders apply the stream's dynamic
# range compression by default (drc_scale 1), which a player with DRC off does not.
# Every decode in this package takes these input options (other decoders ignore them).
DECODE_OPTS = ["-drc_scale", "0"]


def decode_cmd(ffmpeg: str, path: str, audio_index: int = 0) -> list[str]:
    return [ffmpeg, "-nostdin", "-v", "error", *DECODE_OPTS, "-i", path,
            "-map", f"0:a:{audio_index}", "-f", "f64le", "-"]


Popen = Callable[..., subprocess.Popen]


def lead(first_pts_s: float, rate: int) -> tuple[int, int]:
    """(sync_ms, pad_frames) that put sample 0 of the decode at `first_pts_s`.

    Matroska timestamps are whole milliseconds, so the offset is split into a
    whole-ms mkvmerge --sync and leading digital silence for the remainder (< 1 ms).
    A negative start (pre-roll before 0) can't be represented: it starts at 0.
    """
    ms = max(0.0, first_pts_s) * 1000.0
    whole = math.floor(ms + 1e-6)
    pad = int(round((ms - whole) * rate / 1000.0))
    if pad >= rate // 1000:  # rounding reached a full millisecond
        whole, pad = whole + 1, 0
    return whole, pad


GAP_THRESHOLD_S = 0.002  # above the jitter of millisecond-rounded Matroska timestamps


@dataclass(slots=True)
class FrameMap:
    first_pts: float | None
    frames: int                       # decoded frames (samples per channel)
    gaps: list[tuple[int, int]]       # (source frame index, frames of silence before it)

    @property
    def gap_frames(self) -> int:
        return sum(n for _, n in self.gaps)


def find_gaps(ffprobe: str, path: str, rate: int, audio_index: int = 0,
              threshold_s: float = GAP_THRESHOLD_S, timeout: float = 4 * 3600,
              on_progress: Callable[[float], None] | None = None,
              popen: Popen = subprocess.Popen) -> FrameMap:
    """Walk the decoded frames' timestamps; report where the decoded audio falls
    behind its own timeline by more than `threshold_s` (a gap to fill).

    A walk that stops short proves nothing about what comes after it, so a non-zero exit
    or running past `timeout` (the whole walk, not only its end) raises RuntimeError."""
    proc = popen(
        [ffprobe, "-v", "error", "-select_streams", f"a:{audio_index}",
         "-show_entries", "frame=pts_time,nb_samples", "-of", "csv=p=0", path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1 << 20,
    )
    first: float | None = None
    cum = inserted = lines = 0
    gaps: list[tuple[int, int]] = []
    timed_out = threading.Event()

    def stop() -> None:
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout, stop)
    watchdog.daemon = True
    watchdog.start()
    try:
        for ln in proc.stdout:
            # "pts_time,nb_samples" - some decoders (DTS) add an empty trailing field
            f = ln.strip().split(",")
            if len(f) < 2:
                continue
            a, b = f[0], f[1]
            try:
                nb = int(b)
            except ValueError:
                continue
            try:
                pts = float(a)
            except ValueError:
                pts = math.nan
            if math.isfinite(pts):
                lines += 1
                if on_progress is not None and lines % 20000 == 0:
                    on_progress(pts)
                if first is None:
                    first = pts
                else:
                    d = pts - (first + (cum + inserted) / rate)
                    if d > threshold_s:
                        n = int(round(d * rate))
                        gaps.append((cum, n))
                        inserted += n
            cum += nb
        rc = proc.wait()
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
    if rc != 0:
        raise RuntimeError(f"ffprobe timed out after {timeout:.0f} s" if timed_out.is_set()
                           else f"ffprobe failed (code {rc})")
    return FrameMap(first, cum, gaps)


class PCMStream:
    """Read an ffmpeg f64le pipe in frames: `read(n)` -> (≤n, channels) float64."""

    def __init__(self, cmd: list[str], channels: int, popen: Popen | None = None,
                 what: str = "audio", on_error: Callable[[str], None] | None = None):
        self.ch = int(channels)
        self.what = what
        self.frames = 0
        self._fb = 8 * self.ch
        self._buf = b""
        self._eof = False
        opener = popen or subprocess.Popen
        self.proc = opener(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._err: list[bytes] = []
        self._on_error = on_error
        self._th = threading.Thread(target=self._drain_stderr, daemon=True)
        self._th.start()

    def _drain_stderr(self) -> None:
        for raw in self.proc.stderr:
            self._err.append(raw)
            if self._on_error is not None:
                self._on_error(raw.decode(errors="replace").strip())

    def read(self, n: int) -> np.ndarray:
        want = int(n) * self._fb
        while len(self._buf) < want and not self._eof:
            chunk = self.proc.stdout.read(max(want - len(self._buf), 1 << 16))
            if not chunk:
                self._eof = True
                break
            self._buf += chunk
        k = min(len(self._buf), want) // self._fb * self._fb
        raw, self._buf = self._buf[:k], self._buf[k:]
        x = np.frombuffer(raw, dtype="<f8").reshape(-1, self.ch)
        self.frames += len(x)
        return x

    def close(self, check: bool = True) -> None:
        try:
            self.proc.stdout.close()
        except OSError:
            pass
        if not check and self.proc.poll() is None:
            self.proc.kill()
        rc = self.proc.wait()
        self._th.join(timeout=5)
        if check and rc != 0:
            msg = b"".join(self._err).decode(errors="replace").strip()[-300:]
            raise RuntimeError(f"ffmpeg could not decode the {self.what} (code {rc}) {msg}")

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        self.close(check=et is None)


class GapFilled:
    """A PCMStream with digital silence inserted at `gaps` (see find_gaps)."""

    def __init__(self, inner: PCMStream, gaps: list[tuple[int, int]]):
        self.inner = inner
        self.ch = inner.ch
        self.what = getattr(inner, "what", "audio")
        self.frames = 0
        self._gaps = list(gaps)
        self._gi = 0
        self._src = 0
        self._zeros = 0

    def read(self, n: int) -> np.ndarray:
        parts: list[np.ndarray] = []
        need = int(n)
        while need > 0:
            if self._zeros:
                z = min(need, self._zeros)
                parts.append(np.zeros((z, self.ch)))
                self._zeros -= z
                need -= z
                continue
            if self._gi < len(self._gaps) and self._gaps[self._gi][0] <= self._src:
                self._zeros = self._gaps[self._gi][1]
                self._gi += 1
                continue
            lim = need
            if self._gi < len(self._gaps):
                lim = min(lim, self._gaps[self._gi][0] - self._src)
            x = self.inner.read(lim)
            if len(x) == 0:
                break
            self._src += len(x)
            need -= len(x)
            parts.append(x)
        y = np.concatenate(parts) if len(parts) > 1 else (parts[0] if parts else
                                                          np.zeros((0, self.ch)))
        self.frames += len(y)
        return y

    def close(self, check: bool = True) -> None:
        self.inner.close(check)

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        self.close(check=et is None)


@dataclass(slots=True)
class Fidelity:
    ok: bool
    frames: int = 0               # source frames compared
    pad_frames: int = 0           # leading silence expected before the source
    offset_frames: int = 0        # extra leading frames the encoder added (0 = none)
    trailing_frames: int = 0      # extra frames after the end (must be silent)
    max_err_lsb: float = 0.0      # max |decoded - expected| in 24-bit LSBs
    clipped: int = 0              # samples the expected signal pushed past full scale
    problem: str = ""
    channels_off: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "frames": self.frames, "pad_frames": self.pad_frames,
                "offset_frames": self.offset_frames, "trailing_frames": self.trailing_frames,
                "max_err_lsb": round(self.max_err_lsb, 3), "clipped": self.clipped,
                "problem": self.problem}


def verify(source: PCMStream, output: PCMStream, sos: np.ndarray, gain: float, pad: int,
           tol_lsb: float = FIDELITY_TOLERANCE_LSB, max_offset: int = 48000, block: int = 1 << 17,
           tick: Callable[[int], None] | None = None) -> Fidelity:
    """Compare every sample of `output` with BEQ(source) × gain (24-bit, dithered).

    The output must be `pad` silent frames, then the filtered source within
    `tol_lsb` (the dither and rounding alone), then optionally a short
    silent tail (an encoder rounding the length up to its frame size). An encoder
    that adds its own leading silence is detected (`offset_frames`) rather than
    failed, as long as no source sample is missing.
    """
    ch = source.ch
    res = Fidelity(ok=False, pad_frames=pad)
    if output.ch != ch:
        res.problem = f"output has {output.ch} channels, source {ch}"
        return res
    tol = tol_lsb * LSB24
    zi = np.zeros((sos.shape[0], 2, ch))

    # leading silence: `pad` frames by design, maybe more from the encoder. Read
    # enough output to cover the source's own silent opening as well.
    first = source.read(max(block, 960000))  # ~20 s: past a silent opening
    exp0, zi = sosfilt_channels(sos, first, zi)
    exp0 = exp0 * gain
    lead_exp = _leading_silent(exp0, tol)
    head = output.read(pad + max_offset + min(lead_exp, len(exp0)) + 4096)
    lead_out = _leading_silent(head, tol)
    # output silent lead = pad + offset + (expected's own silent lead)
    offset = max(0, min(lead_out, len(head)) - pad - lead_exp) if lead_exp < len(exp0) else 0
    offset = _refine_offset(head, exp0, pad, offset, tol)
    if offset is None:
        res.problem = ("the encoded track does not start with the filtered source "
                       "(samples missing or altered at the start)")
        return res
    res.offset_frames = offset
    skip = pad + offset
    if not np.abs(head[:skip]).max(initial=0.0) <= tol:
        res.problem = "leading padding is not silent"
        return res
    obuf = head[skip:]
    exp = exp0
    err_max = 0.0
    while True:
        if len(exp) == 0:
            x = source.read(block)
            if len(x) == 0:
                break
            exp, zi = sosfilt_channels(sos, x, zi)
            exp = exp * gain
        if len(obuf) < len(exp):
            more = output.read(len(exp) - len(obuf))
            obuf = np.concatenate([obuf, more]) if len(obuf) else more
        m = min(len(exp), len(obuf))
        if m == 0:
            res.problem = f"encoded track ends early, after {res.frames} source frames"
            break
        e, o = exp[:m], obuf[:m]
        res.clipped += int(np.count_nonzero(np.abs(e) > 1.0 - LSB24))
        d = np.abs(np.clip(e, -1.0, 1.0 - LSB24) - o)
        err_max = max(err_max, float(d.max()))
        bad = ~(d <= tol)  # a NaN compares false against everything: never a pass
        if bad.any():
            res.channels_off = [int(i) for i in np.nonzero(bad.any(axis=0))[0]]
            at = int(np.argmax(bad.any(axis=1)))
            res.problem = (f"sample {res.frames + at} differs from the filtered source by "
                           f"{float(d.max()) / LSB24:.1f} LSB (channels {res.channels_off})")
            break
        res.frames += m
        if tick:
            tick(res.frames)
        exp, obuf = exp[m:], obuf[m:]
    res.max_err_lsb = err_max / LSB24
    if res.problem:
        return res
    # the rest of the output must be a short silent tail
    tail = obuf
    while True:
        more = output.read(block)
        if len(more) == 0:
            break
        tail = np.concatenate([tail, more])
        if len(tail) > max_offset:
            break
    res.trailing_frames = len(tail)
    if len(tail) > max_offset or not np.abs(tail).max(initial=0.0) <= tol:
        res.problem = f"encoded track has {len(tail)} unexpected frames after the source ends"
        return res
    res.ok = True
    return res


def _leading_silent(x: np.ndarray, tol: float) -> int:
    loud = np.nonzero(np.abs(x).max(axis=1) > tol)[0]
    return int(loud[0]) if len(loud) else len(x)


def _refine_offset(head: np.ndarray, exp: np.ndarray, pad: int, guess: int,
                   tol: float) -> int | None:
    """The encoder's extra lead: the k near `guess` where the output matches the
    expected signal over its first non-silent stretch."""
    s0 = _leading_silent(exp, tol)
    n = min(4096, len(exp) - s0)
    if n <= 0:  # nothing but silence to compare yet
        return guess
    e = np.clip(exp[s0: s0 + n], -1.0, 1.0 - LSB24)
    for k in sorted({0, guess, *range(max(0, guess - 64), guess + 65)}):
        o = head[pad + k + s0: pad + k + s0 + n]
        if len(o) == n and np.abs(e - o).max(initial=0.0) <= tol:
            return k
    return None
