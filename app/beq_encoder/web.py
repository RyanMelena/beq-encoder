"""FastAPI app: library, run (prepare) -> review candidates -> approve -> encode, job queue."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from functools import partial
from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote

import numpy as np
from fastapi import FastAPI, Form, HTTPException, Request
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

from dataclasses import fields as dc_fields

from . import __version__, assess, charts, config, designer, support, worker
from .biquad import FilterSpec, response_db
from .catalogue import Catalogue, track_match
from .db import ACTIVE_STATUSES, DB
from .media import AudioTrack, ProbeResult
from .library import Library
from .pipeline import file_changed
from .worker import JobQueue, Scanner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("beq_encoder")

HERE = Path(__file__).parent
S = config.load()
db = DB(S.db_path)
catalogue = Catalogue(S.catalogue_path, S.catalogue_url, S.catalogue_max_age_days)
jobs = JobQueue(S, db, catalogue)
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
    jobs.start()
    library_watch.start()
    yield
    library_watch.stop()
    jobs.stop()


app = FastAPI(title="BEQ Encoder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
tpl = Jinja2Templates(directory=HERE / "templates")
tpl.env.globals.update(
    version=__version__, dee=lambda: S.dee().status(),
    designer_status=lambda: designer_status(),
)


def _fmt_time(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _fmt_clock(seconds: float | None) -> str:
    """Every duration on screen, with its units: 8 s, 8 min 48 s, 1 h 3 min."""
    if seconds is None:
        return ""
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h} h {m} min"
    if m:
        return f"{m} min {s} s" if s else f"{m} min"
    return f"{s} s"


def _fmt_dur(a: float | None, b: float | None) -> str:
    return _fmt_clock((b or time.time()) - a) if a else ""


def _plain(text: str) -> str:
    """Pipeline and support wording is pinned; people read what the tool does."""
    text = text.replace(support.LOSSY_OUT_OF_SCOPE, "BEQ Encoder only makes lossless tracks")
    text = text.replace("The designer found no roll-off to correct: no BEQ needed.",
                        "Measuring the bass found no roll-off to correct, so no BEQ is needed.")
    text = re.sub(r"^Designer declined: \w+(?: \((.*)\))?$",
                  lambda m: m[1] or "The automatic designer did not propose a filter.", text)
    return re.sub(r"(\d+) designer candidate(?:\(s\)|s?)",
                  lambda m: f"{m[1]} designed filter{'' if m[1] == '1' else 's'}", text)


def _fmt_db(v) -> str:
    return "" if v is None else f"{float(v):+.1f} dB"


def film_name(title: str | None, year=None) -> str:
    """A film as every screen writes it: Klaus (2019). Empty when the title is unknown."""
    return f"{title} ({year})" if title and year else title or ""


def _tmdb(tmdb_id) -> str:
    return f"TMDB {tmdb_id}"


FILTER_TYPES = {"LowShelf": "Low shelf", "HighShelf": "High shelf", "PeakingEQ": "Peaking"}


def _band(f: dict) -> dict:
    """One filter band as the filter list and the detail table both show it."""
    return {"type": FILTER_TYPES.get(f["type"], f["type"]), "freq": f"{f['freq']:.1f} Hz",
            "gain": f"{f['gain']:+.1f} dB", "q": f"{f['q']:.2f}", "count": f.get("count") or 1}


# ffmpeg's layout suffixes, as people say them.
LAYOUT_SHAPES = {"side": "side surrounds", "back": "back surrounds", "wide": "wide surrounds",
                 "wide-side": "wide side surrounds", "front": "front centres"}


def _layout_note(track: AudioTrack) -> str:
    """What the layout name adds to the speaker count: 5.1(side) -> side surrounds."""
    shape = (track.layout or "").replace(track.layout_label, "")
    if shape in ("", "stereo", "mono"):
        return ""
    paren = re.search(r"\((.+)\)$", shape)
    return LAYOUT_SHAPES.get(paren[1], paren[1]) if paren else shape


# ISO 639 codes a film's audio tracks commonly carry; anything else shows its code.
LANGUAGES = {
    "en": "English", "eng": "English", "es": "Spanish", "spa": "Spanish", "fr": "French",
    "fre": "French", "fra": "French", "de": "German", "ger": "German", "deu": "German",
    "it": "Italian", "ita": "Italian", "pt": "Portuguese", "por": "Portuguese", "ja": "Japanese",
    "jpn": "Japanese", "ko": "Korean", "kor": "Korean", "zh": "Chinese", "chi": "Chinese",
    "zho": "Chinese", "cmn": "Mandarin", "yue": "Cantonese", "ru": "Russian", "rus": "Russian",
    "nl": "Dutch", "dut": "Dutch", "nld": "Dutch", "pl": "Polish", "pol": "Polish", "sv": "Swedish",
    "swe": "Swedish", "da": "Danish", "dan": "Danish", "no": "Norwegian", "nor": "Norwegian",
    "nb": "Norwegian", "nob": "Norwegian", "fi": "Finnish", "fin": "Finnish", "tr": "Turkish",
    "tur": "Turkish", "hi": "Hindi", "hin": "Hindi", "ar": "Arabic", "ara": "Arabic", "th": "Thai",
    "tha": "Thai", "cs": "Czech", "cze": "Czech", "ces": "Czech", "hu": "Hungarian",
    "hun": "Hungarian", "el": "Greek", "gre": "Greek", "ell": "Greek", "he": "Hebrew",
    "heb": "Hebrew", "uk": "Ukrainian", "ukr": "Ukrainian", "vi": "Vietnamese", "vie": "Vietnamese",
    "id": "Indonesian", "ind": "Indonesian",
}


def _language(code: str | None) -> str:
    """A track's language by name; empty when the file does not say."""
    if not code or code == "und":
        return ""
    return LANGUAGES.get(code.split("-")[0].lower(), code)


DASH = Markup('<span class="muted">\u2014</span>')  # every empty table cell

tpl.env.filters.update(t=_fmt_time, dbs=_fmt_db, clock=_fmt_clock, plain=_plain, tmdb=_tmdb,
                       band=_band, layout_note=_layout_note, language=_language)
tpl.env.globals.update(dur=_fmt_dur, film=film_name, dash=DASH)


JOB_FOR_FILE = "A job for this file is already queued, running or awaiting approval"
FILE_CHANGED = "The file is no longer the one this job was prepared from; run it again"
TARGET_PEAK_RANGE = (-60.0, 0.0)  # dBFS
FIXED_GAIN_RANGE = (-60.0, 12.0)  # dB


def _enqueue(media_id: int | None, path: str, params: dict, prepare: dict | None = None) -> int:
    job_id = db.create_job(media_id, path, params, prepare)
    if job_id is None:
        raise HTTPException(409, JOB_FOR_FILE)
    jobs.kick()
    return job_id


def _level(value: float, bounds: tuple[float, float], what: str) -> float:
    if not (math.isfinite(value) and bounds[0] <= value <= bounds[1]):
        raise HTTPException(400, f"{what} must be between {bounds[0]:g} and {bounds[1]:g}")
    return value


def _under_roots(p: Path) -> bool:
    rp = p.resolve()
    return any(rp.is_relative_to(r.resolve()) for r in S.media_roots)


