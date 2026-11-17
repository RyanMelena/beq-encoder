"""FastAPI app: the Library, the film detail page, the wizard that adds a BEQ track or makes
a BEQ clip, the Activity log and the Settings page (docs/ux-build-spec.md)."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager
from functools import partial
from http import HTTPStatus
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlencode, urlparse

import numpy as np
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.exceptions import HTTPException as StarletteHTTPException


from . import __version__, activity, clips, config, designer, store, support, wizard, worker
from . import film as films
from .curve import CUTOFF_FLOOR_DB, compare_svg, span_db, swatch
from .words import (TERMS, bass_restored, cutoff_words, file_note, film_name, fmt_hms,
                    fmt_time, ordinal)
from .biquad import HIGHPASS_MAX_HZ, check_highpass
from .catalogue import Catalogue
from .db import DB
from .media import AudioTrack, ProbeResult
from .library import Library
from .pipeline import FileStamp, source_pin
from .pipeline.clip import MAX_FILTERS as CLIP_MAX_FILTERS, parse_start
from .worker import JobQueue, Scanner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("beq_encoder")

HERE = Path(__file__).parent
S = config.load()
db = DB(S.db_path)
catalogue = Catalogue(S.catalogue_path, S.catalogue_url, S.catalogue_max_age_days)
jobs = JobQueue(S, db, catalogue)
clip_queue = clips.ClipQueue(S, db)
scanner = Scanner(S, db)
library_watch = Library(S, db, catalogue, scanner, jobs,
                        designer_info=lambda: designer_status(),
                        dee_outputs=lambda: _dee_outputs())
jobs.on_scanned = scanner.on_scanned = library_watch.on_scanned
scan_file = partial(worker.scan_file, on_scanned=library_watch.on_scanned)

def _dee_outputs() -> bool:
    """Whether DEE can make TrueHD (Atmos and channel-based) right now."""
    return S.dee().status()["ok"]


_health_cache: dict = {"at": 0.0, "value": None}


def designer_status(max_age: float = 30.0) -> dict:
    """Designer /health, cached briefly so page loads don't wait on it."""
    now = time.monotonic()
    if _health_cache["value"] is None or now - _health_cache["at"] > max_age:
        _health_cache["value"] = designer.health(S.designer_url, timeout=3.0)
        _health_cache["at"] = now
    return _health_cache["value"]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    threading.Thread(target=catalogue.refresh, daemon=True).start()
    def dee_check() -> None:
        d = S.dee()
        if d.encoder.is_file():
            d.probe()
        for problem in d.status()["problems"]:
            log.warning("DEE: %s", problem)
        d.reprobe_while_failing()

    threading.Thread(target=dee_check, daemon=True).start()
    threading.Thread(target=lambda: [log.warning("Designer: %s", p) for p in
                                     designer_status(0)["problems"]], daemon=True).start()
    retire_waiting()
    jobs.start()
    clip_queue.start()
    library_watch.start()
    yield
    library_watch.stop()
    clip_queue.stop()
    jobs.stop()


def retire_waiting() -> None:
    """Close every job left waiting for the user to choose a filter, from before the wizard.
    Its BEQ Forge filters join the film's filter list, so none is lost."""
    for r in db.q("SELECT * FROM jobs WHERE status='awaiting'"):
        prep = json.loads(r["prepare_json"] or "null") or {}
        m = db.one("SELECT probe_json FROM media WHERE id=?", (r["media_id"],)) \
            if r["media_id"] else None
        design = prep.get("design") or {}
        track_id = (prep.get("track") or {}).get("track_id")
        if m and m["probe_json"] and design.get("candidates") and track_id is not None:
            probe = ProbeResult.from_json(m["probe_json"])
            t = probe.track(track_id)
            if t is not None and not t.beq:
                store.save_filters(db, r["media_id"], probe.track_key(t), prep.get("source"),
                                   design["candidates"], r["id"])
        db.update_job(r["id"], status="done", finished_at=time.time(),
                      message="Its filters are in the film's filter list.")
    db.x("UPDATE jobs SET status='cancelled', finished_at=? WHERE phase='prepare' AND "
         "status='queued'", (time.time(),))


