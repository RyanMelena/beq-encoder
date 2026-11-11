"""The object routes: TrueHD Atmos (truehdd) and DD+ Atmos (Cavern). Each decodes the
objects to a DAMF, keeps it for the check, BEQs a second copy and encodes that with DEE.
atmos.verify checks the encode sample by sample, and its object positions frame by frame.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

from .. import atmos, caf, media, pcmio
from ..beqfilter import FilterJob, apply, report_dict
from ..biquad import design_sos
from ..dee import ATMOS_INPUT
from . import tools
from .job import Encoded, Job
from .processes import JobFailed, every

_SR = tools.SR
_OUT = "beq.mlp"
JOC_METHOD = "objects decoded by Cavern, encoded sample-exact from that decode"


def _require_continuous_timeline(track: media.AudioTrack, fmap: pcmio.FrameMap) -> None:
    """A gap in the track's timeline cannot be filled here: a decoded DAMF's audio and its
    objects' metadata would both have to move, and silence inserted into the audio alone
    would put every later position off. So a track with one is refused rather than encoded
    with everything after the gap playing early. The map's frame count is what the
    decode must then match (`_require_complete_decode`)."""
    if fmap.gaps:
        raise JobFailed(f"The {track.kind_name} track has {len(fmap.gaps)} gap(s) in its "
                        f"timeline ({1000 * fmap.gap_frames / _SR:.1f} ms in all, first at "
                        f"{fmap.gaps[0][0] / _SR:.3f} s). An Atmos track cannot be filled "
                        "without moving its objects, and leaving a gap out would make "
                        "everything after it play early, so it was left alone")


def _require_complete_decode(decoder: str, frames: int, fmap: pcmio.FrameMap,
                             track: media.AudioTrack) -> None:
    """The object decoders are not the ones that mapped the timeline, so the decode must
    be shown to hold the same samples: truehdd exits 0 even when it skips to the next major
    sync after a decode error, leaving a shorter programme with everything after the loss
    playing early. (A clean decode's count equals the map's: measured, truehdd 0.6.2 on
    TrueHD streams.)"""
    if frames != fmap.frames:
        raise JobFailed(f"{decoder} decoded {frames} samples of the {track.kind_name} track, "
                        f"which holds {fmap.frames}: the decode is not complete, so it was "
                        "left alone")


def encode_truehd_atmos(job: Job, dee, track: media.AudioTrack, source: tools.LocalTrack,
                        fj: FilterJob, duration: float | None) -> Encoded:
    _require_continuous_timeline(track, source.timeline)
    raw = tools.extract_raw(job, source.path, track)
    dialnorm = tools.truehd_dialnorm(job, raw)
    src = job.work / "source"  # the decode, kept unfiltered for the check
    _truehdd_decode(job, raw, src, "Decoding TrueHD Atmos (truehdd)", duration)
    _require_complete_decode("truehdd", caf.read_info(str(atmos.files(src)[1])).frames,
                             source.timeline, track)
    job.log(f"Source Atmos programme: {atmos.read_layout(atmos.files(src)[0]).describe()}, "
            f"dialogue level {dialnorm} dBFS")
    return _filter_encode_verify(job, dee, src, source.path, fj, dialnorm, duration)


def encode_ddp_atmos(job: Job, dee, track: media.AudioTrack, source: tools.LocalTrack,
                     fj: FilterJob, duration: float | None) -> Encoded:
    _require_continuous_timeline(track, source.timeline)
    ec3 = _extract_ec3(job, source.path, track)
    dialnorm = tools.source_dialnorm(job, track, source.path)
    src = job.work / "source"  # the decode, kept unfiltered for the check
    decoder = _cavern_decode(job, ec3, src, int(track.channels or 6), duration)
    _require_complete_decode("Cavern", decoder["damf_frames"], source.timeline, track)
    job.log(f"Source Atmos programme (Cavern): "
            f"{atmos.read_layout(atmos.files(src)[0]).describe()}, dialogue level "
            f"{dialnorm} dBFS")
    return _filter_encode_verify(job, dee, src, source.path, fj, dialnorm, duration,
                                 min_object_corr=atmos.JOC_MIN_OBJECT_CORR,
                                 method=JOC_METHOD, decoder=decoder)


def _filter_encode_verify(job: Job, dee, src: Path, local: Path, fj: FilterJob, dialnorm: int,
                          duration: float | None, min_object_corr: float = atmos.MIN_OBJECT_CORR,
                          method: str = atmos.METHOD,
                          decoder: dict | None = None) -> Encoded:
    """BEQ every element of the master at `src` into a second set, encode that with DEE,
    drop it, and check the result against `src` as decoded."""
    out = job.work / _OUT
    filt = job.work / "atmos"
    atmos.derive(src, filt)
    # rounded, not dithered: DEE codes a dithered master with samples 1-4 LSB off
    rep = apply(str(atmos.files(src)[1]), dataclasses.replace(fj, dither=False),
                job.checked_progress, out_path=str(atmos.files(filt)[1]))
    report = report_dict(rep)
    try:
        tools.run_dee(job, dee, ATMOS_INPUT, atmos.files(filt)[0], out, "TrueHD Atmos",
                      dialnorm)
    finally:
        if not job.s.keep_work:
            for p in atmos.files(filt):
                p.unlink(missing_ok=True)
    report["fidelity"] = _verify(job, src, out, fj, rep.gain, duration, min_object_corr, method,
                                 decoder)
    report["dialnorm"] = dialnorm
    return Encoded(out, report, local)


def _extract_ec3(job: Job, local: Path, track: media.AudioTrack) -> Path:
    ec3 = job.work / "source.ec3"
    job.procs.run([job.s.ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(local),
                   "-map", "0:a:0", "-c", "copy", "-f", "eac3", str(ec3)])
    if not ec3.is_file() or ec3.stat().st_size == 0:
        raise JobFailed(f"Could not read the {track.kind_name} bitstream")
    return ec3


def _cavern_decode(job: Job, ec3: Path, base: Path, core_channels: int,
                   duration: float | None) -> dict:
    """cavern-dump: E-AC-3 + JOC -> DAMF, rendered past the end so the objects' last
    samples leave the decoder; then aligned to the stream's own timeline and length,
    and checked against ffmpeg's plain E-AC-3 decode (bed = LFE, objects ~ mains)."""
    exe = job.s.cavern_dump
    if not exe or not Path(exe).is_file():
        raise JobFailed("DD+ Atmos needs cavern-dump (built into the image; see the "
                        "Dockerfile's cavern stage)")
    for p in atmos.files(base):
        p.unlink(missing_ok=True)
    stage = "Decoding DD+ Atmos objects (Cavern)"
    job.progress(stage, 0)
    length: list[int] = []

    def on_line(ln: str) -> None:
        m = re.search(r"\blength=(\d+)", ln)
        if m and not length:
            length.append(int(m.group(1)))
        m = re.match(r"progress (\d+) (\d+)", ln.strip())
        if m:
            job.progress(stage, 100 * int(m.group(1)) / max(1, int(m.group(2))))

    job.procs.run([exe, "damf", str(ec3), f"{base}.atmos", "1536", str(atmos.CAVERN_FLUSH)],
                  on_line=on_line, filt=lambda ln: "" if ln.startswith("progress ") else ln)
    frames = length[0] if length else 0
    if not frames or not all(p.is_file() for p in atmos.files(base)):
        raise JobFailed("cavern-dump produced no Dolby Atmos master file set")
    try:
        atmos.align_joc(base, frames)
    except ValueError as exc:
        raise JobFailed(f"Cavern's decode could not be aligned: {exc}") from None
    core = tools.decode_stream(job, ec3, core_channels, "DD+ core")
    try:
        alignment = atmos.joc_alignment(base, core)
    finally:
        core.close(check=False)
    alignment["frames"] = frames
    job.log(f"Cavern decode: {json.dumps(alignment)}")
    if alignment["damf_frames"] != frames or alignment["core_frames"] != frames:
        raise JobFailed(f"Cavern decoded {alignment['damf_frames']} samples, ffmpeg "
                        f"{alignment['core_frames']}, the stream holds {frames}: not complete")
    for k in ("lfe_corr", "objects_corr"):
        if alignment[k] is not None and alignment[k] < atmos.MIN_ALIGN_CORR:
            raise JobFailed(f"Cavern's decode does not line up with the plain E-AC-3 "
                            f"decode ({k} {alignment[k]:.4f} < {atmos.MIN_ALIGN_CORR}): its "
                            "timing has changed; the DD+ Atmos route needs re-measuring")
    g = alignment.get("lfe_gain_db")
    if g is not None and abs(g) > atmos.MAX_JOC_LFE_GAIN_DB:
        raise JobFailed(f"Cavern's LFE is {g:+.2f} dB against the plain E-AC-3 decode "
                        "(it must be bit-identical): its decode has changed; the "
                        "DD+ Atmos route needs re-measuring")
    return alignment


