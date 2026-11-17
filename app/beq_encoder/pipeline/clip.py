"""A clip: a short excerpt of a film with its original track and one BEQ track for each
filter asked for, written to CLIP_DIR so the filters can be heard before one is chosen.
The film is only read, and only the part around the excerpt.

cut:      ffmpeg copies the video and the track over a window WINDOW_MARGIN_S wider than
          the clip on each side, reading only that part of the film (-ss seeks by the
          cues; -copyts keeps the film's timestamps, so -to is absolute) -> mkvmerge cuts
          it from the keyframe at or before the start -> the track alone, its first packet
          re-stamped where the cut gave it the keyframe's time (first_packet_fix)
beq:      each filter runs the full encode's route on that track: the same FilterJob
          (encode.filter_job), the same sample-by-sample check, at the gain the whole film
          would get (programme_peak_dbfs, from the 1 kHz analysis)
deliver:  mkvmerge muxes the video, the original track and the BEQ tracks to a hidden temp
          file in CLIP_DIR -> every track whole, and each BEQ track's A/V sync and channel
          order checked against the original -> renamed to its own name, never over a file
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import analysis, media, pcmio, preflight, support
from ..biquad import FilterSpec, design_sos, sosfilt_channels
from . import artifacts, remux, route_atmos, route_pcm, tools
from .encode import (SYNC_TOLERANCE_MS, _require_dee, _require_lossless_output, _start_offset,
                     codec_label, filter_job)
from .job import Job, source_pin
from .prepare import _analyse, _analysis_key
from .processes import JobFailed

WINDOW_MARGIN_S = 15.0  # read this much either side of the clip: the cut needs a keyframe before it
# The first packet of a cut is late-stamped when it sits further from the second than one
# packet length and this much more (Matroska timestamps are whole milliseconds)
FIRST_PACKET_SLACK_S = 0.0015
PACKETS_LOOKED_AT = 12
MAX_FILTERS = 4  # BEQ tracks in one clip: each is a whole encode of the excerpt
FILE_WORD = "BEQ demo"  # "Pacific Rim (2013) - BEQ demo 01.21.12.mkv"
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def hms(seconds: float, sep: str = ":") -> str:
    """01:21:12 (whole seconds, rounded down)."""
    s = int(max(0.0, seconds))
    return f"{s // 3600:02d}{sep}{s % 3600 // 60:02d}{sep}{s % 60:02d}"


def _mkv_time(seconds: float) -> str:
    """A timestamp as mkvmerge's --split parts: takes it, to the millisecond."""
    ms = int(round(max(0.0, seconds) * 1000))
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def parse_start(text: str | None) -> float | None:
    """A start time as people type it: "1:21:12", "81:12", "4872", "1:21:12.5"; empty is
    None (the clip picks its own). ValueError for anything else."""
    t = (text or "").strip()
    if not t:
        return None
    parts = t.split(":")
    if len(parts) > 3 or not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        raise ValueError(f"{t!r} is not a time: type it as h:mm:ss, m:ss or seconds")
    if any("." in p for p in parts[:-1]) or any(float(p) >= 60 for p in parts[1:]):
        raise ValueError(f"{t!r} is not a time: type it as h:mm:ss, m:ss or seconds")
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + float(p)
    return seconds


def clip_title(src: Path, start_s: float, sep: str = ":") -> str:
    """"<Film (Year)> - BEQ demo hh:mm:ss": what the clip is, as its title says it."""
    names = media.parse_name(src)
    film = names.title or src.stem
    if names.year:
        film = f"{film} ({names.year})"
    return f"{film} - {FILE_WORD} {hms(start_s, sep)}"


def clip_name(src: Path, start_s: float) -> str:
    """The clip's file name: "<Film (Year)> - BEQ demo hh.mm.ss.mkv"."""
    return _UNSAFE.sub("", f"{clip_title(src, start_s, '.')}.mkv").strip()