app = FastAPI(title="BEQ Encoder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
tpl = Jinja2Templates(directory=HERE / "templates")


tpl.env.globals.update(
    version=__version__, dee=lambda: S.dee().status(),
    designer_status=lambda: designer_status(),
)


DASH = Markup('<span class="muted">\u2014</span>')  # every empty table cell

tpl.env.filters.update(t=fmt_time)
tpl.env.globals.update(dash=DASH)


def _under_roots(p: Path) -> bool:
    rp = p.resolve()
    return any(rp.is_relative_to(r.resolve()) for r in S.media_roots)


LABELS = {"flac": "FLAC 24-bit (test only: not a bitstream)"}


def _output_formats(t: AudioTrack) -> list[tuple[str, str]]:
    """(value, label) of the outputs offered for a track."""
    p = support.plan(t)
    dee_ok = S.dee().status()["ok"]
    out = []
    for fmt in support.formats(t, S.allow_test_formats):
        if fmt == "truehd":
            if dee_ok:
                how = {"damf": "every Atmos object kept",
                       "joc": "Atmos objects decoded from DD+"}.get(
                           p.route, "lossless" if t.lossless else "from a lossy track")
                out.append(("truehd", f"{p.label} ({how})"))
        elif fmt == "flac":
            out.append(("flac", LABELS["flac"]))
    return out


# -- error pages ---------------------------------------------------------------
# Why a typed path was refused, as a JSON client reads it. The Library says what is wrong with
# the path at the field (_refusal_words).
PATH_REFUSALS = {
    "no_file": "Not an existing .mkv file",
    "not_mkv": "Not an existing .mkv file",
    "outside": "File is outside the configured media roots",
    "no_folder": "Folder must be an existing folder under the media roots",
}


class PathRejected(HTTPException):
    """A refused Library tools path: the Library shows it at the field it came from."""

    def __init__(self, code: str, typed: str, view: dict):
        super().__init__(400, PATH_REFUSALS[code])
        self.code = code
        self.typed = typed
        self.view = view  # the list the form was on


# -- pages ---------------------------------------------------------------------


PATH_FORMS = {"/media/add": ("Could not scan that path", "Scan one film or folder"),
              "/scan": ("Could not scan that folder", "Scan one film or folder")}
ACTION_HEADINGS = {"/add": "Could not add the BEQ track", "/design": "Could not design a filter",
                   "/retry": "Could not try again", "/clip": "Could not make the BEQ clip",
                   "/cancel": "Could not cancel"}
# A page that is not there: its heading, what happened, and the one way back (label, href, tab).
MISSING = {
    "media": ("Film not found", "This film is not in the Library. It may have been removed, or "
              "the link is out of date.", ("Back to the Library", "/", "library")),
    "jobs": ("Not found", "This work is not in the activity log. It may have been removed, or "
             "the link is out of date.", ("Back to Activity", "/activity", "activity")),
    "films": ("Film not found", "This film is not in the Library. It may have been removed, or "
              "the link is out of date.", ("Back to the Library", "/", "library")),
    "page": ("Page not found", "The address may be mistyped, or the link is out of date.",
             ("Back to the Library", "/", "library")),
}


def _error_view(method: str, path: str, status: int, detail: str | None) -> dict:
    """What the HTML error page says: heading, message, next step, and the one link back."""
    library = ("Back to the Library", "/")
    said = f"{detail.rstrip('.')}." if detail and detail != HTTPStatus(status).phrase else None
    item = re.match(r"/(media|films|jobs)/(\d+)(/[a-z]+)?", path)
    # Where the page sits, as every other page's breadcrumb says it.
    crumb = ("Activity", "/activity") if item and item[1] == "jobs" else ("Library", "/")
    if method == "POST" and path in PATH_FORMS:
        heading, form = PATH_FORMS[path]
        return {"heading": heading, "message": said or "No path was entered.", "crumb": crumb,
                "next_step": f"Enter a path, then try again from {form} on the Settings page.",
                "links": [library]}
    if said or (method == "POST" and status == 422):
        noun = {"media": "film", "films": "film", "jobs": "work"}[item[1]] if item else None
        return {"heading": ACTION_HEADINGS.get(item[3] if item else "", "Something went wrong"),
                "message": said or "A required field was empty or not valid.", "crumb": crumb,
                "next_step": f"See where things stand on this {noun}, then try again."
                             if noun else "Go back and try again.",
                "links": [(f"Open the {noun}", f"/films/{item[2]}" if noun == "film"
                            else "/activity") if noun else library]}
    heading, message, (label, href, tab) = MISSING[item[1] if item and not item[3] else "page"]
    return {"heading": heading, "message": message, "requested": path, "tab": tab,
            "crumb": crumb,
            "search": tab == "library", "links": [(label, href)]}


def _wants_html(request: Request) -> bool:
    return (not f"{request.url.path}/".startswith("/api/")
            and "text/html" in request.headers.get("accept", ""))


def _render_error(request: Request, status: int, view: dict, headers) -> HTMLResponse:
    return tpl.TemplateResponse(request, "error.html", view, status_code=status, headers=headers)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if not _wants_html(request):
        return await http_exception_handler(request, exc)
    if isinstance(exc, PathRejected):  # back on a Library address, with the error at its field
        return _to_library(exc.view, refused=exc.code, path=exc.typed)
    view = _error_view(request.method, request.url.path, exc.status_code, exc.detail)
    return await run_in_threadpool(_render_error, request, exc.status_code, view, exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if not _wants_html(request):
        return await request_validation_exception_handler(request, exc)
    view = _error_view(request.method, request.url.path, 422, None)
    return await run_in_threadpool(_render_error, request, 422, view, None)


PAGE_ROWS = 200

def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _bass_check_now(audio: dict) -> str:
    """What the bass check is doing now: why a film is not checked yet."""
    if not audio["enabled"]:
        return "The bass check is off."
    if audio["running"]:
        return f"The bass check is on {audio['current'].split('/')[-1]} now."
    if audio["waiting"] == "scan":
        return ("The bass check waits while files are scanned, and carries on by itself when the "
                "scan finishes.")
    if audio["waiting"]:
        return ("The bass check waits while other work runs, and carries on by itself when it "
                "finishes.")
    return "The bass check runs in the background."


def unknown_counts() -> Counter:
    """The films rated Unknown, by which Unknown each is. The Settings page and the Library's
    rows say the same words, so their counts agree."""
    rows = db.q("SELECT error, probe_json IS NOT NULL AS scanned, rolloff_json, has_beq FROM "
                "media WHERE COALESCE(rating, 'unknown')='unknown'")
    return Counter(films.unknown(r["error"], bool(r["scanned"]),
                                 (json.loads(r["rolloff_json"] or "null") or {}).get("verdict"),
                                 bool(r["has_beq"]))[0]
                   for r in rows)


def library_status(scan: dict, lib: dict, bass: bool = True) -> str:
    """The one thing the library is doing in the background now. `bass`: include the bass
    check."""
    audio = lib["audio"]
    if lib["watch_error"]:
        return f"Watching for new files failed: {lib['watch_error']}."
    if scan.get("running"):
        return f"Scanning {scan['done']} of {_count(scan['found'], 'file')}."
    if lib["pending"]:
        return f"{_count(lib['pending'], 'new file')} still copying."
    if not bass:
        return ""
    todo = db.one(
        f"SELECT COUNT(*) n FROM media WHERE {_RATING}='unknown' AND error IS NULL AND "
        "probe_json IS NOT NULL AND rolloff_json IS NULL AND NOT COALESCE(has_beq, 0)")["n"]
    if audio["enabled"] and not todo:
        return "The bass check has checked every film."
    now = _bass_check_now(audio)
    return f"{now} {_count(todo, 'film')} still to check." if todo else now


def _last_scan() -> float | None:
    """When the library folders were last scanned, by the schedule or by hand."""
    return max(filter(None, (scanner.state.get("finished_at"),
                             library_watch.state["last_scan_at"])), default=None)


def _next_scan() -> float | None:
    """When the schedule scans next. None when nothing is scheduled or the scan is due."""
    at = library_watch.state["next_scan_at"]
    return at if at and at > time.time() else None


def _catalogue_at() -> float | None:
    """When the catalogue was last downloaded."""
    age = catalogue.age_days()
    return None if age is None else time.time() - age * 86400


def _refusal_words(code: str, typed: str) -> tuple[str, str]:
    """What the Settings page says of a refused path: what is wrong with the path as it was
    typed, which the banner at the top of the page gives too, and the likeliest cause, which
    stands at the field."""
    p = Path(os.path.normpath(typed))
    if code == "not_mkv":
        return (f"\u201c{p.name}\u201d is not an .mkv file.",
                "BEQ Encoder reads only .mkv film files.")
    if not _under_roots(p):
        roots = " or ".join(str(r) for r in S.media_roots)
        return ("That path is not inside a library folder.",
                f"A path here starts with {roots}.")
    nothing = f"No {'file' if p.suffix else 'folder'} at {p}."
    root = next(r for r in S.media_roots if p.resolve().is_relative_to(r.resolve()))
    if not root.exists():
        return nothing, (f"Check the spelling. If the path is right, the library folder {root} "
                         "can't be reached, so make sure its drive or share is connected.")
    there = next(a for a in p.parents if a.exists())
    missing = p.relative_to(there).parts[0]
    kind = "file" if missing == p.name and p.suffix else "folder"
    return nothing, (f"The folder {there} exists, but it has no {kind} named "
                     f"\u201c{missing}\u201d. Check the spelling.")


@app.post("/media/add")
def add_media(path: str = Form(...), show: str = Form(""), q: str = Form(""),
              sort: str = Form("")):
    """Reads one film file now, or every film file in one folder."""
    p = Path(os.path.normpath(path.strip()))
    view = {"show": show, "q": q, "sort": sort}
    refusal = ("no_file" if not p.exists() else "outside" if not _under_roots(p) else
               "not_mkv" if p.is_file() and p.suffix.lower() != ".mkv" else None)
    if refusal:
        raise PathRejected(refusal, path.strip(), view)
    if p.is_dir():
        scanner.start(root=p)
        return _to_library(view, notice="checking")
    scanner.start(force=True, files=[p])
    return _to_library(view, notice="checking")


def _to_library(view: dict, fragment: str = "", **more: str) -> RedirectResponse:
    """Back to the Library list a form was on, with anything the list should say about it."""
    query = urlencode({k: v for k, v in more.items() if v})
    return RedirectResponse(f"/settings{'?' if query else ''}{query}#scan", status_code=303)


@app.post("/scan")
def scan(full: bool = Form(False), folder: str = Form(""), show: str = Form(""),
         q: str = Form(""), sort: str = Form("")):
    root = None
    view = {"show": show, "q": q, "sort": sort}
    if folder.strip():
        root = Path(os.path.normpath(folder.strip()))
        if not root.is_dir() or not _under_roots(root):
            raise PathRejected("no_folder", folder.strip(), view)
    scanner.start(force=full, root=root)
    return _to_library(view, notice="checking")


@app.post("/catalogue/refresh")
def catalogue_refresh(show: str = Form(""), q: str = Form(""), sort: str = Form("")):
    threading.Thread(target=catalogue.refresh, kwargs={"force": True}, daemon=True).start()
    return _to_library({"show": show, "q": q, "sort": sort}, notice="catalogue")


def _clip_or_404(clip_id: int) -> dict:
    c = clips.get(db, clip_id)
    if c is None:
        raise HTTPException(404, "No such clip")
    return c


@app.get("/clips/{clip_id}/file")
def clip_file(clip_id: int):
    """The finished clip, to save or play."""
    r = _clip_or_404(clip_id)["result"] or {}
    path = Path(r.get("path") or "")
    if not r.get("path") or path.parent != S.clip_dir or not path.is_file():
        raise HTTPException(404, "This clip is not in the clip folder any more")
    return FileResponse(path, media_type="video/x-matroska", filename=path.name)


@app.get("/clips/{clip_id}/log", response_class=PlainTextResponse)
def clip_log(clip_id: int):
    c = _clip_or_404(clip_id)
    if not c["log_path"] or not Path(c["log_path"]).exists():
        raise HTTPException(404)
    return Path(c["log_path"]).read_text(errors="replace")


@app.get("/api/clips/{clip_id}")
def api_clip(clip_id: int, log_lines: int = 0):
    c = _clip_or_404(clip_id)
    if log_lines and c["log_path"] and Path(c["log_path"]).exists():
        with open(c["log_path"], errors="replace") as fh:
            c["log"] = "".join(fh.readlines()[-log_lines:])
    return c


# -- JSON API --------------------------------------------------------------------
def _work_json(w: activity.Work) -> dict:
    return {**{k: getattr(w, k) for k in ("id", "kind", "status", "media_id", "film", "path",
                                          "stage", "progress", "result", "created_at",
                                          "started_at", "finished_at")},
            "what": w.what, "label": w.label, "failure": w.failure and w.failure.what}


@app.get("/api/jobs")
def api_jobs(active: bool = False):
    works = [w for w in activity.everything(db) if w.kind != "clip"]
    return [_work_json(w) for w in works if w.active or not active]


@app.get("/api/jobs/{job_id}")
def api_job(job_id: int, log_lines: int = 0):
    w = activity.get(db, f"job-{job_id}")
    if w is None:
        raise HTTPException(404)
    d = _work_json(w)
    r = db.one("SELECT log_path FROM jobs WHERE id=?", (job_id,))
    if log_lines and r["log_path"] and Path(r["log_path"]).exists():
        with open(r["log_path"], errors="replace") as fh:
            d["log"] = "".join(fh.readlines()[-log_lines:])
    return d


CHART_FILES = {"spectrum": ("spectrum.png", "image/png"),
               "heatmap": ("heatmap.png", "image/png"),
               "report": ("beq.json", "application/json")}


@app.get("/jobs/{job_id}/charts/{kind}")
def job_chart(job_id: int, kind: str):
    """A BEQ track's before/after chart (or its JSON report), as embedded in the MKV."""
    if kind not in CHART_FILES:
        raise HTTPException(404)
    name, mime = CHART_FILES[kind]
    path = S.charts_dir / f"job-{int(job_id)}" / name
    if not path.is_file():
        raise HTTPException(404, "No chart for this job")
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "max-age=3600"})


