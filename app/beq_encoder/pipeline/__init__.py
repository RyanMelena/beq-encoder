"""One BEQ job, in two phases.

prepare:  probe -> best track (or the one picked) -> catalogue match for that track's
          type -> if none: 1 kHz analysis + beqforge designer -> candidates for review
          (or "no BEQ": a catalogue "no filters" record, or the designer declining)

encode:   the chosen filter on the chosen track, by route (support.Plan):
            damf (TrueHD Atmos):  truehdd -> DAMF -> BEQ into a second DAMF (the first is
                                  kept for the check) -> DEE TrueHD Atmos -> Atmos check
            joc (DD+ Atmos):      Cavern -> DAMF, then as damf
            pcm (anything else):  ffmpeg PCM in the source layout -> BEQ -> DEE TrueHD
                                  (test-only: FLAC 24-bit) -> sample-by-sample check
          -> charts -> mkvmerge to a temp MKV -> track, frame, chapter and attachment check
          -> A/V sync + channel-order check (re-mux if off) -> embed charts -> atomic swap

processes owns the child processes of a job. Each phase is a function of a `Job` (job.py),
which holds what its stages share; what a stage produces, it returns.
"""

from __future__ import annotations

import shutil

from .encode import run_encode
from .job import FileStamp, Job, ProgressCb, file_changed, source_pin, temp_mkv
from .prepare import run_prepare
from .processes import Cancelled, JobFailed

__all__ = ["Cancelled", "FileStamp", "JobFailed", "JobRunner", "file_changed", "source_pin",
           "temp_mkv"]


class JobRunner:
    """One job from its first step to its clean-up."""

    def __init__(self, settings, job_id: int, path: str, params: dict, progress: ProgressCb,
                 log_path, catalogue=None):
        self.job = Job(settings, job_id, path, params, progress, log_path, catalogue)
        self.src = self.job.src
        self.log = self.job.log

    def cancel(self) -> None:
        self.job.procs.cancel()

    def run(self) -> dict:
        job = self.job
        try:
            job.procs.check()
            run_phase = run_prepare if job.params.get("phase") == "prepare" else run_encode
            return run_phase(job)
        except Cancelled:
            raise
        except Exception as exc:
            # killing a tool on cancel makes whatever was reading it fail; that is the cancel
            if job.procs.cancelled:
                raise Cancelled() from exc
            raise
        finally:
            job.procs.reap()
            try:
                job.tmp_mkv.unlink(missing_ok=True)
            except OSError as exc:  # the media root has gone: the work dir must still go
                job.log(f"WARNING: could not remove {job.tmp_mkv}: {exc}")
            if not job.s.keep_work and job.work.exists():
                shutil.rmtree(job.work, ignore_errors=True)