def free_name(folder: Path, name: str) -> Path:
    """`name` in `folder`, or "<stem> (2).mkv" and up when that is taken: a clip never
    replaces a file."""
    path = folder / name
    n = 2
    while path.exists():
        path = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    return path


def first_packet_fix(pts: list[float]) -> float | None:
    """Where the first packet of a cut track belongs, when the cut stamped it too early.

    ffmpeg and mkvmerge both give the first audio packet after a cut the time of the video
    keyframe the cut starts on, 30 to 80 ms before the next packet (measured on TrueHD:
    the access units themselves follow on without a break, so the packet belongs one
    packet length before the next). None when the packets already follow on."""
    if len(pts) < 3:
        return None
    steps = np.diff(np.asarray(pts[1:], dtype=np.float64))
    step = float(np.median(steps))
    if pts[1] - pts[0] <= step + FIRST_PACKET_SLACK_S:
        return None
    return pts[1] - step


def busiest_start(blocks: Iterable[np.ndarray], channels: list[str],
                  filters: list[list[dict]], length_s: float, fs: int = analysis.FS,
                  highpass_hz: float = 0.0) -> float:
    """Where the BEQ changes the film most: the start (whole seconds) of the `length_s`
    window in which the filters add the most energy to the mono mix (mains -20.2 dB,
    LFE -10.2 dB, as the designer's), summed over the filters. `blocks` is the 1 kHz
    analysis (analysis.read_channels). Each filter runs with the clip's high-pass floor. With
    no filter that changes anything there, the window where the mix itself is loudest."""
    gains = analysis.mono_gains(tuple(channels))
    soses = []
    for bands in filters:
        specs = [FilterSpec.from_catalogue(f) for f in bands]
        if specs and all(s.freq < preflight.MAX_BAND_HZ for s in specs):
            soses.append(design_sos(specs, fs, highpass_hz))
    states = [np.zeros((len(s), 2, 1)) for s in soses]
    added: list[np.ndarray] = []   # per second: what the filters add
    level: list[np.ndarray] = []   # per second: the mix itself
    done = 0
    for x in blocks:
        m = (x @ gains)[:, None]
        d2 = np.zeros(len(m))
        for k, sos in enumerate(soses):
            y, states[k] = sosfilt_channels(sos, m, states[k])
            d2 += ((y - m)[:, 0]) ** 2
        sec = (done + np.arange(len(m))) // fs
        added.append(np.bincount(sec, weights=d2, minlength=int(sec[-1]) + 1))
        level.append(np.bincount(sec, weights=m[:, 0] ** 2, minlength=int(sec[-1]) + 1))
        done += len(m)

    def per_second(parts: list[np.ndarray]) -> np.ndarray:
        n = max((len(p) for p in parts), default=0)
        return np.sum([np.pad(p, (0, n - len(p))) for p in parts], axis=0) if parts else \
            np.zeros(0)

    window = max(1, math.ceil(length_s))
    for energy in (per_second(added), per_second(level)):
        if len(energy) <= window:
            return 0.0
        c = np.concatenate([[0.0], np.cumsum(energy)])
        sums = c[window:] - c[:-window]
        if sums.max() > 0:
            return float(int(np.argmax(sums)))
    return 0.0


@dataclass(slots=True)
class ClipTrack:
    """One BEQ track of the clip, encoded and ready to mux."""
    entry: dict
    name: str
    out: Path
    tags: Path
    sync_ms: float
    report: dict
    frames: remux.BeqFrames
    # what set its gain: the film's estimated peak, the clip's own peak where it is higher,
    # or the fixed gain asked for
    gain_from: str  # "film" | "clip_peak" | "fixed"