@app.get("/jobs/{job_id}/log", response_class=PlainTextResponse)
def job_log(job_id: int):
    r = db.one("SELECT log_path FROM jobs WHERE id=?", (job_id,))
    if not r or not r["log_path"] or not Path(r["log_path"]).exists():
        raise HTTPException(404)
    return Path(r["log_path"]).read_text(errors="replace")


@app.get("/api/library")
def api_library():
    return JSONResponse({"scan": scanner.state, **library_watch.state,
                         "status": library_status(scanner.state, library_watch.state)})


@app.get("/api/scan")
def api_scan():
    return scanner.state


@app.get("/healthz")
def healthz():
    d = designer_status(5)
    return JSONResponse(
        {"ok": True, "catalogue_entries": len(catalogue.entries),
         "dee": S.dee().status(),
         "designer": {k: d.get(k) for k in ("url", "ok", "problems", "contract_version",
                                            "designer_release", "design_version",
                                            "design_settings", "parallel")},
         "running_jobs": list(jobs.running)}
    )


@app.get("/api/debug/threads", response_class=PlainTextResponse)
def debug_threads():
    """Stack of every thread: for diagnosing a stuck job without a shell."""
    import sys
    import threading as _t
    import traceback as _tb

    names = {t.ident: t.name for t in _t.enumerate()}
    out = []
    for ident, frame in sys._current_frames().items():
        out.append(f"--- {names.get(ident, ident)}\n" + "".join(_tb.format_stack(frame)))
    return "\n".join(out)


# -- the film detail page, the wizard, the Activity log and the Settings page ----------------
# docs/ux-build-spec.md describes each page. A job is how the app does slow work: no page here
# waits for one, and the user never opens one to decide something.

def cutoff_default() -> float:
    """The low-end cutoff a new BEQ track starts with: the Settings page's, else the
    configuration's (BEQ_HIGHPASS_HZ)."""
    saved = store.setting(db, "cutoff_hz")
    return float(saved) if saved is not None else S.highpass_hz


def _film(media_id: int) -> films.Film:
    f = films.load(db, catalogue, media_id)
    if f is None:
        raise HTTPException(404)
    return f


def _back_to(f: films.Film, ref: str = "") -> RedirectResponse:
    return RedirectResponse(f"/films/{f.id}{'#' if ref else ''}{ref}", status_code=303)


def _ladder(custom: float | None = None) -> list[float]:
    return store.cutoff_ladder(cutoff_default(), custom)


def _formats(f: films.Film, track_id: int | None) -> list[tuple[str, str]]:
    t = f.probe.track(track_id) if f.probe and track_id is not None else None
    return _output_formats(t) if t else []


def _pin(f: films.Film, track_id: int) -> dict:
    """What the work will read: the file as it stands now and its track. The film's row must
    still describe the file, or its filters and levels may be for another one."""
    m = db.one("SELECT size, mtime FROM media WHERE id=?", (f.id,))
    try:
        stamp = FileStamp.of(Path(f.path))
    except OSError:
        raise HTTPException(409, "The film's file is not there any more; rescan it") from None
    if m["size"] != stamp.size or abs(m["mtime"] - stamp.mtime_ns / 1e9) > store.MTIME_SLACK_S:
        raise HTTPException(409, "The film's file has changed since it was scanned; rescan it")
    return source_pin(stamp, f.probe.track(track_id))


def _measure(f: films.Film, track_id: int | None, custom: float | None = None) -> None:
    """Measure the level change of every filter on a track at each cutoff the wizard offers,
    and at the custom one the user set, where that is not known or under way. Opening the
    wizard, or setting a custom cutoff, is how the user asks."""
    t = f.track(track_id)
    if t is None or not t.can or f.activity("measure") or f.activity("encode"):
        return
    gaps = {(x.digest, hz) for x in f.filters for hz in _ladder(custom)
            if hz not in x.levels.get(t.id, {})}
    if not gaps:
        return
    digests = {digest for digest, _ in gaps}
    if db.create_job(f.id, f.path, {"phase": "measure", "track_id": t.id,
                                    "filters": [x.entry for x in f.filters if x.digest in digests],
                                    "cutoffs": sorted({hz for _, hz in gaps})}):
        jobs.kick()


def _levels_view(f: films.Film, track_id: int | None,
                 custom: float | None = None) -> dict[str, dict]:
    """Each filter's level change on a track at each cutoff, as the wizard shows it."""
    target = S.default_target_peak_dbfs
    return {x.digest: {f"{hz:g}": f.level_said(x, track_id, hz, target)
                       for hz in _ladder(custom)} for x in f.filters}


def _bass_view(f: films.Film, custom: float | None = None) -> dict[str, dict]:
    return {x.digest: {f"{hz:g}": bass_restored(x.bands, hz) for hz in _ladder(custom)}
            for x in f.filters}