# Every job status the database can hold: the words and symbol people see for it.
JOB_STATUS = {
    "queued": ("Queued", "\u22ef"),
    "running": ("Running", "\u21bb"),
    "awaiting": ("Awaiting approval", "!"),
    "done": ("Done", "\u2713"),
    "failed": ("Failed", "\u2715"),
    "no_beq": ("No BEQ needed", "\u25cb"),
    "unsupported": ("Not supported", "\u2298"),
    "cancelled": ("Cancelled", "\u25a0"),
}


# A problem with the file itself; a failed job keeps its cross. U+FE0E asks for the text glyph, not an emoji.
FILE_PROBLEM = "\u26a0\ufe0e"

# Every verdict a film can carry: its words, symbol and pill class, the same on every screen.
FILM_STATE = {
    "candidate": ("BEQ candidate", "\u25c6", "ok"),
    "maybe": ("Worth a look", "\u25c7", "maybe"),
    "current": ("BEQ up to date", "\u2713", "ok"),
    "stale": ("Stale BEQ", "\u21ba", "stale"),
    "no_need": (*JOB_STATUS["no_beq"], ""),
    "unsupported": (*JOB_STATUS["unsupported"], "s-unsupported"),
    "not_checked": ("Not checked yet", "?", ""),
    "too_little": ("Too little bass", "~", ""),
    "check_failed": ("Bass check failed", "\u25b3", "warn"),
    "no_audio": ("No audio", FILE_PROBLEM, "bad"),
    "unreadable": ("Can't be read", FILE_PROBLEM, "bad"),
    "scan_failed": ("Scan failed", FILE_PROBLEM, "bad"),
    "not_usable": ("Not usable", FILE_PROBLEM, "bad"),
}


def _pill(cls: str, symbol: str, label: str, attrs: dict) -> Markup:
    extra = Markup("").join(Markup(' {}="{}"').format(k, v) for k, v in attrs.items() if v)
    return Markup('<span class="badge {}" data-sym="{}"{}>{}</span>').format(cls, symbol, extra, label)


def status_badge(status: str, **attrs: str) -> Markup:
    """The job status pill. The live refresh keeps it current with setBadge in base.html."""
    label, symbol = JOB_STATUS[status]
    return _pill(f"s-{status}", symbol, label, attrs)


def state_pill(state: str, **attrs: str) -> Markup:
    """The pill for a film's verdict."""
    label, symbol, cls = FILM_STATE[state]
    return _pill(cls, symbol, label, attrs)


def _answer(state: str, note: str = "", headline: str = "", detail: str = "", hint: str = "") -> dict:
    return {"state": state, "note": note, "headline": headline, "detail": detail, "hint": hint}


_BASS_CHECK_PENDING = "The bass check runs in the background while no jobs are running."


def film_verdict(a: dict | None, ro: dict | None, last_job: str | None = None) -> dict:
    """The one answer for a film without a BEQ track, so its Library row and its own page agree.
    `state` picks the pill, `note` is the Library's sentence, `hint` its tooltip, and `headline`
    and `detail` are what the film page says."""
    if not a:
        return _answer("not_checked", _BASS_CHECK_PENDING, headline="This film hasn't been checked for a BEQ yet")
    best = a.get("best")
    if not best:
        return _answer("not_usable", headline="This file has no usable audio track",
                       detail="Commentary, descriptive and forced tracks can't be used. "
                              "Rescan the file if it has changed.")
    if not best.get("supported"):
        reason = _plain(best.get("reason") or "")
        return _answer("unsupported", "This track can't get a BEQ. Open the film to choose another track.",
                       hint=reason, headline="This film's best track can't get a BEQ",
                       detail=reason)
    track, kind, candidate = best["label"], a.get("catalogue"), a.get("candidate")
    bass = (ro or {}).get("verdict")
    corner = (ro or {}).get("corner_hz")
    below = f" below ~{corner:.0f} Hz" if corner else ""
    n = a.get("catalogue_count") or 0
    hints = []
    if kind == "exact":
        hints.append(f"{n} catalogue entr{'y' if n == 1 else 'ies'} for this track")
    elif kind == "other_track":
        hints.append("The catalogue has BEQs for another track of this film")
    if bass in ("clear", "some"):
        hints.append(f"{ro['slope_db_oct']} dB/octave at {ro['slope_at_hz']} Hz")
    if bass == "clear":
        hints.append(f"plateau {ro['plateau_dbfs']} dBFS")
        hints.append(f"{ro['seconds']} s of audio sampled")
    hint = "; ".join(hints)

    if kind == "no_beq_needed":
        return _answer("no_need", "The catalogue marks this track as not needing one.",
                       "This film doesn't need a BEQ",
                       f"The catalogue marks {track} as not needing one.", hint)
    no_entry = "The catalogue has no entry for this film, but you can design a filter for it."
    if candidate == "catalogue":
        authors = ", ".join(a.get("authors") or [])
        note = f"Catalogue filter by {authors}."
        if bass == "clear":
            note += f" Bass rolls off{below}."
        return _answer("candidate", note, "The catalogue has a filter for this film",
                       f"{n} entr{'y' if n == 1 else 'ies'} for {track}, by {authors}.", hint)
    if last_job == "no_beq":
        if candidate:
            return _answer("maybe", "The last job found nothing to correct, so a filter is optional.",
                           "The last job found nothing for a BEQ to correct",
                           "You can still design a filter.", hint)
        return _answer("no_need", "The bass shows no roll-off." if bass == "none"
                       else "The last job found nothing for a BEQ to correct.",
                       "This film doesn't need a BEQ", "You can still design a filter.", hint)
    if candidate == "rolloff":
        return _answer("candidate", f"Bass rolls off{below}, -{ro['depth15_db']:.0f} dB at 15 Hz.",
                       "The bass looks rolled off, so a BEQ should help",
                       (f"The catalogue only has filters for another track of this film, "
                        f"but you can design one for {track}." if kind == "other_track" else no_entry),
                       hint)
    if candidate == "maybe":
        if kind == "other_track":
            tail = {"none": " This track shows no roll-off.", "some": f" Some roll-off{below}.",
                    "unknown": " Too little bass to judge.", "error": " The bass check failed."}
            return _answer("maybe", "The catalogue only has a filter for another track of this film."
                           + tail.get(bass, ""), "The catalogue only has a filter for another track",
                           f"Its entries are for a different track of this film. You can still "
                           f"design a filter for {track}.", hint)
        return _answer("maybe", f"Some roll-off{below}.", "The bass is partly rolled off, so a BEQ may help",
                       no_entry, hint)
    if bass == "none":
        return _answer("no_need", "The bass shows no roll-off.", "Nothing suggests this film needs a BEQ",
                       "The catalogue has no entry for this film and its bass isn't rolled off. "
                       "You can still design a filter.", hint)
    if bass == "unknown":
        return _answer("too_little", "Not enough bass in the sampled audio to judge.",
                       "There's too little bass to judge this film",
                       "You can still design a filter.", hint)
    if bass == "error":
        return _answer("check_failed", "The bass check could not read this film's audio, so there is "
                       "no roll-off result. You can still run a job.",
                       "The bass check could not read this film's audio",
                       "There is no roll-off result, but you can still run a job.", hint)
    return _answer("not_checked", _BASS_CHECK_PENDING, headline="This film's bass hasn't been checked yet",
                   detail="The catalogue has no entry for this film. The bass check runs in the "
                          "background while no jobs are running; you can design a filter meanwhile.",
                   hint=hint)


