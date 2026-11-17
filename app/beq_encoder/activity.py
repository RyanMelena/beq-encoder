"""Slow work as people see it: an activity while it is queued or running, a history entry
once it has ended. A job or a clip is how the app does the work. The screens show what the
work is for, how far along it is and how it came out."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from .db import DB
from .media import ProbeResult
from .words import (TERMS, cutoff_words, film_name, filter_name, fmt_clock, fmt_db, fmt_dur,
                    fmt_time, level_sentence, plain, track_name)

# Where each stage the pipeline reports starts and ends within its step, in percent of the
# step. The shares are rough: stage durations are not stored, and they vary with the track's
# format. They let the bar and its figure cover the whole step, where the stored `percent`
# starts again for every stage.
JOB_SPANS = {
    "prepare": ((re.compile(r"^(Starting|Checking source)"), 0, 2),
                (re.compile(r"^Matching the BEQ"), 2, 5),
                (re.compile(r"^Analysis"), 5, 60),
                (re.compile(r"^Checking the designer"), 60, 62),
                (re.compile(r"^Designing"), 62, 85),
                (re.compile(r"^Estimating"), 85, 100)),
    "design": ((re.compile(r"^(Starting|Checking source)"), 0, 2),
               (re.compile(r"^Analysis"), 2, 55),
               (re.compile(r"^Checking the designer"), 55, 57),
               (re.compile(r"^Designing"), 57, 85),
               (re.compile(r"^Estimating"), 85, 100)),
    "measure": ((re.compile(r"^(Starting|Checking source)"), 0, 2),
                (re.compile(r"^Analysis"), 2, 70),
                (re.compile(r"^Estimating"), 70, 97),
                (re.compile(r"^Finding"), 97, 100)),
    "encode": ((re.compile(r"^(Starting|Checking source)"), 0, 1),
               (re.compile(r"^Reading "), 1, 11),
               (re.compile(r"^Mapping "), 11, 13),
               (re.compile(r"^Decoding "), 13, 21),
               (re.compile(r"^BEQ filter: measuring"), 21, 28),
               (re.compile(r"^BEQ filter"), 28, 36),
               (re.compile(r"^Encoding "), 36, 71),
               (re.compile(r"^Checking: "), 71, 77),
               (re.compile(r"^Checking the TrueHD Atmos encode"), 77, 83),
               (re.compile(r"^Verifying the "), 71, 83),
               (re.compile(r"^Charting"), 83, 89),
               (re.compile(r"^Remuxing"), 89, 96),
               (re.compile(r"^Verifying$"), 96, 97),
               (re.compile(r"^Checking A/V sync"), 97, 99),
               (re.compile(r"^Embedding"), 99, 100)),
    "clip": ((re.compile(r"^(Starting|Checking source)"), 0, 2),
             (re.compile(r"^Analysis"), 2, 20),
             (re.compile(r"^Estimating"), 20, 23),
             (re.compile(r"^Finding"), 23, 25),
             (re.compile(r"^Reading the clip"), 25, 35),
             (re.compile(r"^Cutting"), 35, 40),
             (re.compile(r"^Writing the clip"), 88, 96),
             (re.compile(r"^Checking the clip"), 96, 100)),
}
# The encoder reports its own steps, and its percentage starts again for each.
DEE_STEP_RE = re.compile(r"\(DEE step (\d+)/(\d+)")
# A clip encodes its BEQ tracks one after another, between cutting and writing, and names
# each one's stages "BEQ track 1 of 2: ...". A BEQ track's own stages are the encode's, from
# decoding to checking.
CLIP_PART_RE = re.compile(r"^BEQ track (\d+) of (\d+): (.*)$")
CLIP_PARTS = (40, 88)
ENCODE_PART = (13, 83)


def job_progress(phase: str, stage: str, percent: float | None) -> float | None:
    """How far through its step a running job is, in percent; None for a stage the step's
    spans do not know."""
    span = next(((lo, hi) for pattern, lo, hi in JOB_SPANS.get(phase, ()) if pattern.match(stage)),
                None)
    if span is None:
        return None
    within = min(100.0, max(0.0, percent or 0.0)) / 100
    dee = DEE_STEP_RE.search(stage)
    if dee and int(dee[2]):
        within = (min(int(dee[1]), int(dee[2])) - 1 + within) / int(dee[2])
    return span[0] + (span[1] - span[0]) * within


def clip_progress(stage: str, percent: float | None) -> float | None:
    """How far through a running clip is, in percent; None for a stage it does not know."""
    part = CLIP_PART_RE.match(stage)
    if not part:
        return job_progress("clip", stage, percent)
    n = max(1, int(part[2]))
    at = job_progress("encode", part[3], percent)
    lo, hi = ENCODE_PART
    within = 0.0 if at is None else min(1.0, max(0.0, (at - lo) / (hi - lo)))
    done = (min(int(part[1]), n) - 1 + within) / n
    return CLIP_PARTS[0] + (CLIP_PARTS[1] - CLIP_PARTS[0]) * done


def time_left(progress: float | None, usual_s: float | None, elapsed: float | None) -> str:
    """A running job's time left. Where this kind of work has finished here before, it is the
    share of that usual time still to do: it moves only with the progress, so every page
    that shows the job says the same. Else it comes from how long the part done so far took.
    Rough, as the progress is, so it waits for enough of the step to judge by and is rounded."""
    if progress is None or not 10 <= progress < 100:
        return ""
    if usual_s:
        left = usual_s * (100 - progress) / 100
    elif elapsed and elapsed >= 60:
        left = elapsed * (100 - progress) / progress
    else:
        return ""
    unit = 300 if left >= 600 else 60
    return f"roughly {fmt_clock(max(1, round(left / unit)) * unit)} left"


# Every status a row can hold: its words and its symbol. No status waits for the user.
STATUS = {
    "queued": ("Queued", "⋯"),
    "running": ("Running", "↻"),
    "done": ("Finished", "✓"),
    "failed": ("Failed", "✕"),
    "cancelled": ("Cancelled", "■"),
    # how jobs ended before the wizard existed
    "awaiting": ("Finished", "✓"),
    "no_beq": ("Finished", "✓"),
    "unsupported": ("Finished", "✓"),
}
ACTIVE = ("queued", "running")
# Where a piece of work stands, as its badge says it on every page: its words, its symbol and
# its tone. Work that has ended says how it ended, so only a result the user asked for and
# got reads as a success.
OUTCOMES = {
    "queued": ("Queued", "⋯", "prog"),
    "running": ("Running", "↻", "prog"),
    "added": ("BEQ track added", "✓", "good"),
    "designed": ("Filter designed", "✓", "good"),
    "clip": ("BEQ clip made", "✓", "good"),
    "nothing": ("Nothing to restore", "○", "plain"),
    "measured": ("Measured", "✓", "plain"),
    "found": ("Filters found", "✓", "plain"),
    # The cause is the one the candidate rating Not supported names, so it has that name.
    "unsupported": ("Not supported", "⊘", "plain"),
    "cancelled": ("Cancelled", "■", "plain"),
    "failed": ("Failed", "✕", "bad"),
}
# How a job that ended without failing came out, by its kind.
DONE = {"encode": "added", "design": "designed", "measure": "measured", "prepare": "found"}

# What each kind of work is called while it runs.
KINDS = {
    "encode": "Adding a BEQ track",
    "design": "Designing a filter",
    "measure": "Measuring level changes",
    "clip": "Making a BEQ clip",
    "prepare": "Designing a filter",  # jobs from before the wizard, which asked BEQ Forge too
}
# How long each kind takes when nothing here has finished one yet.
USUALLY = {
    "encode": "several minutes, longer for a long film",
    "design": "a few minutes",
    "measure": "a few minutes",
    "clip": "about a minute",
}

# A failed job's stored message, matched on the pipeline's own wording: what went wrong in one
# sentence, the same in a few words for a Library row, the fix most likely to help ("retry",
# "rescan", or None when nothing here can), what to do when that fix fails too, and whether
# trying again can help. The advice is (words before, what its link leads to, words after),
# so that advice to change something is a link to where it changes (_failure).
ONE_OFF = "Trying again can work when the fault was a one-off."
FAILURES = (
    (re.compile(r"Lossless check failed|not the lossless BEQ|does not carry the BEQ-filtered"),
     "The app compares each new BEQ track with the original audio after the filter. This one "
     "differed, so the app did not add it.",
     "The new track failed its check", "retry",
     ("If it fails the same way again, ", "choices", " and start again."), ONE_OFF),
    (re.compile(r"out of sync"),
     "The new track did not stay in time with the original track.",
     "The new track went out of time", "retry",
     ("If it fails the same way again, ", "choices", " and start again."), ONE_OFF),
    (re.compile(r"Channel order differs"),
     "The new track's channels were not in the same order as the original track's.",
     "The channel order changed", "retry", None, ONE_OFF),
    (re.compile(r"Source file not found|not in the file any more|is not the file|changed while"),
     "The film's file changed or went missing while the app worked.",
     "The file changed or went missing", "rescan",
     ("After the rescan, start again from Add BEQ track.", "", ""), ""),
    (re.compile(r"^Not supported"), "This audio track can't get a BEQ track.",
     "The audio track can't get a BEQ track", None, ("", "track", "."),
     "Trying again can't help."),
    (re.compile(r"Interrupted by restart"),
     "BEQ Encoder restarted while this was running.", "BEQ Encoder restarted", "retry", None,
     "Trying again starts the work from the beginning."),
    (re.compile(r"[Dd]esigner|beqforge"), "BEQ Forge did not answer.",
     "BEQ Forge did not answer", "retry", ("", "settings", " says whether it is."),
     "Trying again works once BEQ Forge is available."),
    (re.compile(r"exited with code|encode failed|Encoder finished|ffmpeg decoded|truehdd"
                r"|[Cc]avern|Could not read the"),
     "The app could not read or write the film's audio, so it stopped.",
     "The audio could not be read or written", "retry",
     ("First check that ", "file", " plays, and that the app's disk has free space."),
     "Trying again works once the cause is gone."),
    (re.compile(r""), "Something unexpected stopped the work.", "Something unexpected stopped it",
     "retry", None, ONE_OFF),
)
FIXES = {"retry": "Try again", "rescan": "Rescan file"}
# What a failure or a cancel left alone, by kind.
KEPT = {"clip": "No clip was made, and the film file was not changed.",
        "design": "No filter was added, and the film file was not changed.",
        "measure": "The film file was not changed."}
FILE_KEPT = "The film file was not changed."


@dataclass(slots=True)
class Failure:
    what: str         # what went wrong, in one sentence
    kept: str         # what the app did not change
    fix: str | None   # the key of FIXES most likely to help
    raw: str          # the stored message, for technical details
    short: str = ""   # what went wrong, in a few words
    again: str = ""   # whether trying again can help
    # What to do when the fix fails too: (words before, a link's words, its address, words after).
    advice: tuple[str, str, str, str] | None = None


# Where the result of work under way will show, by kind. `{page}` is "this page" on the film
# detail page and "the film's page" in the Activity log.
LANDS = {
    "encode": "The new BEQ track will show under Audio tracks on {page}. The film file changes "
              "only when the new track has passed its checks.",
    "design": "The new filter will join Filters on {page}.",
    "clip": "The BEQ clip's file name and download link will show at the top of {page}. The "
            "film file does not change.",
    "measure": "Each level change will fill in under Filters on {page}. The film file does not "
               "change.",
}
# What a finished row's Result says, by outcome, where the work left no figure or file: why
# it ended as its badge says.
ENDED = {
    "nothing": "No roll-off found",
    "unsupported": "The audio track can't get one",
    "cancelled": "Nothing was changed",
}
# How a running activity says it has passed the time its kind of work usually takes here.
# The usual time is rounded to a minute, so a little over it is not late yet.
OVERDUE = "Taking longer than usual"
OVERDUE_FACTOR = 1.1


@dataclass(slots=True)
class Work:
    """One piece of slow work for a film, running or ended: a job or a clip. An activity while
    it is queued or running, and a history entry afterwards."""
    id: int
    kind: str                 # a key of KINDS
    status: str               # a key of STATUS
    media_id: int | None
    film: str
    path: str
    outcome: str = ""         # a key of OUTCOMES
    said: str = ""            # the outcome's words when they carry a count: 2 filters designed
    stage: str = ""           # what a running one is doing, as the pipeline names it
    progress: float | None = None   # percent of the whole work
    eta: str = ""
    usual: str = ""           # how long this kind of work takes here
    overdue: bool = False     # it shows no percentage and has run for longer than that
    queue_note: str = ""
    result: str = ""          # how an ended one came out, in a sentence
    failure: Failure | None = None
    superseded: bool = False  # a failure that newer work for the film has ended since
    track_id: int | None = None
    track: str = ""           # the audio track it reads, by its one name
    cutoff_hz: float = 0.0
    filters: list[dict] = field(default_factory=list)   # the filter entries it used
    created_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    has_log: bool = False
    charts: dict = field(default_factory=dict)          # kind -> address, for a finished encode
    figures: list[tuple[str, str]] = field(default_factory=list)
    clip_file: str = ""
    clip_folder: str = ""
    track_name: str = ""      # the name written into the file, for technical details
    replaced: bool = False
    level: str = ""           # a finished BEQ track's level change
    starts: bool = False      # players start with a finished BEQ track

    @property
    def ref(self) -> str:
        """Its anchor on the film detail page and in the activity log."""
        return f"{'clip' if self.kind == 'clip' else 'job'}-{self.id}"

    @property
    def what(self) -> str:
        return KINDS[self.kind]

    @property
    def label(self) -> str:
        return self.said or OUTCOMES[self.outcome][0]

    @property
    def symbol(self) -> str:
        return OUTCOMES[self.outcome][1]

    @property
    def tone(self) -> str:
        return OUTCOMES[self.outcome][2]

    @property
    def using(self) -> list[tuple[str, str]]:
        """The choices it was started with: what a row shows, and what Try again reuses."""
        if self.kind not in ("encode", "clip"):
            return [("Audio track", self.track)] if self.track else []
        names = [filter_name(e) for e in self.filters]
        return [(k, v) for k, v in (("Audio track", self.track),
                                    ("Filters" if len(names) > 1 else "Filter", ", ".join(names)),
                                    ("Low-end cutoff", cutoff_words(self.cutoff_hz))) if v]

    @property
    def reuses(self) -> str:
        """What Try again starts with, in words: the same track, filter and low-end cutoff."""
        said = ["track" if k == "Audio track" else k[:1].lower() + k[1:]
                for k, _ in self.using]
        if len(said) > 1:
            return f"the same {', '.join(said[:-1])} and {said[-1]}"
        return f"the same {said[0]}" if said else ""

    def wizard_href(self, step: str) -> str:
        """The wizard at a step, with the choices this work was started with, so that the
        user changes one and starts again."""
        if not self.media_id:
            return ""
        clip = self.kind == "clip"
        q = [("step", step)]
        if self.track_id is not None:
            q.append(("track", str(self.track_id)))
        digests = [e["digest"] for e in self.filters if e.get("digest")]
        q += [("filters" if clip else "filter", d) for d in (digests if clip else digests[:1])]
        if self.kind in ("encode", "clip"):
            q.append(("cutoff", f"{self.cutoff_hz:g}"))
        return f"/films/{self.media_id}/{'clip' if clip else 'add'}?{urlencode(q)}#{step}"

    @property
    def brief(self) -> str:
        """The Result of a finished row in the Activity log: the figure the work left, or
        why it ended as its badge says. "" where the badge says it all. A BEQ clip's result
        is its download link, which the row adds."""
        if self.failure:
            return self.failure.short
        if self.outcome == "added":
            starts = ", and players start with it" if self.starts else ""
            return f"Level change {self.level}{starts}" if self.level else ""
        if self.outcome == "measured":
            return self.result.rstrip(".")
        return ENDED.get(self.outcome, "")

    @property
    def hear(self) -> str:
        """How to hear a BEQ track it added."""
        return ("Players start with it." if self.starts
                else "Choose it in your player's audio menu.")

    def lands(self, page: str) -> str:
        """Where its result will show while it is queued or running."""
        return LANDS.get(self.kind, "").format(page=page)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def at(self) -> float | None:
        """When it reached the status it has now."""
        return self.finished_at or self.started_at or self.created_at

    @property
    def when(self) -> str:
        """When it reached where it stands: Since 7 Nov 2025 17:30. Work that has ended
        gives the time alone, because its badge or its heading says how it ended."""
        said = "Since" if self.active else ""
        return f"{said} {fmt_time(self.at)}".strip()

    @property
    def took(self) -> str:
        if self.status == "running":
            return f"{fmt_dur(self.started_at, None)} so far" if self.started_at else ""
        return fmt_dur(self.started_at, self.finished_at) if self.finished_at else ""

    @property
    def progress_label(self) -> str:
        """A running one's progress and time left, the same on every page. The stage it is
        in is the pipeline's business, so it shows under technical details. In a stage with
        no known share of the whole it gives the time so far, and once that has passed the
        usual time it says so, so that a long wait does not read as a stall."""
        takes = f"this usually takes {self.usual}" if self.usual else ""
        so_far = f"{fmt_dur(self.started_at, None)} so far" if self.started_at else ""
        if self.overdue:
            return " · ".join((OVERDUE, so_far, takes.replace("this usually", "it usually"),
                               "still working"))
        if self.progress is None:
            return " · ".join(filter(None, ("Working", so_far, takes)))
        return " · ".join(filter(None, (f"About {self.progress:.0f}% done", self.eta or takes)))

    @property
    def status_line(self) -> str:
        """What stands under its badge while it is queued or running, on every page and in
        every live refresh: a running one's progress, a queued one's place in the queue."""
        if self.status == "running":
            return self.progress_label
        return self.queue_note if self.status == "queued" else ""

    @property
    def base(self) -> str:
        return f"/clips/{self.id}" if self.kind == "clip" else f"/jobs/{self.id}"

    @property
    def href(self) -> str:
        """Where its row leads: its entry on the film detail page."""
        if self.media_id:
            return f"/films/{self.media_id}#{self.ref}"
        return f"/activity#{self.ref}"


def _failure(w: Work, message: str, filters: int | None) -> Failure:
    """`filters`: how many filters the film has, so the advice names only what it can change."""
    what, short, fix, advice, again = next(x[1:] for x in FAILURES if x[0].search(message))
    if advice:
        before, key, after = advice
        choice = "the filter or the low-end cutoff" if (filters or 0) > 1 else \
            "the low-end cutoff"
        words, href = {"choices": (f"change {choice}", w.wizard_href("filter")),
                       "track": ("Choose another audio track", w.wizard_href("track")),
                       "settings": ("System status on the Settings page", "/settings#status"),
                       "file": ("the film's file", f"/films/{w.media_id}#file"
                                if w.media_id else ""),
                       }.get(key, ("", ""))
        # Work whose film is gone has nowhere to change anything.
        advice = (before, words, href, after) if href or not key else None
    return Failure(what, KEPT.get(w.kind, FILE_KEPT), fix, message, short, again, advice)


def _track_said(params: dict, probe: ProbeResult | None) -> str:
    """The audio track a piece of work read, by its one name. Track numbers shift when a BEQ
    track is added, so the track is found by what the work recorded of it."""
    track_id = params.get("track_id")
    pinned = {k: v for k, v in ((params.get("source") or {}).get("track") or {}).items()
              if k != "track_id"}
    if probe:
        originals = [a for a in probe.audio if not a.beq]
        same = [a for a in originals if pinned and all(getattr(a, k, None) == v
                                                       for k, v in pinned.items())]
        found = next((a for a in same if a.track_id == track_id), None) or (
            same[0] if len(same) == 1 else None) or (
            None if pinned else next((a for a in originals if a.track_id == track_id), None))
        if found:
            return track_name(found.track_id, found.label, found.language)
    return f"Track {track_id}" if track_id is not None else ""


def _figures(report: dict) -> list[tuple[str, str]]:
    """A finished BEQ track's loudest peak as page text. The charts print it too small. The
    result's sentence gives the level change, so it is not a row here."""
    if report.get("peak_in_dbfs") is None or report.get("peak_out_dbfs") is None:
        return []
    return [("Loudest peak", f"{report['peak_in_dbfs']:.1f} dBFS in the original track, "
                             f"{report['peak_out_dbfs']:.1f} dBFS in the BEQ track "
                             f"({TERMS['dbfs']})")]