def _compare(f: films.Film, groups: list[list[films.Filter]], cutoffs: list[float],
             active: float, custom: float | None = None
             ) -> tuple[dict[str, int], list[str], dict[str, int]]:
    """What sets a film's filters side by side: each filter's number, counted through the
    groups, one chart for each group, which draws that group's filters together, and for
    each filter whose curve is the same as an earlier one's in its group, that one's number
    (two identical curves draw as one line, so the row and the legend say so). Every chart
    of a film has the same gain axis, on its page and in the wizard: the one that holds
    every filter the film has at every cutoff the wizard offers."""
    span = span_db([(x.bands, hz) for x in f.filters for hz in _ladder(custom)],
                   CUTOFF_FLOOR_DB)
    numbers, charts, same = {}, [], {}
    for group in groups:
        first = len(numbers)
        numbers.update({x.digest: first + i + 1 for i, x in enumerate(group)})
        seen: dict[str, int] = {}
        for x in group:
            twin = seen.setdefault(json.dumps(x.bands, sort_keys=True), numbers[x.digest])
            if twin != numbers[x.digest]:
                same[x.digest] = twin
        legend = [(f"{numbers[x.digest]} \u00b7 {said}"
                   + (f", identical to {same[x.digest]}" if x.digest in same else ""), x.bands)
                  for x, said in zip(group, _legend(group), strict=True)]
        charts.append(compare_svg(legend, cutoffs, active, span, first) if group else "")
    return numbers, charts, same


def _legend(group: list[films.Filter]) -> list[str]:
    """What a chart's legend calls each filter of a group, after its number: its author, or
    BEQ Forge, and what tells it from another of the same origin in the group, as its row's
    name does. The legend sits inside the chart, so it leaves out "BEQ Catalogue"."""
    said = []
    for x in group:
        tell = x.name.partition(" (")[2].rstrip(")")
        short = x.origin.split(" \u00b7 ")[-1]
        twins = [y for y in group if y.origin == x.origin]
        said.append(f"{short}, {tell}" if tell and (x.gone or len(twins) > 1) else short)
    return said


def _time_ticks(running_s: float) -> list[tuple[float, str]]:
    """The time axis under the bass-over-time chart: how far across each mark sits, in
    percent, and its time as h:mm:ss, the form the clip's start box and its label use."""
    if running_s <= 0:
        return []
    step = next((n for n in (60, 300, 600, 900, 1800, 3600) if running_s / n <= 8), 7200)
    return [(100 * t / running_s, fmt_hms(t))
            for t in range(0, int(running_s * 0.96) + 1, step)]  # a label fits after each


def _heaviest(profile: dict | None, seconds: float) -> float | None:
    """Where the film's bass is heaviest over `seconds`: the start time to recommend. The
    wizard works out the same in the page when the duration changes (wizard.html,
    recommend), so the two round a half bucket the same way, up."""
    if not profile or not profile.get("db"):
        return None
    power = 10 ** (np.asarray(profile["db"]) / 10)
    n = max(1, math.floor(seconds / profile["bucket_s"] + 0.5))
    if len(power) <= n:
        return 0.0
    sums = np.convolve(power, np.ones(n), "valid")
    return float(int(np.argmax(sums)) * profile["bucket_s"])


def profile_svg(profile: dict | None, w: int = 640, h: int = 80) -> str:
    """The bass level over the running time, one bar for each part, under the clip's window."""
    if not profile or not profile.get("db"):
        return ""
    levels = profile["db"]
    lo, hi = min(levels), max(levels)
    span = (hi - lo) or 1.0
    step = w / len(levels)
    bars = "".join(
        f'<rect x="{i * step:.1f}" y="{h - 4 - (v - lo) / span * (h - 8):.1f}" '
        f'width="{max(step - 0.5, 0.5):.1f}" height="{4 + (v - lo) / span * (h - 8):.1f}"/>'
        for i, v in enumerate(levels))
    return (f'<svg class="profile" viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
            f'aria-hidden="true">{bars}</svg>')


def _wizard_view(f: films.Film, d: wizard.Draft) -> dict:
    track = f.track(d.track_id)
    made, others = f.filters_for(d.track_id) if track else ([], [])
    designing = f.activity("design", d.track_id)
    measuring = f.activity("measure")
    chosen = [f.filter(x) for x in d.chosen if f.filter(x)]
    profile = store.profile(db, db.one("SELECT * FROM media WHERE id=?", (f.id,)),
                            track.key) if track else None
    seconds = d.seconds or S.clip_seconds
    heaviest = _heaviest(profile, seconds)
    formats = _formats(f, d.track_id)
    design_takes = activity.takes(db, "design")
    numbers, charts, same = _compare(f, [made, others], _ladder(d.cutoff), d.cutoff,
                                     d.cutoff)
    running_s = len(profile["db"]) * profile["bucket_s"] if profile else 0
    return {
        "f": f, "d": d, "track": track, "made": made, "others": others, "chosen": chosen,
        "gap": f.gap_said(design_takes), "adding": f.activity("encode"),
        "measure_takes": activity.takes(db, "measure"),
        "ck": f"{d.cutoff or 0:g}", "target": S.default_target_peak_dbfs,
        "designing": designing, "measuring": bool(measuring),
        "design_takes": design_takes,
        "presets": _ladder(), "custom": d.cutoff not in _ladder(),
        "cutoff_default": cutoff_default(), "cutoff_max": HIGHPASS_MAX_HZ,
        "levels": _levels_view(f, d.track_id, d.cutoff),
        "preselected": f.default_filter(d.track_id) if track else None,
        "preselected_why": f.default_why(d.track_id) if track else "",
        "bass": _bass_view(f, d.cutoff),
        "numbers": numbers, "charts": charts, "same": same,
        "forge": any(x.forge for x in made + others),
        "time_ticks": _time_ticks(running_s),
        "formats": formats, "format": d.format if d.format in dict(formats) else
        (formats[0][0] if formats else ""),
        "clip_problem": clips.problem(S) if d.output == "clip" else "",
        "clip_folder": str(S.clip_dir), "clip_max": CLIP_MAX_FILTERS,
        "seconds": f"{seconds:g}", "seconds_range": config.CLIP_SECONDS_RANGE,
        "profile": profile, "profile_svg": profile_svg(profile),
        "heaviest": "" if heaviest is None
        else fmt_hms(heaviest), "running_s": running_s,
        "steps": wizard.STEPS, "step_names": wizard.STEP_NAMES,
        "next": wizard.NEXT, "outputs": wizard.OUTPUTS,
        "encode_takes": activity.takes(db, "encode"), "clip_takes": activity.takes(db, "clip"),
    }


tpl.env.filters.update(cutoff=cutoff_words, hms=fmt_hms, ordinal=ordinal)
tpl.env.globals.update(activity_counts=lambda: activity.counts(db), FIXES=activity.FIXES,
                       RATINGS=films.RATINGS, RATING_SYMBOLS=films.RATING_SYMBOLS, TERMS=TERMS,
                       RECOMMENDED=films.RECOMMENDED, BEST_LOSSY=films.BEST_LOSSY, swatch=swatch)


@app.get("/films/{media_id}", response_class=HTMLResponse)
def film_page(request: Request, media_id: int):
    f = _film(media_id)
    track = f.default_track
    cutoff = cutoff_default()
    groups = f.filter_groups
    numbers, charts, same = _compare(f, [g for _, _, g in groups], [cutoff], cutoff)
    target = S.default_target_peak_dbfs
    # A film opened from the Activity log leads back to it.
    came = urlparse(request.headers.get("referer", "")).path
    return tpl.TemplateResponse(request, "film.html", {
        "f": f, "cutoff": cutoff, "track": track,
        "back": ("Activity", "/activity") if came == "/activity" else ("Library", "/"),
        "tab": "activity" if came == "/activity" else "library",
        # The group that is not made for the recommended track says under each filter's
        # name what it was made for. Every group has the same columns.
        "groups": [{"heading": heading, "note": note, "filters": group, "chart": chart,
                    "made_for": not (track and heading == f"Made for {track.name}")}
                   for (heading, note, group), chart in zip(groups, charts, strict=True)],
        "numbers": numbers, "same": same, "forge": any(x.forge for x in f.filters),
        "bass": {x.digest: bass_restored(x.bands, cutoff) for x in f.filters},
        "levels": {x.digest: f.level_shown(x, track.id if track else None, cutoff, target)
                   for x in f.filters},
        "gap": f.gap_said(activity.takes(db, "design")),
        "measures_next": f.measures_next(track.id, cutoff, target) if track else ""})


