"""The wizard's state: the choices for one film, kept between visits (docs/ux-decisions.md, 1).

One wizard serves both outputs. A BEQ track and a BEQ clip get the same track, filter and
options, so switching the output keeps every choice. Every choice has a default, taken from
the film and the Settings page when the user has not made it."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from . import store
from .biquad import check_highpass
from .film import Film
from .pipeline.clip import MAX_FILTERS as CLIP_MAX_FILTERS

OUTPUTS = {"add": "Add BEQ track", "clip": "Make BEQ clip"}
STEPS = ("track", "filter", "options")
STEP_NAMES = {"track": "Track", "filter": "Filter", "options": "Options"}
# What each step's primary button says, by output. The last step's button starts the work.
NEXT = {"track": "Continue to filter", "filter": "Continue to options"}


@dataclass(slots=True)
class Draft:
    output: str = "add"
    step: str = "filter"
    track_id: int | None = None
    digest: str | None = None                       # the one filter, to add a BEQ track
    digests: list[str] | None = None  # the filters, to make a clip; None: not chosen yet
    cutoff: float | None = None
    format: str = ""
    default_flag: bool | None = None
    replace: bool = True
    start: str = ""
    seconds: float | None = None

    @classmethod
    def load(cls, db, media_id: int) -> Draft:
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in store.draft(db, media_id).items() if k in known})

    def save(self, db, media_id: int) -> None:
        store.save_draft(db, media_id, asdict(self))

    def take(self, q) -> None:
        """Apply the choices a request carries: a link from the film detail page, a step's
        form, or a change the page sends as the user makes it."""
        if q.get("output") in OUTPUTS:
            self.output = q["output"]
        if q.get("step") in STEPS:
            self.step = q["step"]
        if (q.get("track") or "").isdigit():
            if self.track_id != int(q["track"]):
                self.digest, self.digests = None, None  # filters are made for one track
            self.track_id = int(q["track"])
        if q.get("filter"):
            self.digest = q["filter"]
            if q["filter"] not in (self.digests or []):
                self.digests = [q["filter"], *(self.digests or [])][:CLIP_MAX_FILTERS]
        if hasattr(q, "getlist") and "filters" in q:
            self.digests = [x for x in dict.fromkeys(q.getlist("filters")) if x][:CLIP_MAX_FILTERS]
        # The cutoff menu's last entry, Custom, sends its value in a field of its own.
        cutoff = q.get("custom_hz") if q.get("cutoff") == "custom" else q.get("cutoff")
        if cutoff not in (None, ""):
            try:
                self.cutoff = check_highpass(cutoff)
            except ValueError:
                pass
        for key in ("format", "start"):
            if key in q:
                setattr(self, key, str(q[key]).strip())
        if "seconds" in q:
            try:
                self.seconds = float(q["seconds"])
            except ValueError:
                pass
        if "options" in q:  # the Options step's form: an unticked box sends nothing
            self.default_flag = "default_flag" in q
            self.replace = "replace" in q

    def resolve(self, film: Film, cutoff_default: float, flag_default: bool) -> None:
        """Fill every choice the user has not made with its default, and drop choices that
        no longer apply (a filter gone from the list, a track gone from the file)."""
        if film.track(self.track_id) is None or not film.track(self.track_id).can:
            if self.track_id is not None:  # a track gone from the file: its filters go too
                self.digest, self.digests = None, None
            self.track_id = film.default_track.id if film.default_track else None
        if self.track_id is None:
            return
        known = {f.digest for f in film.offered}
        if self.digest not in known:
            fit = film.default_filter(self.track_id)
            self.digest = fit.digest if fit else None
        made, _ = film.filters_for(self.track_id)
        if self.digests is None:
            self.digests = [f.digest for f in made][:CLIP_MAX_FILTERS] or (
                [self.digest] if self.digest else [])
        self.digests = [d for d in self.digests if d in known]
        if self.cutoff is None:
            self.cutoff = cutoff_default
        if self.default_flag is None:
            self.default_flag = flag_default

    @property
    def chosen(self) -> list[str]:
        """The filters the work will use."""
        if self.output == "clip":
            return self.digests or []
        return [self.digest] if self.digest else []
