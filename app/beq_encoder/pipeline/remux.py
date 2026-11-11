"""The temp MKV: built next to the source, checked in full, then swapped over it."""

from __future__ import annotations

import math
import os
import re
import stat
import subprocess
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import NamedTuple

from .. import layouts, media, synccheck
from . import tools
from .job import FileStamp, Job
from .processes import JobFailed

_GUI_WARNING = "#GUI#warning"
# what mkvmerge says when it left out or re-timed part of a stream; such a remux is refused
_LOSS_WARNING_RE = re.compile(r"skipp|dropp|invalid data|synchroni[sz]ation may have been lost",
                              re.I)


@dataclass(frozen=True, slots=True)
class BeqFrames:
    """The encoded BEQ track's frames, before and after a --sync."""
    packets: int
    frame_ms: Fraction

    def kept(self, sync_ms: float) -> int:
        """A negative --sync moves frames before 0, and mkvmerge leaves those out: every
        frame whose timestamp is below the offset (exact for 135 offsets on TrueHD and FLAC,
        measured). A positive one only moves them."""
        offset = round(sync_ms)
        return self.packets - (math.ceil(Fraction(-offset) / self.frame_ms) if offset < 0 else 0)


@dataclass(frozen=True, slots=True)
class MuxPlan:
    """What the remux must hold, from mkvmerge's view of the source (`mkvmerge -J`)."""
    tracks: list[tuple[str, str, str]]  # (type, codec, language) of every track, in order
    beq_index: int                      # the BEQ track's place in `tracks`
    chapters: int
    attachments: Counter[str]           # file names
    positions: list[int | None]         # each track's place in the source (None: the BEQ track)
    beq: BeqFrames


@dataclass(frozen=True, slots=True)
class MuxSpec:
    """The inputs of a remux: the file, the track to add, how it goes in, what to expect."""
    probe: media.ProbeResult
    track: media.AudioTrack
    out: Path                 # the encoded BEQ track
    name: str
    replace_ids: list[int]    # existing BEQ tracks this run removes
    make_default: bool
    want: MuxPlan
    source: tools.SourceTracks


class Remuxed(NamedTuple):
    probe: media.ProbeResult
    orig: media.AudioTrack  # the source track, as it is in the remux
    beq: media.AudioTrack
    warnings: list[str]     # what mkvmerge warned about while building it


def _track_key(t: dict) -> tuple[str, str, str]:
    return t["type"], t.get("codec", ""), t.get("properties", {}).get("language", "und")


def _chapters(ident: dict) -> int:
    return sum(int(c.get("num_entries", 0)) for c in ident.get("chapters", []))


def plan_remux(source: dict, source_id: int, beq_codec: str, replace_ids: list[int],
               dropped_attachments: set[int], beq: BeqFrames) -> MuxPlan:
    """What mkvmerge must produce from `source` (its -J output): every track but those
    replaced, the BEQ track straight after its source track, chapters and attachments."""
    tracks: list[tuple[str, str, str]] = []
    positions: list[int | None] = []
    beq_index = -1
    for pos, t in enumerate(source["tracks"]):
        if t["id"] in replace_ids:
            continue
        key = _track_key(t)
        tracks.append(key)
        positions.append(pos)
        if t["id"] == source_id:
            beq_index = len(tracks)
            tracks.append(("audio", beq_codec, key[2]))
            positions.append(None)
    return MuxPlan(tracks, beq_index, _chapters(source),
                   Counter(a["file_name"] for a in source.get("attachments", [])
                           if a["id"] not in dropped_attachments), positions, beq)


# what reading a file back can raise: the tools fail in several ways on a damaged one
_READ_BACK_ERRORS = (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired)


def identify(job: Job, path: Path) -> dict:
    try:
        return media.mkvmerge_identify(job.s.mkvmerge, str(path), popen=job.procs.popen)
    except _READ_BACK_ERRORS as exc:
        raise JobFailed(f"Could not read {path.name}: {exc}") from None


def measure_beq(job: Job, encoded: Path, rate: int) -> BeqFrames:
    """The encoded BEQ track's packets and the length of its first frame."""
    r = job.procs.capture([job.s.ffprobe, "-v", "error", "-select_streams", "a:0",
                           "-show_entries", "frame=nb_samples", "-read_intervals", "%+#1",
                           "-of", "csv=p=0", str(encoded)], timeout=300)
    samples = r.stdout.strip().split(",")[0]
    if r.returncode != 0 or not samples.isdigit():
        raise JobFailed(f"Could not read the frames of {encoded.name}: "
                        f"{r.stderr.strip()[-300:]}")
    return BeqFrames(tools.count_packets(job, encoded)[0], Fraction(int(samples) * 1000, rate))