@app.get("/media/{media_id}")
def old_film_page(media_id: int):
    return RedirectResponse(f"/films/{media_id}", status_code=301)


@app.post("/films/{media_id}/rescan")
@app.post("/media/{media_id}/rescan")
def rescan(media_id: int):
    m = db.one("SELECT path FROM media WHERE id=?", (media_id,))
    if not m:
        raise HTTPException(404)
    p = Path(m["path"])
    if not p.exists():
        db.x("DELETE FROM media WHERE id=?", (media_id,))
        return RedirectResponse("/", status_code=303)
    scan_file(S, db, p)
    return RedirectResponse(f"/films/{media_id}#file", status_code=303)


def _draft(f: films.Film, output: str, q) -> wizard.Draft:
    d = wizard.Draft.load(db, f.id)
    d.output = output
    d.take(q)
    d.resolve(f, cutoff_default(), S.beq_default_flag)
    d.save(db, f.id)
    return d


@app.get("/films/{media_id}/{output}", response_class=HTMLResponse)
def wizard_page(request: Request, media_id: int, output: str):
    if output not in wizard.OUTPUTS:
        raise HTTPException(404)
    f = _film(media_id)
    d = _draft(f, output, request.query_params)
    _measure(f, d.track_id, d.cutoff)
    f = _film(media_id)  # with the measurement it may have queued
    return tpl.TemplateResponse(request, "wizard.html", _wizard_view(f, d))


@app.post("/films/{media_id}/draft")
async def save_draft(request: Request, media_id: int):
    """A choice the wizard sends as the user makes it, so that a reload keeps it."""
    f = _film(media_id)
    d = wizard.Draft.load(db, f.id)
    d.take(await request.form())
    d.resolve(f, cutoff_default(), S.beq_default_flag)
    d.save(db, f.id)
    return {"ok": True}


@app.get("/api/films/{media_id}/wizard")
def wizard_state(media_id: int, track: int = -1, cutoff: float | None = None):
    """What the wizard fills in as it arrives: each filter's level changes, and the work
    under way for the film. A custom cutoff left to measure is queued once the film's other
    measurement ends."""
    f = _film(media_id)
    track_id = track if track >= 0 else None
    if cutoff is not None and not f.activity("measure"):
        _measure(f, track_id, cutoff)
        f = _film(media_id)
    measuring = bool(f.activity("measure"))
    return {"levels": _levels_view(f, track_id, cutoff),
            "measuring": measuring, "filters": [x.digest for x in f.filters],
            "active": [{"ref": w.ref, "kind": w.kind, "progress": w.progress,
                        "label": w.status_line} for w in f.activities]}


def _chosen_filters(f: films.Film, d: wizard.Draft) -> list[films.Filter]:
    picked = [f.filter(x) for x in d.chosen]
    if not picked or any(x is None or not x.bands for x in picked):
        raise HTTPException(400, "Choose a filter first")
    return picked


def _common(f: films.Film, d: wizard.Draft) -> dict:
    """The settings a BEQ track and a BEQ clip share."""
    track = f.track(d.track_id)
    if track is None or not track.can:
        raise HTTPException(400, "That audio track can't get a BEQ track; rescan the file")
    formats = dict(_formats(f, d.track_id))
    fmt = d.format if d.format in formats else next(iter(formats), "")
    if not fmt:
        raise HTTPException(409, "No output format is available for this track right now")
    return {"track_id": track.id, "source": _pin(f, track.id), "format": fmt,
            "target_peak_dbfs": S.default_target_peak_dbfs, "fixed_gain_db": None,
            "highpass_hz": check_highpass(d.cutoff or 0.0), "default_flag": bool(d.default_flag)}


def _stop_measuring(f: films.Film) -> None:
    """Starting the work makes the measurement's figures needless."""
    w = f.activity("measure")
    if w:
        jobs.cancel(w.id)


@app.post("/films/{media_id}/add")
async def add_beq_track(request: Request, media_id: int):
    f = _film(media_id)
    d = _draft(f, "add", await request.form())
    picked = _chosen_filters(f, d)[0]
    params = {"phase": "encode", "entry": picked.entry, **_common(f, d),
              "replace_existing_beq": bool(d.replace) and bool(f.beq_tracks),
              "chosen_at": time.time()}
    _stop_measuring(f)
    job_id = db.create_job(f.id, f.path, params)
    if job_id is None:
        raise HTTPException(409, "A BEQ track is already being added to this film")
    store.clear_draft(db, f.id)
    jobs.kick()
    return _back_to(f, f"job-{job_id}")


@app.post("/films/{media_id}/clip")
async def make_clip(request: Request, media_id: int):
    f = _film(media_id)
    d = _draft(f, "clip", await request.form())
    problem = clips.problem(S)
    if problem:
        raise HTTPException(409, f"BEQ clips are off: {problem}")
    picked = _chosen_filters(f, d)
    lo, hi = config.CLIP_SECONDS_RANGE
    seconds = d.seconds or S.clip_seconds
    if not (math.isfinite(seconds) and lo <= seconds <= hi):
        raise HTTPException(400, f"A BEQ clip is {lo:g} to {hi:g} seconds long")
    try:
        start = parse_start(d.start)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    common = _common(f, d)
    cutoff = common["highpass_hz"]
    # each BEQ track plays at the level the whole track gets (docs/ux-decisions.md, 4)
    items = [{"entry": x.entry, "peak_dbfs": (x.levels.get(common["track_id"], {})
                                              .get(cutoff) or {}).get("peak_dbfs")}
             for x in picked]
    clip_id = clips.create(db, f.id, None, f.path, {**common, "start_s": start,
                                                    "length_s": seconds, "filters": items})
    _stop_measuring(f)
    clip_queue.kick()
    return _back_to(f, f"clip-{clip_id}")


@app.post("/films/{media_id}/design")
def design_filter(media_id: int, track: int = Form(...), output: str = Form("add")):
    """BEQ Forge designs a filter for a track in the background. The wizard stays usable,
    and the filter joins its list when the design ends."""
    f = _film(media_id)
    t = f.track(track)
    if t is None or not t.can:
        raise HTTPException(400, "That audio track can't get a BEQ track; rescan the file")
    if db.create_job(f.id, f.path, {"phase": "design", "track_id": t.id,
                                    "cutoffs": _ladder()}) is None:
        raise HTTPException(409, "BEQ Forge is already designing a filter for this film")
    jobs.kick()
    out = output if output in wizard.OUTPUTS else "add"
    return RedirectResponse(f"/films/{f.id}/{out}?step=filter&track={t.id}#filter",
                            status_code=303)


def _work(ref: str) -> activity.Work:
    w = activity.get(db, ref)
    if w is None:
        raise HTTPException(404)
    return w


def _after(w: activity.Work) -> RedirectResponse:
    return RedirectResponse(w.href if w.media_id else "/activity", status_code=303)


@app.post("/work/{ref}/cancel")
def cancel_work(ref: str):
    w = _work(ref)
    if w.kind == "clip":
        clip_queue.cancel(w.id)
    else:
        jobs.cancel(w.id)
    return _after(w)


