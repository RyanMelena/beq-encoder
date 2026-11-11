"""The route for channel-based sources: bit-exact decode -> BEQ -> WAV/FLAC -> check."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import beqfilter, caf, layouts, media, pcmio
from ..beqfilter import FilterJob, WriteBlock
from ..biquad import design_sos
from ..dee import PCM_INPUTS, WAV_CHANNEL_ORDER
from . import tools
from .job import Encoded, Job
from .processes import JobFailed

CHUNK_FRAMES = 1 << 17
FULL_SCALE_24 = 2**23


@dataclass(frozen=True, slots=True)
class PcmExpected:
    """What the encoded track must decode to: `pad` silent frames, then BEQ(source) x
    gain, with silence at `gaps`."""
    source: Path                # the track's local copy
    gaps: list[tuple[int, int]]
    sos: np.ndarray
    gain: float
    pad: int
    channels: int
    rate: int


def block_writer(emit: Callable[[bytes], object], order: list[int] | None) -> WriteBlock:
    """Quantise to 24 bits as left-aligned s32le, optionally re-ordering the columns."""
    def write(y: np.ndarray) -> int:
        q, clipped = caf.quantise(y, FULL_SCALE_24)
        q <<= 8
        if order is not None:
            q = q[:, order]
        emit(q.astype("<i4").tobytes())
        return clipped

    return write


def _source_stream(job: Job, path: Path, channels: int, what: str,
                   gaps: list[tuple[int, int]]) -> pcmio.GapFilled:
    return pcmio.GapFilled(tools.decode_stream(job, path, channels, what), gaps)


def _encode_args(container: str) -> tuple[list[str], str]:
    if container == "flac":
        return ["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
                "-compression_level", "5"], "FLAC"
    return ["-c:a", "pcm_s24le", "-rf64", "auto", "-f", "wav"], "WAV"  # RF64 past 4 GB, for DEE


def _filter_to_file(job: Job, track: media.AudioTrack, source: tools.LocalTrack, fj: FilterJob,
                    out: Path, container: str, duration: float | None,
                    pad: int) -> tuple[dict, PcmExpected]:
    """BEQ on the bit-exact decode of the track's local copy, in its own
    layout and rate, -> 24-bit FLAC or WAV. `pad` frames of digital silence go first (the
    part of the track's start offset below a millisecond, see pcmio.lead)."""
    t0 = time.monotonic()
    ch = int(track.channels or 0)
    try:
        layout = layouts.output_layout(ch, track.layout)
    except ValueError as exc:
        raise JobFailed(str(exc)) from None
    rate = int(track.sample_rate or tools.SR)  # the source's own rate: resampling loses data
    sos = design_sos(fj.filters, float(rate))
    local, fmap = source.path, source.timeline
    if fmap.gaps:
        job.log(f"NOTE: the decoder produced nothing for {len(fmap.gaps)} stretch(es) of "
                f"the timeline ({1000 * fmap.gap_frames / rate:.1f} ms in all; first at "
                f"{fmap.gaps[0][0] / rate:.3f} s): filled with digital silence, as a player "
                "would, to keep the rest in sync")

    total = (duration or 0) * rate

    def read(stage: str) -> Iterator[np.ndarray]:
        tick = job.ticker(stage, total)
        try:
            with _source_stream(job, local, ch, f"{track.kind_name} track", fmap.gaps) as src:
                while len(x := src.read(CHUNK_FRAMES)):
                    tick(src.frames)
                    yield x
        except RuntimeError as exc:  # the decoder failed
            raise JobFailed(str(exc)) from None
        if src.frames == 0:
            raise JobFailed(f"ffmpeg decoded no audio from the {track.kind_name} track")
        if src.inner.frames != fmap.frames:
            raise JobFailed(f"ffmpeg decoded {src.inner.frames} frames but the timeline "
                            f"map has {fmap.frames}: the decode is not repeatable")

    codec, label = _encode_args(container)
    order = WAV_CHANNEL_ORDER.get(ch) if container == "wav" else None
    if order is not None:
        job.log(f"WAV for DEE in Dolby channel order: columns {order} of the {layout} decode")
    cmd = [job.s.ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "s32le", "-ar", str(rate),
           "-ch_layout", layout, "-i", "-", *codec, str(out)]
    with job.procs.feed(cmd, f"{label} encode") as emit:
        if pad:
            emit(np.zeros((pad, ch), dtype="<i4").tobytes())
        res = beqfilter.filter_stream(read, block_writer(emit, order), sos, ch,
                                      fj.target_peak_dbfs, fj.fixed_gain_db, pcmio.LSB24,
                                      f"BEQ filter: writing {label}")
    limited_by = ("fixed gain" if fj.fixed_gain_db is not None else
                  "none needed" if res.gain_db == 0.0 else "target")
    report = {"path": str(out), "channels": ch, "layout": layout, "sample_rate": float(rate),
              "bits": 24, "frames": res.frames, "pad_frames": pad, "gaps": len(fmap.gaps),
              "gap_frames": fmap.gap_frames, "source_frames": fmap.frames,
              "peak_in_dbfs": beqfilter.rounded_dbfs(res.peak_in),
              "peak_filtered_dbfs": beqfilter.rounded_dbfs(res.peak_filtered),
              "gain_db": round(res.gain_db, 2),
              "peak_out_dbfs": beqfilter.rounded_dbfs(res.peak_out),
              "peak_target_dbfs": fj.target_peak_dbfs, "gain_limited_by": limited_by,
              "clipped_samples": res.clipped, "seconds": round(time.monotonic() - t0, 1)}
    return report, PcmExpected(local, fmap.gaps, sos, res.gain, pad, ch, rate)


def _verify(job: Job, expected: PcmExpected, encoded: Path, label: str,
            duration: float | None) -> dict:
    """Decode the encoded BEQ track in full and check every sample against the
    filtered source: proves nothing but the BEQ filter, gain and dither changed."""
    stage = f"Verifying the {label} track sample by sample"
    job.progress(stage, 0)
    tick = job.ticker(stage, (duration or 0) * expected.rate)
    src = _source_stream(job, expected.source, expected.channels, "source track", expected.gaps)
    enc = tools.decode_stream(job, encoded, expected.channels, f"encoded {label} track")
    try:
        res = pcmio.verify(src, enc, expected.sos, expected.gain, expected.pad, tick=tick)
    finally:
        src.close(check=False)
        enc.close(check=False)
    d = res.to_dict()
    job.log(f"Fidelity check ({label}): {json.dumps(d)}")
    if not res.ok:
        raise JobFailed(f"The encoded {label} track is not the lossless BEQ of the "
                        f"source: {res.problem}")
    if res.offset_frames:
        job.log(f"NOTE: the encoder added {res.offset_frames} frames of leading silence")
    return d


def encode_pcm(job: Job, dee, track: media.AudioTrack, source: tools.LocalTrack, fj: FilterJob,
               fmt: str, duration: float | None, pad: int) -> Encoded:
    if fmt == "flac":
        out = job.work / "beq.flac"
        report, expected = _filter_to_file(job, track, source, fj, out, "flac", duration, pad)
        label = "FLAC"
    else:
        dialnorm = tools.source_dialnorm(job, track, source.path)  # before the long passes
        wav = job.work / "beq.wav"
        report, expected = _filter_to_file(job, track, source, fj, wav, "wav", duration, pad)
        out = job.work / "beq.mlp"
        tools.run_dee(job, dee, PCM_INPUTS[expected.channels], wav, out, "TrueHD", dialnorm)
        report["dialnorm"] = dialnorm
        if not job.s.keep_work:
            wav.unlink(missing_ok=True)
        label = "TrueHD"
    report["fidelity"] = _verify(job, expected, out, label, duration)
    return Encoded(out, report, expected.source)
