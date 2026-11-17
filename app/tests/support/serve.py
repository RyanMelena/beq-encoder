"""Boot the real web app on a throwaway database that holds every UI state, for the journey
tests and the hill climb's screenshots.

Read it top to bottom: the films, the catalogue and BEQ Forge's filters as tables, then the
files on disk, the film rows, the measurements, the job history and the clips. It ends by
printing each film's address by role and serving the app on port 8080.

The queues are stubbed, so nothing here runs: a queued row stays queued and a running row
stays running. Every film has a small real .mkv at its path (two seconds of FLAC 5.1). Its
row describes the film the table names, so a rescan replaces that with what the file holds.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

import numpy as np

SEED_STARTED = time.monotonic()
# Everything the seed writes is under one folder that it owns and empties at each boot. The
# settings are set here, over the image's and the caller's, so that it never empties a real
# library or config folder.
SEED = Path(tempfile.gettempdir()) / "beq-seed"
CONFIG, ROOT, CLIP_DIR = SEED / "config", SEED / "library", SEED / "clips"
os.environ |= {"CONFIG_DIR": str(CONFIG), "WORK_DIR": str(SEED / "work"),
               "MEDIA_ROOTS": str(ROOT), "CLIP_DIR": str(CLIP_DIR),  # outside the library
               "CATALOGUE_URL": "http://127.0.0.1:9/none", "WATCH": "off",
               "SCAN_ON_START": "false"}
NOW = time.time()
MIN, HOUR, DAY = 60, 3600, 86400
DURATION_S = 6810.0  # what every film's row says it runs for

# ---------------------------------------------------------------------------- the films

# a source track: (kind, codec, channels, layout)
ATMOS = ("truehd_atmos", "TrueHD Atmos", 8, "7.1")
THD71 = ("truehd", "TrueHD", 8, "7.1")
THD51 = ("truehd", "TrueHD", 6, "5.1(side)")
DTS71 = ("dtshd_ma", "DTS-HD Master Audio", 8, "7.1")
DTS51 = ("dtshd_ma", "DTS-HD Master Audio", 6, "5.1(side)")
FLAC51 = ("flac", "FLAC", 6, "5.1(side)")
AC351 = ("ac3", "AC-3", 6, "5.1(side)")
PCM51 = ("pcm", "PCM", 6, "5.1(side)")
DTSX = ("dtsx", "DTS-HD Master Audio", 8, "7.1")
JOC = ("eac3_joc", "E-AC-3", 6, "5.1(side)")
DDP = ("eac3", "E-AC-3", 6, "5.1(side)")


def bass_check(verdict, corner=None, depth=0.0, slope=0.0, at=None, plateau=-31.4):
    return {"schema": 1, "verdict": verdict, "corner_hz": corner, "slope_db_oct": slope,
            "slope_at_hz": at, "depth15_db": depth, "plateau_dbfs": plateau, "seconds": 300,
            "track": None}


CLEAR = bass_check("clear", 27.0, 21.0, 24.0, 22.0)
SOME = bass_check("some", 21.0, 8.0, 12.0, 18.0)
NONE = bass_check("none", None, 2.0)
TOO_QUIET = bass_check("unknown", None, 0.0, plateau=-99.0)
CHECK_FAILED = {"schema": 1, "verdict": "error", "error": "ffmpeg exited 1 reading segment 4",
                "seconds": 0, "track": None}
NOT_RUN = None


class Film(NamedTuple):
    id: int
    role: str      # what a capture script asks for; printed with the film's address
    title: str
    year: str
    edition: str
    tmdb: str
    source: tuple | None  # the default track; None: the file gave no audio
    bass: dict | None     # the bass check's result, which the candidate rating comes from
    beq: tuple = ()       # its BEQ tracks: (filter key, low-end cutoff in Hz)
    error: str = ""       # why the scan could not use the file
    why: str = ""


F = Film
FILMS = [
    # Candidate ratings, on films with no BEQ track.
    F(1, "strong-several-filters", "Edge of Tomorrow", "2014", "", "137113", ATMOS, CLEAR,
      why="Strong. Four catalogue filters: two for its track, one for a related track, one "
          "for another release. All measured, with a bass profile and a finished BEQ clip."),
    F(2, "strong-one-filter", "Equilibrium", "2002", "", "7299", THD51, CLEAR,
      why="Strong. One catalogue filter, measured: the film to add a BEQ track to with the "
          "defaults. Nothing is running on it."),
    F(3, "strong-no-filter", "Donnie Darko", "2001", "Director's Cut", "141", FLAC51, CLEAR,
      why="Strong. No filter of any kind and no history: the film to start BEQ Forge on."),
    F(4, "forge-filter", "Cloudy with a Chance of Meatballs", "2009", "", "22794", AC351, CLEAR,
      why="Strong. No catalogue filter, two BEQ Forge filters from a finished design, both "
          "measured. A BEQ clip of the two is being made."),
    F(5, "possible", "Kung Fu Hustle", "2004", "", "9470", PCM51, SOME,
      why="Possible. No filter yet: BEQ Forge is designing one now."),
    F(6, "unlikely", "Big Hero 6", "2014", "", "177572", DTS71, NONE,
      why="Unlikely, with one catalogue filter all the same. A BEQ track is being added."),
    F(7, "failed-encode", "Megamind", "2010", "", "38055", THD71, NOT_RUN,
      why="Unknown: the bass check has not run. One catalogue filter, measured. Its last "
          "encode failed the lossless check: the film to recover on."),
    F(8, "unknown-too-little-bass", "Arrival", "2016", "", "329865", THD71, TOO_QUIET,
      why="Unknown: too little bass to judge. A design that the user cancelled."),
    F(9, "bass-check-failed", "Sicario", "2015", "", "273481", ATMOS, CHECK_FAILED,
      why="The bass check ran and could not read the audio."),
    F(10, "not-supported", "Atomic Blonde", "2017", "", "341013", DTSX, NOT_RUN,
      why="Not supported: DTS:X is its only track. Holds an old job with the retired "
          "unsupported status."),
    F(11, "measuring", "The Wedding Ringer", "2015", "", "294254", DTS51, SOME,
      why="Three catalogue filters and figures for one of them, so the other two show "
          "Measuring. A measurement is queued, and a BEQ clip failed."),
    F(12, "forge-declined", "Dune: Part Two", "2024", "", "693134", ATMOS, NONE,
      why="Unlikely, and BEQ Forge agreed: a finished design that designed no filter."),
    F(13, "no-beq-needed", "Alien", "1979", "", "348", DTS51, NONE,
      why="The catalogue lists it as needing no BEQ: an entry with no filter."),
    F(14, "long-title",
      "Dr. Strangelove or: How I Learned to Stop Worrying and Love the Bomb", "1964",
      "Criterion Collection 4K Restoration", "935", DTS51, NONE,
      why="The longest title and edition a row has to hold. Holds an old job with the "
          "retired no_beq status."),
    # Films with BEQ tracks.
    F(15, "one-beq-track", "Klaus", "2019", "", "508965", JOC, CLEAR, (("cat-klaus", 0.0),),
      why="One BEQ track from a catalogue filter, with the encode that made it and its "
          "charts."),
    F(16, "one-beq-track-with-cutoff", "The Burial", "2023", "", "1000837", DDP, SOME,
      (("cat-burial", 15.0),),
      why="One BEQ track made with a 15 Hz low-end cutoff."),
    F(17, "two-beq-tracks", "Mad Max: Fury Road", "2015", "Black & Chrome", "76341", ATMOS,
      CLEAR, (("cat-fury-a", 0.0), ("cat-fury-b", 10.0)),
      why="Two BEQ tracks from two catalogue filters, each with its encode and charts."),
    F(18, "forge-beq-track", "Blade Runner 2049", "2017", "", "335984", ATMOS, NOT_RUN,
      (("forge-br-flatten", 0.0),),
      why="A BEQ track made from a BEQ Forge filter: the end of the BEQ Forge journey. The "
          "catalogue has a filter for it too."),
    F(19, "withdrawn-filter", "Interstellar", "2014", "", "157336", DTS51, CLEAR,
      (("cat-inter-2019", 0.0),),
      why="Its BEQ track's filter is no longer in the catalogue, which now lists a newer "
          "one by the same author. No job made the track: it predates this database."),
    # Files the scan could not use.
    F(20, "unreadable", "The Thing", "1982", "", "1091", None, NOT_RUN,
      error="mkvmerge -J failed: the file could not be read (truncated EBML header at 0x0FA0)",
      why="The file on disk is not a Matroska file."),
    F(21, "no-audio", "Koyaanisqatsi", "1982", "", "11314", None, NOT_RUN,
      error="No audio tracks in this file",
      why="The file on disk holds video only."),
]
FILM = {f.id: f for f in FILMS}


# ---------------------------------------------------------------------------- the filters

def bands(*rows):
    return [{"type": t, "freq": f, "gain": g, "q": q, "count": c} for t, f, g, q, c in rows]


DEEP = bands(("LowShelf", 18.0, 5.5, 0.9, 3), ("LowShelf", 26.0, 3.0, 0.8, 2),
             ("PeakingEQ", 34.5, -1.5, 1.4, 1))
DEEPER = bands(("LowShelf", 16.0, 6.0, 0.75, 4), ("PeakingEQ", 28.0, 1.8, 1.1, 1))
GENTLE = bands(("LowShelf", 22.0, 4.5, 0.85, 2), ("LowShelf", 30.0, 2.5, 0.7, 1))

# The BEQ Catalogue: (key, film, audio types, author, source, edition, mv, warning, note, bands).
CATALOGUE = [
    ("cat-edge-a", 1, ["Atmos 7.1"], "aron7awol", "Disc", "", "0", "",
     "Matches the UHD and the Blu-ray.", DEEP),
    ("cat-edge-b", 1, ["Atmos 7.1"], "mobe1969", "Disc", "", "-1.5",
     "Hot master: expect limiting near 0 dBFS.", "", DEEPER),
    ("cat-edge-c", 1, ["TrueHD 7.1"], "halcyon888", "Disc", "", "0", "", "", GENTLE),
    ("cat-edge-d", 1, ["DTS-HD MA 5.1"], "aron7awol", "Streaming", "", "0", "", "", GENTLE),
    ("cat-equi", 2, ["TrueHD 5.1"], "mobe1969", "Disc", "", "0", "", "", GENTLE),
    ("cat-hero", 6, ["DTS-HD MA 7.1"], "aron7awol", "Disc", "", "0", "", "", DEEP),
    ("cat-mega", 7, ["TrueHD 7.1"], "aron7awol", "Disc", "", "0", "", "", DEEPER),
    ("cat-ring-a", 11, ["DTS-HD MA 5.1"], "mobe1969", "Disc", "", "0", "", "", GENTLE),
    ("cat-ring-b", 11, ["DTS-HD MA 5.1"], "aron7awol", "Disc", "", "0", "", "", DEEP),
    ("cat-ring-c", 11, ["DTS-HD MA 5.1"], "halcyon888", "Disc", "", "-1", "", "", DEEPER),
    ("cat-alien", 13, ["DTS-HD MA 5.1"], "aron7awol", "Disc", "", "0", "",
     "Full bandwidth: does not require BEQ.", []),
    ("cat-klaus", 15, ["DD+ Atmos 5.1"], "aron7awol", "Streaming", "", "0", "", "", DEEP),
    ("cat-burial", 16, ["DD+ 5.1"], "mobe1969", "Streaming", "", "0", "", "", GENTLE),
    ("cat-fury-a", 17, ["Atmos 7.1"], "aron7awol", "Disc", "Black & Chrome", "0", "", "", DEEP),
    ("cat-fury-b", 17, ["Atmos 7.1"], "mobe1969", "Disc", "Black & Chrome", "0", "", "",
     DEEPER),
    ("cat-br", 18, ["Atmos 7.1"], "aron7awol", "Disc", "", "0", "", "", DEEPER),
    ("cat-inter", 19, ["DTS-HD MA 5.1"], "mobe1969", "Disc", "", "0", "",
     "Revised in 2024 from a better capture.", GENTLE),
]
# What the catalogue listed when a BEQ track was made and has dropped since.
WITHDRAWN = [
    ("cat-inter-2019", 19, ["DTS-HD MA 5.1"], "mobe1969", "Disc", "", "0", "", "", DEEP),
]

# BEQ Forge's answers: film -> (key, method, strategy, bands, confidence, gain reduction,
# residual RMS, why it was rejected). A rejected filter is not offered.
FORGE = {
    4: [("forge-cloudy-flatten", "shelf cascade", "flatten", DEEPER, 0.91, -1.8, 1.6, ()),
        ("forge-cloudy-parametric", "parametric", "parametric", GENTLE, 0.74, None, 2.3, ()),
        ("forge-cloudy-aggressive", "shelf cascade", "aggressive", DEEP, 0.32, -6.4, 4.8,
         ("Needs 6.4 dB of gain reduction: above the 4 dB limit",
          "Residual RMS 4.8 dB: above the 3 dB limit"))],
    18: [("forge-br-flatten", "shelf cascade", "flatten", DEEP, 0.88, -2.1, 1.4, ()),
         ("forge-br-parametric", "parametric", "parametric", GENTLE, 0.69, None, 2.6, ())],
}
FORGE_RELEASE, DESIGN_VERSION = "0.3.3", 7


def digest(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def catalogue_json(rows) -> str:
    return json.dumps([
        {"digest": digest(key), "title": FILM[fid].title, "year": int(FILM[fid].year),
         "theMovieDB": FILM[fid].tmdb, "content_type": "film", "audioTypes": types,
         "author": author, "source": source, "edition": edition, "mv": mv, "warning": warning,
         "note": note, "avs": "", "filters": filters,
         # spread over the last few years, some revised since, so each filter has its own age
         "created_at": int(NOW - (60 + 113 * i) * DAY),
         "updated_at": int(NOW - (60 + 113 * i) * DAY + (41 * i % 300) * DAY * (i % 3 == 0)),
         "catalogue_url": f"https://beqcatalogue.readthedocs.io/en/latest/{author}/"
                          f"#{FILM[fid].title.lower().replace(' ', '-')}"}
        for i, (key, fid, types, author, source, edition, mv, warning, note, filters)
        in enumerate(rows)])


# ---------------------------------------------------------------------------- files on disk

def run(*cmd: str) -> None:
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)


def film_path(f: Film) -> Path:
    name = f"{f.title.replace(':', ' -')} ({f.year})"
    edition = f" {{edition-{f.edition}}}" if f.edition else ""
    return ROOT / f"{name} {{tmdb-{f.tmdb}}}" / f"{name}{edition} [Remux-2160p].mkv"


shutil.rmtree(SEED, ignore_errors=True)
for folder in (ROOT, CLIP_DIR, CONFIG / "catalogue", CONFIG / "dee"):
    folder.mkdir(parents=True)
DEE_EXE = CONFIG / "dee" / "dee.exe"
DEE_EXE.touch()

NOISE = "anoisesrc=color=brown:sample_rate=48000:amplitude=0.3:duration="
TEMPLATE = CONFIG / "seed-film.mkv"
run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", NOISE + "2", "-ac", "6", "-c:a", "flac",
    str(TEMPLATE))
for f in FILMS:
    path = film_path(f)
    path.parent.mkdir(parents=True)
    if f.source:
        shutil.copyfile(TEMPLATE, path)
    elif f.role == "no-audio":
        run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
            "-c:v", "mpeg4", str(path))
    else:
        path.write_bytes(np.random.default_rng(f.id).bytes(4000))
    changed = int(NOW) - f.id * DAY  # whole seconds: the row and the file agree to the tick
    os.utime(path, (changed, changed))

CATALOGUE_FILE = CONFIG / "catalogue" / "database.json"


def write_catalogue(rows) -> None:
    CATALOGUE_FILE.write_text(catalogue_json(rows))
    fetched = NOW - 0.4 * DAY - len(rows)  # fresh enough that the app does not download one
    os.utime(CATALOGUE_FILE, (fetched, fetched))


write_catalogue(CATALOGUE + WITHDRAWN)

# ---------------------------------------------------------------------------- the app

from beq_encoder import config  # noqa: E402


class StubDee:
    path = encoder = DEE_EXE

    def status(self):
        return {"path": str(DEE_EXE), "windows": True, "formats": ["truehd"], "pcm_truehd": True,
                "ok": True, "problems": []}

    def probe(self, *a, **k):
        return None

    def reprobe_while_failing(self):
        return None


config.Settings.dee = lambda self: StubDee()

from beq_encoder import assess, charts, clips, layouts, media, store, web, worker  # noqa: E402
from beq_encoder.pipeline import prepare, source_pin  # noqa: E402
from beq_encoder.pipeline.clip import clip_name  # noqa: E402
from beq_encoder.pipeline.job import FileStamp  # noqa: E402

db = web.db
web.designer_status = lambda max_age=30.0: {
    "url": web.S.designer_url, "ok": True, "problems": [], "contract_version": "1.5",
    "designer_release": FORGE_RELEASE, "design_version": DESIGN_VERSION,
    "design_settings": "default", "shared_root": True, "parallel": 1}
# Nothing runs. A started queue would also fail every running row as interrupted.
for queue in (web.jobs, web.clip_queue, web.library_watch):
    queue.start = queue.stop = lambda: None
web.jobs.kick = web.clip_queue.kick = lambda: None

# The withdrawn entry is read the way the app read it, then dropped from the catalogue.
web.catalogue.load()
ENTRY = {key: web.catalogue.get(digest(key)).to_dict() for key, *_ in WITHDRAWN}
write_catalogue(CATALOGUE)
web.catalogue.load()
ENTRY |= {key: web.catalogue.get(digest(key)).to_dict() for key, *_ in CATALOGUE}


def forge_entry(fid, key, method, strategy, filters, confidence, reduction, rms, reasons):
    """A BEQ Forge filter, in the shape designer.candidate_entry gives one."""
    f = FILM[fid]
    return {"digest": f"design:{hashlib.sha256(key.encode()).hexdigest()[:24]}",
            "origin": "designer", "label": f"beqforge {FORGE_RELEASE} {strategy}",
            "author": f"beqforge {FORGE_RELEASE}", "title": f.title, "year": f.year,
            "edition": "", "source": "designer", "audio_types": [], "mv": "0.0", "url": "",
            "warning": "", "note": "", "filters": filters, "strategy": strategy,
            "rank": 0, "rejected": bool(reasons), "rejection_reasons": list(reasons),
            "confidence": confidence, "gain_reduction_db": reduction, "method": method,
            "residual_db": None, "residual_rms_db": rms, "channel_scope": "all channels",
            "commentary": {
                "Roll-off": "A 4th-order high-pass near 27 Hz, consistent across the film.",
                "Headroom": "Peaks below 30 Hz stay 3 dB under full scale after the filter."},
            "designer_release": FORGE_RELEASE, "design_version": DESIGN_VERSION,
            "design_settings": "default", "designer_build": "seed"}


ENTRY |= {row[0]: forge_entry(fid, *row) for fid, rows in FORGE.items() for row in rows}

# ---------------------------------------------------------------------------- levels

CUTOFFS = store.cutoff_ladder(web.S.highpass_hz)
TARGET_PEAK = -1.0


def source_peak(fid: int) -> float:
    return round(-0.2 - 0.3 * (fid % 5), 2)


def filtered_peak(fid: int, key: str, cutoff: float) -> float:
    """The peak a film's track ends at through a filter and a cutoff, in dBFS. Made up, and
    the same wherever it shows: a deeper filter raises it, a higher cutoff lowers it."""
    lift = sum(b["gain"] * b["count"] for b in ENTRY[key]["filters"] if b["type"] == "LowShelf")
    return round(source_peak(fid) + 0.3 * lift * (1 - cutoff / 45), 2)


def level_change(fid: int, key: str, cutoff: float) -> float:
    return round(min(0.0, TARGET_PEAK - filtered_peak(fid, key, cutoff)), 1)


def levels_of(fid: int, keys) -> dict:
    """{cutoff: {digest: peak}}, as a design or measuring job's result holds it."""
    return {f"{hz:g}": {ENTRY[k]["digest"]: {"peak_dbfs": filtered_peak(fid, k, hz),
                                             "channel": "LFE"} for k in keys}
            for hz in CUTOFFS}


