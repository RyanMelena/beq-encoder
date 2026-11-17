"""The one name and the one format of each thing the screens show (docs/ux-guidelines.md,
"Call each thing by one name")."""

from __future__ import annotations

import re
import time
from pathlib import Path

import numpy as np

from .biquad import FilterSpec, response_db
from .media import AudioTrack

def fmt_time(ts: float | None) -> str:
    """Every date with a time: 7 Nov 2025 17:30. It is fmt_day with the time after it, so a
    date reads the same wherever it shows."""
    if not ts:
        return ""
    return f"{fmt_day(ts)} {time.strftime('%H:%M', time.localtime(ts))}"


def fmt_day(ts: float | None) -> str:
    """Every date without a time: 7 Nov 2025."""
    if not ts:
        return ""
    then = time.localtime(ts)
    return f"{then.tm_mday} {time.strftime('%b %Y', then)}"


def fmt_clock(seconds: float | None) -> str:
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


def fmt_dur(a: float | None, b: float | None) -> str:
    return fmt_clock((b or time.time()) - a) if a else ""


def fmt_db(v) -> str:
    """Every level in dB, signed: -7.2 dB, and 0.0 dB for no change."""
    if v is None:
        return ""
    return "0.0 dB" if abs(float(v)) < 0.05 else f"{float(v):+.1f} dB"


def film_name(title: str | None, year=None) -> str:
    """A film as every screen writes it: Klaus (2019). Empty when the title is unknown."""
    return f"{title} ({year})" if title and year else title or ""


# ffmpeg's layout suffixes, as people say them.
LAYOUT_SHAPES = {"side": "side surrounds", "back": "back surrounds", "wide": "wide surrounds",
                 "wide-side": "wide side surrounds", "front": "front centres"}


def layout_note(track: AudioTrack) -> str:
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


def language(code: str | None) -> str:
    """A track's language by name; empty when the file does not say."""
    if not code or code == "und":
        return ""
    return LANGUAGES.get(code.split("-")[0].lower(), code)


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


def track_name(track_id: int, label: str, lang: str | None = None) -> str:
    """An audio track's one name: Track 1 · TrueHD Atmos 7.1 · English."""
    return " · ".join(filter(None, (f"Track {track_id}", label, language(lang))))


def origin(entry: dict) -> str:
    """Where a filter came from: BEQ Catalogue with its author, or BEQ Forge."""
    if entry.get("origin") == "designer" or entry.get("source") == "designer":
        return "BEQ Forge"
    who = entry.get("author") or ""
    return f"BEQ Catalogue · {who}" if who else "BEQ Catalogue"


def peak_boost(bands: list[dict], cutoff_hz: float = 0.0) -> tuple[float, float] | None:
    """The most a filter changes the bass and where, with the low-end cutoff applied: (dB,
    Hz). The dB is negative for a filter that only cuts. None for bands that do not parse.
    The search starts below the cutoff too: a filter's shelves often still climb there, so
    the most it adds can sit an octave under the cutoff."""
    try:
        specs = [FilterSpec.from_catalogue(f) for f in bands or []]
    except (KeyError, ValueError):
        return None
    if not specs:
        return None
    f = np.geomspace(1.0, 160.0, 600)
    r = response_db(specs, f, 48000.0, highpass_hz=cutoff_hz)
    i = int(np.argmax(r))
    if r[i] <= 0:
        i = int(np.argmin(r))
    return float(r[i]), float(f[i])


def bass_restored(bands: list[dict], cutoff_hz: float = 0.0) -> str:
    """The filter's biggest change and where, with the low-end cutoff applied."""
    peak = peak_boost(bands, cutoff_hz)
    if peak is None:
        return ""
    gain, at = peak
    if gain <= 0:
        return f"none: it cuts up to {abs(gain):.1f} dB, at {at:.0f} Hz"
    return f"up to {gain:+.1f} dB, at {at:.0f} Hz"


