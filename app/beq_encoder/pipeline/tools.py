"""The source-track and DEE steps more than one encode route uses."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .. import media, pcmio
from ..dee import (DEFAULT_DIALNORM, PERCENT_RE, STEP_RE, Dee, Input, redact,
                   summary_errors)
from .job import Job
from .processes import JobFailed

SR = 48000
# ffprobe reading a whole film: as long as one decode of it may take
PACKET_COUNT_TIMEOUT_S = 4 * 3600
# As much of a TrueHD stream as truehdd info is shown: its presentations are in the first
# major sync, at the very start
DIALNORM_HEAD_BYTES = 4 << 20
_FRAMES_TAG = "tag_number_of_frames"
_GUI_PROGRESS_RE = re.compile(r"#GUI#progress\s+(\d+)%")
_OUT_TIME_RE = re.compile(r"out_time_us=(\d+)")
_PROGRESS_KV_RE = re.compile(r"^\s*\w+=\s*\S*\s*$")  # ffmpeg -progress key=value lines


def gui_progress(line: str) -> float | None:
    """The percentage on a `--gui-mode` line of mkvtoolnix, if it is one."""
    m = _GUI_PROGRESS_RE.search(line)
    return float(m.group(1)) if m else None


def frames_tag(track: dict) -> int | None:
    """mkvmerge's statistics tag for a track of its -J output, if it carries one."""
    v = track.get("properties", {}).get(_FRAMES_TAG)
    return int(v) if v else None