def _mkvmerge_cmd(job: Job, spec: MuxSpec, sync_ms: float, tags: Path) -> list[str]:
    cmd = [job.s.mkvmerge, "--gui-mode", "-o", str(job.tmp_mkv)]
    if spec.replace_ids:
        cmd += ["--audio-tracks", "!" + ",".join(str(i) for i in spec.replace_ids)]
        old = [str(a["id"]) for a in spec.probe.beq_attachments if a.get("id") is not None]
        if old:  # the replaced BEQ track's charts and report go with it
            cmd += ["--attachments", "!" + ",".join(old)]
    if spec.make_default:
        for a in spec.probe.audio:
            if a.default and a.track_id not in spec.replace_ids:
                cmd += ["--default-track-flag", f"{a.track_id}:0"]
    elif any(a.default and a.track_id in spec.replace_ids for a in spec.probe.audio):
        # the track removed here held the flag an earlier run took from the original
        cmd += ["--default-track-flag", f"{spec.track.track_id}:1"]
    cmd.append(str(job.src))
    cmd += [
        "--language", f"0:{spec.track.language or 'und'}",
        "--track-name", f"0:{spec.name}",
        "--default-track-flag", f"0:{1 if spec.make_default else 0}",
        "--tags", f"0:{tags}",
    ]
    if round(sync_ms):  # mkvmerge takes whole milliseconds
        cmd += ["--sync", f"0:{round(sync_ms)}"]
    cmd.append(str(spec.out))
    order: list[str] = []
    for t in spec.probe.tracks:
        if t["id"] in spec.replace_ids:
            continue
        order.append(f"0:{t['id']}")
        if t["id"] == spec.track.track_id:
            order.append("1:0")
    cmd += ["--track-order", ",".join(order)]
    return cmd


def remux(job: Job, spec: MuxSpec, sync_ms: float, tags: Path) -> Remuxed:
    """Build the temp MKV (from nothing, each time) and verify it. mkvmerge's exit 1 only
    means it warned: every warning is read here, and one that says it skipped or re-timed
    part of a stream refuses the remux, as the frame counts would not show that."""
    job.tmp_mkv.unlink(missing_ok=True)
    stage = "Remuxing (mkvmerge)"
    job.progress(stage, 0)
    warnings: list[str] = []

    def on_line(line: str) -> None:
        pct = tools.gui_progress(line)
        if pct is not None:
            job.progress(stage, pct)
        elif line.startswith(_GUI_WARNING):
            warnings.append(line[len(_GUI_WARNING):].strip())

    job.procs.run(_mkvmerge_cmd(job, spec, sync_ms, tags), on_line=on_line, ok_codes=(0, 1),
                  filt=lambda ln: "" if tools.gui_progress(ln) is not None else ln)
    for w in warnings:
        if _LOSS_WARNING_RE.search(w):
            raise JobFailed(f"mkvmerge reports lost data while remuxing: {w}")
    return verify_temp_mkv(job, spec, sync_ms)._replace(warnings=warnings)


def _require_whole_tracks(spec: MuxSpec, ident: dict, sync_ms: float) -> None:
    """Every track of the temp MKV holds all its frames. The container's duration (its
    longest track) cannot show a shorter track, so this is compared before any swap."""
    want = spec.want
    for key, pos, t in zip(want.tracks, want.positions, ident["tracks"]):
        got = tools.frames_tag(t)
        expected = want.beq.kept(sync_ms) if pos is None else spec.source.frames(pos, got)
        if got != expected:
            raise JobFailed(f"Remuxed {key[0]} track ({key[1]}) has {got} frames, expected "
                            f"{expected}: a track lost content")