NOTHING = "BEQ Forge found no roll-off to restore, so it designed no filter."


def _job_end(kind: str, status: str, params: dict, result: dict, message: str
             ) -> tuple[str, str, str]:
    """How a job that did not fail ended: its outcome, the outcome's words when they carry a
    count, and the sentence that says more."""
    if status == "cancelled":
        return "cancelled", "", KEPT.get(kind, FILE_KEPT)
    if status == "no_beq":
        return "nothing", "", f"{NOTHING} {FILE_KEPT}"
    if status == "unsupported":
        return "unsupported", "", f"The audio track can't get a BEQ track. {FILE_KEPT}"
    if status == "awaiting":
        return "found", "", "They are in the film's filter list."
    if kind == "encode":
        gain = (result.get("filter_report") or {}).get("gain_db")
        return "added", "", level_sentence(gain)
    if kind == "design":
        d = result.get("design") or {}
        if d.get("declined"):
            return "nothing", "", f"{NOTHING} {FILE_KEPT}"
        n = len(d.get("candidates") or [])
        return ("designed", f"{n} filter{'' if n == 1 else 's'} designed",
                f"{'It is' if n == 1 else 'They are'} in the film's filter list.")
    if kind == "measure":
        n = len(next(iter((result.get("levels") or {}).values()), {}))
        return ("measured", "",
                f"The level change of {n} filter{'' if n == 1 else 's'}, at each low-end "
                f"cutoff.")
    return DONE[kind], "", plain(message)


