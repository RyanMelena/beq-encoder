"""A film as the screens show it: a file with audio tracks, filters, a candidate rating,
activities and a history (docs/ux-guidelines.md, "Model a film").

Every page reads this one model, so the Library row, the film detail page and the wizard say
the same thing about a film. Each part is shown as itself. Nothing here folds several parts
into one status."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from . import activity, assess, store, support
from .activity import Work
from .curve import response_svg
from .db import DB
from .media import AudioTrack, ProbeResult
from .words import (TERMS, bass_restored, cutoff_words, file_note, film_name, filter_name,
                    fmt_clock, fmt_day, fmt_db, language, level_estimate, level_sentence,
                    origin, peak_boost, track_name)

# Every candidate rating: its word, in the order the Library sorts them.
RATINGS = {"strong": "Strong", "possible": "Possible", "unlikely": "Unlikely",
           "unknown": "Unknown", "unsupported": "Not supported"}
RATING_SYMBOLS = {"strong": "◆", "possible": "◇", "unlikely": "○",
                  "unknown": "?", "unsupported": "⊘"}
_VERDICT_RATING = {"clear": "strong", "some": "possible", "none": "unlikely"}
# What each rating the bass check gives means for the film, after what the check found.
RATING_MEANS = {"strong": "a BEQ track is likely to help", "possible": "a BEQ track may help",
                "unlikely": "a BEQ track is unlikely to help"}
# The tag on the track the app picks, and what it adds when that track is lossy, so that the
# recommendation and the lossy caveat under the list read as one state.
RECOMMENDED = "Recommended for a BEQ track"
BEST_LOSSY = ": the best track here that can get one, though lossy"

RECENT_S = 86400.0  # how long a finished result stays at the top of the film detail page


# Why a scan could not use a file, matched on the scanner's own wording: what the rating's
# label adds, and a sentence a person can act on.
SCAN_PROBLEMS = (
    (re.compile(r"[Nn]o audio"), "no audio tracks",
     "The last scan found no audio tracks in the file, so there is nothing to make a BEQ "
     "track from."),
    (re.compile(r""), "file can't be read",
     "The app could not read the file as a film. It may be damaged, still copying, or not an "
     ".mkv file."),
)


# Which Unknown a rating is, as its label says it.
NOT_CHECKED, QUIET, UNREAD = "not checked", "too little bass", "bass check failed"


def unknown(error: str | None, scanned: bool, verdict: str | None, has_beq: bool = False
            ) -> tuple[str, str]:
    """Which Unknown a film's rating is: the words its label adds, and the sentence that says
    why. A film not checked and a film that can't be checked both rate Unknown, so the
    label says which (docs/ux-guidelines.md, "Candidate rating"). `has_beq`: the file holds a
    BEQ track, and the bass check reads only films with none (library.py, _audio_next). Both
    read "not checked", because the label is the same fact to its reader, and the sentence
    says whether the check is still to come."""
    if error:
        return next((note, why) for pattern, note, why in SCAN_PROBLEMS if pattern.search(error))
    if not scanned:
        return "not scanned yet", "The file has not been scanned yet."
    if verdict == "unknown":
        return QUIET, ("The bass check found too little bass on the recommended track to "
                       "judge.")
    if verdict == "error":
        return UNREAD, "The bass check could not read the audio. It tries again by itself."
    if has_beq:
        return NOT_CHECKED, ("This film got its BEQ track before the bass check reached it. "
                             "The bass check skips films that already have a BEQ track.")
    return NOT_CHECKED, ("The bass check has not reached this film yet. It runs by itself in "
                         "the background.")


def rating_label(rating: str, note: str = "") -> str:
    """A candidate rating as every page writes it: Strong, Unknown: not checked."""
    return f"{RATINGS[rating]}: {note}" if note else RATINGS[rating]


def row_rating(m) -> tuple[str, str]:
    """A film row's rating and its label, as the Library shows them. The row stores the
    rating once the film has been assessed."""
    rolloff = json.loads(m["rolloff_json"] or "null")
    if m["rating"] in RATINGS and m["rating"] != "unknown":
        return m["rating"], rating_label(m["rating"])
    probe = ProbeResult.from_json(m["probe_json"]) if m["probe_json"] else None
    rating, note, _ = rate(probe, rolloff, m["error"])
    return rating, rating_label(rating, note)


def rate(probe: ProbeResult | None, rolloff: dict | None, error: str | None
         ) -> tuple[str, str, str]:
    """A film's candidate rating, what its label adds, and the sentence that says why. The
    rating comes only from the bass check of the original audio, so a filter or a BEQ track
    never changes it."""
    verdict = (rolloff or {}).get("verdict")
    if error or probe is None:
        return "unknown", *unknown(error, False, verdict)
    if default_track(probe) is None:
        return "unsupported", "", ("No audio track in this film can get a BEQ track."
                                   if probe.audio else "The file has no audio tracks.")
    if verdict in _VERDICT_RATING:
        found = bass_words(rolloff)
        return (_VERDICT_RATING[verdict], "",
                f"The bass check found {found[:1].lower()}{found[1:]}.")
    return "unknown", *unknown(None, True, verdict, bool(probe.beq_tracks))


def row_bass(rating: str, rolloff_json: str | None, probe_json: str | None = None) -> str:
    """What a Library row says beside its rating, in a few words: the figure two films
    compare on (how far the bass has fallen by 15 Hz), or why no track of a Not supported
    film can get a BEQ track."""
    if rating == "unsupported":
        probe = ProbeResult.from_json(probe_json) if probe_json else None
        return unsupported_why(probe) if probe else ""
    if rating not in ROW_FOUND:
        return ""
    depth = (json.loads(rolloff_json or "null") or {}).get("depth15_db")
    if rating == "unlikely" or depth is None:
        return ROW_FOUND[rating]
    return f"Bass {depth:.0f} dB down at 15 Hz"


# What a Library row says of a rating the bass check gave, when it has no figure to give.
ROW_FOUND = {"strong": "Clear roll-off", "possible": "Some roll-off", "unlikely": "No roll-off"}


def unsupported_why(probe: ProbeResult) -> str:
    """Why a Not supported film has no track that can get a BEQ track, in a few words: the
    format of its main track. The film page's Audio tracks gives each track's full reason."""
    main = probe.best or next((t for t in probe.audio if not t.beq), None)
    return f"{main.label} audio" if main else "No audio tracks"


def bass_words(rolloff: dict | None) -> str:
    """What the bass check found on a track, with its figures."""
    ro = rolloff or {}
    verdict = ro.get("verdict")
    below = f" below about {ro['corner_hz']:.0f} Hz" if ro.get("corner_hz") else ""
    if verdict in ("clear", "some"):
        depth = ro.get("depth15_db")
        down = f", {depth:.0f} dB down at 15 Hz" if depth is not None else ""
        return f"{'Clear' if verdict == 'clear' else 'Some'} roll-off{below}{down}"
    return {"none": "No roll-off", "unknown": "Too little bass to judge",
            "error": "Couldn't read the audio"}.get(verdict, "Not checked yet")


def can_get_beq(track: AudioTrack) -> bool:
    return not track.beq and bool(track.channels) and support.plan(track).ok


def default_track(probe: ProbeResult) -> AudioTrack | None:
    """The track the wizard preselects: the best track, else the first one that can get a
    BEQ track. Commentary and descriptive tracks are never the default."""
    if probe.best and can_get_beq(probe.best):
        return probe.best
    return next((t for t in probe.audio if t.pickable and can_get_beq(t)), None)


@dataclass(slots=True)
class Curve:
    """What a filter does at one low-end cutoff."""
    svg: str
    bass: str     # bass restored


@dataclass(slots=True)
class Use:
    """A BEQ track in the file that was made from a filter."""
    track_id: int
    cutoff_hz: float
    gain_db: float | None


@dataclass(slots=True)
class Filter:
    """One bass EQ curve the app can apply. Every filter is the same kind of thing, whatever
    its origin."""
    entry: dict
    matches: dict[int, str] = field(default_factory=dict)  # track id -> exact|related|other
    made_for: str = ""        # the film's track it was made for, or the catalogue's mix
    uses: list[Use] = field(default_factory=list)          # the BEQ tracks made from it
    labels: list[str] = field(default_factory=list)        # "revised since", ...
    gone: str = ""            # why the list holds it only while a BEQ track uses it
    newer: str = ""           # the digest of the filter that replaced it
    # track id -> cutoff -> the peak measured on that track
    levels: dict[int, dict[float, dict]] = field(default_factory=dict)
    designed_at: float | None = None  # when BEQ Forge designed it

    @property
    def digest(self) -> str:
        return self.entry["digest"]

    @property
    def origin(self) -> str:
        return origin(self.entry)

    @property
    def name(self) -> str:
        return filter_name(self.entry)

    @property
    def forge(self) -> bool:
        return self.entry.get("origin") == "designer"

    @property
    def bands(self) -> list[dict]:
        return self.entry.get("filters") or []

    @property
    def dated(self) -> tuple[str, str]:
        """When it last changed, so the user can tell how new it is: (label, date). Every
        filter has the row, and one whose date nothing records says so."""
        if self.forge:
            return "Designed", fmt_day(self.designed_at) or NOT_RECORDED
        return "Last updated", fmt_day(self.entry.get("updated_at")
                                       or self.entry.get("created_at")) or NOT_RECORDED

    @property
    def sure(self) -> str:
        """How sure BEQ Forge is of the filter, as its column says it: 91 of 100. "" for a
        catalogue filter, which has its author instead."""
        sure = self.entry.get("confidence")
        return "" if not self.forge or sure is None else f"{sure * 100:.0f} of 100"

    @property
    def in_use(self) -> bool:
        return bool(self.uses)

    @property
    def mix(self) -> str:
        """The mix the BEQ Catalogue lists it for, where that is not a track of this film."""
        return "" if "exact" in self.matches.values() else _mix(self.entry)

    @property
    def gone_why(self) -> str:
        return GONE.get(self.gone, "")

    @property
    def url(self) -> str:
        """Its BEQ Catalogue page."""
        return self.entry.get("url") or ""

    def fits(self, track_id: int) -> bool:
        """Whether it was made for this track of the film."""
        return self.matches.get(track_id) == "exact"

    def curve(self, cutoff_hz: float = 0.0, span: tuple[float, float] | None = None) -> Curve:
        return Curve(response_svg([(self.origin, self.bands, cutoff_hz)], legend=False,
                                  span=span),
                     bass_restored(self.bands, cutoff_hz))

    def level(self, track_id: int | None, cutoff_hz: float, target_dbfs: float) -> str:
        """Its level change on a track at a cutoff: what the BEQ track made from it at that
        cutoff plays at, else the figure measured ahead, else "". One place, so a filter
        never says it is unmeasured beside a BEQ track that shows its level."""
        used = next((u for u in self.uses if u.cutoff_hz == float(cutoff_hz)
                     and u.gain_db is not None), None)
        if used:
            return fmt_db(used.gain_db)
        found = self.levels.get(track_id, {}).get(float(cutoff_hz))
        return level_estimate(found["peak_dbfs"], target_dbfs) if found else ""

    @property
    def details(self) -> list[tuple[str, str]]:
        """What only its origin knows, for technical details."""
        e = self.entry
        if self.forge:
            fit = e.get("residual_rms_db")
            rows = [("BEQ Forge version", e.get("designer_release") or ""),
                    ("Design method", e.get("strategy") or ""),
                    ("Fit", "" if fit is None else f"within {fit:.1f} dB of the target"),
                    ("Channels filtered", e.get("channel_scope") or "")]
        else:
            mv = e.get("mv")
            rows = [("Listed for", _mix(e)),
                    ("Author's volume advice", f"{mv} dB on the master volume"
                     if mv and mv != "0" else ""),
                    ("Note", e.get("note") or ""), ("Warning", e.get("warning") or "")]
        return [(k, v) for k, v in rows if v]


def _mix(entry: dict) -> str:
    """The mix a catalogue filter is listed for: Atmos 7.1 · Disc."""
    types = ", ".join(entry.get("audio_types") or [])
    return " · ".join(filter(None, (types, entry.get("source"), entry.get("edition"))))


@dataclass(slots=True)
class Track:
    """One audio track in the file."""
    id: int
    name: str                 # Track 1 · TrueHD Atmos 7.1 · English
    beq: bool
    fmt: str = ""             # TrueHD Atmos 7.1
    lang: str = ""            # English
    default: bool = False     # the track the wizard preselects
    can: bool = False         # a BEQ track can be made from it
    why_not: str = ""
    gets: str = ""            # the format its BEQ track would have
    lossy: bool = False       # a track that can get a BEQ track and is lossy (TERMS["lossy"])
    extra: bool = False       # commentary or descriptive
    bass: str = ""            # the bass check's result, on the one track it reads
    key: str = ""
    beqs: list[int] = field(default_factory=list)   # the BEQ tracks made from it
    # a BEQ track's facts, read from its tags
    filter: str = ""          # the origin of the filter it was made from
    digest: str = ""
    cutoff: str = ""
    source: str = ""          # the track it was made from
    source_id: int | None = None   # that track, when it is still in the file
    level: str = ""
    volume: str = ""          # its level change in plain words: what to do with the volume
    file_name: str = ""       # the name written into the file
    notes: list[str] = field(default_factory=list)
    made: Work | None = None  # the work that added it, where the history still has it


def _tag_float(tags: dict, key: str) -> float | None:
    try:
        return float(tags.get(key) or "")
    except ValueError:
        return None


def _tag_bands(tags: dict) -> list[dict]:
    """A BEQ track's filter bands from its tags. Anyone can edit a tag, so one that does not
    parse counts as missing."""
    try:
        bands = json.loads(tags.get("filters") or "[]")
    except ValueError:
        return []
    return [b for b in bands if isinstance(b, dict)] if isinstance(bands, list) else []


def _tag_entry(tags: dict) -> dict:
    """The filter a BEQ track was made from, as far as its tags record it, in the shape of a
    catalogue entry."""
    return {"digest": tags.get("digest") or "", "origin": tags.get("source") or "catalogue",
            "author": tags.get("author") or tags.get("label") or "",
            "audio_types": [x for x in (tags.get("audio_types") or "").split(", ") if x],
            "filters": _tag_bands(tags)}


NOT_RECORDED = "Not recorded"

# Each assess.py reason a BEQ track is out of date, as a short label on the track and on the
# filter it was made from.
STALE_LABELS = (
    ("has been updated", "revised since"),
    ("changed or been withdrawn", "no longer in the catalogue"),
    ("designer's answers have changed", "BEQ Forge has changed since"),
    ("made before", "made by an older BEQ Encoder"),
    ("is now possible", "a better output format is now possible"),
)
FILTER_LABELS = ("revised since", "no longer in the catalogue", "BEQ Forge has changed since")
# Why the list still holds a filter that is no longer offered, by the label it carries.
GONE = {
    "revised since": "The BEQ Catalogue has revised this filter since the BEQ track was made. "
                     "The BEQ track still plays as before.",
    "no longer in the catalogue": "The BEQ Catalogue no longer lists this filter. The BEQ "
                                  "track made from it still plays as before.",
    "an earlier BEQ Forge design": "BEQ Forge has designed for this file again since. The BEQ "
                                   "track made from this design still plays as before.",
}


def _stale_labels(reasons: list[str]) -> list[str]:
    return [label for r in reasons for needle, label in STALE_LABELS if needle in r]


@dataclass(slots=True)
class Film:
    id: int
    name: str                 # Klaus (2019)
    title: str
    edition: str
    tmdb: str
    path: str
    file_note: str
    scanned_at: float | None
    problem: str              # why the file can't be read, or ""
    probe: ProbeResult | None
    rating: str               # a key of RATINGS
    rating_note: str          # which Unknown it is
    rating_why: str
    rolloff: dict | None
    needless: bool = False    # the BEQ Catalogue lists the film as needing no filter
    tracks: list[Track] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    work: list[Work] = field(default_factory=list)   # newest first

    @property
    def rating_label(self) -> str:
        return rating_label(self.rating, self.rating_note)

    @property
    def tmdb_url(self) -> str:
        return f"https://www.themoviedb.org/movie/{self.tmdb}" if self.tmdb else ""

    @property
    def default_track(self) -> Track | None:
        return next((t for t in self.tracks if t.default), None)

    @property
    def beq_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.beq]

    @property
    def sources(self) -> list[Track]:
        """The tracks a BEQ track can be made from, in the file's order."""
        return [t for t in self.tracks if t.can]

    @property
    def cannot(self) -> list[Track]:
        return [t for t in self.tracks if not t.beq and not t.can]

    def track(self, track_id: int | None) -> Track | None:
        return next((t for t in self.tracks if t.id == track_id), None)

    def filter(self, digest: str | None) -> Filter | None:
        return next((f for f in self.filters if f.digest == digest), None)

    @property
    def offered(self) -> list[Filter]:
        """The filters a new BEQ track can be made from. One that the catalogue revised or
        dropped, or that a newer design replaced, stays in the film's list while a BEQ track
        uses it, and is not offered again."""
        return [f for f in self.filters if not f.gone and f.bands]

    def filters_for(self, track_id: int) -> tuple[list[Filter], list[Filter]]:
        """The film's filters as the wizard lists them for one track: those made for it, then
        the rest."""
        made = [f for f in self.offered if f.fits(track_id)]
        return made, [f for f in self.offered if not f.fits(track_id)]

    def track_for(self, f: Filter) -> int | None:
        """The track to add a filter to: the default track when the filter was made for it,
        else the first track it was made for that can get a BEQ track, else the default."""
        default = self.default_track
        if default and f.fits(default.id):
            return default.id
        made = next((t.id for t in self.sources if f.fits(t.id)), None)
        return made if made is not None else default.id if default else None

    def add_href(self, x: Filter) -> str:
        """Where a filter's Add BEQ track link leads: the add flow's Options step with the
        filter chosen. A filter in the BEQ clip the page leads with opens with the clip's
        track and low-end cutoff, so the BEQ track is the one the user heard."""
        q = {"step": "options", "track": self.track_for(x), "filter": x.digest}
        lead = self.latest
        if x.digest in self.clip_marks:
            q.update(track=lead.track_id, cutoff=f"{lead.cutoff_hz:g}")
        return f"/films/{self.id}/add?{urlencode(q)}"

    def name_work(self, works: list[Work]) -> None:
        """Give the filters a piece of work used the names this film's list gives them, so a
        BEQ Forge filter reads the same in a row of work as in its own row (FORGE_TELLS)."""
        for w in works:
            for e in w.filters:
                listed = self.filter(e.get("digest"))
                if listed and listed.entry.get("tell"):
                    e["tell"] = listed.entry["tell"]

    def made_by(self, w: Work) -> Track | None:
        """The BEQ track in the file that a finished piece of work added."""
        return next((t for t in self.beq_tracks if t.made and t.made.ref == w.ref), None)

    def default_filter(self, track_id: int) -> Filter | None:
        """The filter the wizard preselects: the first made for the track. A filter made for
        another track is never applied without the user choosing it."""
        made, _ = self.filters_for(track_id)
        return made[0] if made else None

    def default_why(self, track_id: int) -> str:
        """Why the wizard preselects the filter it does, where the track has several to
        choose from, and how much bass it restores beside the others, so that keeping the
        default is a choice."""
        made, _ = self.filters_for(track_id)
        if len(made) < 2:
            return ""
        first = made[0]
        if not first.forge:
            alone = sum(not x.forge for x in made) == 1
            why = ("it is the BEQ Catalogue's filter for this track" if alone
                   else "the BEQ Catalogue lists it first for this track")
        else:
            sure = [x.entry.get("confidence") or 0.0 for x in made if x.forge]
            why = ("BEQ Forge is most confident in it"
                   if len(sure) > 1 and (first.entry.get("confidence") or 0.0) >= max(sure)
                   else "BEQ Forge listed it first")
        boosts = [(peak_boost(x.bands) or (0.0, 0.0))[0] for x in made]
        n = {2: "two", 3: "three", 4: "four"}.get(len(made), f"{len(made)}")
        if boosts[0] > max(boosts[1:]) + 0.05:
            why += f". It restores the most bass of the {n}"
        elif boosts[0] < min(boosts[1:]) - 0.05:
            why += f". It restores the least bass of the {n}"
        return why

    @property
    def filter_groups(self) -> list[tuple[str, str, list[Filter]]]:
        """The film's filters as its page lists them, set apart as the wizard sets them:
        those made for the recommended track, then the rest. Each group is (heading, what to
        know of it, filters). The heading says what the group's filters were made for, so no
        filter under it says so again. A film with no recommended track has one group with no
        heading."""
        track = self.default_track
        made = [x for x in self.filters if track and x.fits(track.id)]
        rest = [x for x in self.filters if not (track and x.fits(track.id))]
        if not track:
            return [("", "", self.filters)]
        groups = [(f"Made for {track.name}", "", made)] if made else []
        return groups + [("Made for another track or another mix of this film",
                          f"These may not suit {track.name}.", rest)] * bool(rest)

    def level_said(self, x: Filter, track_id: int | None, cutoff_hz: float, target_dbfs: float
                   ) -> str:
        """A filter's level change as every page writes it, in one column: its figure at that
        cutoff, else where its measurement stands (MEASURING, MEASURE_QUEUED, NOT_MEASURED).
        No page says "measured later" while the app measures it."""
        found = x.level(track_id, cutoff_hz, target_dbfs)
        if found:
            return found
        measuring = self.activity("measure")
        if measuring:
            return MEASURING if measuring.status == "running" else MEASURE_QUEUED
        return NOT_MEASURED

    def level_shown(self, x: Filter, track_id: int | None, cutoff_hz: float, target_dbfs: float
                    ) -> tuple[str, str]:
        """A filter's level change on the film page, and what the figure is of when it is
        not the page's cutoff. A filter in use that has no figure at the page's cutoff shows
        the level its BEQ track plays at, with that track's cutoff, so the row never says
        "not measured" beside a BEQ track that shows its level."""
        if not x.level(track_id, cutoff_hz, target_dbfs):
            used = next((u for u in x.uses if u.gain_db is not None), None)
            if used:
                return fmt_db(used.gain_db), (
                    f"at a low-end cutoff of {cutoff_words(used.cutoff_hz)}"
                    if used.cutoff_hz else "with the low-end cutoff off")
        return self.level_said(x, track_id, cutoff_hz, target_dbfs), ""

    def measures_next(self, track_id: int | None, cutoff_hz: float, target_dbfs: float) -> str:
        """What will measure the level changes the film page shows as not measured yet, said
        once over its filters; "" when none is, or when the app is measuring already."""
        if self.activity("measure") or all(
                self.level_shown(x, track_id, cutoff_hz, target_dbfs)[0].endswith("dB")
                for x in self.filters):
            return ""
        return MEASURES_NEXT["after" if self.activity("encode") else "asks"]

    @property
    def clip_marks(self) -> dict[str, int]:
        """Where each filter sits in the BEQ clip the page leads with: digest -> the clip's
        audio track. The clip's first audio track is the original."""
        lead = self.latest
        if not lead or lead.outcome != "clip":
            return {}
        return {e.get("digest"): n for n, e in enumerate(lead.filters, 2)}

    @property
    def activities(self) -> list[Work]:
        """The work the app is doing for the film now, the running one first."""
        return sorted((w for w in self.work if w.active),
                      key=lambda w: (w.status != "running", w.id))

    @property
    def history(self) -> list[Work]:
        """The finished jobs, apart from the result the page leads with. A clip is
        disposable and a measurement is the app's own business, so neither is listed."""
        lead = self.latest
        return [w for w in self.work if not w.active and w.kind not in ("clip", "measure")
                and (lead is None or w.ref != lead.ref)]

    @property
    def latest(self) -> Work | None:
        """The result the page leads with: the newest ended work when it failed, or when it
        ended recently. A measurement is not a result anyone waits for. The header and the
        Library count the same failures (activity._standing)."""
        ended = [w for w in self.work if not w.active and w.status != "cancelled"
                 and w.kind != "measure"]
        if not ended:
            return None
        w = ended[0]
        recent = (w.finished_at or 0) > time.time() - RECENT_S
        if w.status == "failed":
            return w
        # A new filter, a BEQ clip or a design that made none is news until the user acts on
        # it. Once work started since is under way, History holds it, and the page leads
        # with that work alone. A new BEQ track leads for its day.
        acted = w.outcome != "added" and any(
            (a.created_at or 0) > (w.finished_at or 0) for a in self.activities)
        return w if w.status == "done" and recent and not acted else None

    def activity(self, kind: str, track_id: int | None = None) -> Work | None:
        return next((w for w in self.activities if w.kind == kind
                     and (track_id is None or w.track_id == track_id)), None)

    @property
    def retrying(self) -> Work | None:
        """The work that is doing again what the lead failure failed at, when some is."""
        lead = self.latest
        return self.activity(lead.kind) if lead and lead.failure else None

    @property
    def fix(self) -> str:
        """The fix the page leads with, as a key of activity.FIXES: the lead failure's,
        unless its work is already under way again. It is the page's one primary button."""
        lead = self.latest
        if lead and lead.failure and lead.failure.fix and not self.retrying:
            return lead.failure.fix
        return ""

    @property
    def gap(self) -> str:
        """Why the film has no filter to add, as a key of GAPS; "" when it has one. Every
        page that speaks of the missing filter reads this, so none says BEQ Forge can design
        one beside the news that it just declined to."""
        if self.offered or not self.default_track:
            return ""
        if self.activity("design"):
            return "designing"
        asked = next((w for w in self.work if w.kind in ("design", "prepare") and not w.active
                      and w.status not in ("cancelled", "failed")), None)
        if asked and asked.outcome == "nothing":
            return "declined"
        if self.needless:
            return "needless"
        if self.rating == "unlikely":
            return "unlikely"
        return {QUIET: "quiet", UNREAD: "unread"}.get(self.rating_note, "open")

    def gap_said(self, takes: str = "") -> tuple[str, str]:
        """The missing filter in words: (for the page's heading, for its Filters part).
        `takes`: how long a BEQ Forge design takes here, which the heading gives with the
        one way to start one. While the page leads with the design that made no filter, that
        result says why, and the Filters part says only that there is none."""
        if not self.gap:
            return "", ""
        asked = next((w for w in self.work if w.outcome == "nothing"), None)
        head, more = GAPS[self.gap]
        lead = self.latest
        if self.gap == "declined" and lead and lead.outcome == "nothing":
            more = "None."
        return (head.format(takes=takes or activity.USUALLY["design"]),
                more.format(track=self.default_track.name,
                            when=fmt_day(asked.at) if asked else ""))

    @property
    def add(self) -> Add:
        """Whether Add BEQ track is the film page's main action, and why not when it is not."""
        if not self.default_track:
            return Add(False, False, self.rating_why)
        if self.activity("encode"):
            return Add(True, False, "You can add another BEQ track when the one in progress "
                                    "ends.")
        if self.fix:  # the failure's fix is the page's one primary button
            return Add(True, False)
        if self.gap:  # the way in to BEQ Forge: the page's main action only while none was asked
            return Add(True, self.gap == "open")
        made, _ = self.filters_for(self.default_track.id)
        if made and all(x.in_use for x in made):
            return Add(True, False, ALL_IN_USE)
        return Add(True, True)

    @property
    def rating_short(self) -> str:
        """Why the film has its rating, in a few words beside the badge in the page's
        heading: what the bass check found. "" for an Unknown, whose label says which, and
        for Not supported, whose reason the heading gives in a sentence."""
        if self.rating in RATING_MEANS:
            return f"{bass_words(self.rolloff)}, so {RATING_MEANS[self.rating]}"
        return ""

    @property
    def rating_rows(self) -> list[tuple[str, str]]:
        """The Candidate rating part: what the bass check measured on the recommended track.
        The heading's badge gives the finding in a few words, so this part holds the figures
        behind it, or the sentence that says why for a rating with no figures."""
        ro = self.rolloff or {}
        if not self.rating_short:
            return [("Why", self.rating_why)]
        slope, at, level = ro.get("slope_db_oct"), ro.get("slope_at_hz"), ro.get("plateau_dbfs")
        corner = ro.get("corner_hz")
        rows = [("Checked track", self.default_track.name if self.default_track else ""),
                ("Steepest roll-off", f"{slope:.0f} dB per octave, at {at:.0f} Hz"
                 if slope and at else ""),
                (f"Bass level above {corner:.0f} Hz" if corner else "",
                 f"{level:.1f} dBFS ({TERMS['dbfs']})"
                 if level is not None and corner else ""),
                ("Audio sampled", fmt_clock(ro["seconds"]) if ro.get("seconds") else "")]
        return [(k, v) for k, v in rows if k and v]

    @property
    def rating_aside(self) -> str:
        """What an Unlikely rating leaves out, on a film that has a filter all the same."""
        n = len(self.offered)
        if self.rating != "unlikely" or not n:
            return ""
        return (f"The rating comes from the bass check alone, so it does not count the "
                f"{'filter' if n == 1 else f'{n} filters'} this film has.")

    @property
    def no_audio(self) -> bool:
        """The file was scanned and holds no audio track."""
        return bool(self.probe is not None and not self.tracks
                    or self.rating_note == SCAN_PROBLEMS[0][1])


