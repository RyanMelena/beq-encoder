"""What is written about a BEQ track: its name, tags, charts and embedded report."""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from .. import __version__, assess, charts, layouts, media
from .job import Job
from .processes import Cancelled, JobFailed
from .remux import MuxSpec, verify_temp_mkv


@dataclass(frozen=True, slots=True)
class BeqTrack:
    """The finished BEQ track, as its charts, tags and report describe it."""
    source: media.AudioTrack
    codec: str
    name: str
    entry: dict
    report: dict    # the job's filter_report


@dataclass(slots=True)
class ChartSet:
    """The before/after charts and report of one BEQ track (kept under /config/charts)."""
    dir: Path
    names: dict[str, str]          # kind -> attachment file name in the MKV
    files: dict[str, Path]         # kind -> file on disk
    report: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"dir": str(self.dir), "names": self.names,
                "files": {k: p.name for k, p in self.files.items()}}


def track_name(job: Job, track: media.AudioTrack, codec: str, entry: dict) -> str:
    source = entry.get("label") or entry.get("author") or "BEQ"
    try:
        name = job.s.track_name_template.format(
            orig=track.name or track.label, codec=codec, bitrate="",
            author=entry.get("author", ""), source=source,
        ).strip()
    except (KeyError, IndexError, ValueError) as exc:
        raise JobFailed(f"TRACK_NAME_TEMPLATE cannot be used: {exc!r}") from None
    name = re.sub(r"\s*\(\s*\)", "", name)
    if not media.BEQ_NAME_RE.search(name):
        name = f"BEQ {name}"
    return name


def _stream_shape(job: Job, path: Path) -> tuple[int, str | None]:
    r = job.procs.capture([job.s.ffprobe, "-v", "error", "-select_streams", "a:0",
                           "-show_entries", "stream=channels,channel_layout", "-of", "json",
                           str(path)], timeout=300)
    st = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
    return int(st.get("channels") or 0), st.get("channel_layout") or None


def _measure(job: Job, path: Path, reports_progress: bool,
             duration: float | None) -> charts.Measurement:
    ch, layout = _stream_shape(job, path)
    if not ch:
        raise RuntimeError(f"no audio stream in {path.name}")
    names = layouts.names_or_generic(ch, layout)
    tick = job.ticker("Charting the original and the BEQ track",
                      (duration or 0) * charts.FS if reports_progress else 0)
    return charts.measure_stream(
        charts.decode_mono(job.s.ffmpeg, str(path), 0, ch, names, popen=job.procs.popen,
                           on_frames=tick))


def make_charts(job: Job, beq: BeqTrack, out: Path, local: Path,
                duration: float | None) -> ChartSet | None:
    """Measure the original and the encoded BEQ track, draw both charts. A chart that
    cannot be made is logged, never a reason to fail the BEQ track itself."""
    try:
        return _charts(job, beq, out, local, duration)
    except Cancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - charts are an extra
        job.log(f"WARNING: no charts for this BEQ track: {exc}")
        return None


def _charts(job: Job, beq: BeqTrack, out: Path, local: Path,
            duration: float | None) -> ChartSet:
    job.progress("Charting the original and the BEQ track", 0)
    t0 = time.monotonic()
    with ThreadPoolExecutor(2) as ex:
        fb = ex.submit(_measure, job, local, True, duration)
        fa = ex.submit(_measure, job, out, False, duration)
        before, after = fb.result(), fa.result()
    film = media.parse_name(job.src)
    title = film.title or job.src.stem
    if film.year:
        title = f"{title} ({film.year})"
    track, entry, report = beq.source, beq.entry, beq.report
    lang = track.language if track.language not in (None, "", "und") else ""
    source_track = " · ".join(x for x in (track.label, lang, f"track {track.track_id}") if x)
    if entry.get("origin") == "designer":
        beq_source = entry.get("label") or "beqforge"
    else:
        beq_source = " · ".join(x for x in ("BEQ Catalogue", entry.get("author"),
                                             ", ".join(entry.get("audio_types") or []))
                                if x)
    try:
        mv = float(entry.get("mv")) if entry.get("mv") not in (None, "", "0") else None
    except (TypeError, ValueError):
        mv = None
    facts: list[tuple[str, str]] = []
    fid = report.get("fidelity")
    if fid and fid.get("method"):  # TrueHD Atmos
        facts.append(("Lossless check",
                      f"passed · {fid['frames']:,} samples of {fid['objects']} objects + "
                      f"{fid['bed'] or 'no'} bed, every one the filtered original, "
                      "and every object where the original puts it"))
    elif fid:
        facts.append(("Lossless check",
                      f"passed · {fid['frames']:,} samples per channel, every one "
                      "the filtered original" if fid.get("ok") else
                      f"failed: {fid.get('problem')}"))
    facts.append(("BEQ track", f"{beq.codec}"))
    info = charts.ChartInfo(
        title=title, source_track=source_track, beq_track=beq.name, beq_source=beq_source,
        filters=entry.get("filters") or [], gain_db=float(report.get("gain_db") or 0.0),
        peak_in_dbfs=report.get("peak_in_dbfs"), peak_out_dbfs=report.get("peak_out_dbfs"),
        mv_db=mv, encoder=f"beq-encoder {__version__}",
        date=datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d"), facts=facts)
    d = job.s.charts_dir / f"job-{job.id}"
    d.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^A-Za-z0-9]", "", str(entry.get("digest") or ""))[-8:] or "track"
    files = {"spectrum": d / "spectrum.png", "heatmap": d / "heatmap.png",
             "report": d / "beq.json"}
    names = {"spectrum": f"beq-{tag}-spectrum.png", "heatmap": f"beq-{tag}-heatmap.png",
             "report": f"beq-{tag}.json"}
    charts.render_spectrum(before, after, info, files["spectrum"])
    charts.render_heatmap(before, after, info, files["heatmap"])
    data = charts.summary(before, after, info)
    files["report"].write_text(charts.dumps(data))  # completed with sync in embed_charts
    job.log(f"Charts: {', '.join(p.name for p in files.values())} in {d} "
            f"({time.monotonic() - t0:.1f} s)")
    return ChartSet(d, names, files, data)