def _probe(r) -> ProbeResult | None:
    return ProbeResult.from_json(r["probe_json"]) if r["probe_json"] else None


def from_job(r) -> Work:
    """A `jobs` row joined with its film's `title`, `year`, `probe_json` and `filter_count`."""
    params = json.loads(r["params_json"] or "{}")
    result = json.loads(r["result_json"] or "null") or {}
    kind = r["phase"] if r["phase"] in KINDS else "encode"
    status = r["status"] if r["status"] in STATUS else "failed"
    w = Work(
        id=r["id"], kind=kind, status=status, media_id=r["media_id"], path=r["path"],
        film=film_name(r["title"], r["year"]) or Path(r["path"]).name,
        track_id=params.get("track_id"), cutoff_hz=params.get("highpass_hz") or 0.0,
        filters=[params["entry"]] if params.get("entry") else [],
        created_at=r["created_at"], started_at=r["started_at"], finished_at=r["finished_at"],
        has_log=bool(r["log_path"]), track_name=result.get("track_name") or "",
        replaced=bool(result.get("replaced_tracks")), outcome=status,
        track=_track_said(params, _probe(r)),
    )
    if status == "running":
        w.stage = r["stage"] or ""
        w.progress = job_progress(kind, w.stage, r["percent"])
    elif status == "failed":
        w.failure = _failure(w, r["message"] or "", r["filter_count"])
        w.result = f"{w.failure.what} {w.failure.kept}"
    elif status != "queued":
        w.outcome, w.said, w.result = _job_end(kind, status, params, result, r["message"] or "")
    if status == "done" and kind == "encode":
        w.figures = _figures(result.get("filter_report") or {})
        w.level = fmt_db((result.get("filter_report") or {}).get("gain_db"))
        w.starts = bool(params.get("default_flag"))
        w.result = " ".join(filter(None, (w.result, w.hear)))
        if result.get("charts"):
            w.charts = {k: f"/jobs/{w.id}/charts/{k}" for k in ("spectrum", "heatmap")}
    return w