def run_clip(job: Job) -> dict:
    p = job.params
    fmt = p.get("format", "truehd")
    items = p.get("filters") or []
    if not items or not all(i.get("entry", {}).get("filters") for i in items):
        raise JobFailed("No BEQ filter was chosen for this clip")
    stamp, probe = job.read_source()
    track = probe.track(int(p["track_id"]))
    if track is None:
        raise JobFailed(f"Track {p['track_id']} is not in the file any more; rescan it")
    if p.get("source") is not None and p["source"] != source_pin(stamp, track):
        raise JobFailed(f"{job.src.name} is not the file its filters were found for: find "
                        "filters again")
    pl = support.plan(track)
    if not pl.ok:
        raise JobFailed(f"Not supported: {pl.reason}")
    route = pl.route
    _require_lossless_output(route, track, fmt)
    if fmt not in support.formats(track, job.s.allow_test_formats):
        raise JobFailed(f"Output {fmt!r} is not allowed (lossless TrueHD only)")
    dee = _require_dee(job, fmt)
    codec = codec_label(route, track, fmt)
    artifacts.track_name(job, track, codec, items[0]["entry"])  # a bad template fails here
    job.work.mkdir(parents=True, exist_ok=True)
    job.s.clip_dir.mkdir(parents=True, exist_ok=True)

    duration = probe.duration_s or 0.0
    length = float(p["length_s"])
    start, picked = _start_and_levels(job, track, probe, items, p.get("start_s"), length)
    if duration and start >= duration:
        raise JobFailed(f"The film is only {hms(duration)} long, so a clip cannot start at "
                        f"{hms(start)}")
    if duration:
        start = max(0.0, min(start, duration - length))
    _check_space(job, probe, length)

    cut, original, began = _cut(job, probe, track, start, length)
    rate = int(track.sample_rate or tools.SR)
    local = _local_track(job, original, track, rate, length)
    job.log(f"Clip: {hms(began)} ({began:.3f} s) for {length:g} s"
            + (", picked where the filters add the most bass" if picked else "")
            + f"; the track starts {local.timeline.first_pts:.3f} s into it")
    beqs = [_encode_one(job.part(job.work / f"beq-{i}", f"BEQ track {i} of {len(items)}"),
                        dee, route, track, local, item, fmt, codec, rate, length)
            for i, item in enumerate(items, 1)]
    return _deliver(job, probe, track, cut, original, beqs, began, length, picked,
                    p.get("start_s"))


def _start_and_levels(job: Job, track: media.AudioTrack, probe: media.ProbeResult,
                      items: list[dict], start: float | None,
                      length: float) -> tuple[float, bool]:
    """The clip's start (picked from the 1 kHz analysis when none was given), and each
    filter's filtered peak over the whole film where the review did not estimate it
    (`peak_dbfs` set in place). Both read the analysis; it is made here when the cache
    has none, which reads the whole track. A filter whose level over the whole track cannot
    be measured fails the clip: a clip at its own level would play louder or quieter than
    the BEQ track it previews."""
    fixed = job.params.get("fixed_gain_db") is not None
    floor = job.params.get("highpass_hz") or 0.0
    unknown = {i["entry"]["digest"]: i["entry"]["filters"] for i in items
               if i.get("peak_dbfs") is None and not fixed}
    if start is not None and not unknown:
        return float(start), False
    key = _analysis_key(job, track)
    picked = start is None
    try:
        with analysis.in_use(key):
            meta = _analyse(job, track, probe, key)
            if unknown:
                job.progress("Estimating the gain of each filter", None)
                found = preflight.predict(analysis.read_channels(job.s.work_dir, meta),
                                          meta["channels"], unknown, floor)
                for i in items:
                    if i["entry"]["digest"] in found:
                        i["peak_dbfs"] = found[i["entry"]["digest"]]["peak_dbfs"]
            if picked:
                job.progress("Finding where the filters change the most", None)
                start = busiest_start(analysis.read_channels(job.s.work_dir, meta),
                                      meta["channels"], [i["entry"]["filters"] for i in items],
                                      length, highpass_hz=floor)
                job.log(f"Clip start picked from the 1 kHz analysis: {hms(start)}")
    except JobFailed as exc:
        raise JobFailed(f"The level change over the whole track could not be measured ({exc}), "
                        "so the clip was not made") from None
    missing = [i["entry"].get("label") or i["entry"]["digest"] for i in items
               if i.get("peak_dbfs") is None and not fixed]
    if missing:
        raise JobFailed(f"The level change over the whole track could not be measured for "
                        f"{', '.join(missing)}, so the clip was not made")
    return float(start), picked