@app.post("/work/{ref}/retry")
def retry_work(ref: str):
    """The same work again, on the file as it stands now."""
    w = _work(ref)
    if w.status not in ("failed", "cancelled") or not w.media_id:
        raise HTTPException(409, "Only failed or cancelled work can be tried again")
    f = _film(w.media_id)
    if w.kind == "clip":
        params = clips.get(db, w.id)["params"]
    else:
        params = json.loads(db.one("SELECT params_json FROM jobs WHERE id=?",
                                   (w.id,))["params_json"])
    t = f.track(params.get("track_id"))
    if t is None or not t.can or (f.probe and f.probe.track(t.id).beq):
        raise HTTPException(409, "The audio track is not in the file any more; rescan it")
    if w.kind in ("encode", "clip") or "source" in params:
        params["source"] = _pin(f, t.id)
    if w.kind == "clip":
        new = clips.create(db, f.id, None, f.path, params)
        clip_queue.kick()
        return _back_to(f, f"clip-{new}")
    new = db.create_job(f.id, f.path, params)
    if new is None:
        raise HTTPException(409, "This work is already queued or running for this film")
    jobs.kick()
    return _back_to(f, f"job-{new}")


@app.get("/api/films/{media_id}/activity")
def film_activity(media_id: int):
    """The film detail page's live refresh: each activity's progress."""
    f = _film(media_id)
    return {"active": [{"ref": w.ref, "status": w.status, "progress": w.progress,
                        "label": w.status_line} for w in f.activities]}


# -- the Activity log -------------------------------------------------------------------
# A failure stays under Failed while it stands. Once newer work for its film has ended, it is
# history, and sits under Finished with its Failed badge. Every row under Finished has the same
# fields, and its badge says how it ended.
ACTIVITY_GROUPS = (("Failed", lambda w: w.status == "failed" and not w.superseded),
                   ("Running", lambda w: w.status == "running"),
                   ("Queued", lambda w: w.status == "queued"),
                   ("Finished", lambda w: not w.active
                    and (w.status != "failed" or w.superseded)))


@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request):
    works = activity.everything(db)
    # Work started before BEQ Forge's filters had their tells names them as their film does.
    untold = {w.media_id for w in works if w.media_id and any(
        e.get("origin") == "designer" and not e.get("tell") for e in w.filters)}
    for media_id in untold:
        f = films.load(db, catalogue, media_id)
        if f:
            f.name_work([w for w in works if w.media_id == media_id])
    groups = [(name, [w for w in works if test(w)]) for name, test in ACTIVITY_GROUPS]
    return tpl.TemplateResponse(request, "activity.html", {"groups": groups, "tab": "activity"})


@app.get("/api/activity")
def api_activity():
    return activity.counts(db)


@app.get("/jobs")
def old_jobs_page():
    return RedirectResponse("/activity", status_code=301)


@app.get("/jobs/{job_id}")
def old_job_page(job_id: int):
    """A job's page from before the wizard: its entry on its film's page."""
    r = db.one("SELECT media_id FROM jobs WHERE id=?", (job_id,))
    if not r:
        raise HTTPException(404)
    to = f"/films/{r['media_id']}#job-{job_id}" if r["media_id"] and \
        db.one("SELECT 1 FROM media WHERE id=?", (r["media_id"],)) else f"/activity#job-{job_id}"
    return RedirectResponse(to, status_code=301)


# -- the Settings page --------------------------------------------------------------------
SETTINGS_NOTICES = {"checking": "Scanning the library folders.",
                    "catalogue": "Refreshing the BEQ Catalogue. New filters show up in a moment."}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, notice: str = "", refused: str = "", path: str = ""):
    scan = scanner.state
    logs = [w for w in activity.everything(db, 40) if w.has_log][:20]
    return tpl.TemplateResponse(request, "settings.html", {
        "tab": "settings", "cutoff": cutoff_default(), "ladder": store.CUTOFFS,
        "cutoff_max": HIGHPASS_MAX_HZ, "saved": notice == "cutoff",
        "clip_folder": str(S.clip_dir), "clip_problem": clips.problem(S),
        "scan": scan, "lib": library_watch.state, "roots": S.media_roots,
        "last_scan": _last_scan(), "next_scan": _next_scan(), "catalogue_at": _catalogue_at(),
        "cat": catalogue, "status_line": library_status(scan, library_watch.state),
        "unknown": unknown_counts(),
        "notice": SETTINGS_NOTICES.get(notice, ""), "logs": logs, "S": S,
        "rejected": dict(zip(("what", "cause"), _refusal_words(refused, path), strict=True),
                         typed=path) if refused in PATH_REFUSALS else None})


@app.post("/settings/cutoff")
def save_cutoff(cutoff_hz: str = Form(""), custom_hz: str = Form("")):
    """The default low-end cutoff: the one chosen from the list, or the custom one when the
    list's Custom entry is chosen."""
    try:
        hz = check_highpass(custom_hz.strip() if cutoff_hz in ("custom", "") else cutoff_hz)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    store.set_setting(db, "cutoff_hz", f"{hz:g}")
    return RedirectResponse("/settings?notice=cutoff#cutoff", status_code=303)


@app.get("/system")
def old_system_page():
    return RedirectResponse("/settings", status_code=301)

# -- the Library ------------------------------------------------------------------------------
# One list of films. The user sorts and filters it, and the app does not decide which film
# comes first. The sort and the filters live in the address and in a cookie, so they are the
# same when the user comes back from a film.
_ACTIVE_WORK = ("EXISTS (SELECT 1 FROM jobs j WHERE j.media_id=m.id AND j.status='{0}') OR "
                "EXISTS (SELECT 1 FROM clips c WHERE c.media_id=m.id AND c.status='{0}')")
# A film that has not been assessed yet has no stored rating, and rates Unknown.
_RATING = "COALESCE(rating, 'unknown')"
_RATING_ORDER = "CASE COALESCE(m.rating, 'unknown') " + " ".join(
    f"WHEN '{k}' THEN {i}" for i, k in enumerate(films.RATINGS)) + " ELSE 9 END"


def _film_rows(failed: list[int]) -> str:
    """The films as the list sorts and filters them: each with what its Activity column
    holds and its rating's place in the order. `failed`: the films whose newest work failed
    (activity.standing_failures), which the header and the film detail page count too. A
    film can have a failure that stands and work under way, so the two are separate columns
    and the Failed filter finds every film the header counts."""
    ids = ",".join(str(int(i)) for i in failed) or "NULL"
    return (f"(SELECT m.*, CASE WHEN {_ACTIVE_WORK.format('running')} THEN 'running' "
            f"WHEN {_ACTIVE_WORK.format('queued')} THEN 'queued' ELSE '' END AS act, "
            f"m.id IN ({ids}) AS failed, {_RATING_ORDER} AS rating_rank FROM media m) f")

# Every column the list sorts by: its heading, its ORDER BY, and how its two directions read.
LIBRARY_SORTS = {
    "title": ("Title", "COALESCE(title, path) COLLATE NOCASE", ("A to Z", "Z to A")),
    "rating": ("Candidate rating", "rating_rank", ("Strongest first", "Weakest first")),
    "filters": ("BEQ filters", "filter_count", ("Fewest first", "Most first")),
    "beq": ("BEQ tracks", "beq_count", ("Fewest first", "Most first")),
    "activity": ("Activity", "CASE WHEN act='running' THEN 0 WHEN act='queued' THEN 1 "
                             "WHEN failed THEN 2 ELSE 3 END", ("Busiest first", "Idle first")),
}
# Every filter on the list: its values, each with its words and its WHERE. The rating is the
# row of chips; the others are the menus beside it.
LIBRARY_FILTERS = {
    "rating": {k: (v, f"{_RATING}='{k}'") for k, v in films.RATINGS.items()},
    "filters": {"some": ("Has filters", "filter_count>0"), "none": ("No filters", "filter_count=0")},
    "beq": {"some": ("Has BEQ tracks", "beq_count>0"), "none": ("No BEQ tracks", "beq_count=0")},
    "activity": {"some": ("Running or queued", "act IN ('running', 'queued')"),
                 "failed": ("Failed", "failed"),
                 "none": ("No activity", "act='' AND NOT failed")},
}
FILTER_HEADINGS = {"rating": "Candidate rating", "filters": "BEQ filters", "beq": "BEQ tracks",
                   "activity": "Activity"}