@dataclass(slots=True)
class Add:
    can: bool          # the film has a track a BEQ track can be made from
    main: bool         # adding one is the page's main action
    why: str = ""      # the reason, when it is not


# Why a film has no filter to add: what the page's heading says over its buttons, and what
# its Filters part says. The heading is the one place that says how to get a filter: both
# buttons lead to BEQ Forge, and the heading gives how long a design takes. The reason there
# is none lives in the Filters part, whatever the age of the work that gave it.
NO_FILTER = "This film has no filter."
_LEADS = "Both buttons lead to BEQ Forge, which can {design} in {{takes}}."
FORGE_LINK = "Design a filter with BEQ Forge"
GAPS = {
    "designing": ("BEQ Forge is designing this film's first filter. Add BEQ track shows the "
                  "filter when it is ready.", "BEQ Forge is designing one now."),
    "declined": (f"{NO_FILTER} {_LEADS.format(design='try a design again')}",
                 "BEQ Forge looked at {track} on {when} and designed none, because it found "
                 "no roll-off to restore."),
    "needless": (f"{NO_FILTER} {_LEADS.format(design='design one anyway')}",
                 "The BEQ Catalogue lists the film as needing none."),
    "unlikely": (f"{NO_FILTER} {_LEADS.format(design='design one anyway')}",
                 "The bass check found no roll-off, so one is unlikely to help."),
    "quiet": (f"{NO_FILTER} {_LEADS.format(design='design one anyway')}",
              "The bass check found too little bass on {track} to judge, so a filter may have "
              "nothing to restore."),
    "unread": (f"{NO_FILTER} {_LEADS.format(design='design one')}",
               "None yet. The bass check could not read the audio, so the app can't say "
               "whether one would help."),
    "open": ("No filter has been made for this film yet. Both buttons start by having BEQ "
             "Forge design one, which takes {takes}.",
             "None yet."),
}
# Why Add BEQ track is not the page's main action on a film whose filters each have a BEQ
# track, and what it still does.
ALL_IN_USE = ("Every filter made for the recommended track is in use. Add BEQ track can still "
              "add one at another low-end cutoff, or from a filter that BEQ Forge designs.")
