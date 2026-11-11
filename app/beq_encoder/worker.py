"""Background job queue and library scanner."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
import os
import shutil
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path

from . import media
from .config import Settings
from .db import DB
from .pipeline import Cancelled, JobFailed, JobRunner, temp_mkv

log = logging.getLogger(__name__)

FINAL_WRITE_RETRY_S = 5.0  # a job's last row write that failed is tried again this often

OnScanned = Callable[[int], object]


def _hidden(name: str) -> bool:
    return name.startswith(".")


def in_library(path: str | Path, roots: Iterable[Path]) -> bool:
    """The one rule for what the scan and the watch take into the library: an .mkv with
    nothing hidden below a media root, so a recycle bin (`.recycle/`, `.Trash-1000/`) and
    a job's temp MKV stay out. A hidden folder in the root's own path does not count."""
    p = Path(path)
    return p.suffix.lower() == ".mkv" and any(
        p.is_relative_to(root) and not any(map(_hidden, p.relative_to(root).parts))
        for root in roots)


def _run_to_outcome(runner: JobRunner) -> tuple[dict, str | None]:
    """Run the job: what to write on its row afterwards, and the last line for its log."""
    try:
        result = runner.run()
        if result.get("phase") == "prepare":
            awaiting = result["outcome"] == "awaiting"
            return dict(
                status=result["outcome"], percent=None,
                stage={"awaiting": "Awaiting approval", "no_beq": "No BEQ"}.get(
                    result["outcome"], "Not supported"),
                prepare_json=json.dumps(result), result_json=None,
                finished_at=None if awaiting else time.time(), message=result["message"],
            ), f"Prepared: {result['outcome']}: {result['message']}"
        return dict(
            status="done", stage="Done", percent=100,
            result_json=json.dumps(result), finished_at=time.time(),
            message=f"Added track “{result['track_name']}” "
            f"(gain {result['filter_report']['gain_db']} dB)"
            + "".join(f"; {w}" for w in result["warnings"]),
        ), None
    except Cancelled:
        return dict(status="cancelled", stage="Cancelled", finished_at=time.time()), "Cancelled"
    except JobFailed as exc:
        return dict(status="failed", stage="Failed", message=str(exc),
                    finished_at=time.time()), f"FAILED: {exc}"
    except Exception as exc:
        return dict(status="failed", stage="Failed", message=f"{type(exc).__name__}: {exc}",
                    finished_at=time.time()), "FAILED:\n" + traceback.format_exc()


class JobQueue:
    def __init__(self, settings: Settings, db: DB, catalogue=None):
        self.s = settings
        self.db = db
        self.catalogue = catalogue
        self.on_scanned: OnScanned | None = None  # given the media id after a file is rescanned
        self.running: dict[int, JobRunner] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="job-queue", daemon=True)

    def start(self) -> None:
        self._recover_interrupted()
        self._thread.start()

    def _recover_interrupted(self) -> None:
        """Jobs that were running when the process stopped can't be resumed: remove the
        work dir and temp MKV only they could have made, then record them as failed. A
        crash part-way through recovery is safe: the rows are still "running" next time."""
        for row in self.db.q("SELECT id, path FROM jobs WHERE status='running'"):
            job_id = int(row["id"])
            if not self.s.keep_work:
                shutil.rmtree(self.s.work_dir / f"job-{job_id}", ignore_errors=True)
            try:
                temp_mkv(Path(row["path"]), job_id).unlink(missing_ok=True)
            except OSError:  # the media root is unavailable: never keep the job from failing
                log.exception("Could not remove job %s's temp MKV", job_id)
            self.db.update_job(job_id, status="failed", message="Interrupted by restart",
                               finished_at=time.time())

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            for r in self.running.values():
                r.cancel()

    def kick(self) -> None:
        self._wake.set()

    def cancel(self, job_id: int) -> bool:
        with self._lock:
            r = self.running.get(job_id)
        if r:
            r.cancel()
            return True
        return self.db.transition(job_id, "queued", status="cancelled", stage="Cancelled",
                                  finished_at=time.time())

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                free = self.s.max_concurrent_jobs - len(self.running)
            if free > 0:
                try:
                    rows = self.db.q(
                        "SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT ?", (free,)
                    )
                    for row in rows:
                        self._start(row)
                except Exception:  # the queue must outlive any one bad job row
                    log.exception("Job queue: starting a job failed")
            self._wake.wait(3)
            self._wake.clear()

    def _start(self, row) -> None:
        job_id = int(row["id"])
        log_path = self.s.log_dir / f"job-{job_id}.log"
        last = [0.0, None]

        def progress(stage: str, pct: float | None) -> None:
            # throttle DB writes; always write stage changes
            now = time.monotonic()
            if stage != last[1] or now - last[0] >= 1.0:
                last[0], last[1] = now, stage
                self.db.update_job(job_id, stage=stage, percent=pct)

        runner = JobRunner(
            self.s, job_id, row["path"], json.loads(row["params_json"]), progress, log_path,
            catalogue=self.catalogue,
        )
        with self._lock:  # before the claim, so a cancel from here on reaches the runner
            self.running[job_id] = runner
        started = False
        try:
            if self.db.transition(job_id, "queued", status="running", started_at=time.time(),
                                  log_path=str(log_path), stage="Starting", percent=None):
                self._launch(job_id, runner, row["media_id"])
                started = True
        finally:
            if not started:  # no thread exists to free this slot; the row is tried again
                with self._lock:
                    self.running.pop(job_id, None)

    def _launch(self, job_id: int, runner: JobRunner, media_id: int | None) -> None:
        try:
            threading.Thread(target=self._execute, args=(job_id, runner, media_id),
                             name=f"job-{job_id}", daemon=True).start()
        except RuntimeError:  # the system is out of threads: back in the queue
            self.db.transition(job_id, "running", status="queued", stage="Queued", started_at=None)
            raise

    def _execute(self, job_id: int, runner: JobRunner, media_id: int | None) -> None:
        fields, last_line = _run_to_outcome(runner)
        if last_line:
            try:
                runner.log(last_line)
            except Exception:  # e.g. a full /config: the row must still get its status
                log.exception("Could not write job %s's log", job_id)
        self._write_final_status(job_id, fields)
        with self._lock:
            self.running.pop(job_id, None)
        if media_id:
            try:  # incremental: only a swapped file has changed
                scan_file(self.s, self.db, Path(runner.src), force=False,
                          on_scanned=self.on_scanned)
            except Exception:
                log.exception("Rescan after job %s failed", job_id)
        self._wake.set()

    def _write_final_status(self, job_id: int, fields: dict) -> None:
        """Retried until it lands: a row left "running" with no runner locks its file. If
        the service stops first, the row is still "running" and start-up recovery ends it."""
        while True:
            try:
                self.db.update_job(job_id, **fields)
                return
            except Exception:
                log.exception("Could not record job %s's final status", job_id)
            if self._stop.wait(FINAL_WRITE_RETRY_S):
                return


def scan_file(s: Settings, db: DB, path: Path, force: bool = True,
              on_scanned: OnScanned | None = None) -> int | None:
    """Probe one file into the library (`force=False`: only if it changed); `on_scanned`
    is called with its media id. None if nothing needed scanning."""
    st = path.stat()
    if not force:
        row = db.one("SELECT size, mtime, probe_json FROM media WHERE path=?", (str(path),))
        if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime and row["probe_json"]:
            return None
    names = media.parse_name(path)
    fields = dict(
        path=str(path), size=st.st_size, mtime=st.st_mtime,
        title=names.title, year=names.year, edition=names.edition, tmdb=names.tmdb,
        scanned_at=time.time(),
    )
    try:
        pr = media.probe(str(path), s.mkvmerge, s.ffprobe)
        best = pr.best
        fields.update(
            has_atmos=int(bool(pr.atmos_tracks)), has_beq=int(bool(pr.beq_tracks)),
            has_audio=int(best is not None),
            best_json=json.dumps({"track_id": best.track_id, "label": best.label,
                                  "kind": best.kind, "objects": best.objects}) if best else None,
            beq_names=json.dumps([a.name for a in pr.beq_tracks]),
            probe_json=pr.to_json(), error=None,
        )
    except Exception as exc:
        fields.update(has_atmos=0, has_beq=0, has_audio=0, best_json=None, beq_names=None,
                      probe_json=None, error=str(exc)[:500])
    media_id = db.upsert_media(**fields)
    if on_scanned:
        try:
            on_scanned(media_id)
        except Exception:
            log.exception("Post-scan callback failed for %s", path)
    return media_id


class Scanner:
    def __init__(self, settings: Settings, db: DB):
        self.s = settings
        self.db = db
        self.state = {"running": False, "done": 0, "found": 0, "current": "", "finished_at": None,
                      "error": None}
        self.on_scanned: OnScanned | None = None  # given the media id after each file
        self._thread: threading.Thread | None = None  # set exactly while a scan is under way
        self._queue: list[tuple[Path, bool]] = []
        self._qlock = threading.Lock()

    def start(self, force: bool = False, root: Path | None = None,
              files: list[Path] | None = None) -> bool:
        """Start a scan in the background. Single files asked for while a scan runs are
        queued and scanned right after it (so adding a file never fails as "busy")."""
        with self._qlock:
            if self._thread is not None:
                if files:
                    self._queue.extend((Path(f), force) for f in files)
                    return True
                return False
            self._launch(self._scan, force, root, files)
            return True

    def _launch(self, target: Callable[..., None], *args) -> None:
        """Start a scan thread; `_qlock` is held."""
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._thread = thread

    def _finish(self) -> None:
        """A scan thread's last step. Going idle and checking the queue are one critical
        section with `start`, so nothing can be queued behind a thread that has stopped
        looking."""
        with self._qlock:
            self.state.update(running=False, current="", finished_at=time.time())
            self._thread = None
            if self._queue:
                self._launch(self._scan_queue)

    def _scan_queue(self) -> None:
        self.state.update(running=True, done=0, found=0, current="", error=None)
        try:
            self._drain()
        finally:
            self._finish()

    def _scan_one(self, f: Path, force: bool) -> None:
        self.state["current"] = str(f)
        try:
            scan_file(self.s, self.db, f, force=force, on_scanned=self.on_scanned)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Scan failed for %s", f)

    def _drain(self) -> None:
        while True:
            with self._qlock:
                if not self._queue:
                    return
                items, self._queue = self._queue, []
            self.state["found"] += len(items)
            for f, force in items:
                self._scan_one(f, force)
                self.state["done"] += 1

    def _scan(self, force: bool, only: Path | None = None,
              given: list[Path] | None = None) -> None:
        st = self.state
        st.update(running=True, done=0, found=0, current="", error=None)
        seen: set[str] = set()
        try:
            files: list[Path] = list(given or [])
            roots = [] if given else [only] if only else self.s.media_roots
            yielded: set[Path] = set()  # roots this walk saw files in
            for root in roots:
                if not root.is_dir():
                    st["error"] = f"Media root {root} not found"
                    continue
                before = len(files)
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in dirnames if not _hidden(d)]
                    found = (Path(dirpath) / fn for fn in filenames)
                    files += [f for f in found if in_library(f, self.s.media_roots)]
                if len(files) > before:
                    yielded.add(root)
            st["found"] = len(files)
            seen.update(str(f) for f in files)

            count = threading.Lock()

            def one(f: Path) -> None:
                self._scan_one(f, force)
                with count:
                    st["done"] += 1

            # probing is latency-bound (a network share answers slowly, not little), so
            # a few files at once; files added meanwhile are taken between batches
            workers = max(1, self.s.scan_workers)
            with ThreadPoolExecutor(workers) as ex:
                for i in range(0, len(files), workers * 4):
                    list(ex.map(one, files[i:i + workers * 4]))
                    if self._queue:
                        self._drain()
            # forget files that disappeared from a root this scan saw files in, or that
            # no longer belong (moved to a recycle bin); a root that is missing or empty
            # may just be unmounted, so its rows are kept
            withheld: Counter[Path] = Counter()
            for row in ([] if given else self.db.q("SELECT id, path FROM media")):
                p = row["path"]
                root = next((r for r in roots if p.startswith(f"{r}/")), None)
                if root is None or p in seen:
                    continue
                if root not in yielded:
                    withheld[root] += 1
                elif not in_library(p, self.s.media_roots) or not Path(p).exists():
                    self.db.x("DELETE FROM media WHERE id=?", (row["id"],))
            if withheld:
                st["error"] = "; ".join(
                    f"Media root {r} is missing or empty: kept its {n} file(s) in the library"
                    for r, n in withheld.items())
            self._drain()
        except Exception as exc:
            st["error"] = str(exc)
            log.exception("Library scan failed")
        finally:
            self._finish()