def verify_temp_mkv(job: Job, spec: MuxSpec, sync_ms: float,
                    attached: Collection[str] = ()) -> Remuxed:
    """The temp MKV holds what `spec.want` says (plus the attachments `attached`): every
    track in its place and whole, chapters, attachments. Nothing less may replace the
    source."""
    job.progress("Verifying", None)
    try:
        new = media.probe(str(job.tmp_mkv), job.s.mkvmerge, job.s.ffprobe,
                          popen=job.procs.popen)
    except _READ_BACK_ERRORS as exc:
        raise JobFailed(f"Could not read the remuxed file: {exc}") from None
    ident = identify(job, job.tmp_mkv)
    want = spec.want
    got = [_track_key(t) for t in ident["tracks"]]
    if got != want.tracks:
        raise JobFailed(f"Remuxed file has tracks {got}, expected {want.tracks}")
    _require_whole_tracks(spec, ident, sync_ms)
    if _chapters(ident) != want.chapters:
        raise JobFailed(f"Remuxed file has {_chapters(ident)} chapters, "
                        f"expected {want.chapters}")
    names = Counter(a.get("file_name") for a in ident.get("attachments", []))
    if names != want.attachments + Counter(attached):
        raise JobFailed(f"Remuxed file has attachments {sorted(names)}, expected "
                        f"{sorted(want.attachments + Counter(attached))}")
    beq = new.track(ident["tracks"][want.beq_index]["id"])
    orig = new.track(ident["tracks"][want.beq_index - 1]["id"])
    if beq is None or orig is None or beq.name != spec.name:
        raise JobFailed("BEQ track missing from remuxed file")
    duration = spec.probe.duration_s
    if duration and new.duration_s and abs(new.duration_s - duration) > 2:
        raise JobFailed(f"Remuxed duration {new.duration_s:.1f}s differs from source "
                        f"{duration:.1f}s")
    return Remuxed(new, orig, beq, [])


def sync_check(job: Job, muxed: Remuxed, track: media.AudioTrack) -> synccheck.CheckResult:
    job.progress("Checking A/V sync and channel order", None)
    orig, beq = muxed.orig, muxed.beq
    # what ffmpeg decodes: TrueHD Atmos is its 7.1 presentation
    co = int(orig.channels or 2)
    cb = int(beq.channels or 2)
    if co == cb:
        ch, downmix = co, False
        names = layouts.names_or_generic(co, orig.layout or track.layout)
    else:
        ch, downmix = min(co, cb), True
        names = layouts.names_or_generic(ch, None)
    try:
        r = synccheck.compare(job.s.ffmpeg, str(job.tmp_mkv), orig.audio_index, beq.audio_index,
                              muxed.probe.duration_s, ch, names=names, downmix=downmix,
                              popen=job.procs.popen)
    except Exception as exc:
        job.log(f"Sync check failed to run: {exc}")
        raise JobFailed("The A/V sync and channel-order check could not run (see log)") \
            from None
    job.log(f"Sync check at {r.at_s}s: BEQ track lag {r.lag_ms:+.2f} ms "
            f"(confidence {r.confidence}); per-channel correlation ({r.channels} ch"
            f"{', downmixed' if downmix else ''}): "
            + ", ".join(f"{k} {v}" for k, v in r.diag.items()))
    if not r.reliable:
        job.log("WARNING: sync check inconclusive (quiet or uncorrelated passage)")
    elif r.mismatches:
        raise JobFailed("Channel order differs from the original: " + "; ".join(r.mismatches))
    return r


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _carry_over(job: Job, what: str, change: Callable[..., None], *args: object) -> None:
    """Give the remux a property of the source. Filesystems may refuse (an ACL dataset, an
    SMB mount); the remux then keeps what its directory gives it, and the swap goes ahead."""
    try:
        change(*args)
    except OSError as exc:
        job.log(f"WARNING: could not carry the source's {what} over to the remux: {exc}")


def swap_into_place(job: Job, stamp: FileStamp) -> FileStamp:
    """Put the verified remux over the source: durable before the rename, so a crash
    leaves one whole file, and only if the source is still the file this job read."""
    stt = job.src.stat()
    _carry_over(job, "mode", os.chmod, job.tmp_mkv, stat.S_IMODE(stt.st_mode))
    _carry_over(job, "owner", os.chown, job.tmp_mkv, stt.st_uid, stt.st_gid)
    _fsync(job.tmp_mkv)
    job.require_unchanged(stamp)
    os.replace(job.tmp_mkv, job.src)
    try:
        _fsync(job.src.parent)
    except OSError as exc:  # the rename is done: only its durability is in doubt
        job.log(f"WARNING: could not sync {job.src.parent}: {exc}")
    job.log(f"Replaced {job.src}")
    return FileStamp.of(job.src)
