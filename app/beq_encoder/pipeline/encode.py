"""Phase 2 of a job: the chosen filter on the chosen track, delivered into the file."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .. import media, pcmio, support
from ..beqfilter import FilterJob
from ..biquad import FilterSpec
from . import artifacts, remux, route_atmos, route_pcm, tools
from .job import Encoded, FileStamp, Job, source_pin
from .processes import JobFailed

SYNC_TOLERANCE_MS = 1.0
# DAMF essence written by truehdd: up to 16 channels of 24-bit 48 kHz PCM
DAMF_BYTES_PER_SECOND = 48000 * 16 * 3
# The largest a TrueHD track can be: its format's peak rate (18 Mbit/s) for the whole film
TRUEHD_PEAK_BYTES_PER_SECOND = 18_000_000 // 8
# The remux beside the source is the source plus the new track; mkvmerge's own framing and
# the charts it may carry are well inside this
REMUX_SIZE_MARGIN = 1.01


def _require_lossless_output(route: str, track: media.AudioTrack, fmt: str) -> None:
    """A lossy output format, or a PCM-only format for an object track (which would keep
    only its bed), is refused whatever the caller asked for."""
    if fmt not in ("flac", "truehd"):
        raise JobFailed(f"Output {fmt!r} is not lossless: {support.LOSSY_OUT_OF_SCOPE}")
    if route in ("damf", "joc") and fmt != "truehd":
        raise JobFailed(f"{fmt.upper()} from a {track.kind_name} track would keep only its "
                        f"bed: {support.LOSSY_OUT_OF_SCOPE}")


def codec_label(route: str, track: media.AudioTrack, fmt: str) -> str:
    """What the BEQ track is: "TrueHD Atmos", "TrueHD 5.1", "FLAC 7.1" (test only)."""
    if route in ("damf", "joc"):
        return "TrueHD Atmos"
    return f"{'FLAC' if fmt == 'flac' else 'TrueHD'} {track.layout_label}"


def _require_dee(job: Job, fmt: str):
    """The DEE this job will use, refused here rather than after the decode."""
    if fmt == "flac":
        return None
    dee = job.s.dee()
    problems = dee.status()["problems"]
    for problem in problems:
        job.log(f"DEE: {problem}")
    if problems:
        raise JobFailed(problems[0])
    return dee


def _check_space(job: Job, probe: media.ProbeResult, route: str, fmt: str,
                 track: media.AudioTrack) -> None:
    dur = probe.duration_s or 3 * 3600
    pcm = dur * int(track.sample_rate or tools.SR) * 3 * int(track.channels or 8)
    if route in ("damf", "joc"):
        # track copy + bitstream + the decoded DAMF (kept for the check) + the
        # filtered one + DEE temp (Dolby: ~7.8 GB/h) + output; then decoded + check
        need_work = int(dur * DAMF_BYTES_PER_SECOND * 3.2)
    elif fmt == "truehd":
        need_work = int(pcm * 2.4)  # WAV + DEE temp + MLP
    else:
        need_work = int(pcm * 0.8)  # FLAC
    free_work = shutil.disk_usage(job.s.work_dir).free
    if free_work < need_work:
        raise JobFailed(f"Work dir needs ~{need_work / 1e9:.0f} GB free, "
                        f"has {free_work / 1e9:.0f} GB")
    track_bound = dur * TRUEHD_PEAK_BYTES_PER_SECOND if fmt == "truehd" else pcm
    need_src = int(job.src.stat().st_size * REMUX_SIZE_MARGIN + track_bound)
    free_src = shutil.disk_usage(job.src.parent).free
    if free_src < need_src:
        raise JobFailed(f"Media volume needs ~{need_src / 1e9:.0f} GB free for the remux, "
                        f"has {free_src / 1e9:.0f} GB")


def _start_offset(job: Job, route: str, start: float, rate: int) -> tuple[float, int]:
    """Where the track starts on the film's timeline: (whole ms via mkvmerge --sync,
    frames of leading silence for the sub-millisecond rest; PCM routes only)."""
    if route in ("damf", "joc"):
        sync_ms, pad = round(max(0.0, start) * 1000), 0
    else:
        sync_ms, pad = pcmio.lead(start, rate)
    job.log(f"Track starts at {start} s: --sync {sync_ms} ms"
            + (f" + {pad} frames of leading silence" if pad else ""))
    return sync_ms, pad


def run_encode(job: Job) -> dict:
    p = job.params
    fmt = p.get("format", "truehd")
    entry = p.get("entry")
    if not entry or not entry.get("filters"):
        raise JobFailed("No BEQ filter was chosen for this job")

    stamp, probe = job.read_source()
    track = probe.track(int(p["track_id"]))
    if track is None:
        raise JobFailed(f"Track {p['track_id']} is not in the file any more; rescan it")
    if track.beq:
        raise JobFailed(f"Track {track.track_id} is itself a BEQ track: run the original instead")
    if p.get("source") is not None and p["source"] != source_pin(stamp, track):
        raise JobFailed(f"{job.src.name} is not the file this job was prepared from, so "
                        "it was left alone: run the file again")
    pl = support.plan(track)
    if not pl.ok:
        raise JobFailed(f"Not supported: {pl.reason}")
    route = pl.route
    _require_lossless_output(route, track, fmt)
    if fmt not in support.formats(track, job.s.allow_test_formats):
        raise JobFailed(f"Output {fmt!r} is not allowed (lossless TrueHD only)")
    job.log(f"Source track: id={track.track_id} a:{track.audio_index} {track.label} "
            f"({track.codec}) layout={track.layout} lang={track.language} "
            f"name={track.name!r} delay={track.delay_ms}ms; route {route}, output {fmt}")

    dee = _require_dee(job, fmt)
    codec = codec_label(route, track, fmt)
    artifacts.track_name(job, track, codec, entry)  # a bad template fails before the long stages
    _check_space(job, probe, route, fmt, track)
    job.work.mkdir(parents=True, exist_ok=True)

    fj = FilterJob(
        filters=[FilterSpec.from_catalogue(f) for f in entry["filters"]],
        target_peak_dbfs=float(p.get("target_peak_dbfs", -1.0)),
        fixed_gain_db=p.get("fixed_gain_db"),
    )
    job.log(f"BEQ: {entry.get('title')} ({entry.get('year')}) "
            f"{', '.join(entry.get('audio_types') or [])} by {entry.get('label')} — "
            f"{len(fj.filters)} filters")

    duration = probe.duration_s
    rate = int(track.sample_rate or tools.SR)
    source = tools.SourceTracks(job, remux.identify(job, job.src))
    local = tools.read_track(job, track, rate, duration, source)
    sync_ms, pad = _start_offset(job, route, local.timeline.first_pts, rate)
    if route == "damf":
        encoded = route_atmos.encode_truehd_atmos(job, dee, track, local, fj, duration)
    elif route == "joc":
        encoded = route_atmos.encode_ddp_atmos(job, dee, track, local, fj, duration)
    else:
        encoded = route_pcm.encode_pcm(job, dee, track, local, fj, fmt, duration, pad)
    report = encoded.report
    job.log(f"Filter report: {json.dumps(report)}")
    if report.get("clipped_samples"):
        raise JobFailed(
            f"{report['clipped_samples']} samples clip after the BEQ filter "
            f"(gain {report.get('gain_db')} dB): the track would not be the lossless "
            "BEQ of the source; lower the fixed gain or use an automatic headroom mode")
    if not encoded.out.is_file() or encoded.out.stat().st_size == 0:
        raise JobFailed(f"Encoder finished but wrote no {encoded.out.name}")
    lead = int(report["fidelity"]["offset_frames"])
    if lead:  # the check measured them to the sample; they would play as a delay
        sync_ms -= 1000 * lead / rate
        job.log(f"The encoder added {lead} frames of leading silence: --sync now {sync_ms:.2f} ms")
    # dialnorm is the source's own, so this gain is the whole level change against it
    name = artifacts.track_name(job, track, codec, entry, report.get("gain_db"))
    job.log(f"BEQ track name: {name}")

    return _deliver(job, stamp, probe, source, track, fmt, route, sync_ms,
                    artifacts.BeqTrack(track, codec, name, entry, report), encoded, rate)


def _deliver(job: Job, stamp: FileStamp, probe: media.ProbeResult, source: tools.SourceTracks,
             track: media.AudioTrack, fmt: str, route: str, sync_ms: float,
             beq: artifacts.BeqTrack, encoded: Encoded, rate: int) -> dict:
    """Charts, remux, sync check, attach, swap: the encoded track becomes part of the file."""
    p, out = job.params, encoded.out
    art = artifacts.make_charts(job, beq, out, encoded.source, probe.duration_s) \
        if job.s.charts else None
    tags = artifacts.write_tags(job, beq, art)
    replace_ids = (
        [a.track_id for a in probe.beq_tracks] if p.get("replace_existing_beq", True) else []
    )
    dropped = {a["id"] for a in probe.beq_attachments} if replace_ids else set()
    want = remux.plan_remux(source.ident, track.track_id,
                            remux.identify(job, out)["tracks"][0]["codec"], replace_ids, dropped,
                            remux.measure_beq(job, out, rate))
    spec = remux.MuxSpec(probe, track, out, beq.name, replace_ids,
                         bool(p.get("default_flag", job.s.beq_default_flag)), want, source)

    def build(tags_path: Path) -> remux.Remuxed:
        job.require_unchanged(stamp)
        return remux.remux(job, spec, sync_ms, tags_path)

    muxed = build(tags)
    sync = remux.sync_check(job, muxed, track)
    if sync.reliable and abs(sync.lag_ms) > SYNC_TOLERANCE_MS:
        sync_ms -= sync.lag_ms
        job.log(f"Correcting BEQ track sync by {-sync.lag_ms:+.2f} ms and re-muxing")
        muxed = build(tags)
        sync = remux.sync_check(job, muxed, track)
        if sync.reliable and abs(sync.lag_ms) > SYNC_TOLERANCE_MS:
            raise JobFailed(f"BEQ track still {sync.lag_ms:+.2f} ms out of sync")

    sync_info = {"lag_ms": sync.lag_ms, "confidence": sync.confidence, "applied_ms": sync_ms,
                 "channels": sync.channels, "channel_corr": sync.diag}
    warnings: list[str] = []
    if not sync.reliable:
        warnings.append("the A/V sync check was inconclusive (a quiet or uncorrelated passage)")
    if art and not artifacts.embed_charts(job, spec, beq, art, sync_info):
        # a failed in-place edit leaves the temp MKV in an unknown state: a fresh one
        # without the charts, the track still whole (a chart never fails the job)
        muxed = build(artifacts.write_tags(job, beq))
        warnings.append("the charts could not be embedded in the file")
    warnings += [f"mkvmerge: {w}" for w in muxed.warnings]

    job.procs.check()
    after = remux.swap_into_place(job, stamp)
    try:
        job.progress("Done", 100)
    except Exception as exc:  # noqa: BLE001 - the file is replaced: a report must not undo that
        job.log(f"WARNING: could not report the end of the job: {exc}")
    return {
        "phase": "encode",
        "track_name": beq.name,
        "format": fmt,
        "route": route,
        "codec": beq.codec,
        "filter_report": beq.report,
        "output_bytes": out.stat().st_size,
        "replaced_tracks": replace_ids,
        "sync": sync_info,
        "charts": art.public() if art else None,
        "warnings": warnings,
        "source": source_pin(after, track),
    }