# Where a level change that has no figure stands, in the column its figure will fill.
MEASURING, MEASURE_QUEUED, NOT_MEASURED = "Measuring", "Queued for measuring", "Not measured yet"
# What will measure it, said once over the film page's filters: the user opening the add
# flow, or the app once the BEQ track in progress ends.
MEASURES_NEXT = {"asks": "Opening Add BEQ track measures each level change that is not "
                         "measured yet.",
                 "after": "The app measures the rest after the BEQ track in progress."}


def _tracks(probe: ProbeResult, rolloff: dict | None, assessment: dict, work: list[Work]
            ) -> list[Track]:
    default = default_track(probe)
    stale = {b["name"]: b for b in assessment.get("beq") or []}
    added = [w for w in work if w.kind == "encode" and w.status == "done"]
    out, made_from = [], {}
    for a in probe.audio:
        name = track_name(a.track_id, a.label, a.language)
        if a.beq:
            tags = a.beq_tags or {}
            info = stale.get(a.name) or {}
            gain, hz = _tag_float(tags, "gain_db"), _tag_float(tags, "highpass_hz")
            made = next((w for w in added if w.track_name == a.name and (
                not tags.get("digest") or not w.filters
                or w.filters[0].get("digest") == tags["digest"])), None)
            made_from[a.track_id] = (tags.get("source_track") or "").split(" (")[0]
            out.append(Track(
                id=a.track_id, name=f"BEQ track · {name}", beq=True, fmt=a.label,
                lang=language(a.language),
                filter=filter_name(_tag_entry(tags)) if tags else "",
                digest=tags.get("digest") or "", cutoff=cutoff_words(hz) if tags else "",
                source=made_from[a.track_id],
                level=fmt_db(gain) if gain is not None else "",
                volume=level_sentence(gain), file_name=a.name,
                notes=_stale_labels(info.get("reasons") or [])
                + ([] if tags else ["not made by BEQ Encoder"]),
                made=made))
            continue
        plan = support.plan(a)
        # A commentary track is offered beside a main track, never by itself: a film whose
        # main audio can't get a BEQ track has no track that can.
        lone = a.extra and default is None
        can = bool(a.channels) and plan.ok and not lone
        out.append(Track(
            id=a.track_id, name=name, beq=False, fmt=a.label, lang=language(a.language),
            key=probe.track_key(a),
            default=default is not None and a.track_id == default.track_id, can=can,
            why_not="" if can else "It is a commentary or description track, and the film's "
            "main audio can't get a BEQ track." if lone and plan.ok
            else plan.said or "It has no audio channels.",
            gets=(plan.label if a.lossless else f"{plan.label} (from a lossy track)")
            if can else "",
            lossy=can and not a.lossless,
            extra=a.extra,
            bass=bass_words(rolloff) if probe.best and a.track_id == probe.best.track_id
            else ""))
    # Each BEQ track names the track it was made from by that track's one name, and each
    # original track knows the BEQ tracks made from it.
    originals = {a.track_id: a.label for a in probe.audio if not a.beq}
    by_id = {t.id: t for t in out}
    for t in (t for t in out if t.beq):
        ids = [i for i, label in originals.items() if label == made_from[t.id]]
        source = t.made.track_id if t.made and t.made.track_id in originals else \
            ids[0] if len(ids) == 1 else None
        if source is not None:
            t.source, t.source_id = by_id[source].name, source
            by_id[source].beqs.append(t.id)
    return out