def _truehdd_decode(job: Job, raw: Path, base: Path, stage: str,
                    duration: float | None) -> None:
    """truehdd: a TrueHD (Atmos) bitstream file -> DAMF at `base` (no bed conform,
    no warp: the elements as they are in the stream)."""
    job.progress(stage, 0)
    for p in atmos.files(base):
        p.unlink(missing_ok=True)
    audio = atmos.files(base)[1]

    def show_decoded_length() -> None:  # progress from the growing CAF
        try:
            info = caf.read_info(str(audio))
            secs = (audio.stat().st_size - info.data_offset) / info.frame_bytes / _SR
        except (OSError, ValueError, ZeroDivisionError):
            return
        if duration:
            job.progress(stage, min(99.0, 100 * secs / duration))

    try:
        with every(2, show_decoded_length):
            job.procs.run([job.s.truehdd_path, "--loglevel", "warn", "decode",
                           "--output-path", str(base), str(raw)])
    except JobFailed:
        raise JobFailed("truehdd decode failed (see log)") from None
    if not all(p.is_file() for p in atmos.files(base)):
        raise JobFailed("truehdd produced no Dolby Atmos master file set")


def _verify(job: Job, src: Path, out: Path, fj: FilterJob, gain: float, duration: float | None,
            min_object_corr: float, method: str, decoder: dict | None) -> dict:
    """Decode the encoded track and check it against the source's decode (`src`,
    kept unfiltered), BEQ-filtered afresh here (atmos.verify)."""
    check = job.work / "check"
    _truehdd_decode(job, out, check, "Checking: decoding the encoded TrueHD Atmos", duration)
    stage = "Checking the TrueHD Atmos encode against the filtered source"
    job.progress(stage, 0)
    tick = job.ticker(stage, (duration or 0) * _SR)
    sos = design_sos(fj.filters, float(_SR))
    try:
        res = atmos.verify(src, check, sos, gain, tick=tick, min_object_corr=min_object_corr,
                           method=method)
        if decoder:
            res.decoder = decoder
    finally:
        if not job.s.keep_work:
            for b in (check, src):
                for p in atmos.files(b):
                    p.unlink(missing_ok=True)
    d = res.to_dict()
    job.log(f"TrueHD Atmos check: {json.dumps(d)}")
    if not res.ok:
        raise JobFailed(f"The TrueHD Atmos encode does not carry the BEQ-filtered source: "
                        f"{res.problem}")
    job.log(f"TrueHD Atmos check passed ({res.method}): {res.objects} objects + "
            f"{res.bed or 'no'} bed carried, every sample within {res.max_err_lsb:.2f} LSB, "
            f"positions checked in {res.object_frames} object-frames (updates up to "
            f"{res.max_update_lead} samples early, at the start of their frame)")
    return d