def _check_space(job: Job, probe: media.ProbeResult, length: float) -> None:
    """The window, its cut and the clip, at the film's own bitrate."""
    try:
        rate = job.src.stat().st_size / max(1.0, probe.duration_s or 3 * 3600)
    except OSError:
        return
    window = rate * (length + 2 * WINDOW_MARGIN_S)
    for folder, need in ((job.work, window * 2.2), (job.s.clip_dir, rate * length * 1.3)):
        free = shutil.disk_usage(folder).free
        if free < need:
            raise JobFailed(f"{folder} needs ~{need / 1e9:.1f} GB free for this clip, has "
                            f"{free / 1e9:.1f} GB")


def _packet_times(job: Job, path: Path, stream: str, count: int = PACKETS_LOOKED_AT
                  ) -> list[float]:
    r = job.procs.capture([job.s.ffprobe, "-v", "error", "-select_streams", stream,
                           "-show_entries", "packet=pts_time", "-read_intervals", f"%+#{count}",
                           "-of", "csv=p=0", str(path)], timeout=300)
    return [float(v) for v in r.stdout.split() if re.fullmatch(r"-?\d+(\.\d+)?", v)]


def _keyframes(job: Job, path: Path) -> list[float]:
    r = job.procs.capture([job.s.ffprobe, "-v", "error", "-select_streams", "v:0",
                           "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0",
                           str(path)], timeout=600)
    out = []
    for ln in r.stdout.splitlines():
        f = ln.strip().split(",")
        if len(f) >= 2 and "K" in f[1]:
            try:
                out.append(float(f[0]))
            except ValueError:
                continue
    return sorted(out)


def _cut(job: Job, probe: media.ProbeResult, track: media.AudioTrack, start: float,
         length: float) -> tuple[Path | None, Path, float]:
    """(the video cut, or None for a film without one; the track over the same time,
    starting on its own timeline; where the clip starts in the film)."""
    video = any(t["type"] == "video" for t in probe.tracks)
    lo, hi = max(0.0, start - WINDOW_MARGIN_S), start + length + WINDOW_MARGIN_S
    window = job.work / "window.mkv"
    stage = "Reading the clip's part of the film"
    job.progress(stage, 0)

    def on_line(ln: str) -> None:
        m = re.match(r"out_time_us=(\d+)", ln.strip())
        if m:
            t = int(m.group(1)) / 1e6
            t = t - lo if t >= lo else t  # with -copyts ffmpeg may report the film's time
            job.progress(stage, min(100.0, 100 * t / (hi - lo)))

    job.procs.run([job.s.ffmpeg, "-nostdin", "-v", "error", "-y", "-progress", "pipe:1",
                   "-nostats", "-ss", f"{lo:.3f}", "-copyts", "-i", str(job.src),
                   *(["-map", "0:v:0"] if video else []), "-map", f"0:a:{track.audio_index}",
                   "-c", "copy", "-to", f"{hi:.3f}", "-map_chapters", "-1", "-f", "matroska",
                   str(window)],
                  on_line=on_line, filt=lambda ln: "" if re.match(r"^\s*\w+=\s*\S*\s*$", ln)
                  else ln)
    if not window.is_file() or window.stat().st_size == 0:
        raise JobFailed("Could not read that part of the film")
    began = start
    if video:
        keys = _keyframes(job, window)
        before = [k for k in keys if k <= start + 0.0005]
        if not keys:
            raise JobFailed("The film's video has no keyframe near that time: try another start")
        began = before[-1] if before else keys[0]
    job.progress("Cutting the clip", None)
    cut = job.work / "cut.mkv"
    job.procs.run([job.s.mkvmerge, "--gui-mode", "-o", str(cut), "--no-chapters",
                   "--no-global-tags", "--split",
                   f"parts:{_mkv_time(began)}-{_mkv_time(began + length)}", str(window)],
                  ok_codes=(0, 1), filt=lambda ln: "" if tools.gui_progress(ln) is not None
                  else ln)
    if not cut.is_file():  # mkvmerge numbers the parts of a split when it makes several
        cut = next(iter(sorted(job.work.glob("cut-*.mkv"))), cut)
    if not cut.is_file() or cut.stat().st_size == 0:
        raise JobFailed("mkvmerge could not cut the clip from the film")
    if not job.s.keep_work:
        window.unlink(missing_ok=True)
    original = _track_on_its_time(job, cut, "a:0")
    return (cut if video else None), original, began