def bass_profile(fid: int) -> dict:
    """The bass level in each ten seconds of the film, with one passage that stands out."""
    rng = np.random.default_rng(fid)
    t = np.arange(int(DURATION_S // prepare.PROFILE_BUCKET_S))
    heavy = 14 * np.exp(-((t - (150 + 60 * fid)) / 9.0) ** 2)
    db = -44 + 9 * np.sin(t / 37) ** 2 + heavy + rng.normal(0, 2.2, t.size)
    return {"bucket_s": prepare.PROFILE_BUCKET_S, "db": [round(float(v), 1) for v in db]}


# ---------------------------------------------------------------------------- film rows

def track(tid, idx, kind, codec, channels, layout, name="", lang="eng", **kw):
    return media.AudioTrack(track_id=tid, audio_index=idx, codec=codec, language=lang, name=name,
                            default=kw.pop("default", False), channels=channels,
                            atmos=kind == "truehd_atmos", beq=kw.pop("beq", False), kind=kind,
                            layout=layout, sample_rate=48000, **kw)


def beq_track(f: Film, source: media.AudioTrack, n: int, key: str, cutoff: float):
    """A BEQ track as the app writes one: named by decision 7 and tagged with its filter."""
    entry, plan = ENTRY[key], web.support.plan(source)
    gain = level_change(f.id, key, cutoff)
    output = f"{plan.label} {source.layout_label}" if "Atmos" in plan.label else plan.label
    tags = {"source": entry["origin"], "label": entry["label"], "author": entry["author"],
            "digest": entry["digest"], "audio_types": ", ".join(entry["audio_types"]),
            "strategy": entry.get("strategy", ""),
            "designer_release": entry.get("designer_release", ""),
            "design_version": str(entry.get("design_version") or ""),
            "design_settings": entry.get("design_settings") or "",
            "filters": json.dumps(entry["filters"]),
            "highpass_hz": f"{cutoff:g}" if cutoff else "", "highpass_order": "2" if cutoff else "",
            "gain_db": str(gain), "source_track": f"{source.label} ({source.codec})",
            "output": output, "encoder": "beq-encoder 0.1.0",
            "encoder_revision": str(assess.ENCODER_REVISION),
            "chart_spectrum": f"beq-{n}-spectrum.png", "chart_heatmap": f"beq-{n}-heatmap.png",
            "report": f"beq-{n}.json"}
    name = (f"BEQ {output} ({entry['author']})" if f.role == "withdrawn-filter"  # an old name
            else f"BEQ [{gain:+.1f} dB] {output}")
    return track(1 + n, n, "truehd_atmos" if "Atmos" in output else "truehd", "TrueHD",
                 source.channels, source.layout, name, beq=True,
                 beq_tags={k: v for k, v in tags.items() if v})


PROBE: dict[int, media.ProbeResult] = {}
for f in FILMS:
    path = film_path(f)
    st = path.stat()
    row = dict(id=f.id, path=str(path), size=st.st_size, mtime=st.st_mtime, title=f.title,
               year=f.year, edition=f.edition, tmdb=f.tmdb, scanned_at=NOW - 2 * HOUR)
    if f.source is None:
        db.upsert_media(**row, has_atmos=0, has_beq=0, has_audio=0, probe_json=None,
                        best_json=None, beq_names=None, error=f.error)
        continue
    source = track(1, 0, *f.source, default=True, original=True)
    beqs = [beq_track(f, source, n, key, hz) for n, (key, hz) in enumerate(f.beq, start=1)]
    after = 2 + len(beqs)
    extras = [track(after, after - 1, "ac3", "AC-3", 2, "stereo", "Director's Commentary",
                    commentary=True)]
    if web.support.plan(source).ok:  # a dub could get a BEQ track, so Not supported has none
        extras.append(track(after + 1, after, "ac3", "AC-3", 6, "5.1(side)", "Español",
                            lang="spa"))
    probe = PROBE[f.id] = media.ProbeResult(path=str(path), duration_s=DURATION_S, tracks=[],
                                            audio=[source, *beqs, *extras])
    db.upsert_media(
        **row, has_atmos=int(source.atmos), has_beq=int(bool(beqs)), has_audio=1,
        probe_json=probe.to_json(), error=None, best_json=probe.best_json,
        beq_names=json.dumps([t.name for t in beqs]),
        rolloff_json=json.dumps(f.bass) if f.bass else None,
        rolloff_key=assess.rolloff_key(st.st_size, st.st_mtime, 1) if f.bass else None,
        rolloff_at=NOW - 90 * MIN if f.bass else None)

web.library_watch.state.update(
    watch=f"inotify: {ROOT}", next_scan_at=NOW + 13 * HOUR, last_scan_at=NOW - 2 * HOUR,
    audio={"enabled": True, "running": False, "current": "", "done": 14, "todo": 3,
           "error": None, "waiting": "jobs"})


# ---------------------------------------------------------------------------- job history

def original(fid: int) -> media.AudioTrack:
    return PROBE[fid].track(1)


def pin(fid: int) -> dict:
    """The film's file as it stands, which its filters and figures are held against."""
    return source_pin(FileStamp.of(film_path(FILM[fid])), original(fid))


def film_dict(fid: int) -> dict:
    f = FILM[fid]
    return {"tmdb": f.tmdb, "title": f.title, "year": f.year, "edition": f.edition}


def write_log(name: str, lines, start: float, end: float) -> str:
    path = web.S.log_dir / f"{name}.log"
    step = (end - start) / max(1, len(lines) - 1)
    path.write_text("".join(
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start + i * step))} {line}\n"
        for i, line in enumerate(lines)))
    return str(path)