MENUS = ("filters", "beq", "activity")
PAGE_SIZES = (25, 50, 100, 200)
LIBRARY_COOKIE = "library"
# What the search box matches, each word against any of these.
SEARCHED = ("title", "year", "edition", "path")
# The Library's views before the redesign, as the filters that hold the same films.
OLD_VIEWS = {"all": "", "candidates": "rating=strong&rating=possible&beq=none",
             "maybe": "rating=possible", "beq": "beq=some", "stale": "beq=some",
             "none": "rating=unlikely", "todo": "rating=unlikely", "unchecked": "rating=unknown",
             "needs": "activity=failed", "review": "activity=failed", "errors": "activity=failed",
             "working": "activity=some"}


class LibraryView(NamedTuple):
    q: str
    sort: str
    desc: bool
    chosen: dict[str, list[str]]  # filter -> the values chosen
    per: int = 50

    @classmethod
    def parse(cls, params) -> LibraryView:
        sort = params.get("sort") if params.get("sort") in LIBRARY_SORTS else "title"
        chosen = {k: [v for v in params.getlist(k) if v in values]
                  for k, values in LIBRARY_FILTERS.items()}
        per = int(params["per"]) if (params.get("per") or "").isdigit() and \
            int(params["per"]) in PAGE_SIZES else 50
        return cls((params.get("q") or "").strip(), sort, params.get("dir") == "desc",
                   {k: v for k, v in chosen.items() if v}, per)

    def query(self, **change) -> str:
        """This view's address with something changed."""
        v = self._replace(**change)
        pairs = [("q", v.q)] * bool(v.q) + [("sort", v.sort)] * (v.sort != "title") + \
            [("dir", "desc")] * v.desc + [(k, x) for k, xs in v.chosen.items() for x in xs] + \
            [("per", str(v.per))] * (v.per != 50)
        return urlencode(pairs)

    def with_filter(self, key: str, value: str | None) -> str:
        """This view's address with one filter set to one value, or cleared."""
        chosen = {k: v for k, v in self.chosen.items() if k != key}
        if value:
            chosen[key] = [value]
        return self.query(chosen=chosen)

    def where(self, leave_out: str = "") -> tuple[str, list]:
        """The WHERE of this view; `leave_out` names a filter to ignore, for its own counts."""
        parts, args = ["1=1"], []
        for k, xs in self.chosen.items():
            if k != leave_out:
                parts.append("(" + " OR ".join(LIBRARY_FILTERS[k][x][1] for x in xs) + ")")
        for word in self.q.split():
            parts.append("(" + " OR ".join(f"COALESCE({c}, '') LIKE ?" for c in SEARCHED) + ")")
            args += [f"%{word}%"] * len(SEARCHED)
        return " AND ".join(parts), args

    @property
    def direction(self) -> str:
        return LIBRARY_SORTS[self.sort][2][self.desc]

    @property
    def words(self) -> list[str]:
        """The active sort and filters, as the line above the list says them."""
        by = LIBRARY_SORTS[self.sort][0]
        by = by if by.startswith("BEQ") else by.lower()
        said = [f"Sorted by {by}, {self.direction.lower()}"]
        for k, xs in self.chosen.items():
            said.append(f"{FILTER_HEADINGS[k]}: " + " or ".join(
                LIBRARY_FILTERS[k][x][0] for x in xs))
        if self.q:
            said.append(f"Search: “{self.q}”")
        return said

    @property
    def narrowed(self) -> bool:
        return bool(self.chosen or self.q or self.sort != "title" or self.desc)


def _film_names(ids: list[int]) -> list[str]:
    if not ids:
        return []
    rows = db.q(f"SELECT title, year, path FROM media WHERE id IN ({','.join('?' * len(ids))})",
                ids)
    return [film_name(r["title"], r["year"]) or Path(r["path"]).name for r in rows]


def _row_activity(works: list[activity.Work], failed: activity.Work | None) -> dict:
    """What a Library row says in its Activity column: the work under way, and the failure
    that still stands, with its cause in a few words."""
    live = sorted(works, key=lambda w: (w.status != "running", w.id))
    said = {}
    if live:
        w = live[0]
        more = f" and {len(live) - 1} more" if len(live) > 1 else ""
        said.update(work=w, text=f"{w.what}{more}")
    if failed:
        said.update(failed=failed, failed_text=failed.what, cause=failed.failure.short)
    return said


# The work that a film waits for before a BEQ track is added to it: one being added, a filter
# on its way, and a BEQ clip the user asked for to choose a filter by.
WAITS_FOR = {"encode", "design", "clip"}


def _can_add(r: dict, live: list[activity.Work]) -> bool:
    """Whether a Library row has a tick box: Add BEQ track to selected can start from the
    row. The film has a filter that no BEQ track in it uses yet, and none of the work it
    waits for is under way (WAITS_FOR)."""
    return bool(r["probe_json"]) and r["rating_key"] != "unsupported" \
        and r["filter_count"] > r["beq_count"] and not WAITS_FOR & {w.kind for w in live}


class Step(NamedTuple):
    """A Library row's Next step: its words, and the address they lead to. `post`: the step
    is the fix itself, a button that does it, where a link only opens a page. With no
    address the words say why the film needs no step now."""
    say: str
    to: str = ""
    post: bool = False


# Why a film needs no step now, as its row says it: what it waits for, by the kind of work
# under way, and why nothing is left to add.
WAITS_SAID = {"encode": "Adding one now", "design": "After the new filter",
              "clip": "After the BEQ clip"}
ALL_USED, NO_NEED, CHECK_AGAIN = "Every filter in use", "No filter", "Bass check will retry"


def _row_next(r: dict, live: list[activity.Work], failed: activity.Work | None,
              declined: set[int]) -> Step:
    """A Library row's Next step: the one step its film needs now, or in a few words why it
    needs none, so that a row with no tick box says why. A failure's step is its fix, by
    name. A film with no filter is offered the add flow's Filter step, where BEQ Forge
    designs one, unless its page says one is unlikely to help (film.Film.gap), which the
    rating beside it shows. A film waits while work is under way for it."""
    film = f"/films/{r['id']}"
    busy = {w.kind for w in live}
    if failed and failed.kind not in busy:
        fix = failed.failure.fix
        if fix == "retry":
            return Step(activity.FIXES["retry"], f"/work/{failed.ref}/retry", True)
        if fix == "rescan":
            return Step(activity.FIXES["rescan"], f"{film}/rescan", True)
        return Step("Open the film to see why", f"{film}#result")
    if r["error"]:
        return Step(activity.FIXES["rescan"], f"{film}/rescan", True)
    if not r["probe_json"]:
        return Step("After its first scan")
    if r["rating_key"] == "unsupported":
        return Step("No track can get one")
    waits = next((said for kind, said in WAITS_SAID.items() if kind in busy), "")
    if waits:
        return Step(waits)
    if r["filter_count"] > r["beq_count"]:
        return Step("Add another BEQ track" if r["beq_count"] else "Add BEQ track",
                    f"{film}/add")
    if r["filter_count"]:
        return Step(ALL_USED)
    needless = (json.loads(r["assess_json"] or "null") or {}).get("catalogue") == "no_beq_needed"
    note = (r.get("rating_label") or "").partition(": ")[2]
    if note == films.UNREAD:
        return Step(CHECK_AGAIN)
    if needless or r["rating_key"] == "unlikely" or note == films.QUIET or r["id"] in declined:
        return Step(NO_NEED)
    return Step(films.FORGE_LINK, f"{film}/add?step=filter")