def _track_on_its_time(job: Job, cut: Path, stream: str) -> Path:
    """The cut's track alone, with its first packet where it belongs (first_packet_fix):
    the bitstream is whole, but a packet stamped early reads as a gap after it, which
    the Atmos routes rightly refuse and which would play as a stutter."""
    pts = _packet_times(job, cut, stream)
    fix = first_packet_fix(pts)
    out = job.work / "original.mka"
    bsf = []
    if fix is not None:
        ms = round(fix * 1000)  # the cut is mkvmerge's: timestamps in milliseconds
        bsf = ["-bsf:a", rf"setts=ts=if(eq(N\,0)\,{ms}\,TS)"]
        job.log(f"The cut stamped the track's first packet {pts[0]:.3f} s, "
                f"{1000 * (pts[1] - pts[0]):.0f} ms before the next: moved to {ms / 1000:.3f} s")
    job.procs.run([job.s.ffmpeg, "-nostdin", "-v", "error", "-y", "-copyts", "-i", str(cut),
                   "-map", f"0:{stream}", "-c", "copy", *bsf, "-f", "matroska", str(out)])
    if not out.is_file() or out.stat().st_size == 0:
        raise JobFailed("Could not take the track out of the clip")
    return out


def _local_track(job: Job, original: Path, track: media.AudioTrack, rate: int,
                 length: float) -> tools.LocalTrack:
    try:
        timeline = pcmio.find_gaps(job.s.ffprobe, str(original), rate, popen=job.procs.popen)
    except RuntimeError as exc:
        raise JobFailed(f"Could not map the clip's {track.kind_name} track ({exc})") from None
    if not timeline.frames or timeline.first_pts is None:
        raise JobFailed(f"The clip's {track.kind_name} track decoded to nothing")
    return tools.LocalTrack(original, timeline)


