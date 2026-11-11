"""Library assessment: which films are worth a BEQ, which BEQ tracks are out of date.

Two kinds of evidence, kept apart because they cost very different amounts:

* **From the probe and the catalogue** (free, recomputed whenever the catalogue,
  the designer or the file changes): does the BEQ Catalogue have an entry for this
  film's best track, and, for a file that already has a BEQ track, is that track
  stale (its catalogue entry changed, a catalogue entry appeared for a designed
  BEQ, the designer's answers moved on, this encoder improved its output since,
  or a better output format is now possible)?
* **From the audio** (reads part of the film): a screen for a mastering roll-off,
  from the peak spectrum of the best track's mono mix over ten 30 s excerpts spread
  through the film. A hint for picking candidates, not a design: the designer
  analyses the whole programme when a run is started.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import analysis, charts, support

# Bump ENCODER_REVISION whenever this encoder's output changes; add what changed.
# BEQ tracks made below AUDIO_REVISION have audio this encoder now makes better.
ENCODER_REVISION = 1
REVISIONS = {
    1: "the first release",
}
AUDIO_REVISION = 1      # tracks older than this: re-encode recommended

ROLLOFF_SCHEMA = 1
SEGMENTS = 10
SEGMENT_S = 30.0


# ------------------------------------------------------------------------ audio

def sample_segments(duration_s: float | None, n: int = SEGMENTS,
                    seg: float = SEGMENT_S) -> list[tuple[float, float]]:
    """(start, length) excerpts spread across the programme, or the whole of it."""
    if not duration_s or duration_s <= n * seg * 1.5:
        return [(0.0, float(duration_s or 0))]
    starts = np.linspace(0.03 * duration_s, 0.97 * duration_s - seg, n)
    return [(float(a), seg) for a in starts]


def sampled_mono(ffmpeg: str, path: str, audio_index: int, channels: int,
                 names: tuple[str, ...], duration_s: float | None, popen=None,
                 cancelled=lambda: False):
    """Yield the 1 kHz mono mix of each excerpt (as charts.decode_mono). An excerpt the
    decoder fails on raises RuntimeError: a screen of part of the film is not a screen."""
    gains = analysis.mono_gains(names)
    for start, length in sample_segments(duration_s):
        if cancelled():
            return
        # a whole-programme "excerpt" decodes to its end, not to the container's duration
        blocks = list(analysis.decode_blocks(ffmpeg, path, audio_index, channels, popen=popen,
                                             start=start, length=length if start else 0.0,
                                             what="excerpt for the bass screen"))
        if blocks:
            yield np.concatenate(blocks) @ gains


@dataclass(slots=True)
class Rolloff:
    verdict: str            # "clear" | "some" | "none" | "unknown"
    corner_hz: float | None  # highest frequency below 60 Hz already 6 dB under the plateau
    slope_db_oct: float     # steepest rise of the peak curve over half an octave, 8-60 Hz
    slope_at_hz: float | None
    depth15_db: float       # plateau minus the peak level at 15 Hz
    plateau_dbfs: float     # median peak level, 40-80 Hz
    seconds: float          # audio examined

    def to_dict(self) -> dict:
        return {"schema": ROLLOFF_SCHEMA, "verdict": self.verdict,
                "corner_hz": _r(self.corner_hz), "slope_db_oct": _r(self.slope_db_oct),
                "slope_at_hz": _r(self.slope_at_hz), "depth15_db": _r(self.depth15_db),
                "plateau_dbfs": _r(self.plateau_dbfs), "seconds": round(self.seconds)}

    @property
    def summary(self) -> str:
        if self.verdict == "unknown":
            return "too little bass to judge"
        if self.verdict == "none":
            return "no roll-off seen"
        corner = f"below ~{self.corner_hz:.0f} Hz" if self.corner_hz else ""
        return (f"{'clear roll-off' if self.verdict == 'clear' else 'some roll-off'} "
                f"{corner}, {self.depth15_db:.0f} dB down at 15 Hz").replace("  ", " ")


def _r(v):
    return None if v is None else round(float(v), 1)


def rolloff(m: charts.Measurement) -> Rolloff:
    """Screen a measured peak spectrum for a mastering high-pass."""
    f = m.freqs
    sel = (f >= 4) & (f <= 120)
    ff = f[sel]
    pk = charts.smooth(ff, m.peak_db[sel], 1 / 6)
    plateau = float(np.median(pk[(ff >= 40) & (ff <= 80)]))
    if plateau < -95:
        return Rolloff("unknown", None, 0.0, None, 0.0, plateau, m.duration_s)
    best, at = 0.0, None
    for i, fc in enumerate(ff):
        if 8 <= fc <= 60:
            j = int(np.searchsorted(ff, fc / math.sqrt(2)))
            s = (pk[i] - pk[j]) / 0.5
            if s > best:
                best, at = float(s), float(fc)
    below = np.nonzero((ff < 60) & (pk < plateau - 6))[0]
    corner = float(ff[below[-1]]) if len(below) else None
    d15 = plateau - float(np.interp(15.0, ff, pk))
    if corner and corner >= 18 and d15 >= 12 and best >= 18:
        verdict = "clear"
    elif corner and d15 >= 6:
        verdict = "some"
    else:
        verdict = "none"
    return Rolloff(verdict, corner, best, at, d15, plateau, m.duration_s)


def rolloff_key(size: int, mtime: float, track_id: int) -> str:
    return f"{ROLLOFF_SCHEMA}:{size}:{mtime!r}:{track_id}"


# ------------------------------------------------------------------------ probe

def _int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def film_of(row) -> dict:
    """What identifies a library row's film to the catalogue."""
    return {"tmdb": row["tmdb"], "title": row["title"] or Path(row["path"]).stem,
            "year": row["year"], "edition": row["edition"]}