def filter_name(entry: dict) -> str:
    """A filter's one name, with the fact that tells it from another of the same origin: the
    mix a BEQ Catalogue filter is listed for, or which of BEQ Forge's designs for the track
    it is (film.FORGE_TELLS), where BEQ Forge made several.
    BEQ Catalogue · aron7awol (Atmos 7.1), BEQ Forge (fuller)."""
    if entry.get("gone"):
        tell = entry["gone"]
    elif origin(entry) == "BEQ Forge":
        tell = entry.get("tell") or ""
    else:
        tell = ", ".join(entry.get("audio_types") or [])
    return f"{origin(entry)} ({tell})" if tell else origin(entry)


def cutoff_words(hz: float | None) -> str:
    """A low-end cutoff as every screen writes it."""
    return f"{hz:g} Hz" if hz else "Off"


# What each word means, in a few words, where the user decides on it: a column's heading, a
# part's first line, a field's help.
TERMS = {
    "bass": "The most the filter adds back, and at what frequency, with the low-end cutoff "
            "applied",
    "level": "How far the app turns the BEQ track down to make room for the restored bass, "
             "with the low-end cutoff applied. An estimate until the track is made",
    "rating": "How likely a BEQ track is to help. The bass check sets it from the film's "
              "roll-off: deep bass that falls away, which a filter can restore",
    "confidence": "How sure BEQ Forge is that its design suits this track, out of 100",
    "cutoff": "Cuts bass below this frequency, so that bass too low for your subwoofer does "
              "not make the app turn the rest of the track down.",
    "curve": "Each line is what its filter, with the low-end cutoff, does to the original "
             "track. Mastering turned the track's deepest bass down, and the filter turns it "
             "back up, so the restored bass is close to flat. The dashed upright line is the "
             "low-end cutoff: below it, the restored bass falls away by 24 dB an octave",
    "dbfs": "0 dBFS is the loudest a track can hold",
    "lossy": "Lossy: compressed with some of the sound removed. A BEQ track made from a lossy "
             "track can't put that back.",
}


def ordinal(n: int) -> str:
    """A place in an order: 1st, 2nd, 3rd, 4th."""
    return f"{n}{'th' if 10 < n % 100 < 14 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def fmt_hms(seconds: float) -> str:
    """A point in a film's running time, as the clip's start box takes it: 0:34:30."""
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def level_sentence(gain_db: float | None) -> str:
    """A BEQ track's measured level change in plain words: what to do with the volume."""
    if gain_db is None:
        return ""
    by = f"{abs(gain_db):.1f} dB"
    if abs(gain_db) < 0.05:
        return "It plays at the same level as the original track."
    if gain_db > 0:
        return f"It plays {by} louder than the original track."
    return (f"It plays {by} quieter than the original track, so turn the volume up by {by} "
            f"to match.")


def level_estimate(peak_dbfs: float | None, target_dbfs: float) -> str:
    """The level change a filter is expected to need, from its measured filtered peak, in
    the format of a BEQ track's own level change (fmt_db). TERMS["level"] says it is an
    estimate, once, where the figures show."""
    if peak_dbfs is None:
        return ""
    return fmt_db(min(0.0, target_dbfs - peak_dbfs))


# The pipeline and the designer word things their own way. The screens use the guidelines' names.
_PLAIN = (
    (re.compile(r"[Tt]he (?:automatic )?designer"), "BEQ Forge"),
    (re.compile(r"[Dd]esigner"), "BEQ Forge"),
    (re.compile(r"beqforge"), "BEQ Forge"),
    (re.compile(r"high-pass floor"), "low-end cutoff"),
    (re.compile(r"BEQ catalogue"), "BEQ Catalogue"),
    (re.compile(r"[Tt]he (?:source )?file was not changed"), "The film file was not changed"),
    (re.compile(r"this job"), "this work"),
    (re.compile(r"This job"), "This work"),
    (re.compile(r"the job"), "the work"),
)


def plain(text: str | None) -> str:
    """Stored pipeline wording in the screens' vocabulary."""
    text = text or ""
    for pattern, said in _PLAIN:
        text = pattern.sub(said, text)
    return text