def add_job(fid, params, status, ago, *, stage=None, percent=None, result=None, message="",
            log=(), prepare=None) -> int:
    """One row of the job history. `ago` is when it was made, started and ended, in minutes
    before now, with None for what has not happened. `result` may be made from the new id."""
    created, started, finished = (None if m is None else NOW - m * MIN for m in ago)
    jid = db.create_job(fid, str(film_path(FILM[fid])), params, prepare)
    assert jid, f"{FILM[fid].title} already has an active {params['phase']} job"
    if callable(result):
        result = result(jid)
    if result and status == "done" and params["phase"] in ("design", "measure"):
        store.record(db, fid, jid, result)  # as the queue does when such a job ends
        message = worker._found(result)
    db.update_job(
        jid, status=status, percent=percent, message=message,
        stage=stage or {"queued": "Queued", "done": "Done", "failed": "Failed",
                        "cancelled": "Cancelled"}[status],
        result_json=json.dumps(result) if result else None,
        created_at=created, started_at=started, finished_at=finished,
        log_path=write_log(f"job-{jid}", log, started, finished or NOW) if log else None)
    return jid


def design_params(fid):
    return {"phase": "design", "track_id": 1, "cutoffs": CUTOFFS}


def design_result(fid, declined=("", "")):
    """What pipeline.prepare.run_design returns."""
    rows = [ENTRY[row[0]] for row in FORGE.get(fid, ())]
    offered = [e for e in rows if not e["rejected"]]
    t = original(fid)
    return {"phase": "design", "film": film_dict(fid), "track": prepare._track_dict(t),
            "track_key": PROBE[fid].track_key(t), "source": pin(fid),
            "design": {"declined": bool(declined[0]), "decline_reason": declined[0] or None,
                       "decline_message": declined[1] or None, "candidates": offered,
                       "rejected": [e for e in rows if e["rejected"]],
                       "release": FORGE_RELEASE, "design_version": DESIGN_VERSION,
                       "design_settings": "default", "designer_build": "seed",
                       "run_record": None, "adjustment_outcomes": None,
                       "bass_management": {"lpf_fs": 80, "lpf_position": "Before",
                                           "headroom_type": "WCS"},
                       "seconds": 21.4,
                       "analysis": {"wav": "", "fs": 1000, "frames": int(DURATION_S * 1000),
                                    "channels": list(layouts.names(t.channels, t.layout)),
                                    "layout": t.layout, "mapping": "ffmpeg_layout_v1",
                                    "coverage": 1.0}},
            "levels": levels_of(fid, [row[0] for row in FORGE.get(fid, ()) if not row[-1]])}