def _filters(db: DB, catalogue, m, probe: ProbeResult, work: list[Work] = ()
             ) -> list[Filter]:
    """Every filter the app knows for the film, in one list: the BEQ Catalogue's entries for
    it, BEQ Forge's designs for its file, and the filter of any BEQ track that is in neither.
    Catalogue entries with no filter bands are left out."""
    sources = [a for a in probe.audio if not a.beq and a.channels]
    names = {a.track_id: track_name(a.track_id, a.label, a.language) for a in sources}
    found: dict[str, Filter] = {}
    if catalogue is not None and catalogue.loaded_at:
        film = assess.film_of(m)
        for a in sources:
            for match, e in assess.matches(a, film, catalogue):
                if e.filters:
                    found.setdefault(e.digest, Filter(e.to_dict())).matches[a.track_id] = match
    # A design's date is when its job ended, as the film's history says it.
    designed = {w.id: w.finished_at for w in work if w.kind == "design"}
    for row in store.forge_filters(db, m):
        track = probe.track_by_key(row["track_key"])
        if track is not None and row["entry"].get("filters") and not row["entry"].get("rejected"):
            f = found.setdefault(row["entry"]["digest"], Filter(row["entry"]))
            f.matches[track.track_id] = "exact"
            f.designed_at = designed.get(row["job_id"]) or row["created_at"]
    for f in found.values():
        made = [names[t] for t, match in f.matches.items() if match == "exact"]
        f.made_for = ", ".join(made) if made else f"Another mix of this film ({_mix(f.entry)})"
    _tell_forge([f for f in found.values() if f.forge])

    for t in probe.beq_tracks:
        tags = t.beq_tags or {}
        digest = tags.get("digest")
        if not digest:
            continue
        if digest not in found:  # revised, withdrawn or replaced since: kept while in use
            forge = tags.get("source") == "designer"
            newer = None if forge else next(
                (f for f in found.values() if not f.forge and not f.gone
                 and f.entry.get("author") == tags.get("author")
                 and "exact" in f.matches.values()), None)
            gone = "an earlier BEQ Forge design" if forge else \
                "revised since" if newer else "no longer in the catalogue"
            found[digest] = Filter(
                {**_tag_entry(tags), "gone": gone},
                made_for=(tags.get("source_track") or "").split(" (")[0], labels=[gone],
                gone=gone, newer=newer.digest if newer else "")
        found[digest].uses.append(Use(t.track_id, _tag_float(tags, "highpass_hz") or 0.0,
                                      _tag_float(tags, "gain_db")))
    return list(found.values())