def from_clip(r) -> Work:
    """A `clips` row joined with its film's `title`, `year`, `probe_json` and `filter_count`."""
    params = json.loads(r["params_json"] or "{}")
    result = json.loads(r["result_json"] or "null") or {}
    w = Work(
        id=r["id"], kind="clip", status=r["status"], media_id=r["media_id"], path=r["path"],
        film=film_name(r["title"], r["year"]) or Path(r["path"]).name,
        track_id=params.get("track_id"), cutoff_hz=params.get("highpass_hz") or 0.0,
        filters=[i["entry"] for i in params.get("filters") or []],
        created_at=r["created_at"], started_at=r["started_at"], finished_at=r["finished_at"],
        has_log=bool(r["log_path"]), outcome=r["status"], track=_track_said(params, _probe(r)),
    )
    if w.status == "running":
        w.stage = r["stage"] or ""
        w.progress = clip_progress(w.stage, r["percent"])
    elif w.status == "failed":
        w.failure = _failure(w, r["message"] or "", r["filter_count"])
        w.result = f"{w.failure.what} {w.failure.kept}"
    elif w.status == "cancelled":
        w.result = KEPT["clip"]
    elif w.status == "done":
        path = Path(result.get("path") or "")
        w.clip_file, w.clip_folder = result.get("file") or path.name, str(path.parent)
        w.outcome, w.result = "clip", f"{w.clip_file}, in {w.clip_folder}. {FILE_KEPT}"
    return w