def measure_params(fid, keys):
    return {"phase": "measure", "track_id": 1, "filters": [ENTRY[k] for k in keys],
            "cutoffs": CUTOFFS}


def measure_result(fid, keys):
    """What pipeline.prepare.run_measure returns."""
    t = original(fid)
    return {"phase": "measure", "track": prepare._track_dict(t),
            "track_key": PROBE[fid].track_key(t), "source": pin(fid),
            "levels": levels_of(fid, keys), "profile": bass_profile(fid)}


def encode_params(fid, key, cutoff=0.0, replace=True, changed_since=True):
    """`changed_since`: the encode replaced the file, so it read an older one than stands."""
    source = pin(fid)
    if changed_since:
        source |= {"size": source["size"] - 4096, "mtime_ns": source["mtime_ns"] - DAY * 10**9}
    return {"phase": "encode", "track_id": 1, "entry": ENTRY[key], "source": source,
            "format": "truehd", "target_peak_dbfs": TARGET_PEAK, "fixed_gain_db": None,
            "highpass_hz": cutoff, "default_flag": False, "replace_existing_beq": replace}


def measurement(rng, boost=False):
    f = np.linspace(1, 165, 660)
    peak = -14 - 48 / (1 + (f / 26) ** 4) - 0.03 * f + rng.normal(0, 0.7, f.size)
    avg = peak - 17 + rng.normal(0, 0.5, f.size)
    hf = np.linspace(1, 60, 120)
    ht = np.linspace(0, 5760, 480)
    heat = (-62 - 44 / (1 + (hf[:, None] / 26) ** 4) + 18 * np.sin(ht[None, :] / 310) ** 2
            + rng.normal(0, 4, (hf.size, ht.size)))
    if boost:
        lift = 44 / (1 + (f / 26) ** 4)
        peak, avg = peak + lift, avg + lift
        heat = heat + 44 / (1 + (hf[:, None] / 26) ** 4)
    return charts.Measurement(f, peak, avg, hf, ht, heat, 5760.0, 276_480_000)


