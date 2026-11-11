"""SQLite persistence for scanned media and jobs."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    size INTEGER, mtime REAL,
    title TEXT, year TEXT, edition TEXT, tmdb TEXT,
    has_atmos INTEGER NOT NULL DEFAULT 0,
    has_beq INTEGER NOT NULL DEFAULT 0,
    probe_json TEXT,
    error TEXT,
    scanned_at REAL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
    path TEXT NOT NULL,
    status TEXT NOT NULL,          -- queued | running | awaiting | no_beq | unsupported | done | failed | cancelled
    stage TEXT, percent REAL,
    params_json TEXT NOT NULL,     -- phase + track (+ approved filter + encode settings)
    result_json TEXT,
    message TEXT,
    log_path TEXT,
    created_at REAL NOT NULL, started_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_media ON jobs(media_id, id);
"""

ACTIVE_STATUSES = "('queued','running','awaiting')"  # a job in one of these owns its file
MIGRATIONS = {
    "media": {"has_audio": "INTEGER NOT NULL DEFAULT 0",
              "best_json": "TEXT", "beq_names": "TEXT",
              # library assessment (assess.py): candidate / stale summary, and the
              # roll-off screen of the best track with the file state it was made for
              "assess_json": "TEXT", "candidate": "TEXT", "stale": "INTEGER NOT NULL DEFAULT 0",
              "rolloff_json": "TEXT", "rolloff_key": "TEXT", "rolloff_at": "REAL"},
    "jobs": {"phase": "TEXT NOT NULL DEFAULT 'encode'", "prepare_json": "TEXT"},
}


class DB:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        for table, add in MIGRATIONS.items():
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in add.items():
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.conn.execute(
            "UPDATE jobs SET status='failed', message='Superseded by a newer job for this file', "
            f"finished_at=? WHERE status IN {ACTIVE_STATUSES} AND id NOT IN "
            f"(SELECT MAX(id) FROM jobs WHERE status IN {ACTIVE_STATUSES} GROUP BY path)",
            (time.time(),),
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_per_path ON jobs(path) "
            f"WHERE status IN {ACTIVE_STATUSES}"
        )

    def q(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple | list = ()) -> int:
        """Run an UPDATE or DELETE; the number of rows it changed."""
        with self._lock:
            return self.conn.execute(sql, args).rowcount

    def run_many(self, sql: str, rows: list[tuple]) -> None:
        """Run one statement per row of `rows`, all in one transaction."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.executemany(sql, rows)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def insert(self, sql: str, args: tuple | list = ()) -> int | None:
        """Run an INSERT; the new row's id, or None if `OR IGNORE` skipped it."""
        with self._lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid if cur.rowcount else None

    # -- media ------------------------------------------------------------------
    def upsert_media(self, **f: Any) -> int:
        cols = list(f)
        sql = (
            f"INSERT INTO media ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(path) DO UPDATE SET "
            + ",".join(f"{c}=excluded.{c}" for c in cols if c != "path")
        )
        self.x(sql, [f[c] for c in cols])
        row = self.one("SELECT id FROM media WHERE path=?", (f["path"],))
        return int(row["id"])

    # -- jobs -------------------------------------------------------------------
    def create_job(self, media_id: int | None, path: str, params: dict,
                   prepare: dict | None = None) -> int | None:
        """A new queued job, or None if the file already has an active one."""
        return self.insert(
            "INSERT OR IGNORE INTO jobs (media_id, path, status, stage, percent, params_json, "
            "phase, prepare_json, created_at) VALUES (?, ?, 'queued', 'Queued', NULL, ?, ?, ?, ?)",
            (media_id, path, json.dumps(params), params.get("phase", "encode"),
             json.dumps(prepare) if prepare else None, time.time()),
        )

    def update_job(self, job_id: int, **f: Any) -> None:
        if not f:
            return
        self.x(
            f"UPDATE jobs SET {','.join(f'{k}=?' for k in f)} WHERE id=?",
            [*f.values(), job_id],
        )

    def transition(self, job_id: int, expected: str, **f: Any) -> bool:
        """Apply `f` only if the job is still in status `expected`. False: it was not,
        or the change would give its file a second active job."""
        sets = ",".join(f"{k}=?" for k in f)
        return bool(self.x(f"UPDATE OR IGNORE jobs SET {sets} WHERE id=? AND status=?",
                           [*f.values(), job_id, expected]))

    def active_job_for_path(self, path: str) -> sqlite3.Row | None:
        return self.one(
            f"SELECT * FROM jobs WHERE path=? AND status IN {ACTIVE_STATUSES} ORDER BY id DESC",
            (path,),
        )
