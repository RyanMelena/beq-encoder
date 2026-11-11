"""What the stages of one job share, and what identifies the file it works on."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import media
from ..config import Settings
from .processes import JobFailed, Processes

ProgressCb = Callable[[str, float | None], None]


@dataclass(frozen=True, slots=True)
class FileStamp:
    """A file as it stood. If these differ later, it is not the file that was read."""
    size: int
    mtime_ns: int

    @classmethod
    def of(cls, path: Path) -> FileStamp:
        st = path.stat()
        return cls(st.st_size, st.st_mtime_ns)


TRACK_IDENTITY = ("track_id", "kind", "codec", "channels", "layout")


def source_pin(stamp: FileStamp, track: media.AudioTrack) -> dict:
    """What a job read, carried from prepare through approval to the encode and any
    retry, so a filter approved for one master is never applied to another."""
    return {"size": stamp.size, "mtime_ns": stamp.mtime_ns,
            "track": {k: getattr(track, k) for k in TRACK_IDENTITY}}


def file_changed(pin: dict | None, path: Path) -> bool:
    """Whether `path` is no longer the file `pin` was made from (no pin: unprovable)."""
    if pin is None:
        return True
    try:
        return FileStamp.of(path) != FileStamp(pin["size"], pin["mtime_ns"])
    except OSError:
        return True


def temp_mkv(src: Path, job_id: int) -> Path:
    """The remux of `src` that job `job_id` is building, next to it and hidden."""
    return src.with_name(f".{src.stem}.beq-tmp-{job_id}.mkv")


@dataclass(slots=True)
class Encoded:
    """What an encode route hands to the rest of the job."""
    out: Path       # the encoded BEQ track, in the work dir
    report: dict    # stored as the job's filter_report
    source: Path    # the track's local copy, measured for the "before" chart


class Job:
    """The context every stage of one job is handed. Nothing here is assigned after
    __init__; what a stage produces, it returns."""

    def __init__(self, settings: Settings, job_id: int, path: str, params: dict,
                 progress: ProgressCb, log_path: Path, catalogue=None):
        self.s = settings
        self.id = job_id
        self.src = Path(path)
        self.params = params
        self.progress = progress
        self.catalogue = catalogue
        self.work = settings.work_dir / f"job-{job_id}"
        self.tmp_mkv = temp_mkv(self.src, job_id)
        self._log_path = log_path
        self._log_lock = threading.Lock()
        self.procs = Processes(self.log)

    def log(self, msg: str) -> None:
        with self._log_lock, open(self._log_path, "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg.rstrip()}\n")

    def checked_progress(self, stage: str, pct: float) -> None:
        self.procs.check()
        self.progress(stage, pct)

    def ticker(self, stage: str, total: float) -> Callable[[float], None]:
        """A callback for a long stage: stops when cancelled, reports n/total when `total`."""
        def tick(n: float) -> None:
            self.procs.check()
            if total:
                self.progress(stage, min(100.0, 100 * n / total))

        return tick

    def read_source(self) -> tuple[FileStamp, media.ProbeResult]:
        """The file as it stands and its probe: stamped first, so a change during the
        probe shows up as a mismatch later rather than being taken for the original."""
        self.progress("Checking source", None)
        if not self.src.is_file():
            raise JobFailed(f"Source file not found: {self.src}")
        stamp = FileStamp.of(self.src)
        return stamp, media.probe(str(self.src), self.s.mkvmerge, self.s.ffprobe,
                                  popen=self.procs.popen)

    def require_unchanged(self, stamp: FileStamp) -> None:
        """Refuse to go on if the source is no longer the file this job read."""
        try:
            current = FileStamp.of(self.src)
        except OSError:
            current = None
        if current != stamp:
            raise JobFailed(f"{self.src.name} changed while this job ran, so it was left "
                            "alone: run the job again")