def _file_adds(path: str, title: str | None, year, edition: str | None) -> str:
    """What a film's file name adds to its row: nothing the title, the year or the edition
    beside it already says."""
    note = file_note(path, title, year)
    if edition and note.startswith(edition):
        note = note[len(edition):].strip(" \u00b7")
    return note


def _chips(view: LibraryView, film_rows: str) -> list[dict]:
    """The rating chips, each with how many films match it and every other filter."""
    where, args = view.where(leave_out="rating")
    n = {r["rating"]: r["n"] for r in db.q(
        f"SELECT {_RATING} rating, COUNT(*) n FROM {film_rows} "
        f"WHERE {where} GROUP BY 1", args)}
    on = view.chosen.get("rating", [])
    chips = [{"label": "All", "count": sum(n.values()), "on": not on,
              "href": view.with_filter("rating", None)}]
    return chips + [{"label": label, "count": n.get(key, 0), "on": key in on, "key": key,
                     "href": view.with_filter("rating", key)}
                    for key, (label, _) in LIBRARY_FILTERS["rating"].items()]


def _library_view(request: Request) -> tuple[LibraryView, bool]:
    """The view the address asks for, or the one the user left; and whether to forget it."""
    params = request.query_params
    clear = "clear" in params
    remembered = request.cookies.get(LIBRARY_COOKIE, "") if not clear else ""
    own = {k for k in params if k not in ("page", "added", "skipped", "clear", "partial")}
    if not own and remembered:
        from starlette.datastructures import QueryParams
        params = QueryParams(remembered)
    return LibraryView.parse(params), clear


@app.get("/", response_class=HTMLResponse)
def library(request: Request, page: int = 1, partial: bool = False):
    params = request.query_params
    if params.get("show") in OLD_VIEWS:
        to = OLD_VIEWS[params["show"]]
        if params.get("q"):
            to = "&".join(filter(None, (to, urlencode({"q": params["q"]}))))
        return RedirectResponse(f"/?{to}" if to else "/?clear=1", status_code=301)
    view, _ = _library_view(request)
    where, args = view.where()
    failed = activity.standing_failures(db)
    film_rows = _film_rows(list(failed))
    total = db.one(f"SELECT COUNT(*) n FROM {film_rows} WHERE {where}", args)["n"]
    pages = max(1, -(-total // view.per))
    page = min(max(1, page), pages)
    order = f"{LIBRARY_SORTS[view.sort][1]} {'DESC' if view.desc else 'ASC'}, " \
            f"COALESCE(title, path) COLLATE NOCASE"
    rows = [dict(r) for r in db.q(
        f"SELECT * FROM {film_rows} WHERE {where} ORDER BY {order} "
        f"LIMIT {view.per} OFFSET {(page - 1) * view.per}", args)]
    works: dict[int, list[activity.Work]] = {}
    for w in activity.active_for(db, [r["id"] for r in rows]):
        works.setdefault(w.media_id, []).append(w)
    declined = activity.declined(db, [r["id"] for r in rows if not r["filter_count"]])
    for r in rows:
        live = works.get(r["id"], [])
        r["name"] = film_name(r["title"], r["year"]) or Path(r["path"]).name
        r["file"] = _file_adds(r["path"], r["title"], r["year"], r["edition"])
        r["activity"] = _row_activity(live, failed.get(r["id"]))
        r["rating_key"], r["rating_label"] = films.row_rating(r)
        r["rating_note"] = films.row_bass(r["rating_key"], r["rolloff_json"], r["probe_json"])
        r["can_add"] = _can_add(r, live)
        r["next"] = _row_next(r, live, failed.get(r["id"]), declined)
    # A file note that every row shares tells no film from another, so the list leaves it out.
    if len(rows) > 1 and len({r["file"] for r in rows}) == 1:
        for r in rows:
            r["file"] = ""
    asked = request.query_params
    context = {
        "rows": rows, "view": view, "sorts": LIBRARY_SORTS, "filters": LIBRARY_FILTERS,
        "menus": MENUS, "headings": FILTER_HEADINGS, "chips": _chips(view, film_rows),
        "page_sizes": PAGE_SIZES, "total": total, "page": page, "pages": pages,
        "first_row": (page - 1) * view.per + 1 if total else 0,
        "last_row": min(page * view.per, total),
        "count": db.one("SELECT COUNT(*) n FROM media")["n"],
        "added": len([x for x in asked.get("added", "").split(",") if x.isdigit() and x != "0"]),
        "asked_add": "added" in asked,
        "skipped": _film_names([int(x) for x in asked.get("skipped", "").split(",") if x.isdigit()]),
        "tab": "library",
        "status_line": library_status(scanner.state, library_watch.state, bass=False)}
    if partial:  # the search box's live update: the chips and the results, nothing else
        response = JSONResponse({
            "chips": tpl.get_template("_library_chips.html").render(context),
            "results": tpl.get_template("_library_results.html").render(context),
            "query": view.query()})
    else:
        response = tpl.TemplateResponse(request, "library.html", context)
    if view.narrowed or view.per != 50:
        response.set_cookie(LIBRARY_COOKIE, view.query(), max_age=365 * 86400, samesite="lax")
    else:
        response.delete_cookie(LIBRARY_COOKIE)
    return response


def _bulk_choice(f: films.Film) -> tuple[wizard.Draft, str]:
    """What Add BEQ track to selected uses for a film: its recommended track, the first
    filter made for that track, and the default options. And why it skips the film, when it
    does: a design needs the user's choice afterwards, so BEQ Forge is not started here."""
    d = wizard.Draft(output="add")
    d.resolve(f, cutoff_default(), S.beq_default_flag)
    why = ("no audio track in it can get a BEQ track" if f.default_track is None else
           "no filter was made for its recommended track" if f.filter(d.digest) is None else
           "a BEQ track is already being added to it" if f.activity("encode") else "")
    return d, why


@app.get("/api/library/plan")
def bulk_plan(film: list[int] = Query(default=[])):
    """What Add BEQ track to selected would do for these films, for the Library to show
    before the user starts it."""
    plan = []
    for f in filter(None, (films.load(db, catalogue, i) for i in film[:PAGE_ROWS])):
        d, why = _bulk_choice(f)
        plan.append({"id": f.id, "film": f.name, "why": why,
                     "track": "" if why else f.track(d.track_id).name,
                     "filter": "" if why else f.filter(d.digest).name,
                     "cutoff": cutoff_words(d.cutoff), "keeps": len(f.beq_tracks)})
    return {"films": plan}


@app.post("/library/add")
async def add_to_selected(request: Request):
    """Add a BEQ track to each selected film with the defaults (_bulk_choice), and name the
    films it skipped."""
    form = await request.form()
    added, skipped = [], []
    for raw in dict.fromkeys(form.getlist("film")):
        f = films.load(db, catalogue, int(raw)) if raw.isdigit() else None
        if f is None:
            continue
        d, why = _bulk_choice(f)
        if why:
            skipped.append(f.id)
            continue
        try:
            picked = _chosen_filters(f, d)[0]
            params = {"phase": "encode", "entry": picked.entry, **_common(f, d),
                      "replace_existing_beq": False, "chosen_at": time.time()}
        except HTTPException:
            skipped.append(f.id)
            continue
        if db.create_job(f.id, f.path, params) is None:
            skipped.append(f.id)
        else:
            added.append(f.id)
    jobs.kick()
    back = form.get("back") or ""
    extra = urlencode({"added": ",".join(map(str, added)) or "0",
                       **({"skipped": ",".join(map(str, skipped))} if skipped else {})})
    return RedirectResponse(f"/?{'&'.join(filter(None, (back, extra)))}", status_code=303)