def chart_set(jid, fid, name, key, gain, peak_in, peak_out):
    """The before and after charts of an encode, drawn by the app's own chart code."""
    f, rng = FILM[fid], np.random.default_rng(jid)
    before, after = measurement(rng), measurement(rng, True)
    info = charts.ChartInfo(
        title=f"{f.title} ({f.year})", source_track=f"{original(fid).label} · English · track 1",
        beq_track=name, beq_source=ENTRY[key]["label"], filters=ENTRY[key]["filters"],
        gain_db=gain, peak_in_dbfs=peak_in, peak_out_dbfs=peak_out,
        encoder="beq-encoder 0.1.0", date=time.strftime("%Y-%m-%d", time.localtime(NOW)))
    out = web.S.charts_dir / f"job-{jid}"
    out.mkdir(parents=True, exist_ok=True)
    charts.render_spectrum(before, after, info, out / "spectrum.png")
    charts.render_heatmap(before, after, info, out / "heatmap.png")
    (out / "beq.json").write_text(json.dumps({"title": info.title, "beq_track": name,
                                              "gain_db": gain}))
    return {"dir": str(out), "files": {"spectrum": "spectrum.png", "heatmap": "heatmap.png",
                                       "report": "beq.json"},
            "names": {"spectrum": f"beq-{jid}-spectrum.png", "heatmap": f"beq-{jid}-heatmap.png",
                      "report": f"beq-{jid}.json"}}


