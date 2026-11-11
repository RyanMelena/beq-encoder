"""The BEQ operation, and its use on a CAF file.

Two passes over the source:
  1. filter and measure the resulting peak (skipped when a fixed gain is given)
  2. filter again from a clean state, apply one uniform gain so the result fits
     the target peak, dither, quantise and write.

The gain is the same on every channel so bed/object balance is untouched; it
is reported so the UI can show how much the track was attenuated.

`filter_stream` is the only implementation of that sequence: the caller supplies
how to read the source and how to write a block. `apply` runs it over a CAF file
in place, or into a new CAF leaving the source untouched.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass

import numpy as np


from . import caf
from .biquad import FilterSpec, design_sos, sosfilt_channels

CHUNK_FRAMES = 1 << 18  # 262144 frames; ~12 MB of 16-ch 24-bit audio
MEASURE_STAGE = "BEQ filter: measuring peak"
WRITE_STAGE = "BEQ filter: writing"

ProgressFn = Callable[[str, float], None]
# One whole pass over the source, from its first sample; the argument names the stage.
ReadBlocks = Callable[[str], Iterable[np.ndarray]]
# Takes a block after gain and dither, quantises and writes it; returns the samples clipped.
WriteBlock = Callable[[np.ndarray], int]


@dataclass(slots=True)
class FilterJob:
    filters: list[FilterSpec]
    target_peak_dbfs: float = -1.0
    fixed_gain_db: float | None = None
    dither: bool = True

    @classmethod
    def load(cls, path: str) -> FilterJob:
        with open(path) as fh:
            d = json.load(fh)
        return cls(
            filters=[FilterSpec.from_catalogue(f) for f in d["filters"]],
            target_peak_dbfs=float(d.get("target_peak_dbfs", -1.0)),
            fixed_gain_db=(
                None if d.get("fixed_gain_db") is None else float(d["fixed_gain_db"])
            ),
            dither=bool(d.get("dither", True)),
        )


@dataclass(slots=True)
class FilterResult:
    """What one run of the BEQ sequence did. Peaks are linear."""
    gain_db: float                 # the gain applied, unrounded
    gain: float                    # the same, linear: what the fidelity check must use
    peak_in: float
    peak_filtered: float | None    # None: a fixed gain skipped the measuring pass
    peak_out: float
    clipped: int
    frames: int


def dbfs(peak: float) -> float:
    return 20 * math.log10(peak) if peak > 0 else -math.inf


def rounded_dbfs(peak: float | None) -> float | None:
    """A linear peak as dBFS for a report, to 2 places (None: no peak or silence)."""
    return round(dbfs(peak), 2) if peak else None


def filter_stream(read: ReadBlocks, write: WriteBlock, sos: np.ndarray, channels: int,
                  target_peak_dbfs: float, fixed_gain_db: float | None = None,
                  dither_lsb: float | None = None, write_stage: str = WRITE_STAGE,
                  rng: np.random.Generator | None = None) -> FilterResult:
    """The BEQ of one programme: measure, derive the gain, refilter, dither, hand to `write`.

    `dither_lsb` is the size of one quantisation step (TPDF dither of +/-1 LSB), or None
    for no dither."""
    def zi() -> np.ndarray:
        return np.zeros((sos.shape[0], 2, channels))

    peak_in = 0.0
    peak_filtered: float | None = None

    if fixed_gain_db is None:
        state = zi()
        peak = 0.0
        for x in read(MEASURE_STAGE):
            peak_in = max(peak_in, float(np.max(np.abs(x))))
            y, state = sosfilt_channels(sos, x, state)
            peak = max(peak, float(np.max(np.abs(y))))
        peak_filtered = peak
        gain_db = min(0.0, target_peak_dbfs - dbfs(peak)) if peak > 0 else 0.0
    else:
        gain_db = fixed_gain_db

    gain = 10 ** (gain_db / 20)
    rng = rng or np.random.default_rng()
    state = zi()
    peak_out = 0.0
    clipped = 0
    frames = 0
    for x in read(write_stage):
        if fixed_gain_db is not None:
            peak_in = max(peak_in, float(np.max(np.abs(x))))
        y, state = sosfilt_channels(sos, x, state)
        y *= gain
        peak_out = max(peak_out, float(np.max(np.abs(y))))
        if dither_lsb is not None:
            y += (rng.random(y.shape) - rng.random(y.shape)) * dither_lsb
        clipped += write(y)
        frames += len(x)
    return FilterResult(gain_db, gain, peak_in, peak_filtered, peak_out, clipped, frames)


@dataclass(slots=True)
class FilterReport:
    path: str
    channels: int
    sample_rate: float
    bits: int
    frames: int
    peak_in_dbfs: float | None     # None: silence
    peak_filtered_dbfs: float | None
    gain_db: float
    peak_out_dbfs: float | None
    clipped_samples: int
    seconds: float
    gain: float = 1.0  # the exact linear gain applied (gain_db is rounded for display)


def _caf_blocks(path: str, info: caf.CafInfo,
                progress: ProgressFn) -> Callable[[str], Iterator[np.ndarray]]:
    block = CHUNK_FRAMES * info.frame_bytes
    total = max(info.data_bytes, 1)

    def read(stage: str) -> Iterator[np.ndarray]:
        done = 0
        with open(path, "rb") as fh:
            fh.seek(info.data_offset)
            while done < info.data_bytes:
                n = min(block, info.data_bytes - done)
                raw = fh.read(n)
                if len(raw) != n:
                    raise OSError("Unexpected end of CAF data")
                yield caf.decode(info, raw)
                done += n
                progress(stage, 100 * done / total)

    return read


def apply(path: str, job: FilterJob, progress: ProgressFn | None = None,
          out_path: str | None = None) -> FilterReport:
    """Filter every channel of the CAF at `path`: in place, or into a new CAF at
    `out_path` (same header and layout), leaving the source untouched."""
    t0 = time.monotonic()
    info = caf.read_info(path)
    sos = design_sos(job.filters, info.sample_rate)
    if out_path is not None:
        with open(path, "rb") as src, open(out_path, "wb") as dst:
            dst.write(src.read(info.data_offset))  # header, chunks, data chunk start
    dither = 1.0 / info.full_scale if job.dither and not info.is_float else None
    progress = progress or (lambda _s, _p: None)
    # the writer overwrites each block after it is read, so one file works in place
    with open(out_path or path, "r+b") as out:
        out.seek(info.data_offset)

        def write(y: np.ndarray) -> int:
            raw, clipped = caf.encode(info, y)
            out.write(raw)
            return clipped

        res = filter_stream(_caf_blocks(path, info, progress), write, sos, info.channels,
                            job.target_peak_dbfs, job.fixed_gain_db, dither)
        out.flush()
        if out_path is None:  # a new file is working data; only in place is this the original
            os.fsync(out.fileno())
    return FilterReport(
        path=path,
        channels=info.channels,
        sample_rate=info.sample_rate,
        bits=info.bits,
        frames=info.frames,
        peak_in_dbfs=rounded_dbfs(res.peak_in),
        peak_filtered_dbfs=rounded_dbfs(res.peak_filtered),
        gain_db=round(res.gain_db, 2),
        peak_out_dbfs=rounded_dbfs(res.peak_out),
        clipped_samples=res.clipped,
        seconds=round(time.monotonic() - t0, 1),
        gain=res.gain,
    )


def report_dict(r: FilterReport) -> dict:
    return asdict(r)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Apply a BEQ filter JSON to a CAF file in place")
    ap.add_argument("caf")
    ap.add_argument("filter_json")
    args = ap.parse_args(argv)
    job = FilterJob.load(args.filter_json)

    def prog(stage: str, pct: float) -> None:
        print(f"\r{stage} {pct:5.1f}%", end="", file=sys.stderr)

    r = apply(args.caf, job, prog)
    print(file=sys.stderr)
    print(json.dumps(report_dict(r), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
