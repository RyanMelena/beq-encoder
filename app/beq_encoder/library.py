"""Keeps the library current without being asked: scans at start-up and on a
schedule, watches the media roots for new or changed files, screens each film's bass
for a mastering roll-off in the background, and re-assesses every file whenever the
catalogue, the designer or the file changes (assess.py).

Watching uses inotify (watchfiles) where the kernel sees the writes — a local bind
mount. It cannot see writes made on another machine over a network
share, so the scheduled scan is the safety net either way; WATCH=poll swaps inotify
for polling. A new file is only probed once its size has stopped changing for
WATCH_SETTLE_SECONDS, so a copy in progress is never read half-written.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from . import assess, charts, layouts, media
from .config import Settings
from .db import DB
from .worker import in_library

log = logging.getLogger(__name__)


NETWORK_FS = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "ceph", "glusterfs", "fuse.sshfs",
              "afs", "fuse.rclone"}

# A bass screen that failed (often just a share that dropped) is tried again after this,
# doubling with each failure up to the longest wait
SCREEN_RETRY_S = 3600.0
SCREEN_RETRY_MAX_S = 7 * 86400.0

LOOP_RETRY_S = 60.0  # a background loop whose pass failed waits this long before the next


def mount_fstype(path: str, mounts: str = "/proc/mounts") -> str:
    """The file system type of the mount `path` lives on ("" if unknown)."""
    try:
        lines = Path(mounts).read_text().splitlines()
    except OSError:
        return ""
    p = str(Path(path).resolve())
    best, fstype = "", ""
    for ln in lines:
        f = ln.split()
        if len(f) < 3:
            continue
        mp = f[1].replace("\\040", " ")
        if (p == mp or p.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best):
            best, fstype = mp, f[2]
    return fstype


def _designer_key(info: dict) -> tuple:
    return info.get("designer_release"), info.get("design_version"), info.get("design_settings")


def _failed_screens(row, key: str) -> int:
    """How many screens in a row have failed for this file state (0: none is on record)."""
    if row["rolloff_key"] != key or not row["rolloff_json"]:
        return 0
    ro = json.loads(row["rolloff_json"])
    return ro.get("failures", 1) if ro.get("verdict") == "error" else 0


def _screen_due(row, key: str, now: float) -> bool:
    if row["rolloff_key"] != key:
        return True
    failures = _failed_screens(row, key)
    return failures > 0 and now >= (row["rolloff_at"] or 0.0) + min(
        SCREEN_RETRY_MAX_S, SCREEN_RETRY_S * 2 ** (failures - 1))


class Library:
    def __init__(self, settings: Settings, db: DB, catalogue, scanner, jobs,
                 designer_info: Callable[[], dict], dee_outputs: Callable[[], bool]):
        self.s = settings
        self.db = db
        self.catalogue = catalogue
        self.scanner = scanner
        self.jobs = jobs
        self.designer_info = designer_info
        self.dee_outputs = dee_outputs
        self._stop = threading.Event()
        self._audio_wake = threading.Event()   # a file was scanned: maybe more to screen
        self._pending: dict[str, tuple[float, int]] = {}   # path -> (seen at, size)
        self._plock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self.audio_start_delay = 30.0
        self._designer_key: tuple | None = None  # the answers last fully assessed against
        self._assess_lock = threading.Lock()  # a pass must read what the last one wrote
        self.state = {
            "watch": "off", "watch_error": None, "pending": 0,
            "next_scan_at": None, "last_scan_at": None,
            "audio": {"enabled": settings.assess_audio, "running": False, "current": "",
                      "done": 0, "todo": 0, "error": None, "waiting": None},
        }

    # -- lifecycle ------------------------------------------------------------------
    def start(self) -> None:
        self.catalogue.on_loaded = self.reassess
        for target, name in ((self._schedule_loop, "library-schedule"),
                             (self._watch_loop, "library-watch"),
                             (self._settle_loop, "library-settle"),
                             (self._audio_loop, "library-audio")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._audio_wake.set()

    @contextmanager
    def _surviving(self, loop: str) -> Iterator[None]:
        """One pass of a long-lived loop. An unexpected error (a full /config, say) must
        not end the thread for the life of the service, nor have it spin."""
        try:
            yield
        except Exception:
            log.exception("Library %s: this pass failed; trying again", loop)
            self._stop.wait(LOOP_RETRY_S)

    def _idle(self, seconds: float) -> None:
        self._audio_wake.wait(seconds)
        self._audio_wake.clear()

    # -- assessment -----------------------------------------------------------------
    def reassess(self, ids: list[int] | None = None) -> int:
        """Recompute the candidate / stale summary (cheap: probe + catalogue only).

        Does nothing until the catalogue has loaded: an empty one would clear every
        candidate and report every catalogue-sourced BEQ track as withdrawn."""
        if not self.catalogue.loaded_at:
            return 0
        q = "SELECT id, path, title, year, edition, tmdb, probe_json, rolloff_json FROM media" \
            " WHERE probe_json IS NOT NULL"
        with self._assess_lock:
            rows = self.db.q(q + (f" AND id IN ({','.join('?' * len(ids))})" if ids else ""),
                             tuple(ids or ()))
            designer = self.designer_info() or {}
            try:
                outputs = self.dee_outputs()
            except Exception:  # noqa: BLE001
                outputs = False
            updates = []
            for r in rows:
                try:
                    probe = media.ProbeResult.from_json(r["probe_json"])
                    ro = json.loads(r["rolloff_json"]) if r["rolloff_json"] else None
                    a = assess.assess(probe, assess.film_of(r), self.catalogue, designer, outputs,
                                      ro)
                    if ro:
                        a["rolloff"] = ro
                    updates.append((assess.dumps(a), a["candidate"], int(a["stale"]), r["id"]))
                except Exception:  # noqa: BLE001 - one odd file must not stop the rest
                    log.exception("Assessment failed for %s", r["path"])
            self.db.run_many("UPDATE media SET assess_json=?, candidate=?, stale=? WHERE id=?",
                             updates)
        if ids is None and designer.get("ok"):
            self._designer_key = _designer_key(designer)
        return len(updates)

    def on_scanned(self, media_id: int | None) -> None:
        if media_id:
            self.reassess([media_id])
            self._audio_wake.set()

    # -- schedule -------------------------------------------------------------------
    def _schedule_loop(self) -> None:
        with self._surviving("first assessment"):
            self.reassess()  # what is already known, straight away (the scan takes a while)
        if self.s.scan_on_start:
            with self._surviving("first scan"):
                self._scan()
        interval = max(0.0, self.s.scan_interval_hours) * 3600
        next_at = time.time() + interval if interval else None
        self.state["next_scan_at"] = next_at
        cat_checked = time.time()
        while not self._stop.wait(60):
            with self._surviving("schedule"):
                if time.time() - cat_checked > 3600:  # fetch a newer catalogue when due
                    cat_checked = time.time()
                    self.catalogue.refresh()
                self._watch_inputs()
                if next_at and time.time() >= next_at:
                    self._scan()
                    next_at = time.time() + interval
                    self.state["next_scan_at"] = next_at

    def _scan(self) -> None:
        self.catalogue.refresh()  # downloads only when older than CATALOGUE_MAX_AGE_DAYS
        # wait for a scan someone else started, then run an incremental one; a started
        # scan marks itself running from its own thread, so `finished_at` is what moves
        while self.scanner.state.get("running") and not self._stop.wait(5):
            pass
        finished_before = self.scanner.state.get("finished_at")
        if self.scanner.start(force=False):
            while (self.scanner.state.get("finished_at") == finished_before
                   and not self._stop.wait(5)):
                pass
        self.state["last_scan_at"] = time.time()
        self.reassess()

    def _watch_inputs(self) -> None:
        """Re-assess everything when the designer's answers move (a catalogue load
        re-assesses by itself, through `Catalogue.on_loaded`, so nothing is due before
        the first load)."""
        if not self.catalogue.loaded_at:
            return
        d = self.designer_info() or {}
        if d.get("ok") and _designer_key(d) != self._designer_key:
            log.info("Designer changed: re-assessing the library")
            self.reassess()

    # -- watch ----------------------------------------------------------------------
    def _watch_loop(self) -> None:
        mode = self.s.watch
        roots = [str(r) for r in self.s.media_roots if Path(r).is_dir()]
        if mode == "off" or not roots:
            self.state["watch"] = "off" if mode == "off" else "no media roots"
            return
        try:
            import watchfiles  # noqa: F401
        except ImportError:
            self.state.update(watch="off", watch_error="watchfiles not installed")
            return
        if mode == "poll":
            groups = [(roots, True)]
            self.state["watch"] = "polling"
        else:
            # inotify only sees writes made on this machine, so a network share gains
            # nothing from it - and adding a watch to every folder over the network takes
            # minutes, during which nothing is seen at all: the scheduled scan covers it
            local = [r for r in roots if mount_fstype(r) not in NETWORK_FS]
            remote = [r for r in roots if r not in local]
            groups = [(local, False)] if local else []
            parts = [f"inotify: {', '.join(local)}"] if local else []
            parts += [f"{r} ({mount_fstype(r)}): scheduled scan" for r in remote]
            self.state["watch"] = " · ".join(parts) or "network shares only: scheduled scan"
        threads = [threading.Thread(target=self._watch_group, args=(g, p), daemon=True,
                                    name=f"library-watch-{i}")
                   for i, (g, p) in enumerate(groups)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _watch_group(self, roots: list[str], polling: bool) -> None:
        from watchfiles import Change, watch
        while not self._stop.is_set():
            try:
                for changes in watch(*roots,
                                     watch_filter=lambda c, p: in_library(p, self.s.media_roots),
                                     stop_event=self._stop, force_polling=polling,
                                     poll_delay_ms=self.s.watch_poll_seconds * 1000,
                                     debounce=2000, step=200, recursive=True,
                                     raise_interrupt=False, ignore_permission_denied=True):
                    for change, path in changes:
                        if change == Change.deleted:
                            self._forget(path)
                        else:
                            self._note(path)
            except Exception as exc:  # noqa: BLE001 - e.g. inotify watch limit reached
                self.state["watch_error"] = str(exc)
                log.warning("Library watch stopped (%s); retrying in 60 s", exc)
                if not polling and "inotify" in str(exc).lower():
                    polling = True
                    self.state["watch"] = f"polling (inotify failed): {', '.join(roots)}"
                self._stop.wait(60)

    def _note(self, path: str) -> None:
        try:
            size = Path(path).stat().st_size
        except OSError:
            return
        with self._plock:
            self._pending[path] = (time.time(), size)
            self.state["pending"] = len(self._pending)

    def _forget(self, path: str) -> None:
        with self._plock:
            self._pending.pop(path, None)
            self.state["pending"] = len(self._pending)
        if not Path(path).exists():
            self.db.x("DELETE FROM media WHERE path=?", (path,))

    def _settle_loop(self) -> None:
        """Probe watched files once their size has held still long enough."""
        while not self._stop.wait(5):
            with self._surviving("watched-file check"):
                self._settle()

    def _settle(self) -> None:
        ready: list[Path] = []
        now = time.time()
        with self._plock:
            for path, (seen, size) in list(self._pending.items()):
                try:
                    cur = Path(path).stat().st_size
                except OSError:
                    self._pending.pop(path, None)
                    continue
                if cur != size:
                    self._pending[path] = (now, cur)
                elif now - seen >= self.s.watch_settle_seconds:
                    ready.append(Path(path))
            if ready and not self.scanner.state.get("running"):
                for p in ready:
                    self._pending.pop(str(p), None)
            else:
                ready = []
            self.state["pending"] = len(self._pending)
        if ready:
            log.info("New or changed: %s", ", ".join(p.name for p in ready))
            self.scanner.start(force=False, files=ready)

    # -- bass screen ----------------------------------------------------------------
    def _audio_next(self) -> list[tuple]:
        """(row, screen key) of each film due a bass screen, the one to do next first."""
        rows = self.db.q(
            "SELECT id, path, size, mtime, probe_json, best_json, rolloff_key, rolloff_json,"
            " rolloff_at, assess_json FROM media WHERE has_audio=1 AND has_beq=0 AND probe_json IS NOT NULL"
            " AND error IS NULL ORDER BY (candidate='catalogue'), mtime DESC")
        todo = []
        now = time.time()
        for r in rows:
            best = json.loads(r["best_json"] or "null")
            if best is None:
                continue
            a = json.loads(r["assess_json"] or "{}")
            if a.get("catalogue") == "no_beq_needed" or not (a.get("best") or {}).get("supported", True):
                continue
            key = assess.rolloff_key(r["size"], r["mtime"], best["track_id"])
            if _screen_due(r, key, now):
                todo.append((r, key))
        return todo

    def _busy(self) -> bool:
        return bool(self.jobs.running) or bool(self.scanner.state.get("running"))

    def _audio_loop(self) -> None:
        st = self.state["audio"]
        if not self.s.assess_audio:
            return
        self._stop.wait(self.audio_start_delay)
        nice = ["nice", "-n", "19"] if shutil.which("nice") else []
        if nice and shutil.which("ionice"):
            nice = ["ionice", "-c", "3", *nice]
        counted = 0.0
        while not self._stop.is_set():
            with self._surviving("bass screen"):
                if self._busy():
                    st["waiting"] = "scan" if self.scanner.state.get("running") else "jobs"
                    if time.time() - counted > 120:  # keep the count roughly current
                        st["todo"] = len(self._audio_next())
                        counted = time.time()
                    self._stop.wait(10)
                    continue
                st["waiting"] = None
                todo = self._audio_next()
                counted = time.time()
                st["todo"] = len(todo)
                if not todo:
                    self._idle(600)
                    continue
                r, key = todo[0]
                st.update(running=True, current=r["path"], error=None)
                try:
                    probe = media.ProbeResult.from_json(r["probe_json"])
                    result = self._screen(r["path"], probe, probe.best, nice)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Bass screen failed for %s: %s", r["path"], exc)
                    st["error"] = f"{Path(r['path']).name}: {exc}"
                    result = {"schema": assess.ROLLOFF_SCHEMA, "verdict": "error",
                              "error": str(exc)[:200], "failures": _failed_screens(r, key) + 1}
                st.update(running=False, current="")
                if result is None:  # interrupted by a job: try again later
                    continue
                self.db.x("UPDATE media SET rolloff_json=?, rolloff_key=?, rolloff_at=?"
                          " WHERE id=?", (assess.dumps(result), key, time.time(), r["id"]))
                self.reassess([r["id"]])
                st["done"] += 1

    def _screen(self, path: str, probe, best, nice: list[str]) -> dict | None:
        ch = int(best.channels or 2)
        names = layouts.names_or_generic(ch, best.layout)
        stopped = {"v": False}

        def cancelled() -> bool:
            stopped["v"] = stopped["v"] or self._stop.is_set() or self._busy()
            return stopped["v"]

        def popen(cmd, **kw):
            return subprocess.Popen([*nice, *cmd] if nice else cmd, **kw)

        t0 = time.monotonic()
        chunks = assess.sampled_mono(self.s.ffmpeg, path, best.audio_index, ch, names,
                                     probe.duration_s, popen=popen, cancelled=cancelled)
        try:
            m = charts.measure_stream(chunks, excerpts=True)
        except ValueError:
            if stopped["v"]:
                return None
            raise
        if stopped["v"]:
            return None
        ro = assess.rolloff(m).to_dict()
        ro["track"] = best.label
        ro["took_s"] = round(time.monotonic() - t0, 1)
        return ro