def _encode_one(job: Job, dee, route: str, track: media.AudioTrack, local: tools.LocalTrack,
                item: dict, fmt: str, codec: str, rate: int, length: float) -> ClipTrack:
    """One BEQ track of the clip, as the full encode makes one (encode.run_encode), at the
    whole film's gain where it is known."""
    entry = item["entry"]
    job.work.mkdir(parents=True, exist_ok=True)
    fj = filter_job(entry, job.params)
    film_peak = item.get("peak_dbfs") if fj.fixed_gain_db is None else None
    fj = dataclasses.replace(fj, programme_peak_dbfs=film_peak)
    job.log(f"BEQ track for {entry.get('label')}: {len(fj.filters)} filters, "
            + ("fixed gain" if fj.fixed_gain_db is not None else
               f"gain for the film's filtered peak of {film_peak:+.2f} dBFS"))
    sync_ms, pad = _start_offset(job, route, local.timeline.first_pts, rate)
    if route == "damf":
        encoded = route_atmos.encode_truehd_atmos(job, dee, track, local, fj, length)
    elif route == "joc":
        encoded = route_atmos.encode_ddp_atmos(job, dee, track, local, fj, length)
    else:
        encoded = route_pcm.encode_pcm(job, dee, track, local, fj, fmt, length, pad)
    report = encoded.report
    job.log(f"Filter report: {json.dumps(report)}")
    if report.get("clipped_samples"):
        raise JobFailed(f"{report['clipped_samples']} samples clip after the BEQ filter "
                        f"(gain {report.get('gain_db')} dB): lower the fixed gain or use the "
                        "automatic level")
    lead = int(report["fidelity"]["offset_frames"])
    if lead:
        sync_ms -= 1000 * lead / rate
    name = artifacts.track_name(job, track, codec, entry, report.get("gain_db"), fj.highpass_hz)
    beq = artifacts.BeqTrack(track, codec, name, entry, report, fj.highpass_hz)
    clip_peak = report.get("peak_filtered_dbfs")
    gain_from = ("fixed" if fj.fixed_gain_db is not None
                 else "clip_peak" if clip_peak is not None and clip_peak > film_peak else "film")
    return ClipTrack(entry, name, encoded.out, artifacts.write_tags(job, beq), sync_ms, report,
                     remux.measure_beq(job, encoded.out, rate), gain_from)


def _mux(job: Job, cut: Path | None, original: Path, track: media.AudioTrack, title: str,
         beqs: list[ClipTrack], default_beq: bool) -> list[str]:
    """Video, original, BEQ tracks, in that order, to the temp file; mkvmerge's warnings."""
    job.tmp_mkv.unlink(missing_ok=True)
    lang = track.language or "und"
    cmd = [job.s.mkvmerge, "--gui-mode", "-o", str(job.tmp_mkv), "--no-global-tags",
           "--title", title]
    if cut is not None:
        cmd += ["-A", "--no-chapters", "--no-attachments", str(cut)]
    cmd += ["--language", f"0:{lang}", "--track-name", f"0:{track.name or track.label}",
            "--default-track-flag", f"0:{0 if default_beq else 1}", str(original)]
    for i, b in enumerate(beqs):
        cmd += ["--language", f"0:{lang}", "--track-name", f"0:{b.name}",
                "--default-track-flag", f"0:{1 if default_beq and i == 0 else 0}",
                "--tags", f"0:{b.tags}"]
        if round(b.sync_ms):
            cmd += ["--sync", f"0:{round(b.sync_ms)}"]
        cmd.append(str(b.out))
    warnings: list[str] = []

    def on_line(line: str) -> None:
        pct = tools.gui_progress(line)
        if pct is not None:
            job.progress("Writing the clip", pct)
        elif line.startswith("#GUI#warning"):
            warnings.append(line[len("#GUI#warning"):].strip())

    job.progress("Writing the clip", 0)
    job.procs.run(cmd, on_line=on_line, ok_codes=(0, 1),
                  filt=lambda ln: "" if tools.gui_progress(ln) is not None else ln)
    for w in warnings:
        if remux._LOSS_WARNING_RE.search(w):
            raise JobFailed(f"mkvmerge reports lost data while writing the clip: {w}")
    return warnings


def _verify(job: Job, video: bool, original: Path, beqs: list[ClipTrack]
            ) -> tuple[media.ProbeResult, list[media.AudioTrack]]:
    """Every track of the clip in its place and whole: the clip's own probe and its audio."""
    job.progress("Checking the clip", None)
    try:
        clip = media.probe(str(job.tmp_mkv), job.s.mkvmerge, job.s.ffprobe, popen=job.procs.popen)
    except (OSError, RuntimeError, ValueError) as exc:
        raise JobFailed(f"Could not read the clip back: {exc}") from None
    ident = remux.identify(job, job.tmp_mkv)
    want = ["video"] * video + ["audio"] * (1 + len(beqs))
    got = [t["type"] for t in ident["tracks"]]
    if got != want:
        raise JobFailed(f"The clip has tracks {got}, expected {want}")
    audio = [t for t in ident["tracks"] if t["type"] == "audio"]
    held = tools.count_packets(job, original)[0]
    expected = [held] + [b.frames.kept(b.sync_ms) for b in beqs]
    for t, n in zip(audio, expected):
        if tools.frames_tag(t) != n:
            raise JobFailed(f"The clip's {t.get('codec')} track has {tools.frames_tag(t)} "
                            f"frames, expected {n}: a track lost content")
    return clip, clip.audio


