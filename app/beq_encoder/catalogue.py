"""BEQ catalogue (beqcatalogue database.json): download, index, search, rank."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .biquad import SUPPORTED_TYPES, FilterSpec

log = logging.getLogger(__name__)

# Catalogue audioTypes ("DTS-HD MA 5.1", "DD+ Atmos", "Atmos", "TrueHD 7.1") -> the
# track kinds in media.KINDS. Order matters: first match wins.
_TYPE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^dd\+.*atmos|^e-?ac-?3.*atmos", re.I), "eac3_joc"),
    (re.compile(r"^(dolby\s+)?atmos\b|^truehd.*atmos", re.I), "truehd_atmos"),
    (re.compile(r"^dts[:\-]?x", re.I), "dtsx"),
    (re.compile(r"^truehd", re.I), "truehd"),
    (re.compile(r"^dts[-. ]?hd[-. ]?(ma|master)|^dts hd-ma", re.I), "dtshd_ma"),
    (re.compile(r"^dts[-. ]?hd[-. ]?(hr|ha|high)", re.I), "dtshd_hra"),
    (re.compile(r"^dts[-. ]?hd\b", re.I), "dtshd"),  # MA or HRA, unspecified
    (re.compile(r"^dts", re.I), "dts"),
    (re.compile(r"^(dd\+|e-?ac-?3|ddp)", re.I), "eac3"),
    (re.compile(r"^(dd\b|dd-ex|ac-?3|dolby digital)", re.I), "ac3"),
    (re.compile(r"^l?pcm", re.I), "pcm"),
    (re.compile(r"^flac", re.I), "flac"),
    (re.compile(r"^aac", re.I), "aac"),
]
_CH_RE = re.compile(r"(?:^|\s)(\d)[. ](\d)\b")

# Lossless kinds whose decode is the same PCM a FLAC/LPCM rip of the disc could hold
_LOSSLESS = {"truehd", "dtshd_ma", "pcm", "flac", "truehd_atmos", "dtsx"}

# A download holding less than this share of the loaded entries is refused as damaged
_MIN_KEPT_ON_DOWNLOAD = 0.5


def parse_audio_type(t: str) -> tuple[str | None, str | None]:
    """("dtshd_ma", "5.1") for "DTS-HD MA 5.1"; (None, None) when unrecognised."""
    s = t.strip()
    kind = next((k for rx, k in _TYPE_RULES if rx.search(s)), None)
    m = _CH_RE.search(s)
    return kind, (f"{m.group(1)}.{m.group(2)}" if m else None)


def track_match(audio_types: list[str], kind: str, layout_label: str) -> str:
    """How well a catalogue entry fits one track: "exact" | "related" | "other".

    exact: same format family and, where the entry states one, the same speaker count.
    related: probably not the same encode, but plausibly the same mix (an Atmos track's
    7.1 presentation vs a "TrueHD 7.1" entry; a FLAC/LPCM track vs a lossless disc
    entry with the same channels; DTS:X vs its DTS-HD MA bed). Shown, never auto-picked.
    """
    best = "other"
    for t in audio_types:
        k, ch = parse_audio_type(t)
        if k is None:
            continue
        ch_ok = ch is None or ch == layout_label
        same = k == kind or (k == "dtshd" and kind in ("dtshd_ma", "dtshd_hra"))
        if same and ch_ok:
            return "exact"
        if ch_ok and (
            (kind in ("flac", "pcm") and k in _LOSSLESS)
            or (kind == "truehd_atmos" and k == "truehd")
            or (kind == "dtsx" and k in ("dtshd_ma", "dtshd"))
            or (kind == "eac3_joc" and k == "eac3")
        ):
            best = "related"
        elif same:
            best = "related"
    return best


@dataclass(slots=True)
class Entry:
    idx: int
    digest: str
    title: str
    year: str
    tmdb: str
    content_type: str
    audio_types: list[str]
    author: str
    source: str
    edition: str
    language: str
    mv: str
    warning: str
    note: str
    url: str
    filters: list[dict]

    @property
    def no_filters(self) -> bool:
        """The catalogue's "does not require BEQ" record for this track."""
        return not self.filters

    @property
    def label(self) -> str:
        bits = [f"{self.title} ({self.year})"]
        if self.edition:
            bits.append(self.edition)
        bits.append(", ".join(self.audio_types))
        bits.append(self.source)
        bits.append(self.author)
        return " · ".join(b for b in bits if b)

    def to_dict(self) -> dict:
        return {
            "digest": self.digest,
            "title": self.title,
            "year": self.year,
            "tmdb": self.tmdb,
            "audio_types": self.audio_types,
            "author": self.author,
            "source": self.source,
            "edition": self.edition,
            "mv": self.mv,
            "url": self.url,
            "warning": self.warning,
            "note": self.note,
            "filters": self.filters,
            "origin": "catalogue",
            "label": self.author or "catalogue",
        }