_JOBS = ("SELECT j.*, m.title, m.year, m.probe_json, m.filter_count FROM jobs j "
         "LEFT JOIN media m ON m.id=j.media_id")
_CLIPS = ("SELECT c.*, m.title, m.year, m.probe_json, m.filter_count FROM clips c "
          "LEFT JOIN media m ON m.id=c.media_id")


def _live_notes(db: DB, works: list[Work]) -> list[Work]:
    """What only the whole queue knows of each piece of work: a queued job's place, how long
    a running kind of work takes here and so how long it has left, and whether a failure
    still stands."""
    queued = [r["id"] for r in db.q("SELECT id FROM jobs WHERE status='queued' ORDER BY id")]
    usual: dict[str, tuple[str, float | None]] = {}
    stands = None
    for w in works:
        if w.status == "queued" and w.kind != "clip" and w.id in queued:
            n = queued.index(w.id)
            w.queue_note = "First in the queue" if n == 0 else f"Number {n + 1} in the queue"
        elif w.status == "running" and w.kind in USUALLY:
            if w.kind not in usual:
                usual[w.kind] = takes(db, w.kind), typical_s(db, w.kind)
            w.usual, usual_s = usual[w.kind]
            elapsed = time.time() - w.started_at if w.started_at else None
            w.eta = time_left(w.progress, usual_s, elapsed)
            w.overdue = bool(w.progress is None and usual_s and elapsed
                             and elapsed > usual_s * OVERDUE_FACTOR)
        elif w.status == "failed":
            stands = set(_standing(db)) if stands is None else stands
            w.superseded = w.ref not in stands
    return works


