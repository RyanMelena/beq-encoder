"""Clips: their rows, and the queue that makes them (pipeline/clip.py does the work).

A clip has a lane of its own, one at a time: a preview of a few seconds never waits
behind a full encode that takes an hour, and it never changes the film, so it needs
none of the one-job-per-file lock that guards the swap.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import traceback
from pathlib import Path

from .config import Settings
from .db import DB
from .pipeline import Cancelled, JobFailed
from .pipeline.clip import run_clip
from .pipeline.job import Job

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
FINAL_WRITE_RETRY_S = 5.0


def problem(s: Settings) -> str:
    """Why clips cannot be made with these settings, or "" when they can."""
    folder = s.clip_dir
    if any(folder == r or folder.is_relative_to(r) for r in s.media_roots):
        return (f"CLIP_DIR ({folder}) is inside a media root, where the library would take "
                "each clip for a film: set it to a folder outside MEDIA_ROOTS")
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"CLIP_DIR ({folder}) cannot be created: {exc.strerror or exc}"
    if not os.access(folder, os.W_OK | os.X_OK):
        return f"CLIP_DIR ({folder}) is not writable by the encoder"
    return ""


def temp_path(s: Settings, clip_id: int) -> Path:
    """The clip being written, hidden in CLIP_DIR until it is whole."""
    return s.clip_dir / f".beq-clip-{clip_id}.mkv"


def create(db: DB, media_id: int | None, job_id: int | None, path: str, params: dict) -> int:
    return int(db.insert(
        "INSERT INTO clips (media_id, job_id, path, status, stage, params_json, created_at) "
        "VALUES (?, ?, ?, 'queued', 'Queued', ?, ?)",
        (media_id, job_id, path, json.dumps(params), time.time())))


def row_dict(r) -> dict:
    d = dict(r)
    d["params"] = json.loads(d.pop("params_json") or "{}")
    d["result"] = json.loads(d.pop("result_json") or "null")
    return d


def for_film(db: DB, media_id: int, limit: int = 10) -> list[dict]:
    return [row_dict(r) for r in db.q(
        "SELECT * FROM clips WHERE media_id=? ORDER BY id DESC LIMIT ?", (media_id, limit))]


def get(db: DB, clip_id: int) -> dict | None:
    r = db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
    return row_dict(r) if r else None


def _transition(db: DB, clip_id: int, expected: str, **f) -> bool:
    sets = ",".join(f"{k}=?" for k in f)
    return bool(db.x(f"UPDATE clips SET {sets} WHERE id=? AND status=?",
                     [*f.values(), clip_id, expected]))


def _update(db: DB, clip_id: int, **f) -> None:
    db.x(f"UPDATE clips SET {','.join(f'{k}=?' for k in f)} WHERE id=?", [*f.values(), clip_id])


class ClipQueue:
    def __init__(self, settings: Settings, db: DB):
        self.s = settings
        self.db = db
        self._job: Job | None = None  # the clip being made
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="clip-queue", daemon=True)

    def start(self) -> None:
        self._recover_interrupted()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            if self._job:
                self._job.procs.cancel()

    def kick(self) -> None:
        self._wake.set()

    def cancel(self, clip_id: int) -> bool:
        with self._lock:
            if self._job is not None and self._job.id == clip_id:
                self._job.procs.cancel()
                return True
        return _transition(self.db, clip_id, "queued", status="cancelled", stage="Cancelled",
                           finished_at=time.time())

    def _recover_interrupted(self) -> None:
        """A clip being made when the process stopped cannot go on: what only it could have
        written goes, and it is recorded as failed."""
        for row in self.db.q("SELECT id FROM clips WHERE status='running'"):
            cid = int(row["id"])
            self._clean(cid)
            _update(self.db, cid, status="failed", stage="Failed",
                    message="Interrupted by restart", finished_at=time.time())

    def _clean(self, clip_id: int) -> None:
        try:
            temp_path(self.s, clip_id).unlink(missing_ok=True)
        except OSError:
            log.exception("Could not remove clip %s's temp file", clip_id)
        if not self.s.keep_work:
            shutil.rmtree(self.s.work_dir / f"clip-{clip_id}", ignore_errors=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                # not from a film whose file a BEQ track is being added to
                row = self.db.one(
                    "SELECT * FROM clips c WHERE status='queued' AND NOT EXISTS (SELECT 1 FROM "
                    "jobs j WHERE j.path=c.path AND j.status='running' AND j.phase='encode') "
                    "ORDER BY id LIMIT 1")
                if row is not None:
                    self._run(row)
                    continue
            except Exception:  # the queue must outlive any one bad row
                log.exception("Clip queue: making a clip failed")
            self._wake.wait(3)
            self._wake.clear()

    def _run(self, row) -> None:
        cid = int(row["id"])
        log_path = self.s.log_dir / f"clip-{cid}.log"
        last = [0.0, None]

        def progress(stage: str, pct: float | None) -> None:
            now = time.monotonic()
            if stage != last[1] or now - last[0] >= 1.0:
                last[0], last[1] = now, stage
                _update(self.db, cid, stage=stage, percent=pct)

        job = Job(self.s, cid, row["path"], json.loads(row["params_json"]), progress, log_path,
                  work=self.s.work_dir / f"clip-{cid}", tmp_mkv=temp_path(self.s, cid))
        with self._lock:
            if not _transition(self.db, cid, "queued", status="running", started_at=time.time(),
                               log_path=str(log_path), stage="Starting", percent=None):
                return
            self._job = job
        try:
            fields = self._outcome(job)
        finally:
            with self._lock:
                self._job = None
            job.procs.reap()
            self._clean(cid)
        self._write_final(cid, fields)

    @staticmethod
    def _outcome(job: Job) -> dict:
        try:
            result = run_clip(job)
            job.log(f"Clip written: {result['path']}")
            return dict(status="done", stage="Done", percent=100, result_json=json.dumps(result),
                        message=result["file"], finished_at=time.time())
        except Exception as exc:  # noqa: BLE001 - every ending is recorded on the row
            if isinstance(exc, Cancelled) or job.procs.cancelled:
                job.log("Cancelled")
                return dict(status="cancelled", stage="Cancelled", finished_at=time.time())
            message = str(exc) if isinstance(exc, JobFailed) else f"{type(exc).__name__}: {exc}"
            job.log("FAILED: " + (message if isinstance(exc, JobFailed)
                                  else "\n" + traceback.format_exc()))
            return dict(status="failed", stage="Failed", message=message,
                        finished_at=time.time())

    def _write_final(self, clip_id: int, fields: dict) -> None:
        while True:
            try:
                _update(self.db, clip_id, **fields)
                return
            except Exception:
                log.exception("Could not record clip %s's final status", clip_id)
            if self._stop.wait(FINAL_WRITE_RETRY_S):
                return