def _text(e: dict, key: str) -> str:
    """A text field of a record: absent or null reads as empty, any other type is malformed."""
    v = e.get(key)
    if v is None:
        return ""
    if not isinstance(v, str):
        raise TypeError(f"{key} is a {type(v).__name__}, not text")
    return v


def _number_or_text(e: dict, key: str) -> str:
    """A field the catalogue writes as text or as a bare number, as text."""
    v = e.get(key)
    if isinstance(v, int | float) and not isinstance(v, bool):
        return str(v)
    return _text(e, key)


def _audio_types(e: dict) -> list[str]:
    types = e.get("audioTypes") or []
    if not (isinstance(types, list) and all(isinstance(t, str) for t in types)):
        raise TypeError("audioTypes is not a list of text")
    return types


def _entry(i: int, e: dict) -> Entry | None:
    """One record of database.json; None if a filter type is one this encoder lacks.
    Raises if the record is malformed."""
    filters = [
        {k: f.get(k) for k in ("type", "freq", "gain", "q", "count")}
        for f in e["filters"]
    ]
    # an empty list is kept: it is the catalogue's "does not require BEQ" record
    if any(f["type"] not in SUPPORTED_TYPES for f in filters):
        return None
    for f in filters:
        FilterSpec.from_catalogue(f)  # a bad value would break the response curves or filter
    title = _text(e, "title")
    if not title:
        raise ValueError("no title")
    return Entry(
        idx=i,
        digest=_text(e, "digest") or str(i),
        title=title,
        year=_number_or_text(e, "year"),
        tmdb=_number_or_text(e, "theMovieDB"),
        content_type=_text(e, "content_type"),
        audio_types=_audio_types(e),
        author=_text(e, "author"),
        source=_text(e, "source"),
        edition=_text(e, "edition"),
        language=_text(e, "language"),
        mv=_number_or_text(e, "mv") or "0",
        warning=_text(e, "warning"),
        note=_text(e, "note"),
        url=_text(e, "catalogue_url"),
        filters=filters,
    )


def _parse(raw: bytes) -> list[Entry]:
    """The entries of a database.json file; ValueError if it holds none it can use."""
    records = json.loads(raw)
    if not isinstance(records, list):
        raise ValueError("not a list of records")
    entries: list[Entry] = []
    for i, e in enumerate(records):
        try:
            entry = _entry(i, e)
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed catalogue entry %d: %s", i, exc)
            continue
        if entry is not None:
            entries.append(entry)
    if not entries:
        raise ValueError(f"no usable entries in {len(records)} record(s)")
    return entries


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