def _standing(db: DB) -> list[str]:
    """The anchor of each failure that still stands: the newest work of its film to end, as
    on the film detail page (film.Film.latest). A cancel or a measurement does not end it."""
    newest: dict[int, tuple[float, str, str]] = {}
    for kind, table, more in (("job", "jobs", "AND phase!='measure'"), ("clip", "clips", "")):
        for r in db.q(
                f"SELECT media_id, id, status, MAX(COALESCE(finished_at, started_at, "
                f"created_at)) AS at FROM {table} WHERE status NOT IN ('queued', 'running', "
                f"'cancelled') {more} AND media_id IN (SELECT id FROM media) GROUP BY media_id"):
            if r["media_id"] not in newest or (r["at"] or 0) > newest[r["media_id"]][0]:
                newest[r["media_id"]] = (r["at"] or 0, f"{kind}-{r['id']}", r["status"])
    return [ref for _, ref, status in newest.values() if status == "failed"]


def standing_failures(db: DB) -> dict[int, Work]:
    """Each film whose newest work failed, with that failure: what its Library row shows."""
    works = [_load(db, ref) for ref in _standing(db)]
    return {w.media_id: w for w in works if w}


def for_film(db: DB, media_id: int, limit: int = 30) -> list[Work]:
    """A film's work, newest first: its jobs and its clips."""
    works = [from_job(r) for r in db.q(
        f"{_JOBS} WHERE j.media_id=? ORDER BY j.id DESC LIMIT ?", (media_id, limit))]
    works += [from_clip(r) for r in db.q(
        f"{_CLIPS} WHERE c.media_id=? ORDER BY c.id DESC LIMIT 5", (media_id,))]
    return sorted(_live_notes(db, works), key=lambda w: w.at or 0, reverse=True)