# What tells one BEQ Forge filter from another made for the same track, in its name: where
# it stands among them by the bass it restores. The word says when to choose it, and the
# Bass restored column gives the figure.
FORGE_TELLS = ("fuller", "gentler", "in between")


def _tell_forge(designs: list[Filter]) -> None:
    """Name each of BEQ Forge's filters for a track apart from the others (FORGE_TELLS). The
    tell goes in the filter's entry, so the work started with the filter names it the same
    way. One filter alone needs none."""
    by_track: dict[tuple, list[Filter]] = {}
    for f in designs:
        by_track.setdefault(tuple(sorted(f.matches)), []).append(f)
    for group in by_track.values():
        boosts = [(peak_boost(f.bands) or (0.0, 0.0))[0] for f in group]
        if len(group) < 2 or max(boosts) - min(boosts) < 0.05:
            continue
        for f, boost in zip(group, boosts, strict=True):
            f.entry["tell"] = FORGE_TELLS[0 if boost == max(boosts) else
                                          1 if boost == min(boosts) else 2]


def summary(db: DB, catalogue, m) -> tuple[str, int, int]:
    """What the Library sorts and filters on: (rating, number of filters, number of BEQ
    tracks). Stored on the film's row whenever the file, the catalogue or a job changes it."""
    probe = ProbeResult.from_json(m["probe_json"]) if m["probe_json"] else None
    rolloff = json.loads(m["rolloff_json"] or "null")
    rating = rate(probe, rolloff, m["error"])[0]
    if probe is None:
        return rating, 0, 0
    return rating, len(_filters(db, catalogue, m, probe)), len(probe.beq_tracks)