def encode_result(fid, n, replaced=()):
    """What pipeline.encode._deliver returns for the film's nth BEQ track: jid -> result."""
    key, cutoff = FILM[fid].beq[n - 1]
    t, beq = original(fid), PROBE[fid].beq_tracks[n - 1]
    route = web.support.plan(t).route
    gain, peak_in = level_change(fid, key, cutoff), source_peak(fid)
    filtered = filtered_peak(fid, key, cutoff)
    names = layouts.names(t.channels, t.layout)
    fidelity = ({"ok": True, "frames": int(DURATION_S * 48000), "max_err_lsb": 1.18,
                 "offset_frames": 0} if route == "pcm" else
                {"method": "objects decoded by Cavern, re-clustered by DEE" if route == "joc"
                 else "objects decoded by truehdd", "objects": 15, "bed": "LFE",
                 "out_elements": 16, "frames": int(DURATION_S * 48000), "max_err_lsb": 0.74,
                 "sum_snr_db": 63.5, "encoder_gain_db": -0.011, "min_object_corr": 0.9878,
                 "offset_frames": 0})
    report = {"channels": 16 if t.objects else t.channels, "bits": 24, "sample_rate": 48000,
              "peak_in_dbfs": peak_in, "peak_filtered_dbfs": filtered, "gain_db": gain,
              "peak_out_dbfs": round(filtered + gain, 2), "clipped_samples": 0,
              "seconds": 318.4, "fidelity": fidelity}
    return lambda jid: {
        "phase": "encode", "track_name": beq.name, "format": "truehd", "route": route,
        "codec": beq.beq_tags["output"], "highpass_hz": cutoff, "filter_report": report,
        "output_bytes": 4_100_000_000 + 1000 * jid, "replaced_tracks": list(replaced),
        "sync": {"lag_ms": 0.0, "confidence": 0.98, "applied_ms": 0, "channels": len(names),
                 "channel_corr": {c: 0.99 for c in names}},
        "charts": chart_set(jid, fid, beq.name, key, gain, peak_in, report["peak_out_dbfs"]),
        "warnings": [], "source": pin(fid)}