def everything(db: DB, limit: int = 200) -> list[Work]:
    """The activity log, newest change first."""
    works = [from_job(r) for r in db.q(
        f"{_JOBS} ORDER BY COALESCE(j.finished_at, j.started_at, j.created_at) DESC LIMIT ?",
        (limit,))]
    works += [from_clip(r) for r in db.q(
        f"{_CLIPS} ORDER BY COALESCE(c.finished_at, c.started_at, c.created_at) DESC LIMIT ?",
        (limit,))]
    return sorted(_live_notes(db, works), key=lambda w: w.at or 0, reverse=True)[:limit]


def _load(db: DB, ref: str) -> Work | None:
    kind, _, n = ref.partition("-")
    if kind not in ("job", "clip") or not n.isdigit():
        return None
    r = db.one(f"{_CLIPS if kind == 'clip' else _JOBS} WHERE {kind[0]}.id=?", (int(n),))
    if not r:
        return None
    return from_clip(r) if kind == "clip" else from_job(r)


def get(db: DB, ref: str) -> Work | None:
    """One piece of work by its anchor: job-12 or clip-3."""
    w = _load(db, ref)
    return _live_notes(db, [w])[0] if w else None


def counts(db: DB) -> dict:
    """What the header's Activity link says: how much is running and queued, and how many
    failures still stand."""
    n = db.one(
        "SELECT (SELECT COUNT(*) FROM jobs WHERE status='running') + "
        "(SELECT COUNT(*) FROM clips WHERE status='running') AS running, "
        "(SELECT COUNT(*) FROM jobs WHERE status='queued') + "
        "(SELECT COUNT(*) FROM clips WHERE status='queued') AS queued")
    failed = len(_standing(db))
    said = [f"{n['running']} running"] * bool(n["running"]) + \
           [f"{n['queued']} queued"] * bool(n["queued"]) + [f"{failed} failed"] * bool(failed)
    return {"running": n["running"], "queued": n["queued"], "failed": failed,
            "counts": " · ".join(said), "active": " · ".join(said[:len(said) - bool(failed)]),
            "failed_said": f"{failed} failed" if failed else ""}