def file_note(path: str, title: str | None, year=None) -> str:
    """What a file's name adds to its film's title and year: the edition and the source tag.
    A name that does not start with the title is shown whole."""
    words = re.findall(r"\w+", title or "")
    lead = r"\W*".join(map(re.escape, words))
    if words and year:
        lead += rf"\W*\(?{re.escape(str(year))}\)?"
    name = Path(path).stem
    start = re.match(rf"\W*{lead}", name, re.I) if words else None
    if not start:
        return Path(path).name
    rest = re.sub(r"\{tmdb-\w+\}", "", name[start.end():])
    rest = re.sub(r"\{edition-([^}]+)\}\s*", lambda m: f"{m[1]} \u00b7 ", rest)
    return re.sub(r"[\[\]]", "", rest).strip(" \u00b7-_.")


def beq_track(name: str) -> dict:
    """A BEQ track's name split for the Library: its format, then where the filter came from."""
    m = re.fullmatch(r"(?:BEQ )?(.*?)(?: \((.+)\))?", name, re.S)
    return {"format": m[1], "source": m[2] or ""}


tpl.env.globals.update(status_badge=status_badge, state_pill=state_pill, file_note=file_note,
                       beq_track=beq_track)

# The name of each job step on every screen: nouns, because the status says when it ran.
JOB_PHASE = {"prepare": "Filter selection", "encode": "New BEQ track"}

# Where a job that has ended got to, by status and phase; a status not listed reads as its phase.
JOB_STEP = {
    "queued": {"prepare": "", "encode": ""},
    "done": {"encode": "BEQ track added"},
    "failed": {"prepare": "Stopped at filter selection", "encode": "Stopped at the BEQ track"},
    "cancelled": {"prepare": "Stopped at filter selection", "encode": "Stopped at the BEQ track"},
    "no_beq": {"prepare": "Ended at filter selection"},
    "unsupported": {"prepare": "Ended at filter selection"},
}

# Stages are stored as the worker and pipeline write them; these are the words people read.
STAGE_WORDS = (
    (re.compile(r"^Encoding\b.*\(DEE: (.+)\)$"), r"Encoding the new track (\1)"),
    (re.compile(r"^Encoding\b.*"), "Encoding the new track"),
    (re.compile(r"^Designing \(beqforge.*\)$"), "Designing a filter"),
    (re.compile(r"^Checking the designer$"), "Checking the automatic designer"),
    (re.compile(r"^Remuxing \(mkvmerge\)$"), "Writing the new track into the file"),
    (re.compile(r"^Decoding DD\+ Atmos objects \(Cavern\)$"), "Decoding the DD+ Atmos objects"),
    (re.compile(r"^Reading the (.+) bitstream$"), r"Reading the \1 track"),
    (re.compile(r"^No BEQ$"), JOB_STATUS["no_beq"][0]),
    (re.compile(r"^Discarded$"), JOB_STATUS["cancelled"][0]),
    (re.compile(r"^Superseded$"), "Replaced by a new design"),
)


def _stage_words(stage: str) -> str:
    for pattern, words in STAGE_WORDS:
        if pattern.match(stage):
            return pattern.sub(words, stage)
    return stage


def _message_words(job: dict) -> str:
    text = (job["message"] or "").removeprefix(f"{job['status_label']}: ")
    if text.startswith("Approved: "):
        entry = job["params"].get("entry") or {}
        return ("Designed filter approved" if entry.get("origin") == "designer"
                else f"Filter by {text.removeprefix('Approved: ')} approved")
    return _plain(text)


# What a job with nothing stored to say tells the person.
QUIET_DETAIL = {"queued": "Waiting in the queue.",
                "cancelled": "Stopped before it finished. The film file was not changed."}

# The banner's advice line asks for the choice, so the stored message need not.
AWAITING_ASK_RE = re.compile(r"\s*[:;]\s*(?:choose (?:one|a filter)|pick one)\.?$", re.I)


def _progress_note(j: dict) -> str:
    """A queued job's place in the queue. A running job gets no time-left figure: `percent`
    belongs to the current stage and stage start times are not stored, so it would be wrong."""
    if j["status"] != "queued":
        return ""
    ahead = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status='queued' AND id<?", (j["id"],))["n"]
    if ahead:
        return f"Number {ahead + 1} in the queue."
    running = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status='running'")["n"]
    return "Next to start, when the running job finishes." if running else "Next to start."


def _job_dict(r) -> dict:
    d = dict(r)
    d["status_label"], d["status_symbol"] = JOB_STATUS[d["status"]]
    d["params"] = json.loads(d.pop("params_json") or "{}")
    d["stage_label"] = _stage_words(d["stage"] or "")
    # The pill already names the status: show only what the stage and message add to it.
    d["stage_note"] = "" if d["stage_label"] == d["status_label"] else d["stage_label"]
    # A discarded job's stored message still asks the person to choose a filter.
    stale = d["stage"] in ("Discarded", "Superseded")
    d["detail"] = ("" if stale else _message_words(d)) or QUIET_DETAIL.get(d["status"], "")
    plain = _failure_words(d["detail"]) if d["status"] == "failed" else None
    if plain:  # the stored wording stays available behind the plain sentence
        d["raw_detail"], d["detail"] = d["detail"], plain[0]
    if d["status"] == "awaiting" and d["detail"]:
        d["detail"] = f"{AWAITING_ASK_RE.sub('', d['detail']).rstrip(' .')} to choose from."
    d["phase_label"] = JOB_PHASE.get(d["phase"], d["phase"])
    d["step_label"] = JOB_STEP.get(d["status"], {}).get(d["phase"], d["phase_label"])
    d["progress_note"] = _progress_note(d)
    d["result"] = json.loads(d.pop("result_json") or "null")
    d["prepare"] = json.loads(d.pop("prepare_json", None) or "null")
    # Time spent waiting on the person is not time the job took.
    d["elapsed"] = "" if d["status"] == "awaiting" else _fmt_dur(d["started_at"], d["finished_at"])
    when, word = next(((v, k) for k, v in (("Finished", d["finished_at"]), ("Started", d["started_at"]),
                                            ("Queued", d["created_at"])) if v), (None, ""))
    d["when"] = f"{word} {_fmt_time(when)}" if when else ""
    d["name"] = Path(d["path"]).name
    film = (d["prepare"] or {}).get("film") or \
        dict(db.one("SELECT title, year FROM media WHERE id=?", (d["media_id"],)) or {})
    d["film"] = film_name(film.get("title"), film.get("year")) or d["name"]
    entry = d["params"].get("entry") or {}
    d["filter_label"] = entry.get("label") or entry.get("author") or ""
    return d


def _other_usable_tracks(j: dict) -> list[AudioTrack] | None:
    """The film's other tracks a BEQ track can be made from; None when it was never scanned."""
    m = db.one("SELECT probe_json FROM media WHERE id=?", (j["media_id"],)) if j["media_id"] else None
    if not m or not m["probe_json"]:
        return None
    tried = ((j["prepare"] or {}).get("track") or {}).get("track_id")
    return [a for a in ProbeResult.from_json(m["probe_json"]).audio
            if a.pickable and a.track_id != tried and support.plan(a).ok]