class Catalogue:
    def __init__(self, path: Path, url: str, max_age_days: int):
        self.path = path
        self.url = url
        self.max_age_days = max_age_days
        self.entries: list[Entry] = []
        self.by_tmdb: dict[str, list[Entry]] = {}
        self.by_digest: dict[str, Entry] = {}
        self.by_title: dict[str, list[Entry]] = {}  # films (not TV), by normalised title
        self.loaded_at: float | None = None
        self.error: str | None = None
        self.on_loaded: Callable[[], object] | None = None  # runs after every load
        self._loaded_stamp: tuple[int, int] | None = None  # the file state last loaded
        self._lock = threading.Lock()  # one refresh at a time

    # -- loading -----------------------------------------------------------
    def age_days(self) -> float | None:
        stamp = self._stamp()
        return None if stamp is None else (time.time() - stamp[0] / 1e9) / 86400

    def _stamp(self) -> tuple[int, int] | None:
        """The file's identity (modified time, size); None when there is no file."""
        try:
            st = self.path.stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            self._load_if_changed()  # first, so a download is judged against the cached copy
            age = self.age_days()
            unusable = self._stamp() != self._loaded_stamp  # only a download can replace it
            if force or unusable or age is None or age > self.max_age_days:
                try:
                    self.download()
                except Exception as exc:
                    log.warning("Catalogue download failed: %s", exc)
                    if age is None:  # otherwise the cached copy keeps serving
                        self.error = f"Catalogue download failed: {exc}"
                self._load_if_changed()

    def _load_if_changed(self) -> None:
        stamp = self._stamp()
        if stamp and stamp != self._loaded_stamp:
            try:
                self.load()
            except Exception as exc:  # whatever is wrong with it, the download must still run
                log.warning("Catalogue load failed: %s", exc)
                self.error = f"Catalogue load failed: {exc}"

    def download(self) -> None:
        """Replace the cached file, only with one that is a usable catalogue."""
        log.info("Downloading BEQ catalogue from %s", self.url)
        req = urllib.request.Request(self.url, headers={"User-Agent": "beq-encoder"})
        tmp = self.path.with_suffix(".tmp")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
                while chunk := resp.read(1 << 20):
                    fh.write(chunk)
            got = len(_parse(tmp.read_bytes()))
            if got < _MIN_KEPT_ON_DOWNLOAD * len(self.entries):
                raise ValueError(f"only {got} usable entries against {len(self.entries)} loaded")
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def load(self) -> None:
        stamp = self._stamp()
        entries = _parse(self.path.read_bytes())
        by_tmdb: dict[str, list[Entry]] = {}
        by_title: dict[str, list[Entry]] = {}
        for en in entries:
            if en.tmdb:
                by_tmdb.setdefault(en.tmdb, []).append(en)
            if en.content_type != "TV":
                by_title.setdefault(_norm(en.title), []).append(en)
        self.entries = entries
        self.by_tmdb = by_tmdb
        self.by_digest = {e.digest: e for e in entries}
        self.by_title = by_title
        self.loaded_at = time.time()
        self.error = None
        self._loaded_stamp = stamp
        log.info("Loaded %d BEQ catalogue entries", len(entries))
        if self.on_loaded:
            try:
                self.on_loaded()
            except Exception:  # the catalogue did load: this is not a problem with the file
                log.exception("Post-load callback failed")

    # -- querying ----------------------------------------------------------
    def get(self, digest: str) -> Entry | None:
        return self.by_digest.get(digest)

    def search(self, query: str, limit: int = 50) -> list[Entry]:
        q = query.strip()
        if not q:
            return []
        if q.isdigit() and q in self.by_tmdb:
            return self.by_tmdb[q][:limit]
        terms = _norm(q).split()
        out = [
            e for e in self.entries if all(t in _norm(f"{e.title} {e.year}") for t in terms)
        ]
        return out[:limit]

    def for_track(
        self, tmdb: str | None, title: str | None, year: str | None, edition: str | None,
        kind: str, layout_label: str,
    ) -> list[tuple[str, Entry]]:
        """This film's entries as (match, entry), exact matches for the track first."""
        order = {"exact": 0, "related": 1, "other": 2}
        out = [(track_match(e.audio_types, kind, layout_label), e)
               for e in self.candidates(tmdb, title, year, edition)]
        return sorted(out, key=lambda me: order[me[0]])  # stable: keeps the edition ranking

    def candidates(
        self, tmdb: str | None, title: str | None, year: str | None, edition: str | None
    ) -> list[Entry]:
        """Entries for this movie, best first (edition, then source, then year)."""
        pool: list[Entry] = []
        if tmdb and tmdb in self.by_tmdb:
            pool = [e for e in self.by_tmdb[tmdb] if e.content_type != "TV"]
        if not pool and title:
            def year_ok(e: Entry) -> bool:
                if not (year and year.isdigit() and e.year.isdigit()):
                    return True
                return abs(int(e.year) - int(year)) <= 1

            pool = [e for e in self.by_title.get(_norm(title), []) if year_ok(e)]
        return sorted(pool, key=lambda e: self._score(e, year, edition), reverse=True)

    @staticmethod
    def _score(e: Entry, year: str | None, edition: str | None) -> tuple:
        disc = 1 if e.source.lower() == "disc" else 0
        ed = 0
        if edition and e.edition:
            ed = 1 if _norm(edition) in _norm(e.edition) or _norm(e.edition) in _norm(edition) else -1
        elif e.edition:
            ed = -1 if not any(k in e.edition.lower() for k in ("theatrical", "uhd")) else 0
        yr = 1 if year and e.year == year else 0
        return (ed, disc, yr, bool(e.filters))