def added(fid, n) -> str:
    key, cutoff = FILM[fid].beq[n - 1]
    return (f"Added track “{PROBE[fid].beq_tracks[n - 1].name}” "
            f"(gain {level_change(fid, key, cutoff)} dB)")


ENCODE_LOG = ["Reading the source track", "Filtering the channels", "Encoding with DEE",
              "Checking the encode against the filtered source", "Rendering before/after charts",
              "Remuxing into a temporary MKV", "A/V sync 0.00 ms, channel order matches",
              "Swapped in the new file, which keeps the original track"]
DESIGN_LOG = ["Checking source", "1 kHz analysis: 12.1 s", "Designer: POST /design",
              "Designer answered"]
MEASURE_LOG = ["Checking source", "1 kHz analysis cached", "Estimating the gain of each filter",
               "Finding the heaviest bass"]

# Oldest first. Two rows keep statuses that new jobs no longer get.
add_job(10, {"phase": "prepare", "track_id": None, "force_designer": False}, "unsupported",
        (9 * 1440, 9 * 1440 - 1, 9 * 1440 - 1.1), stage="Not supported",
        message="Not supported: DTS:X has objects no available decoder can keep, and lossy "
                "conversions are currently out of scope. The file was not changed.",
        prepare={"phase": "prepare", "film": film_dict(10), "catalogue": [], "design": None,
                 "track": prepare._track_dict(original(10)), "outcome": "unsupported",
                 "plan": {"ok": False, "label": "", "native": False, "route": "",
                          "reason": web.support.plan(original(10)).reason},
                 "message": "Not supported: lossy conversions are currently out of scope"},
        log=["Best track: DTS:X 7.1", "Not supported"])
add_job(14, {"phase": "prepare", "track_id": None, "force_designer": False}, "no_beq",
        (8 * 1440, 8 * 1440 - 1, 8 * 1440 - 2), stage="No BEQ",
        message="The designer found no roll-off to correct: no BEQ needed.",
        prepare={"phase": "prepare", "film": film_dict(14), "catalogue": [], "design": None,
                 "track": prepare._track_dict(original(14)), "outcome": "no_beq",
                 "plan": {"ok": True, "label": "TrueHD 5.1", "native": False, "route": "pcm",
                          "reason": ""},
                 "message": "The designer found no roll-off to correct: no BEQ needed."},
        log=["Matching the BEQ catalogue: no entry", "Designer declined"])

# Finished encodes, one for each BEQ track that this database saw made.
add_job(16, encode_params(16, "cat-burial", 15.0), "done", (4300, 4298, 4290), percent=100,
        result=encode_result(16, 1), message=added(16, 1), log=ENCODE_LOG)
add_job(17, encode_params(17, "cat-fury-a"), "done", (2900, 2899, 2880), percent=100,
        result=encode_result(17, 1), message=added(17, 1), log=ENCODE_LOG)
add_job(17, encode_params(17, "cat-fury-b", 10.0, replace=False), "done", (2870, 2869, 2851),
        percent=100, result=encode_result(17, 2), message=added(17, 2), log=ENCODE_LOG)
add_job(18, design_params(18), "done", (1500, 1499, 1492), percent=100,
        result=design_result(18), log=DESIGN_LOG)
add_job(18, encode_params(18, "forge-br-flatten"), "done", (1480, 1479, 1458), percent=100,
        result=encode_result(18, 1), message=added(18, 1), log=ENCODE_LOG)
add_job(15, measure_params(15, ["cat-klaus"]), "done", (200, 199, 195), percent=100,
        result=measure_result(15, ["cat-klaus"]), log=MEASURE_LOG)
add_job(15, encode_params(15, "cat-klaus"), "done", (150, 149, 140), percent=100,
        result=encode_result(15, 1), message=added(15, 1), log=ENCODE_LOG)

# Measurements: every filter of these films at every cutoff, and each film's bass profile.
for fid, keys in ((1, ["cat-edge-a", "cat-edge-b", "cat-edge-c", "cat-edge-d"]),
                  (2, ["cat-equi"]), (7, ["cat-mega"])):
    add_job(fid, measure_params(fid, keys), "done", (130 - fid, 129 - fid, 124 - fid),
            percent=100, result=measure_result(fid, keys), log=MEASURE_LOG)
# The Wedding Ringer was measured when the catalogue held one filter for it.
store.save_levels(db, 11, PROBE[11].track_key(original(11)), pin(11),
                  levels_of(11, ["cat-ring-a"]))

# BEQ Forge: two filters designed, then measured for the bass profile; and no filter designed.
add_job(4, design_params(4), "done", (95, 94, 88), percent=100, result=design_result(4),
        log=DESIGN_LOG)
add_job(4, measure_params(4, ["forge-cloudy-flatten", "forge-cloudy-parametric"]), "done",
        (87, 86, 84), percent=100, log=MEASURE_LOG,
        result=measure_result(4, ["forge-cloudy-flatten", "forge-cloudy-parametric"]))