# The buttons of each status, in the one order every job page uses. On a finished job the
# first is the primary; a job still going has no next step to promote.
JOB_ACTIONS = {
    "queued": ("film", "cancel"),
    "running": ("film", "cancel"),
    "awaiting": ("film", "design", "cancel"),
    "done": ("film", "design", "retry"),
    "no_beq": ("film", "review", "design", "retry"),
    "unsupported": ("film", "design", "retry"),
    "failed": ("retry", "film", "design"),
    "cancelled": ("retry", "film", "design"),
}


def _job_next(j: dict) -> dict:
    """What a job's page tells the person to do next: the advice under its message and the
    buttons that follow it."""
    prep = j["prepare"] or {}
    status = j["status"]
    others = [c for c in prep.get("catalogue") or [] if c["match"] != "exact"]
    count = f" ({len(others)} for other tracks)" if others else ""
    offered = {
        "film": j["media_id"] and {"href": f"/media/{j['media_id']}", "label": "Open film"},
        "review": prep.get("track") and {"post": "review", "label": f"Choose a filter anyway{count}"},
        "design": prep and not prep.get("design") and {"post": "design", "label": "Design a filter"},
        "retry": {"post": "retry", "label": "Run again"},
        "cancel": {"post": "cancel", "label": "Cancel job", "danger": True},
    }
    if status == "queued":
        advice = "You can leave this page; it starts on its own."
    elif status == "running":
        advice = "You can leave this page; the job carries on."
    elif status == "awaiting":
        advice = ("Next: choose a filter below, check its curve, then press Approve & queue. "
                  "Nothing in the file changes until you approve.")
    elif status == "unsupported":
        usable = _other_usable_tracks(j)
        names = ", ".join(f"{a.label} ({a.language})" for a in usable or [])
        if usable is None:
            alternative = "Open the film to see whether another track can be used."
        elif not usable:
            alternative = "This film has no other track that can be used."
        elif len(usable) == 1:
            alternative = f"Another track of this film can be used: {names}. Open the film and choose it."
        else:
            alternative = f"Other tracks of this film can be used: {names}. Open the film and choose one."
        advice = (f"{alternative} Running the job again or designing a filter only helps "
                  "after the file or BEQ Encoder has changed.")
    elif status == "no_beq":
        advice = "The film was left as it is. You can still choose a filter if you want one."
    elif status == "failed":
        changed = "" if "not changed" in j["detail"] else "The film file was not changed. "
        advice = (f"{changed}Run the job again: some problems do not repeat. If it fails the "
                  "same way, open the film and choose a different filter.")
    elif status == "cancelled":
        advice = "Run the job again whenever you are ready."
    else:
        advice = "The new BEQ track sits next to the original, which is still in the file."
    leads = status not in ("queued", "running", "awaiting")
    shown = [offered[k] for k in JOB_ACTIONS[status] if offered[k]]
    return {"advice": advice,
            "actions": [{**a, "primary": leads and i == 0} for i, a in enumerate(shown)]}


