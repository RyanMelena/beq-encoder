"""What the app keeps about a film beside its file and its jobs: the filters BEQ Forge
designed for it, the level each filter was measured at, the bass level over its running time,
the wizard's choices, and the settings the Settings page saves.

A filter and a level belong to the file they were made from. Each row carries that file's
size and modification time, and holds only while the film's row carries the same."""

from __future__ import annotations

import json
import time

from .db import DB

# The low-end cutoffs the wizard offers as presets, in Hz (0: none). They are measured ahead in
# one pass, so choosing one shows its level change at once. Any other cutoff is measured when
# the user sets it.
CUTOFFS = (0.0, 5.0, 10.0, 15.0, 20.0, 25.0)

MTIME_SLACK_S = 0.002  # a float st_mtime against the same time in nanoseconds


def cutoff_ladder(default: float, custom: float | None = None) -> list[float]:
    """Every cutoff the wizard measures: the presets, the Settings page's default, and the
    custom cutoff the user set, if any."""
    return sorted({*CUTOFFS, float(default), *([float(custom)] if custom is not None else [])})


def holds(row, m) -> bool:
    """Whether a stored row was made from the file the film's row describes now."""
    if row["size"] is None or row["mtime_ns"] is None or m["mtime"] is None:
        return False
    return row["size"] == m["size"] and abs(row["mtime_ns"] / 1e9 - m["mtime"]) < MTIME_SLACK_S


def _stamp(pin: dict | None) -> tuple:
    return ((pin or {}).get("size"), (pin or {}).get("mtime_ns"))


def save_filters(db: DB, media_id: int, track_key: str, pin: dict | None, entries: list[dict],
                 job_id: int | None = None) -> None:
    db.run_many(
        "INSERT INTO filters (media_id, track_key, digest, entry_json, size, mtime_ns, job_id, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (media_id, track_key, digest) "
        "DO UPDATE SET entry_json=excluded.entry_json, size=excluded.size, "
        "mtime_ns=excluded.mtime_ns, job_id=excluded.job_id",
        [(media_id, track_key, e["digest"], json.dumps(e), *_stamp(pin), job_id, time.time())
         for e in entries])


def forge_filters(db: DB, m) -> list[dict]:
    """The BEQ Forge filters that hold for the film's file: {track_key, entry, created_at,
    job_id}."""
    rows = db.q("SELECT * FROM filters WHERE media_id=? ORDER BY id", (m["id"],))
    return [{"track_key": r["track_key"], "entry": json.loads(r["entry_json"]),
             "created_at": r["created_at"], "job_id": r["job_id"]} for r in rows if holds(r, m)]


def save_levels(db: DB, media_id: int, track_key: str, pin: dict | None,
                levels: dict[str, dict[str, dict]]) -> None:
    """`levels` is {cutoff: {digest: {"peak_dbfs", "channel"}}}, as a job's result holds it."""
    db.run_many(
        "INSERT OR REPLACE INTO levels (media_id, track_key, digest, cutoff_hz, peak_dbfs, "
        "channel, size, mtime_ns, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(media_id, track_key, digest, float(cutoff), p["peak_dbfs"], p.get("channel"),
          *_stamp(pin), time.time())
         for cutoff, found in levels.items() for digest, p in found.items()])


def levels(db: DB, m, track_key: str) -> dict[str, dict[float, dict]]:
    """{digest: {cutoff: {"peak_dbfs", "channel"}}} for one track of the film's file."""
    out: dict[str, dict[float, dict]] = {}
    for r in db.q("SELECT * FROM levels WHERE media_id=? AND track_key=?", (m["id"], track_key)):
        if holds(r, m):
            out.setdefault(r["digest"], {})[r["cutoff_hz"]] = {
                "peak_dbfs": r["peak_dbfs"], "channel": r["channel"]}
    return out


def save_profile(db: DB, media_id: int, track_key: str, pin: dict | None, profile: dict) -> None:
    db.x("INSERT OR REPLACE INTO profiles (media_id, track_key, profile_json, size, mtime_ns) "
         "VALUES (?, ?, ?, ?, ?)", (media_id, track_key, json.dumps(profile), *_stamp(pin)))


def profile(db: DB, m, track_key: str) -> dict | None:
    """The bass level over the running time: {"bucket_s", "db": [...]}, or None."""
    r = db.one("SELECT * FROM profiles WHERE media_id=? AND track_key=?", (m["id"], track_key))
    return json.loads(r["profile_json"]) if r and holds(r, m) else None


def record(db: DB, media_id: int, job_id: int, result: dict) -> None:
    """Keep what a finished design or measuring job found."""
    key, pin = result.get("track_key"), result.get("source")
    if not key:
        return
    design = result.get("design") or {}
    if design.get("candidates"):
        save_filters(db, media_id, key, pin, design["candidates"], job_id)
    if result.get("levels"):
        save_levels(db, media_id, key, pin, result["levels"])
    if result.get("profile"):
        save_profile(db, media_id, key, pin, result["profile"])


def carry_over(db: DB, media_id: int, old: dict | None, new: dict | None) -> None:
    """The app replaced the film's file with the same film plus a BEQ track: what held for
    the old file holds for the new one."""
    if not old or not new:
        return
    for table in ("filters", "levels", "profiles"):
        db.x(f"UPDATE {table} SET size=?, mtime_ns=? WHERE media_id=? AND size=? AND mtime_ns=?",
             (*_stamp(new), media_id, *_stamp(old)))


def draft(db: DB, media_id: int) -> dict:
    r = db.one("SELECT draft_json FROM drafts WHERE media_id=?", (media_id,))
    return json.loads(r["draft_json"]) if r else {}


def save_draft(db: DB, media_id: int, draft: dict) -> None:
    db.x("INSERT OR REPLACE INTO drafts (media_id, draft_json, updated_at) VALUES (?, ?, ?)",
         (media_id, json.dumps(draft), time.time()))


def clear_draft(db: DB, media_id: int) -> None:
    db.x("DELETE FROM drafts WHERE media_id=?", (media_id,))


def setting(db: DB, key: str) -> str | None:
    r = db.one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else None


def set_setting(db: DB, key: str, value: str) -> None:
    db.x("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