add_job(12, design_params(12), "done", (80, 79, 75), percent=100, log=DESIGN_LOG,
        result=design_result(12, ("no_rolloff", "The bass extends flat to 8 Hz: nothing here "
                                                "for a BEQ to restore.")))

# What went wrong, what was stopped, and what is under way.
add_job(7, encode_params(7, "cat-mega", changed_since=False), "failed", (62, 61, 56),
        message="Lossless check failed: channel 4 (LFE) differs from the filtered source by "
                "3.2 LSB at frame 8,214,403. The source file was not changed.",
        log=ENCODE_LOG[:4] + ["FAILED: Lossless check failed: channel 4 (LFE) differs from "
                              "the filtered source by 3.2 LSB at frame 8,214,403"])
add_job(8, design_params(8), "cancelled", (45, 44, 43), log=["Checking source", "Cancelled"])
add_job(6, encode_params(6, "cat-hero", changed_since=False), "running", (25, 23, None),
        stage="Encoding with DEE", percent=47.3, log=ENCODE_LOG[:3] + ["DEE: step 3 of 6, 47%"])
add_job(5, design_params(5), "running", (4, 3, None),
        stage=f"Designing (beqforge {FORGE_RELEASE})", log=DESIGN_LOG[:3])
add_job(11, measure_params(11, ["cat-ring-a", "cat-ring-b", "cat-ring-c"]), "queued",
        (2, None, None))


# ---------------------------------------------------------------------------- clips

def add_clip(fid, keys, status, ago, *, start_s=None, stage=None, percent=None, result=None,
             message="", log=()) -> int:
    """One clip row, with the parameters the wizard sends and the same `ago` as add_job."""
    created, started, finished = (None if m is None else NOW - m * MIN for m in ago)
    params = {"track_id": 1, "source": pin(fid), "format": "truehd",
              "target_peak_dbfs": TARGET_PEAK, "fixed_gain_db": None, "highpass_hz": 0.0,
              "default_flag": False, "start_s": start_s, "length_s": 30.0,
              "filters": [{"entry": ENTRY[k], "peak_dbfs": filtered_peak(fid, k, 0.0)}
                          for k in keys]}
    cid = clips.create(db, fid, None, str(film_path(FILM[fid])), params)
    fields = dict(
        status=status, percent=percent, message=message,
        stage=stage or {"done": "Done", "failed": "Failed"}[status],
        result_json=json.dumps(result) if result else None,
        created_at=created, started_at=started, finished_at=finished,
        log_path=write_log(f"clip-{cid}", log, started, finished or NOW) if log else None)
    db.x(f"UPDATE clips SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
         [*fields.values(), cid])
    return cid


def clip_file(fid, keys, start_s: float) -> dict:
    """A real clip in the clip folder: five seconds, the original track and one FLAC track
    named for each filter. What pipeline.clip.run_clip returns for it."""
    t = original(fid)
    names = [f"BEQ [{level_change(fid, k, 0.0):+.1f} dB] {ENTRY[k]['label']}" for k in keys]
    path = CLIP_DIR / clip_name(film_path(FILM[fid]), start_s)
    titles = [t.label, *names]
    run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", NOISE + "5", "-ac", "6",
        *[a for _ in titles for a in ("-map", "0:a")],
        *[a for i, title in enumerate(titles) for a in (f"-metadata:s:a:{i}", f"title={title}")],
        "-c:a", "flac", str(path))
    return {"file": path.name, "path": str(path), "bytes": path.stat().st_size,
            "start_s": start_s, "asked_start_s": None, "picked": True, "length_s": 30.0,
            "original": {"name": t.label, "label": t.label},
            "tracks": [{"name": name, "digest": ENTRY[k]["digest"], "label": ENTRY[k]["label"],
                        "gain_db": level_change(fid, k, 0.0), "gain_from": "the whole film",
                        "sync_ms": 0.0, "lag_ms": 0.0, "confidence": 0.97,
                        "check": "sample-exact"} for k, name in zip(keys, names, strict=True)],
            "first_packets": {}, "warnings": []}


CLIP_LOG = ["Checking source", "Cutting the clip", "Encoding each filter",
            "Remuxing the clip"]
edge_clip = clip_file(1, ["cat-edge-a", "cat-edge-b"], 2110.0)
add_clip(1, ["cat-edge-a", "cat-edge-b"], "done", (110, 109, 106), percent=100,
         result=edge_clip, message=edge_clip["file"], log=CLIP_LOG + ["Clip written"])
add_clip(11, ["cat-ring-a", "cat-ring-b"], "failed", (50, 49, 48), start_s=1800.0,
         message="ffmpeg exited with code 1 while it cut the clip. The film file was not "
                 "changed.",
         log=CLIP_LOG[:2] + ["FAILED: ffmpeg exited with code 1 while it cut the clip"])
add_clip(4, ["forge-cloudy-flatten", "forge-cloudy-parametric"], "running", (3, 2, None),
         stage="beqforge 0.3.3 flatten: Encoding with DEE", percent=38.0, log=CLIP_LOG[:3])

# ---------------------------------------------------------------------------- serve

# The app rates each film and counts its filters and BEQ tracks, as it does after a scan or
# a job. It reads the filters above, so it comes last.
web.library_watch.reassess()

print(f"seeded in {time.monotonic() - SEED_STARTED:.1f} s")
print(f"library: {ROOT}   clips: {CLIP_DIR}")
print("base: http://localhost:8080 (the container's port)")
for f in FILMS:
    print(f"{f.role}: /films/{f.id}")
print("", end="", flush=True)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(web.app, host="0.0.0.0", port=8080, log_level="warning")