# -- response curves ------------------------------------------------------------------
def response_svg(curves: list[tuple[str, list[dict]]], w: int = 640, h: int = 240,
                 legend: bool = True) -> str:
    """One or more filter cascades' magnitude response, 5-250 Hz, as inline SVG."""
    f = np.geomspace(5, 250, 240)
    data = []
    for label, filters in curves:
        specs = [FilterSpec.from_catalogue(x) for x in filters]
        data.append((label, response_db(specs, f) if specs else np.zeros_like(f)))
    if not data:
        return ""
    allv = np.concatenate([d for _, d in data])
    lo = math.floor(min(-3.0, float(allv.min())) / 5) * 5
    hi = math.ceil(max(3.0, float(allv.max())) / 5) * 5
    pl, pr, pt, pb = 96, 10, 10, 48

    def x(v: float) -> float:
        return pl + (math.log10(v) - math.log10(5)) / (math.log10(250) - math.log10(5)) * (
            w - pl - pr
        )

    def y(v: float) -> float:
        return pt + (hi - v) / (hi - lo) * (h - pt - pb)

    def text(lx: float, ly: float, anchor: str, s: object, dy: int = 0, cls: str = "ax") -> str:
        return (f'<text class="{cls}" x="{lx / w:.2%}" y="{ly / h:.2%}" dy="{dy}" '
                f'text-anchor="{anchor}">{s}</text>')

    # Marks stretch with the chart; labels sit outside the viewBox so they keep their CSS size.
    marks: list[str] = []
    labels: list[str] = []
    for fr in (10, 20, 50, 100, 200):
        marks.append(f'<line class="grid" x1="{x(fr):.1f}" x2="{x(fr):.1f}" y1="{pt}" y2="{h - pb}"/>')
        labels.append(text(x(fr), h - pb, "middle", fr, dy=16))
    step = 5 if hi - lo <= 30 else 10
    for v in range(int(lo), int(hi) + 1, step):
        cls = "zero" if v == 0 else "grid"
        marks.append(f'<line class="{cls}" x1="{pl}" x2="{w - pr}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        labels.append(text(pl - 6, y(v), "end", f"{v:+d}" if v else "0", dy=4))
    for i, (_label, d) in enumerate(data):
        pts = " ".join(f"{x(a):.1f},{y(b):.1f}" for a, b in zip(f, d, strict=True))
        marks.append(f'<polyline class="curve c{i % 8}" points="{pts}"/>')
    if legend and len(data) > 1:
        for i, (label, _) in enumerate(data):
            ly = pt + 14 + 16 * i
            marks.append(f'<line class="curve c{i % 8}" x1="{w - 30}" x2="{w - 10}" '
                         f'y1="{ly - 4}" y2="{ly - 4}"/>')
            labels.append(text(w - 36, ly, "end", _esc(label[:28])))
    labels.append(text((pl + w - pr) / 2, h, "middle", "Frequency (Hz)", dy=-4, cls="ax ax-title"))
    labels.append(text(0, (pt + h - pb) / 2, "middle", "Gain (dB)", dy=4, cls="ax ax-title ax-y"))
    return (f'<svg class="resp" role="img" aria-label="Filter response: gain in dB against frequency in Hz">'
            f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none">{"".join(marks)}</svg>'
            f'{"".join(labels)}</svg>')


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# -- choices on an awaiting job ----------------------------------------------------------
CHOICE_TAGS = {"catalogue": "Catalogue", "designer": "Automatic design", "related": "Catalogue",
               "other": "Catalogue", "rejected": "Rejected design", "search": "Catalogue"}


def _choices(job: dict, q: str = "") -> list[dict]:
    """Everything the person may approve: same-track catalogue entries first, then designer
    candidates, then other catalogue entries for the film, then search results."""
    prep = job["prepare"] or {}
    tr = prep.get("track") or {}
    out: list[dict] = []
    seen: set[str] = set()

    def add(entry: dict, group: str, note: str = "") -> None:
        if entry["digest"] in seen:
            return
        seen.add(entry["digest"])
        out.append({"entry": entry, "group": group, "tag": CHOICE_TAGS[group], "note": note,
                    "selectable": bool(entry.get("filters")) and not entry.get("rejected")})

    for c in prep.get("catalogue", []):
        if c["match"] == "exact":
            add(c["entry"], "catalogue", "this track")
    for e in ((prep.get("design") or {}).get("candidates") or []):
        add(e, "designer")
    for c in prep.get("catalogue", []):
        if c["match"] == "related":
            add(c["entry"], "related", "probably a different version of this mix")
        elif c["match"] == "other":
            add(c["entry"], "other", "another track of this film")
    for e in ((prep.get("design") or {}).get("rejected") or []):
        add(e, "rejected", "for review only")
    if q:
        for en in catalogue.search(q, 30):
            m = track_match(en.audio_types, tr.get("kind", ""), tr.get("layout_label", ""))
            add(en.to_dict(), "search", "this track" if m == "exact" else "search result")
    return out


LABELS = {"flac": "FLAC 24-bit (test only: not a bitstream)"}


def _audio_track(d: dict) -> AudioTrack:
    known = {f.name for f in dc_fields(AudioTrack)}
    return AudioTrack(**{k: v for k, v in d.items() if k in known})


def _output_formats(track: dict) -> list[tuple[str, str]]:
    """(value, label) of the outputs offered for the prepared track."""
    t = _audio_track(track)
    p = support.plan(t)
    dee_ok = S.dee().status()["ok"]
    out = []
    for fmt in support.formats(t, S.allow_test_formats):
        if fmt == "truehd":
            if dee_ok:
                how = {"damf": "every Atmos object kept",
                       "joc": "Atmos objects decoded from DD+"}.get(p.route, "lossless")
                out.append(("truehd", f"{p.label} ({how})"))
        elif fmt == "flac":
            out.append(("flac", LABELS["flac"]))
    return out


def _existing_beq_notes(m, probe: ProbeResult | None) -> list[str]:
    """What a later run can tell from a designed BEQ track's tags (assess.py decides)."""
    return [f"“{name}” came from the automatic designer; {why}" for name, why in
            assess.designed_staleness(probe, assess.film_of(m), catalogue, designer_status())]


# -- error pages ---------------------------------------------------------------
class PathRejected(HTTPException):
    """A refused Add file / Scan folder path: the Library shows it at the field it came from."""

    def __init__(self, status_code: int, detail: str, typed: str, view: tuple[str, str],
                 sentence: str = ""):
        super().__init__(status_code, detail)
        self.typed = typed
        self.view = view  # (show, q) of the list the form was on
        self.sentence = sentence or detail


PATH_FORMS = {"/media/add": ("Could not add that file", "Add file"),
              "/scan": ("Could not scan that folder", "Scan folder")}
ACTION_HEADINGS = {"/run": "Could not start this job", "/approve": "Could not approve this filter",
                   "/design": "Could not design a filter", "/review": "Could not reopen this job",
                   "/retry": "Could not retry this job"}
MISSING = {
    "media": ("Film not found", "It may have been removed from the Library, or the link is out "
              "of date.", "Find the film in the Library."),
    "jobs": ("Job not found", "It may have been removed, or the link is out of date.",
             "Jobs lists every job. A film's page shows its latest job."),
    "page": ("Page not found", "The address may be mistyped, or the link is out of date.",
             "Start again from the Library."),
}


def _error_view(method: str, path: str, status: int, detail: str | None) -> dict:
    """What the HTML error page says: heading, message, next step, links (primary first)."""
    library, all_jobs = ("Go to Library", "/"), ("Go to Jobs", "/jobs")
    said = f"{detail.rstrip('.')}." if detail and detail != HTTPStatus(status).phrase else None
    if method == "POST" and path in PATH_FORMS:
        heading, form = PATH_FORMS[path]
        return {"heading": heading, "message": said or "No path was entered.",
                "next_step": f"Enter a path, then try again from {form} in the Library.",
                "links": [library]}
    item = re.match(r"/(media|jobs)/(\d+)(/[a-z]+)?", path)
    if said or (method == "POST" and status == 422):
        noun = {"media": "film", "jobs": "job"}[item[1]] if item else None
        return {"heading": ACTION_HEADINGS.get(item[3] if item else "", "Something went wrong"),
                "message": said or "A required field was empty or not valid.",
                "next_step": f"See where things stand on this {noun}, then try again."
                             if noun else "Go back and try again.",
                "links": ([(f"Open the {noun}", f"/{item[1]}/{item[2]}")] if noun else [])
                         + [library, all_jobs]}
    kind = item[1] if item and not item[3] else "page"
    heading, message, next_step = MISSING[kind]
    section = ("jobs", "Jobs", "/jobs") if kind == "jobs" else ("library", "Library", "/")
    return {"heading": heading, "message": message, "next_step": next_step, "requested": path,
            "tab": section[0], "crumb": section[1:],
            "links": [all_jobs, library] if kind == "jobs" else [library, all_jobs]}


def _wants_html(request: Request) -> bool:
    return (not f"{request.url.path}/".startswith("/api/")
            and "text/html" in request.headers.get("accept", ""))


def _render_error(request: Request, status: int, view: dict, headers) -> HTMLResponse:
    return tpl.TemplateResponse(request, "error.html", view, status_code=status, headers=headers)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if not _wants_html(request):
        return await http_exception_handler(request, exc)
    if isinstance(exc, PathRejected):
        rejected = {"action": request.url.path, "typed": exc.typed,
                    "message": f"{exc.sentence.rstrip('.')}."}
        return await run_in_threadpool(_library_page, request, *exc.view, 1, rejected,
                                       exc.status_code)
    view = _error_view(request.method, request.url.path, exc.status_code, exc.detail)
    return await run_in_threadpool(_render_error, request, exc.status_code, view, exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if not _wants_html(request):
        return await request_validation_exception_handler(request, exc)
    view = _error_view(request.method, request.url.path, 422, None)
    return await run_in_threadpool(_render_error, request, 422, view, None)


# -- pages ---------------------------------------------------------------------
PAGE_ROWS = 200

# How the Library words a file that could not be scanned: text the error contains, then
# its FILM_STATE, what went wrong and what to do next. The last entry matches everything.
SCAN_PROBLEMS = (
    ("no audio", "no_audio", "This file has no audio tracks, so there is no bass to EQ.",
     "Replace it with a copy that has audio, then run Scan library."),
    ("mkvmerge -j", "unreadable",
     "The file could not be read. It is probably damaged or still being copied.",
     "Check that it plays, replace it if it doesn't, then run Scan library."),
    ("", "scan_failed", "The file could not be scanned.",
     "Run Scan library to try again. If it fails again, the original message says why."),
)


def _scan_problem(error: str) -> dict:
    state, what, next_step = next(p[1:] for p in SCAN_PROBLEMS if p[0] in error.lower())
    return {"state": state, "what": what, "next": next_step}


# What a stale BEQ track means, most important first: text its reasons contain, then how much
# it matters and what to do. The last entry matches everything.
STALE_HELP = (
    ("catalogue entry", "Worth redoing.",
     "The filter this track was built from has changed in the catalogue. Review the film and "
     "make a new track; the old one keeps working until you replace it."),
    ("now possible", "Worth redoing.",
     "This film can now have a better track than this one. Review the film and make a new "
     "track; the old one keeps working until you replace it."),
    ("the catalogue now has", "Worth comparing.",
     "The catalogue now has a filter for this track. Review the film to compare it with the "
     "automatic design this track uses."),
    ("made before", "Worth redoing when convenient.",
     "Newer versions of this tool make a more accurate track. The current one still plays."),
    ("designer's answers", "Optional.",
     "The automatic designer would answer differently now. Review the film if you want a "
     "fresh design."),
    ("", "Review when convenient.", "The current track still plays."),
)


def _stale_help(reasons: list[str]) -> dict:
    text = " ".join(reasons).lower()
    verdict, advice = next(h[1:] for h in STALE_HELP if h[0] in text)
    return {"verdict": verdict, "advice": advice}


_LAST_JOB = "(SELECT status FROM jobs j WHERE j.media_id=m.id ORDER BY j.id DESC LIMIT 1)"

# Every Library list: its key in the address, the name on screen, which files it holds. A file
# has a BEQ track, has none, or could not be read, so those three counts add up to All films.
LIBRARY_VIEWS = {
    "candidates": ("BEQ candidates", "has_audio=1 AND has_beq=0 AND candidate IS NOT NULL"),
    "todo": ("Without BEQ", "has_audio=1 AND has_beq=0"),
    "stale": ("Stale BEQ", "has_audio=1 AND has_beq=1 AND stale=1"),
    "review": ("Awaiting approval", f"has_audio=1 AND {_LAST_JOB} = 'awaiting'"),
    "beq": ("Has BEQ", "has_audio=1 AND has_beq=1"),
    "all": ("All films", "has_audio=1 OR error IS NOT NULL"),
    "errors": ("Errors", "error IS NOT NULL"),
}


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def library_status(scan: dict, lib: dict) -> str:
    """The one thing the Library is doing now, for the page and its live refresh."""
    audio = lib["audio"]
    left = f"{_count(audio['todo'], 'film')} left to check."
    if lib["watch_error"]:
        return f"Watching for new files failed: {lib['watch_error']}."
    if scan.get("running"):
        return f"Scanning {scan['done']} of {_count(scan['found'], 'file')}."
    if lib["pending"]:
        return f"{_count(lib['pending'], 'new file')} still copying."
    if not audio["enabled"]:
        return "Bass check off."
    if audio["running"]:
        return f"Checking bass: {audio['current'].split('/')[-1]}. {left}"
    if audio["waiting"]:
        cause = "the scan" if audio["waiting"] == "scan" else "running jobs"
        return f"Bass check waits for {cause}. {left}" if audio["todo"] else f"Bass check waits for {cause}."
    return f"Bass check: {left}" if audio["todo"] else "Bass check up to date."


def _candidate_summary(counts) -> str:
    """How the BEQ candidates divide, as a sentence."""
    groups = ((counts["cand_cat"], "has a catalogue filter", "have a catalogue filter"),
              (counts["cand_ro"], "shows bass roll-off", "show bass roll-off"),
              (counts["cand_maybe"], "is worth a look", "are worth a look"))
    parts = [f"{n} {one if n == 1 else many}" for n, one, many in groups if n]
    return f"{', '.join(parts[:-1])} and {parts[-1]}." if len(parts) > 1 else f"{parts[0]}." if parts else ""


@app.get("/", response_class=HTMLResponse)
def library(request: Request, show: str = "", q: str = "", page: int = 1):
    return _library_page(request, show, q, page)


def _library_page(request: Request, show: str, q: str, page: int, rejected: dict | None = None,
                  status: int = 200) -> HTMLResponse:
    """The Library; `rejected` is a refused path form, shown open with its error at the field."""
    if show not in LIBRARY_VIEWS:  # open on the candidates once there are any
        show = "candidates" if db.one(
            "SELECT 1 FROM media WHERE has_beq=0 AND candidate IN ('catalogue','rolloff')"
            " LIMIT 1") else "todo"
    where = [f"({LIBRARY_VIEWS[show][1]})"]
    order, sorted_by = "title, path", "Sorted by title."
    if show == "candidates":
        sorted_by = "Catalogue matches first, then the deepest bass roll-off."
        order = ("CASE candidate WHEN 'catalogue' THEN 0 WHEN 'rolloff' THEN 1 ELSE 2 END, "
                 "json_extract(rolloff_json, '$.depth15_db') DESC, title, path")
    args: list = []
    if q:
        where.append("(path LIKE ? OR title LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    total = db.one(f"SELECT COUNT(*) n FROM media m WHERE {' AND '.join(where)}", args)["n"]
    pages = max(1, -(-total // PAGE_ROWS))
    page = min(max(1, page), pages)
    rows = [dict(r) for r in db.q(
        f"SELECT m.*, {_LAST_JOB} AS job_status FROM media m WHERE {' AND '.join(where)} ORDER BY {order}"
        f" LIMIT {PAGE_ROWS} OFFSET {(page - 1) * PAGE_ROWS}",
        args,
    )]
    for r in rows:
        r["best"] = json.loads(r.get("best_json") or "null")
        r["beq"] = json.loads(r.get("beq_names") or "[]")
        r["a"] = json.loads(r.get("assess_json") or "null")
        r["ro"] = json.loads(r.get("rolloff_json") or "null")
        r["problem"] = _scan_problem(r["error"]) if r["error"] else None
        r["stale_help"] = _stale_help(r["a"]["reasons"]) if r["a"] and r["a"].get("stale") else None
        r["ro"] = r["ro"] or (r["a"] or {}).get("rolloff")
        r["verdict"] = film_verdict(r["a"], r["ro"], r["job_status"])
    view_counts = ", ".join(f'COALESCE(SUM({sql}), 0) AS "{key}"'
                            for key, (_, sql) in LIBRARY_VIEWS.items())
    counts = db.one(
        f"SELECT COUNT(*) total, {view_counts}, "
        "SUM(has_audio AND has_beq=0 AND candidate='catalogue') cand_cat, "
        "SUM(has_audio AND has_beq=0 AND candidate='rolloff') cand_ro, "
        "SUM(has_audio AND has_beq=0 AND candidate='maybe') cand_maybe, "
        "SUM(has_audio AND has_beq=0 AND rolloff_json IS NOT NULL) bass_checked FROM media m"
    )
    return tpl.TemplateResponse(
        request, "library.html",
        {"rows": rows, "show": show, "q": q, "counts": counts, "scan": scanner.state,
         "views": {key: label for key, (label, _) in LIBRARY_VIEWS.items()},
         "lib": library_watch.state, "status_line": library_status(scanner.state, library_watch.state),
         "sorted_by": sorted_by, "candidate_summary": _candidate_summary(counts), "page": page, "pages": pages, "total": total,
         "rejected": rejected, "cat": catalogue, "roots": S.media_roots,
         "S_allow_test": S.allow_test_formats},
        status_code=status,
    )


# A failed job's message as the film page says it, matched on the pipeline's own wording.
_RETRY = ("Run BEQ again below: some problems do not repeat. If it fails the same way, "
          "choose a different filter, or open the job for the full log.")
FAILURE_WORDS = (
    (re.compile(r"Lossless check failed|not the lossless BEQ|does not carry the BEQ-filtered"
                r"|out of sync|Channel order differs"),
     "The new audio track did not match the film's original audio closely enough, so it was "
     "not added.", f"The film file was not changed. {_RETRY}"),
    (re.compile(r"Source file not found|not in the file any more"),
     "The file or audio track this job was using is no longer there.",
     "Rescan the file, then run BEQ again below."),
    (re.compile(r"exited with code|encode failed|Encoder finished|ffmpeg decoded|truehdd"
                r"|[Cc]avern|Could not read the"),
     "One of the audio tools stopped partway through, so the new track was not added.",
     f"The film file was not changed. {_RETRY}"),
    (re.compile(r"^\w+(Error|Exception): "),
     "Something unexpected stopped this job, so the new track was not added.",
     f"The film file was not changed. {_RETRY}"),
)


def _failure_words(message: str) -> tuple[str, str] | None:
    """The plain (what went wrong, what to do) for a failed job's stored message, if it is known."""
    return next(((what, then) for p, what, then in FAILURE_WORDS if p.search(message)), None)

# How much each assess.py stale reason matters, matched on its opening words:
# (prefix, worth replacing, what the person reads).
STALE_REASONS = (
    ("made before", True, "Made by an older version of BEQ Encoder, so it may play at a "
                          "different level from the original film."),
    ("its catalogue entry", True, "The catalogue filter it was made from has since changed."),
    ("the catalogue now has", False, "The catalogue now has a filter for this track, and this "
                                     "BEQ came from the automatic designer."),
    ("the designer's answers", False, "The automatic designer would now design a different "
                                      "filter for this track."),
)


def _chart_figures(job: dict) -> list[tuple[str, str]]:
    """The before/after charts' key figures as page text; the PNGs print them too small to read."""
    report = job["result"].get("filter_report") or {}
    figures = []
    boost = charts._boost((job["params"].get("entry") or {}).get("filters") or [])
    if boost is not None:
        figures.append(("Largest BEQ change", f"{_fmt_db(boost[0])} at {boost[1]:.0f} Hz"))
    if report.get("gain_db") is not None:
        figures.append(("Level gain on every channel", _fmt_db(report["gain_db"])))
    if report.get("peak_in_dbfs") is not None and report.get("peak_out_dbfs") is not None:
        figures.append(("Sample peak", f"{report['peak_in_dbfs']:.1f} \u2192 "
                                       f"{report['peak_out_dbfs']:.1f} dBFS"))
    return figures


def _failure_notice(job: dict) -> dict:
    """What the film page headlines for a failed last job: what went wrong, then what to do."""
    detail = job.get("raw_detail") or job["detail"]
    unchanged = "" if "not changed" in detail else "The film file was not changed. "
    what, next_step = _failure_words(detail) or (detail, f"{unchanged}{_RETRY}")
    return {"job": job["id"], "what": what, "next": next_step}


def _stale_words(reason: str) -> tuple[bool, str]:
    """(worth replacing, plain words) for one stale reason; anything unlisted counts as worth it."""
    for prefix, matters, words in STALE_REASONS:
        if reason.startswith(prefix):
            return matters, words
    return True, reason[:1].upper() + reason[1:]


@app.get("/media/{media_id}", response_class=HTMLResponse)
def media_detail(request: Request, media_id: int):
    m = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not m:
        raise HTTPException(404)
    probe = ProbeResult.from_json(m["probe_json"]) if m["probe_json"] else None
    best = probe.best if probe else None
    job_rows = db.q("SELECT * FROM jobs WHERE media_id=? ORDER BY id DESC LIMIT 10", (media_id,))
    plans = {a.track_id: support.plan(a) for a in probe.audio} if probe else {}
    job_list = [_job_dict(r) for r in job_rows]
    # the newest finished encode's charts, while its BEQ track is still in the file
    beq_names = {a.name for a in probe.beq_tracks} if probe else set()
    scanned = m["scanned_at"] or 0
    charted = next((j for j in job_list if j["status"] == "done" and j.get("result")
                    and j["result"].get("charts")
                    and (j["result"].get("track_name") in beq_names
                         or (j.get("finished_at") or 0) >= scanned)), None)
    assessment = json.loads(m["assess_json"] or "null")
    stale_matters = False
    for b in (assessment or {}).get("beq", []):
        words = [_stale_words(r) for r in b.get("reasons", [])]
        b["plain_reasons"] = [w for _, w in words]
        stale_matters = stale_matters or any(matters for matters, _ in words)
    active = db.active_job_for_path(m["path"])
    failure = (_failure_notice(job_list[0])
               if probe and job_list and job_list[0]["status"] == "failed" and not active else None)
    return tpl.TemplateResponse(
        request, "media.html",
        {"m": m, "probe": probe, "best": best, "plans": plans,
         "jobs": job_list, "charted": charted, "assessment": assessment,
         "chart_figures": _chart_figures(charted) if charted else [],
         "stale_matters": stale_matters, "failure": failure,
         "rolloff": json.loads(m["rolloff_json"] or "null"),
         "verdict": film_verdict(assessment, json.loads(m["rolloff_json"] or "null"),
                                 job_list[0]["status"] if job_list else None),
         "file_problem": _scan_problem(m["error"]) if m["error"] else None,
         "active": active, "notes": _existing_beq_notes(m, probe)},
    )


@app.post("/media/{media_id}/run")
def run_media(media_id: int, track_id: int = Form(-1), force_designer: bool = Form(False)):
    m = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not m or not m["probe_json"]:
        raise HTTPException(404)
    probe = ProbeResult.from_json(m["probe_json"])
    source = probe.track(track_id) if track_id >= 0 else None
    if track_id >= 0 and source is None:
        raise HTTPException(400, "No such audio track; rescan the file")
    if source is not None and source.beq:
        raise HTTPException(400, "A BEQ track can't be the source of another; pick the original")
    if track_id < 0 and probe.best is None:
        raise HTTPException(400, "No usable audio track in this file")
    params = {"phase": "prepare", "track_id": track_id if track_id >= 0 else None,
              "force_designer": force_designer}
    return RedirectResponse(f"/jobs/{_enqueue(media_id, m['path'], params)}", status_code=303)


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
    return RedirectResponse(f"/media/{media_id}", status_code=303)


@app.post("/media/add")
def add_media(path: str = Form(...), show: str = Form(""), q: str = Form("")):
    p = Path(os.path.normpath(path.strip()))
    if not p.is_file() or p.suffix.lower() != ".mkv":
        raise PathRejected(400, "Not an existing .mkv file", path.strip(), (show, q),
                           "That path is not an existing .mkv file. Check the path, or use Scan "
                           "folder for a whole folder")
    if not _under_roots(p):
        raise PathRejected(400, "File is outside the configured media roots", path.strip(),
                           (show, q), "That file is outside the configured media roots. Move it into a library "
                           "folder, then add it again")
    scanner.start(force=True, files=[p])
    return RedirectResponse(f"/?show=all&q={quote(p.name)}", status_code=303)


def _back_to_list(show: str, q: str) -> RedirectResponse:
    query = f"?show={quote(show)}&q={quote(q)}" if show else ""
    return RedirectResponse(f"/{query}", status_code=303)


@app.post("/scan")
def scan(full: bool = Form(False), folder: str = Form(""), show: str = Form(""),
         q: str = Form("")):
    root = None
    if folder.strip():
        root = Path(os.path.normpath(folder.strip()))
        if not root.is_dir() or not _under_roots(root):
            raise PathRejected(400, "Folder must be an existing folder under the media roots",
                               folder.strip(), (show, q),
                               "Folder must be an existing folder under the media roots. "
                               "Check the path and try again")
    scanner.start(force=full, root=root)
    return _back_to_list(show, q)


@app.post("/catalogue/refresh")
def catalogue_refresh(show: str = Form(""), q: str = Form("")):
    threading.Thread(target=catalogue.refresh, kwargs={"force": True}, daemon=True).start()
    return _back_to_list(show, q)


# The Jobs list reads top down: what waits on the person, what is moving, what has ended.
JOB_GROUPS = (("Needs you", ("awaiting",)),
              ("In progress", ("running", "queued")),
              ("Finished", ("done", "failed", "no_beq", "unsupported", "cancelled")))


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    rows = [_job_dict(r) for r in db.q("SELECT * FROM jobs ORDER BY id DESC LIMIT 200")]
    rows.sort(key=lambda j: j["status"] != "running")  # the one moving comes before the queue
    groups = [(title, [j for j in rows if j["status"] in statuses]) for title, statuses in JOB_GROUPS]
    return tpl.TemplateResponse(request, "jobs.html", {"groups": [g for g in groups if g[1]]})


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: int, pick: str = "", q: str = ""):
    r = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not r:
        raise HTTPException(404)
    j = _job_dict(r)
    choices = _choices(j, q) if j["prepare"] else []
    selectable = [c for c in choices if c["selectable"]]
    chosen = None
    if j["params"].get("entry"):
        chosen = j["params"]["entry"]
    if pick:
        chosen = next((c["entry"] for c in choices if c["entry"]["digest"] == pick), chosen)
    # preselect only an entry made for this track; one for another track or film is
    # never applied without an explicit pick
    default = [c for c in selectable if c["group"] in ("catalogue", "designer")]
    suggested = ""
    if chosen is None and j["status"] == "awaiting" and default:
        chosen = default[0]["entry"]
        suggested = chosen["digest"]
    shown = [c for c in choices if c["group"] in ("catalogue", "designer", "search")][:8]
    overlay = response_svg([(c["entry"]["label"], c["entry"]["filters"]) for c in shown
                            if c["entry"].get("filters")])
    one = response_svg([(chosen.get("label", ""), chosen["filters"])]) if chosen and \
        chosen.get("filters") else ""
    track = (j["prepare"] or {}).get("track") or {}
    return tpl.TemplateResponse(
        request, "job.html",
        {"j": j, "choices": choices, "chosen": chosen, "overlay": overlay, "svg": one, "q": q,
         "track": track, "formats": _output_formats(track) if track else [], "S": S,
         "next": _job_next(j), "suggested": suggested},
    )


@app.post("/jobs/{job_id}/approve")
def job_approve(
    job_id: int,
    digest: str = Form(...),
    q: str = Form(""),
    format: str = Form("truehd"),
    gain_mode: str = Form("auto"),
    target_peak_dbfs: float = Form(-1.0),
    fixed_gain_db: float = Form(0.0),
    default_flag: bool = Form(False),
    replace_existing_beq: bool = Form(False),
):
    r = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not r:
        raise HTTPException(404)
    j = _job_dict(r)
    if j["status"] != "awaiting":
        raise HTTPException(409, f"Job is {j['status_label']}, not awaiting approval")
    choice = next((c for c in _choices(j, q) if c["entry"]["digest"] == digest), None)
    if choice is None:
        raise HTTPException(400, "Unknown filter choice; reload the page")
    if not choice["selectable"]:
        raise HTTPException(400, "That filter can't be used: it has no filter bands, or it was rejected")
    track = j["prepare"]["track"]
    plan = support.plan(_audio_track(track))
    if not plan.ok:
        raise HTTPException(400, f"Not supported: {plan.reason}")
    if format not in {v for v, _ in _output_formats(track)}:
        raise HTTPException(400, f"Output {format} is not available for this track")
    target = _level(target_peak_dbfs, TARGET_PEAK_RANGE, "Target peak (dBFS)")
    fixed = _level(fixed_gain_db, FIXED_GAIN_RANGE, "Fixed gain (dB)") \
        if gain_mode == "fixed" else None
    source = j["prepare"].get("source")
    if file_changed(source, Path(j["path"])):
        raise HTTPException(409, FILE_CHANGED)
    params = {
        "phase": "encode",
        "entry": choice["entry"],
        "track_id": track["track_id"],
        "source": source,
        "format": format,
        "target_peak_dbfs": target,
        "fixed_gain_db": fixed,
        "default_flag": default_flag,
        "replace_existing_beq": replace_existing_beq,
        "approved_at": time.time(),
    }
    if not db.transition(job_id, "awaiting", params_json=json.dumps(params), phase="encode",
                         status="queued", stage="Queued", percent=None, started_at=None,
                         finished_at=None, message=f"Approved: {choice['entry'].get('label')}"):
        raise HTTPException(409, "Job is no longer awaiting approval")
    jobs.kick()
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/design")
def job_design(job_id: int):
    """Ask the designer for this film even though the catalogue had an entry."""
    r = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not r:
        raise HTTPException(404)
    j = _job_dict(r)
    if j["status"] in ("queued", "running"):
        raise HTTPException(409, "Job is still running")
    if j["status"] == "awaiting" and not db.transition(
            job_id, "awaiting", status="cancelled", stage="Superseded", finished_at=time.time()):
        raise HTTPException(409, "Job is no longer awaiting approval")
    track_id = (j["prepare"] or {}).get("track", {}).get("track_id", j["params"].get("track_id"))
    new_id = _enqueue(j["media_id"], j["path"], {"phase": "prepare", "track_id": track_id,
                                                 "force_designer": True})
    return RedirectResponse(f"/jobs/{new_id}", status_code=303)


@app.post("/jobs/{job_id}/review")
def job_review(job_id: int):
    """A "no BEQ" answer is advice: let the person pick a catalogue entry anyway (one
    made for another track of the film, or any entry found by search)."""
    r = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not r:
        raise HTTPException(404)
    j = _job_dict(r)
    if j["status"] != "no_beq" or not (j["prepare"] or {}).get("track"):
        raise HTTPException(409, f"Job is {j['status_label']}; only a “{JOB_STATUS['no_beq'][0]}” result can be reviewed")
    if not db.transition(job_id, "no_beq", status="awaiting", stage="Awaiting approval",
                         finished_at=None,
                         message="No BEQ was recommended; choose a catalogue entry to use anyway"):
        raise HTTPException(409, JOB_FOR_FILE)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/cancel")
def job_cancel(job_id: int):
    if not jobs.cancel(job_id):
        db.transition(job_id, "awaiting", status="cancelled", stage="Discarded",
                      finished_at=time.time())
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/retry")
def job_retry(job_id: int):
    r = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not r:
        raise HTTPException(404)
    params = json.loads(r["params_json"])
    prepare = json.loads(r["prepare_json"] or "null")
    if params.get("phase") == "encode":
        result = json.loads(r["result_json"] or "null") or {}
        params["source"] = result.get("source", params.get("source"))  # a finished job's own output
        if file_changed(params["source"], Path(r["path"])):
            raise HTTPException(409, FILE_CHANGED)
    new_id = _enqueue(r["media_id"], r["path"], params,
                      prepare if params.get("phase") == "encode" else None)
    return RedirectResponse(f"/jobs/{new_id}", status_code=303)


# -- JSON API --------------------------------------------------------------------
@app.get("/api/jobs")
def api_jobs(active: bool = False):
    sql = "SELECT * FROM jobs"
    if active:
        sql += f" WHERE status IN {ACTIVE_STATUSES}"
    return [_job_dict(r) for r in db.q(sql + " ORDER BY id DESC LIMIT 200")]


@app.get("/api/jobs/{job_id}")
def api_job(job_id: int, log_lines: int = 0):
    r = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not r:
        raise HTTPException(404)
    d = _job_dict(r)
    if log_lines and d["log_path"] and Path(d["log_path"]).exists():
        with open(d["log_path"], errors="replace") as fh:
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
                         "status_line": library_status(scanner.state, library_watch.state)})


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