def _took(db: DB, kind: str) -> list[int]:
    """How long the last few of a kind of work took here, in whole minutes as seconds,
    shortest first."""
    table, phase = ("clips", "") if kind == "clip" else ("jobs", f"AND phase='{kind}'")
    rows = db.q(f"SELECT finished_at - started_at AS took FROM {table} WHERE status='done' "
                f"{phase} AND started_at IS NOT NULL AND finished_at IS NOT NULL "
                "ORDER BY finished_at DESC LIMIT 5")
    return sorted(max(60, round(r["took"] / 60) * 60) for r in rows)


def typical_s(db: DB, kind: str) -> float | None:
    """How long a kind of work usually takes here, in seconds; None before one has finished."""
    took = _took(db, kind)
    return took[len(took) // 2] if took else None


def takes(db: DB, kind: str) -> str:
    """How long a kind of work takes: from those that last finished here, else the usual."""
    took = _took(db, kind)
    if not took:
        return USUALLY[kind]
    if took[0] == took[-1]:
        return f"about {fmt_clock(took[0])}"
    return f"{fmt_clock(took[0])} to {fmt_clock(took[-1])}"


def declined(db: DB, media_ids: list[int]) -> set[int]:
    """The films among these where BEQ Forge last looked and designed no filter: what
    film.Film.gap calls "declined", for the Library's rows."""
    if not media_ids:
        return set()
    ids = ",".join("?" * len(media_ids))
    last = {r["media_id"]: from_job(r) for r in db.q(
        f"{_JOBS} WHERE j.media_id IN ({ids}) AND j.phase IN ('design', 'prepare') AND "
        f"j.status NOT IN ('queued', 'running', 'cancelled', 'failed') ORDER BY j.id",
        media_ids)}
    return {media_id for media_id, w in last.items() if w.outcome == "nothing"}


def active_for(db: DB, media_ids: list[int]) -> list[Work]:
    """The queued and running work of these films."""
    if not media_ids:
        return []
    ids = ",".join("?" * len(media_ids))
    works = [from_job(r) for r in db.q(
        f"{_JOBS} WHERE j.status IN ('queued', 'running') AND j.media_id IN ({ids})", media_ids)]
    works += [from_clip(r) for r in db.q(
        f"{_CLIPS} WHERE c.status IN ('queued', 'running') AND c.media_id IN ({ids})", media_ids)]
    return _live_notes(db, works)