def embed_charts(job: Job, spec: MuxSpec, beq: BeqTrack, art: ChartSet, sync: dict) -> bool:
    """Embed the charts and the report in the remuxed file (mkvpropedit, in place)
    and verify it. False: the temp MKV can't be trusted any more."""
    track, entry, report, name = beq.source, beq.entry, beq.report, beq.name
    rep = {
        **art.report,
        "beq_track": {"name": name, "codec": beq.codec, "language": track.language},
        "source_track": {"id": track.track_id, "label": track.label,
                         "codec": track.codec, "layout": track.layout,
                         "sample_rate": track.sample_rate},
        "filter": {k: entry.get(k) for k in (
            "origin", "label", "author", "title", "year", "audio_types", "source",
            "digest", "mv", "url", "strategy", "designer_release", "design_version",
            "design_settings", "filters") if entry.get(k) not in (None, "", [])},
        "levels": {k: report.get(k) for k in (
            "gain_db", "peak_in_dbfs", "peak_filtered_dbfs", "peak_out_dbfs",
            "clipped_samples", "sample_rate", "bits", "pad_frames", "gaps",
            "gap_frames") if k in report},
        "fidelity": report.get("fidelity"),
        "sync": sync,
        "charts": {k: v for k, v in art.names.items() if k != "report"},
        "encoder": f"beq-encoder {__version__}",
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    art.report = rep
    art.files["report"].write_text(json.dumps(rep, indent=1))
    what = {"spectrum": ("image/png", f"BEQ peak/average spectrum, before and after: {name}"),
            "heatmap": ("image/png", f"BEQ bass heat map over the running time: {name}"),
            "report": ("application/json", f"BEQ report (filters, levels, checks): {name}")}
    cmd = [job.s.mkvpropedit, str(job.tmp_mkv)]
    for kind, path in art.files.items():
        mime, desc = what[kind]
        cmd += ["--attachment-name", art.names[kind], "--attachment-mime-type", mime,
                "--attachment-description", desc, "--add-attachment", str(path)]
    job.progress("Embedding charts", None)
    try:
        job.procs.run(cmd, ok_codes=(0, 1))
        verify_temp_mkv(job, spec, sync["applied_ms"], attached=art.names.values())
    except JobFailed as exc:
        job.log(f"WARNING: charts not embedded in the file: {exc}")
        return False
    job.log(f"Embedded {', '.join(art.names.values())}")
    return True


def write_tags(job: Job, beq: BeqTrack, art: ChartSet | None = None) -> Path:
    """Matroska track tags recording where the filter came from, so a later run can
    tell a catalogue entry from a design, and which designer version made it."""
    track, entry, report = beq.source, beq.entry, beq.report
    vals = {
        "BEQ_SOURCE": entry.get("origin", "catalogue"),
        "BEQ_LABEL": entry.get("label", ""),
        "BEQ_AUTHOR": entry.get("author", ""),
        "BEQ_DIGEST": entry.get("digest", ""),
        "BEQ_AUDIO_TYPES": ", ".join(entry.get("audio_types") or []),
        "BEQ_STRATEGY": entry.get("strategy", ""),
        "BEQ_DESIGNER_RELEASE": entry.get("designer_release", ""),
        "BEQ_DESIGN_VERSION": str(entry.get("design_version") or ""),
        "BEQ_DESIGN_SETTINGS": entry.get("design_settings") or "",
        "BEQ_FILTERS": json.dumps(entry.get("filters") or []),
        "BEQ_GAIN_DB": str(report.get("gain_db", "")),
        "BEQ_SOURCE_TRACK": f"{track.label} ({track.codec})",
        "BEQ_OUTPUT": beq.codec,
        "BEQ_ENCODER": f"beq-encoder {__version__}",
        "BEQ_ENCODER_REVISION": str(assess.ENCODER_REVISION),
        "BEQ_CHECK": _check_tag(report.get("fidelity")),
        # attachment file names (a player add-on finds the charts from the track)
        "BEQ_CHART_SPECTRUM": art.names.get("spectrum", "") if art else "",
        "BEQ_CHART_HEATMAP": art.names.get("heatmap", "") if art else "",
        "BEQ_REPORT": art.names.get("report", "") if art else "",
    }
    simple = "".join(
        f"<Simple><Name>{escape(k)}</Name><String>{escape(str(v))}</String></Simple>"
        for k, v in vals.items() if v not in ("", None)
    )
    path = job.work / "beq-tags.xml"
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<Tags><Tag><Targets>'
                    f'<TargetTypeValue>50</TargetTypeValue></Targets>{simple}</Tag></Tags>\n')
    return path


def _check_tag(fid: dict | None) -> str:
    """BEQ_CHECK: how the encode was verified against the BEQ-filtered source."""
    if not fid:
        return ""
    if fid.get("method"):
        return (f"{fid['method']}: {fid['frames']} frames of {fid['objects']} objects + "
                f"{fid['bed'] or 'no'} bed within {fid['max_err_lsb']:.2f} LSB (24-bit); "
                f"positions checked in {fid['object_frames']} object-frames"
                + (f", update times read to the {fid['update_times']}"
                   if fid.get("update_times") else ""))
    return (f"sample-exact: {fid['frames']} frames per channel within "
            f"{fid['max_err_lsb']:.2f} LSB (24-bit)")