def load(db: DB, catalogue, media_id: int) -> Film | None:
    m = db.one("SELECT * FROM media WHERE id=?", (media_id,))
    if not m:
        return None
    probe = ProbeResult.from_json(m["probe_json"]) if m["probe_json"] else None
    rolloff = json.loads(m["rolloff_json"] or "null")
    assessment = json.loads(m["assess_json"] or "null") or {}
    rating, note, why = rate(probe, rolloff, m["error"])
    work = activity.for_film(db, media_id)
    film = Film(
        id=m["id"], name=film_name(m["title"], m["year"]) or Path(m["path"]).name,
        title=m["title"] or "", edition=m["edition"] or "", tmdb=m["tmdb"] or "",
        path=m["path"], file_note=file_note(m["path"], m["title"], m["year"]),
        scanned_at=m["scanned_at"], problem=m["error"] or "", probe=probe,
        rating=rating, rating_note=note, rating_why=why, rolloff=rolloff, work=work,
        needless=assessment.get("catalogue") == "no_beq_needed")
    if probe is None:
        return film
    film.tracks = _tracks(probe, rolloff, assessment, work)
    film.filters = _filters(db, catalogue, m, probe, work)
    by_digest = {t.digest: t for t in film.beq_tracks if t.digest}
    for f in film.filters:
        beq = by_digest.get(f.digest)
        if beq is None:
            continue
        beq.filter = f.name
        if not f.gone:  # the list itself says why a filter that is gone stays in it
            f.labels += [n for n in beq.notes if n in FILTER_LABELS and n not in f.labels]
            continue
        # A filter that is gone is known only from its BEQ track. Its name carries its label,
        # so the track does not tag it again, and it was made for the track its BEQ track
        # was made from, by that track's one name.
        beq.notes = [n for n in beq.notes if n != f.gone]
        source = next((t for t in film.tracks if beq.id in t.beqs), None)
        if source:
            f.matches[source.id], f.made_for = "exact", source.name
    film.name_work(work)
    for t in (t for t in film.tracks if t.can):
        for digest, at in store.levels(db, m, t.key).items():
            f = film.filter(digest)
            if f is not None:
                f.levels[t.id] = at
    return film