def _deliver(job: Job, probe: media.ProbeResult, track: media.AudioTrack, cut: Path | None,
             original: Path, beqs: list[ClipTrack], began: float, length: float, picked: bool,
             asked: float | None) -> dict:
    # named for the time asked for, which it holds from the keyframe at or before it
    named = asked if asked is not None and began <= asked < began + length else began
    title = clip_title(job.src, named)
    default_beq = bool(job.params.get("default_flag"))

    def build() -> tuple[list[str], media.ProbeResult, list[media.AudioTrack]]:
        warnings = _mux(job, cut, original, track, title, beqs, default_beq)
        clip, audio = _verify(job, cut is not None, original, beqs)
        return warnings, clip, audio

    warnings, clip, audio = build()
    syncs = _sync_all(job, clip, audio, track, beqs)
    if any(s.reliable and abs(s.lag_ms) > SYNC_TOLERANCE_MS for s in syncs):
        for b, s in zip(beqs, syncs):
            if s.reliable and abs(s.lag_ms) > SYNC_TOLERANCE_MS:
                b.sync_ms -= s.lag_ms
                job.log(f"Correcting {b.name} by {-s.lag_ms:+.2f} ms and writing the clip again")
        warnings, clip, audio = build()
        syncs = _sync_all(job, clip, audio, track, beqs)
        off = [s.lag_ms for s in syncs if s.reliable and abs(s.lag_ms) > SYNC_TOLERANCE_MS]
        if off:
            raise JobFailed(f"A BEQ track of the clip is still {off[0]:+.2f} ms out of sync")
    first = {f"a:{i}": _packet_times(job, job.tmp_mkv, f"a:{i}", 3) for i in range(len(audio))}
    job.log("First audio packets of the clip: "
            + "; ".join(f"{k} {', '.join(f'{t:.3f}' for t in v)}" for k, v in first.items()))
    job.procs.check()
    final = free_name(job.s.clip_dir, clip_name(job.src, named))
    os.replace(job.tmp_mkv, final)
    job.log(f"Wrote {final}")
    job.progress("Done", 100)
    return {
        "file": final.name, "path": str(final), "bytes": final.stat().st_size,
        "start_s": round(began, 3), "asked_start_s": asked, "picked": picked,
        "length_s": round(clip.duration_s or length, 3),
        "original": {"name": track.name or track.label, "label": track.label},
        "tracks": [{"name": b.name, "digest": b.entry.get("digest"),
                    "label": b.entry.get("label"), "gain_db": b.report.get("gain_db"),
                    "gain_from": b.gain_from, "sync_ms": round(b.sync_ms, 2),
                    "lag_ms": s.lag_ms, "confidence": s.confidence,
                    "check": b.report.get("fidelity", {}).get("method") or "sample-exact"}
                   for b, s in zip(beqs, syncs)],
        "first_packets": first,
        "warnings": [f"mkvmerge: {w}" for w in warnings]
        + ["the A/V sync check was inconclusive for " + b.name
           for b, s in zip(beqs, syncs) if not s.reliable],
    }


def _sync_all(job: Job, clip: media.ProbeResult, audio: list[media.AudioTrack],
              track: media.AudioTrack, beqs: list[ClipTrack]) -> list:
    """Each BEQ track against the original, as the full encode checks its own."""
    return [remux.sync_check(job, remux.Remuxed(clip, audio[0], beq, []), track)
            for beq in audio[1:1 + len(beqs)]]