def count_packets(job: Job, path: Path) -> list[int]:
    """Packets per stream of `path`, in stream order (ffprobe reads the whole file to count
    them). One packet is one frame in mkvmerge's statistics (measured for H.264, TrueHD,
    AC-3, PCM and DTS muxed from Matroska); an attachment's "stream" counts as 0."""
    r = job.procs.capture([job.s.ffprobe, "-v", "error", "-count_packets", "-show_entries",
                           "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                          timeout=PACKET_COUNT_TIMEOUT_S)
    if r.returncode != 0:
        raise JobFailed(f"Could not count the packets of {path.name}: "
                        f"{r.stderr.strip()[-300:]}")
    return [int(n) if n.isdigit() else 0 for n in r.stdout.split()]


class SourceTracks:
    """The source as mkvmerge reads it (its -J output), and how many frames each track
    holds: the statistics tag where one agrees with what a copy of the track holds, else
    the packets counted, as tags can be stale or another muxer's. Counting reads the whole
    film, so it happens at most once a job."""

    def __init__(self, job: Job, ident: dict):
        self.ident = ident
        self._job = job
        self._tagged = [frames_tag(t) for t in ident["tracks"]]
        self._counted: list[int] | None = None

    def position(self, track_id: int) -> int:
        return next(i for i, t in enumerate(self.ident["tracks"]) if t["id"] == track_id)

    def frames(self, position: int, seen: int | None) -> int:
        """The frames this track holds, to judge a copy of it holding `seen`."""
        tag = self._tagged[position]
        if tag is not None and tag == seen:
            return tag
        if self._counted is None:
            self._counted = count_packets(self._job, self._job.src)
        return self._counted[position]


@dataclass(frozen=True, slots=True)
class LocalTrack:
    """The source track in the work dir, proven whole, and its decoded timeline."""
    path: Path
    timeline: pcmio.FrameMap


def read_track(job: Job, track: media.AudioTrack, rate: int, duration: float | None,
               source: SourceTracks) -> LocalTrack:
    """The track copied out of the MKV and proven to be all of it, then mapped: every later
    check compares against this copy, so a copy that stopped early would pass them all
    (ffmpeg exits 0 after a read error, with what it had). A timeline with no start cannot
    place the track on the film, so nothing is encoded from one."""
    local = _extract_track(job, track, duration)
    held = count_packets(job, local)[0]
    whole = source.frames(source.position(track.track_id), held)
    if held != whole:
        raise JobFailed(f"The copy of the {track.kind_name} track holds {held} of its {whole} "
                        f"frames: reading {job.src.name} stopped early, so it was left alone")
    timeline = _map_timeline(job, local, track, rate, duration)
    if not timeline.frames or timeline.first_pts is None:
        raise JobFailed(f"Could not map the {track.kind_name} track's timeline (ffprobe listed "
                        "no frames), so where it starts and whether it has gaps is unknown: "
                        "it was left alone")
    return LocalTrack(local, timeline)


def _extract_track(job: Job, track: media.AudioTrack, duration: float | None) -> Path:
    """Copy just this track to the work dir, so every later pass reads a small local
    file rather than the MKV over the network. -copyts keeps the source's own timestamps:
    by default ffmpeg subtracts the file's start time, which another track may set, and
    the track's place on the film's timeline is read from these."""
    out = job.work / "source.mka"
    stage = f"Reading the {track.kind_name} track"
    job.progress(stage, 0)

    def on_line(ln: str) -> None:
        m = _OUT_TIME_RE.match(ln.strip())
        if m and duration:
            job.progress(stage, min(100.0, int(m.group(1)) / 1e6 / duration * 100))

    job.procs.run([job.s.ffmpeg, "-nostdin", "-v", "error", "-y", "-progress", "pipe:1",
                   "-nostats", "-copyts", "-i", str(job.src),
                   "-map", f"0:a:{track.audio_index}",
                   "-c", "copy", "-f", "matroska", str(out)],
                  on_line=on_line, filt=lambda ln: "" if _PROGRESS_KV_RE.match(ln) else ln)
    if not out.is_file() or out.stat().st_size == 0:
        raise JobFailed(f"Could not read the {track.kind_name} track from the file")
    return out


def extract_raw(job: Job, local: Path, track: media.AudioTrack) -> Path:
    """The track's TrueHD elementary stream, byte for byte (mkvextract)."""
    raw = job.work / "source.thd"
    stage = f"Reading the {track.kind_name} bitstream"
    job.progress(stage, 0)

    def on_line(ln: str) -> None:
        pct = gui_progress(ln)
        if pct is not None:
            job.progress(stage, pct)

    job.procs.run([job.s.mkvextract, "--gui-mode", str(local), "tracks", f"0:{raw}"],
                  on_line=on_line, ok_codes=(0, 1),
                  filt=lambda ln: "" if gui_progress(ln) is not None else ln)
    if not raw.is_file() or raw.stat().st_size == 0:
        raise JobFailed(f"Could not read the {track.kind_name} bitstream")
    return raw


def _map_timeline(job: Job, local: Path, track: media.AudioTrack, rate: int,
                  duration: float | None) -> pcmio.FrameMap:
    """The decoded frames against their timestamps: how long, and any gap in the timeline."""
    stage = f"Mapping the {track.kind_name} track's timeline"
    job.progress(stage, 0 if duration else None)
    try:
        return pcmio.find_gaps(job.s.ffprobe, str(local), rate,
                               on_progress=job.ticker(stage, duration or 0),
                               popen=job.procs.popen)
    except RuntimeError as exc:
        raise JobFailed(f"Could not map the {track.kind_name} track's timeline ({exc}), so "
                        "nothing proves a decode of it is complete: it was left alone") from None


def decode_stream(job: Job, path: Path, channels: int, what: str) -> pcmio.PCMStream:
    """Bit-exact f64 decode of the first audio stream of `path` (no resampling)."""
    return pcmio.PCMStream(pcmio.decode_cmd(job.s.ffmpeg, str(path)), channels,
                           popen=job.procs.popen, what=what)


def _dialnorm_in_range(level: int) -> int:
    return max(-31, min(-1, level))


def _truehdd_info(job: Job, path: Path) -> tuple[int, list[str]]:
    """(truehdd info's exit code, the dialogue level of each presentation it lists)."""
    r = job.procs.capture([job.s.truehdd_path, "--loglevel", "warn", "info", str(path)],
                          timeout=3600)
    return r.returncode, re.findall(r"Dialogue Level\s+(-?\d+)\s*dBFS", r.stdout)


def truehd_dialnorm(job: Job, raw: Path) -> int:
    """The dialogue level of the stream's top presentation (truehdd info).

    truehdd lists the presentations from the stream's first major sync and then reads
    the rest only to total it up (a minute and a half for a film), so it is shown the
    start of the stream alone: measured, truehdd 0.6.2 prints the same presentations for
    the first 4 MB of a stream as for all of it, cut mid-unit or not. Should the start
    not give a level, the whole stream is read as before."""
    head = raw.with_name(f"{raw.stem}-head{raw.suffix}")
    try:
        with open(raw, "rb") as src, open(head, "wb") as dst:
            dst.write(src.read(DIALNORM_HEAD_BYTES))
        code, levels = _truehdd_info(job, head)
    finally:
        head.unlink(missing_ok=True)
    if code != 0 or not levels:
        code, levels = _truehdd_info(job, raw)
    if code != 0 or not levels:
        raise JobFailed(f"Could not read the dialogue level of the TrueHD track (truehdd "
                        f"info: exit {code}), so it was left alone: writing a "
                        "guessed level would change how loud it plays on a decoder that "
                        "applies dialnorm")
    return _dialnorm_in_range(int(levels[-1]))


def source_dialnorm(job: Job, track: media.AudioTrack, local: Path) -> int:
    """The source's dialogue level, to carry into the TrueHD track: from the
    bitstream for Dolby sources (ffmpeg's decode does not apply it); DTS, PCM and
    FLAC carry none (-31: no attenuation). A Dolby source whose level cannot be read
    stops the job: nothing else would show the track playing at the wrong level."""
    if track.kind == "truehd":
        return truehd_dialnorm(job, extract_raw(job, local, track))
    if track.kind not in ("ac3", "eac3", "eac3_joc"):
        return DEFAULT_DIALNORM
    try:
        level = media.dialnorm(str(local))
    except Exception as exc:  # noqa: BLE001 - MediaInfo reports failures in many ways
        job.log(f"Reading the dialogue level failed: {exc}")
        level = None
    if level is None:
        raise JobFailed(f"Could not read the dialogue level of the {track.kind_name} track, "
                        "so it was left alone: writing a guessed level would change how "
                        "loud it plays on a decoder that applies dialnorm")
    return _dialnorm_in_range(level)


def run_dee(job: Job, dee: Dee, inp: Input, source: Path, out: Path, label: str,
            dialnorm: int) -> None:
    temp = job.work / "dee-temp"
    temp.mkdir(exist_ok=True)
    stage = [f"Encoding {label} (DEE)"]

    def dee_line(line: str) -> None:
        m = STEP_RE.search(line)
        if m:
            stage[0] = f"Encoding {label} (DEE step {m[1]}/{m[2]}: {m[3].lower()})"
            job.progress(stage[0], 0)
        elif m := PERCENT_RE.match(line.strip()):
            job.progress(stage[0], float(m[1]))

    job.progress(stage[0], None)
    lines: list[str] = []
    try:
        job.procs.run(dee.command(inp, source, out, temp, dialnorm=dialnorm),
                      on_line=dee_line, keep=lines,
                      filt=lambda ln: "" if PERCENT_RE.match(ln.strip()) else redact(ln))
    except JobFailed as exc:
        errs = summary_errors(lines)
        raise JobFailed("; ".join(errs) if errs else str(exc)) from None
    finally:
        if not job.s.keep_work:  # DEE's temp files are as large as its input
            shutil.rmtree(temp, ignore_errors=True)
    errs = summary_errors(lines)
    if errs:
        raise JobFailed("; ".join(errs))