def _matches(best, film: dict, catalogue) -> list:
    if catalogue is None:
        return []
    return catalogue.for_track(film.get("tmdb"), film.get("title"), film.get("year"),
                               film.get("edition"), best.kind, best.layout_label)


def _designed_reasons(tags: dict, with_filters: list, designer: dict | None) -> list[str]:
    """Why a BEQ track the designer made is out of date: the catalogue now has entries
    for this exact track (`with_filters`), or the design version has moved on."""
    reasons = []
    if with_filters:
        reasons.append(f"the catalogue now has {len(with_filters)} entr"
                       f"{'y' if len(with_filters) == 1 else 'ies'} for this track")
    current = _int((designer or {}).get("design_version"))
    made = _int(tags.get("design_version"))
    if current and made and current > made:
        release = (designer or {}).get("designer_release") or ""
        reasons.append(f"the designer's answers have changed since "
                       f"(design version {made} → {current}, beqforge {release})")
    return reasons


def designed_staleness(probe, film: dict, catalogue,
                       designer: dict | None) -> list[tuple[str, str]]:
    """(track name, why it is out of date) for each BEQ track the designer made."""
    best = probe.best if probe else None
    if best is None:
        return []
    with_filters = [e for m, e in _matches(best, film, catalogue) if m == "exact" and e.filters]
    return [(t.name, reason) for t in probe.beq_tracks
            if (t.beq_tags or {}).get("source") == "designer"
            for reason in _designed_reasons(t.beq_tags, with_filters, designer)]


def assess(probe, film: dict, catalogue, designer: dict | None = None,
           dee_outputs: bool = False, rolloff_info: dict | None = None) -> dict:
    """Candidate and staleness summary for one file (JSON-able, stored per file).

    `film`: tmdb/title/year/edition. `designer`: the designer's /health (release,
    design_version), when known. `dee_outputs`: whether DEE can make TrueHD (Atmos and
    channel-based alike) right now.
    """
    out: dict = {"schema": 1, "best": None, "catalogue": "none", "catalogue_count": 0,
                 "candidate": None, "beq": [], "stale": False, "reasons": []}
    best = probe.best if probe else None
    if best is None:
        out["catalogue"] = "no audio"
        return out
    plan = support.plan(best)
    out["best"] = {"track_id": best.track_id, "label": best.label, "kind": best.kind,
                   "supported": plan.ok, "output": plan.label if plan.ok else None,
                   "reason": plan.reason or ""}
    matches = _matches(best, film, catalogue)
    exact = [e for m, e in matches if m == "exact"]
    with_filters = [e for e in exact if e.filters]
    others = [e for m, e in matches if m != "exact" and e.filters]
    if with_filters:
        out["catalogue"] = "exact"
        out["catalogue_count"] = len(with_filters)
        out["authors"] = sorted({e.author for e in with_filters})
    elif exact:
        out["catalogue"] = "no_beq_needed"
    elif others:
        out["catalogue"] = "other_track"
        out["catalogue_count"] = len(others)

    # what the BEQ tracks in the file were made from, and whether that still holds
    for t in probe.beq_tracks:
        tags = t.beq_tags or {}
        info = {"name": t.name, "source": tags.get("source") or "", "label": tags.get("label"),
                "encoder": tags.get("encoder") or "", "reasons": [], "notes": []}
        rev = _int(tags.get("encoder_revision")) or (1 if tags.get("encoder") else None)
        info["revision"] = rev
        if not tags:
            info["notes"].append("not made by beq-encoder (no BEQ tags): its source is unknown")
        else:
            if info["source"] == "catalogue":
                dg = tags.get("digest") or ""
                if dg and catalogue is not None and catalogue.get(dg) is None:
                    same = [e for e in with_filters if e.author == tags.get("author")]
                    info["reasons"].append(
                        f"its catalogue entry by {tags.get('author') or 'its author'} has "
                        + ("been updated" if same else "changed or been withdrawn"))
            elif info["source"] == "designer":
                info["reasons"] += _designed_reasons(tags, with_filters, designer)
            if rev is not None and rev < AUDIO_REVISION:
                info["reasons"].append(
                    "made before " + "; ".join(REVISIONS[r] for r in
                                               range(rev + 1, AUDIO_REVISION + 1)))
            made = tags.get("output") or ""
            possible = plan.ok and bool(dee_outputs)
            what = []
            if made.startswith("FLAC"):
                what.append("a test-only FLAC track, not a bitstream")
            if made.endswith(" bed") or " bed " in made:
                what.append("made from the bed only, without the source's objects")
            if what and possible:
                info["reasons"].append(f"{'; '.join(what)}: {plan.label} is now possible")
            elif what:
                info["notes"].append(f"{'; '.join(what)}")
            src = tags.get("source_track") or ""
            if src and best.label not in src:
                info["notes"].append(f"made from {src}; the best track is now {best.label}")
        out["beq"].append(info)
    out["stale"] = any(b["reasons"] for b in out["beq"])
    out["reasons"] = [f"{b['name']}: {r}" for b in out["beq"] for r in b["reasons"]]

    # candidates: no BEQ track yet and something says it is worth one
    if not probe.beq_tracks:
        ro = (rolloff_info or {}).get("verdict")
        if not plan.ok:
            out["candidate"] = None
        elif out["catalogue"] == "exact":
            out["candidate"] = "catalogue"
        elif out["catalogue"] == "no_beq_needed":
            out["candidate"] = None
        elif ro == "clear":
            out["candidate"] = "rolloff"
        elif ro == "some" or out["catalogue"] == "other_track":
            out["candidate"] = "maybe"
    return out


def dumps(d: dict) -> str:
    return json.dumps(d, separators=(",", ":"))
