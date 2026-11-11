"""Library assessment: candidates, stale BEQ tracks, the roll-off screen, the watch."""

import json
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import butter, sosfilt

from beq_encoder import (assess, catalogue, charts, config, dee, designer, library, media, support,
                         worker)
from beq_encoder.catalogue import Catalogue
from beq_encoder.db import DB
from beq_encoder.library import Library
from beq_encoder.pipeline import Cancelled, JobFailed
from beq_encoder.worker import Scanner

from .test_tracks import CAT, _t, cat, sources  # noqa: F401  (fixtures)

FILM = {"tmdb": "333371", "title": "Test Movie", "year": "2020", "edition": ""}


def _probe(*tracks):
    return media.ProbeResult(path="/x.mkv", duration_s=7200.0, tracks=[], audio=list(tracks))


def _beq(i=5, **tags):
    base = {"source": "catalogue", "digest": "atmos1", "author": "a1", "label": "a1",
            "encoder": "beq-encoder 0.1.0", "encoder_revision": str(assess.ENCODER_REVISION),
            "output": "TrueHD Atmos", "source_track": "TrueHD Atmos 7.1 (TrueHD Atmos)"}
    return _t(i, "truehd", 8, name="BEQ TrueHD Atmos (a1)", beq=True, layout="7.1",
              beq_tags={**base, **tags})


ATMOS = dict(layout="7.1", atmos=True)


# -- candidates ---------------------------------------------------------------------
def test_catalogue_entry_for_the_best_track_makes_a_candidate(cat):
    a = assess.assess(_probe(_t(1, "truehd_atmos", 8, **ATMOS)), FILM, cat)
    assert a["catalogue"] == "exact" and a["candidate"] == "catalogue" and a["authors"] == ["a1"]
    assert not a["stale"]


def test_no_filters_entry_means_no_candidate(cat):
    a = assess.assess(_probe(_t(1, "dtshd_ma", 6, layout="5.1(side)")), FILM, cat)
    assert a["catalogue"] == "no_beq_needed" and a["candidate"] is None


def test_rolloff_screen_makes_a_candidate_without_a_catalogue_entry(cat):
    p = _probe(_t(1, "lpcm", 2, layout="stereo"))
    other = {"tmdb": "1", "title": "Other Film", "year": "1999", "edition": ""}
    assert assess.assess(p, other, cat)["candidate"] is None
    assert assess.assess(p, other, cat, rolloff_info={"verdict": "clear"})["candidate"] == "rolloff"
    assert assess.assess(p, other, cat, rolloff_info={"verdict": "some"})["candidate"] == "maybe"


def test_unsupported_best_track_is_never_a_candidate(cat):
    a = assess.assess(_probe(_t(1, "dtsx", 8, layout="7.1")), FILM, cat,
                      rolloff_info={"verdict": "clear"})
    assert a["best"]["supported"] is False and a["candidate"] is None


# -- stale BEQ tracks ---------------------------------------------------------------
def _stale(cat, beq, best=None, **kw):
    best = best or _t(1, "truehd_atmos", 8, **ATMOS)
    return assess.assess(_probe(best, beq), FILM, cat, **kw)


def test_a_current_beq_track_is_not_stale(cat):
    a = _stale(cat, _beq())
    assert not a["stale"] and a["candidate"] is None and a["beq"][0]["reasons"] == []


def test_withdrawn_or_updated_catalogue_entry_is_stale(cat):
    a = _stale(cat, _beq(digest="gone"))
    assert a["stale"] and "has been updated" in a["reasons"][0]  # a1 still has an entry
    a = _stale(cat, _beq(digest="gone", author="someone"))
    assert "changed or been withdrawn" in a["reasons"][0]


def test_designed_beq_is_stale_once_the_catalogue_has_the_track(cat):
    a = _stale(cat, _beq(source="designer", digest="design:1", design_version="9"))
    assert a["stale"] and "catalogue now has 1 entry" in a["reasons"][0]


def test_designed_beq_is_stale_when_the_designer_answers_differently(cat):
    best = _t(1, "lpcm", 2, layout="stereo")
    beq = _beq(source="designer", digest="design:1", design_version="8", output="TrueHD 2.0",
               source_track="LPCM 2.0 (PCM)")
    a = _stale(cat, beq, best=best,
               designer={"designer_release": "0.4.0", "design_version": 9})
    other_film = assess.assess(_probe(best, beq), {"title": "Nope"}, cat,
                               designer={"designer_release": "0.4.0", "design_version": 9})
    assert other_film["stale"] and "design version 8 → 9" in other_film["reasons"][0]
    assert a["stale"]


def test_a_new_designer_release_with_the_same_answers_is_not_stale(cat):
    best = _t(1, "lpcm", 2, layout="stereo")
    beq = _beq(source="designer", digest="design:1", design_version="8", designer_release="0.3.0",
               output="TrueHD 2.0", source_track="LPCM 2.0 (PCM)")
    a = assess.assess(_probe(best, beq), {"title": "Nope"}, cat,
                      designer={"designer_release": "0.9.0", "design_version": 8})
    assert not a["stale"] and a["beq"][0]["reasons"] == []


def test_tracks_older_than_the_audio_revision_are_stale(cat, monkeypatch):
    monkeypatch.setattr(assess, "AUDIO_REVISION", 2)
    monkeypatch.setitem(assess.REVISIONS, 2, "a better Atmos encode")
    a = _stale(cat, _beq(encoder_revision="1"))
    assert a["stale"] and "made before a better Atmos encode" in a["reasons"][0]
    a = _stale(cat, _beq(encoder_revision=None))  # tagged, but before revisions existed
    assert a["stale"]
    assert not _stale(cat, _beq(encoder_revision="2"))["stale"]


def test_test_only_flac_is_stale_once_truehd_is_possible(cat):
    best = _t(1, "lpcm", 2, layout="stereo")
    beq = _beq(output="FLAC 2.0", source_track="LPCM 2.0 (PCM)")
    a = _stale(cat, beq, best=best)
    assert not a["stale"] and "test-only FLAC" in a["beq"][0]["notes"][0]
    a = _stale(cat, beq, best=best, dee_outputs=True)
    assert a["stale"] and "TrueHD 2.0 is now possible" in a["reasons"][0]


def test_bed_only_flac_of_an_atmos_track_is_stale_once_truehd_atmos_is_possible(cat):
    beq = _beq(output="FLAC 7.1 bed")
    a = _stale(cat, beq, dee_outputs=False)
    assert not a["stale"] and "bed only" in a["beq"][0]["notes"][0]
    a = _stale(cat, beq, dee_outputs=True)
    assert a["stale"] and "bed only" in a["reasons"][0]
    assert "TrueHD Atmos is now possible" in a["reasons"][0]


def test_untagged_beq_track_is_reported_not_judged(cat):
    t = _t(5, "truehd", 8, name="BEQ by hand", beq=True)
    a = _stale(cat, t)
    assert not a["stale"] and "no BEQ tags" in a["beq"][0]["notes"][0]


# -- roll-off screen -----------------------------------------------------------------
def _noise(seconds=600, hpf_hz=None, seed=0):
    rng = np.random.default_rng(seed)
    # bass-heavy programme: pink-ish noise in bursts, like scenes
    x = np.cumsum(rng.standard_normal(seconds * charts.FS)) * 1e-4
    x -= np.convolve(x, np.ones(2000) / 2000, mode="same")  # remove drift below ~0.5 Hz
    env = np.repeat(rng.random(seconds // 5) ** 3, 5 * charts.FS)[: len(x)]
    x = x * env
    x = 0.3 * x / np.abs(x).max()
    if hpf_hz:
        x = sosfilt(butter(8, hpf_hz, "highpass", fs=charts.FS, output="sos"), x)
    return x


def _measure(x):
    return charts.measure_stream(x[i:i + 50_000] for i in range(0, len(x), 50_000))


def test_rolloff_screen_tells_a_mastering_high_pass_from_full_range():
    clear = assess.rolloff(_measure(_noise(hpf_hz=30)))
    assert clear.verdict == "clear" and 20 <= clear.corner_hz <= 40, clear
    assert clear.depth15_db > 20 and "clear roll-off" in clear.summary
    full = assess.rolloff(_measure(_noise()))
    assert full.verdict == "none", full


def test_samples_spread_over_the_film():
    segs = assess.sample_segments(7200.0)
    assert len(segs) == assess.SEGMENTS and segs[0][0] > 0 and segs[-1][0] + segs[-1][1] < 7200
    assert assess.sample_segments(300.0) == [(0.0, 300.0)]


# -- the running library -------------------------------------------------------------
class _Jobs:
    running: dict = {}


@pytest.fixture
def lib_env(tmp_path, monkeypatch, cat):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    monkeypatch.setenv("MEDIA_ROOTS", str(media_dir))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("WATCH_SETTLE_SECONDS", "1")
    monkeypatch.setenv("SCAN_INTERVAL_HOURS", "0")
    s = config.load()
    db = DB(s.db_path)
    scanner = Scanner(s, db)
    lib = Library(s, db, cat, scanner, _Jobs(), designer_info=lambda: {}, dee_outputs=lambda: False)
    lib.audio_start_delay = 0.2
    scanner.on_scanned = lib.on_scanned
    yield s, db, lib, media_dir
    lib.stop()


def _wait(fn, timeout=90.0):  # generous: the full suite runs ffmpeg jobs in parallel
    t = time.time()
    while time.time() - t < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.25)
    return None


def _add_film(db, *tracks, **film):
    return db.upsert_media(path="/media/film.mkv", size=1, mtime=1.0,
                           probe_json=_probe(*tracks).to_json(), **film)


def _row(db, media_id):
    return db.one("SELECT * FROM media WHERE id=? AND assess_json IS NOT NULL", (media_id,))


def test_assessment_waits_for_the_catalogue_and_runs_when_it_loads(lib_env):
    s, db, lib, _ = lib_env
    s.scan_on_start, s.assess_audio, s.watch = False, False, "off"
    loading = Catalogue(lib.catalogue.path, "", 9999)
    lib.catalogue = loading
    media_id = _add_film(db, _t(1, "truehd_atmos", 8, **ATMOS), _beq(),
                         tmdb=FILM["tmdb"], title=FILM["title"], year=FILM["year"], edition="")
    assert lib.reassess() == 0 and _row(db, media_id) is None
    lib.start()
    loading.refresh()
    row = _wait(lambda: _row(db, media_id), 15)
    assert row is not None and not row["stale"]
    assert json.loads(row["assess_json"])["catalogue"] == "exact"


def test_a_designer_appearing_or_changing_reassesses(lib_env):
    s, db, lib, _ = lib_env
    designer: dict = {}
    lib.designer_info = lambda: designer
    media_id = _add_film(db, _t(1, "lpcm", 2, layout="stereo"),
                         _beq(source="designer", digest="design:1", design_version="8",
                              output="TrueHD 2.0", source_track="LPCM 2.0 (PCM)"),
                         title="Nope", year="1999", edition="")
    lib.reassess()
    assert not _row(db, media_id)["stale"]
    designer.update(ok=True, designer_release="0.4.0", design_version=9)
    lib._watch_inputs()
    assert _row(db, media_id)["stale"]
    designer.update(design_version=8)
    lib._watch_inputs()
    assert not _row(db, media_id)["stale"]


def test_a_designer_change_waits_quietly_for_the_catalogue(lib_env, caplog):
    s, db, lib, _ = lib_env
    designer = {"ok": True, "designer_release": "0.4.0", "design_version": 9}
    lib.designer_info = lambda: designer
    media_id = _add_film(db, _t(1, "lpcm", 2, layout="stereo"), title="Nope", year="1999",
                         edition="")
    loading = Catalogue(lib.catalogue.path, "", 9999)
    lib.catalogue = loading
    with caplog.at_level("INFO", logger="beq_encoder.library"):
        lib._watch_inputs()
        lib._watch_inputs()
    assert "Designer changed" not in caplog.text and _row(db, media_id) is None
    loading.load()
    lib.catalogue = loading  # as the running library has it once the first load is done
    with caplog.at_level("INFO", logger="beq_encoder.library"):
        lib._watch_inputs()
    assert "Designer changed" in caplog.text and _row(db, media_id) is not None
    caplog.clear()
    lib._watch_inputs()
    assert "Designer changed" not in caplog.text


def test_a_reassessment_is_one_transaction(lib_env):
    s, db, lib, _ = lib_env
    ids = [db.upsert_media(path=f"/media/film{i}.mkv", size=1, mtime=1.0, title=f"Film {i}",
                           probe_json=_probe(_t(1, "lpcm", 2, layout="stereo")).to_json())
           for i in range(3)]
    statements: list[str] = []
    db.conn.set_trace_callback(statements.append)
    assert lib.reassess() == 3
    db.conn.set_trace_callback(None)
    assert sum(q.upper().startswith("BEGIN") for q in statements) == 1
    assert sum(q.upper().startswith("COMMIT") for q in statements) == 1
    assert all(_row(db, i) for i in ids)


def test_the_scheduled_scan_waits_for_the_scan_it_started(lib_env, monkeypatch):
    """Scanner.start returns before its thread has set state["running"]."""
    s, db, lib, _ = lib_env
    finished = threading.Event()

    class Slow:
        def __init__(self):
            self.state = {"running": False, "finished_at": None}
            self.calls = 0

        def start(self, **kw):
            self.calls += 1
            threading.Thread(target=self.run, daemon=True).start()
            return True

        def run(self):
            assert finished.wait(30)
            self.state["finished_at"] = 1.0

    lib.scanner = Slow()
    lib.catalogue.refresh = lambda *a, **k: None
    monkeypatch.setattr(lib._stop, "wait", lambda t: time.sleep(0.01) or False)
    scan = threading.Thread(target=lib._scan, daemon=True)
    scan.start()
    assert _wait(lambda: lib.scanner.calls == 1, 10)
    time.sleep(0.2)  # a scan seen as finished before it ran would have recorded it by now
    assert lib.state["last_scan_at"] is None
    finished.set()
    scan.join(10)
    assert lib.state["last_scan_at"] is not None


# -- background loops survive a failed pass ------------------------------------------
@pytest.fixture
def quick_loops(lib_env, monkeypatch):
    """The loops' own waits, cut to a moment (still ended by stop())."""
    s, db, lib, media_dir = lib_env
    real_wait = lib._stop.wait
    monkeypatch.setattr(lib._stop, "wait", lambda t: real_wait(min(t, 0.02)))
    monkeypatch.setattr(library, "LOOP_RETRY_S", 0.02)
    lib.catalogue.refresh = lambda *a, **k: None
    s.scan_on_start, s.assess_audio, s.watch = False, False, "off"
    return s, db, lib, media_dir


def _failing(real, times):
    """`real`, raising a database error the first `times` calls."""
    left = [times]

    def call(*args, **kw):
        if left[0]:
            left[0] -= 1
            raise sqlite3.OperationalError("disk I/O error")
        return real(*args, **kw)

    return call


def test_the_schedule_outlives_a_database_error(quick_loops, monkeypatch):
    s, db, lib, _ = quick_loops
    s.scan_on_start = True
    lib.designer_info = lambda: {"ok": True, "designer_release": "1", "design_version": 1}
    media_id = _add_film(db, _t(1, "lpcm", 2, layout="stereo"), title="Nope", year="1999",
                         edition="")
    # the start-up assessment, the one after the first scan and the first scheduled one
    monkeypatch.setattr(lib, "reassess", _failing(lib.reassess, 3))
    lib.start()
    assert _wait(lambda: _row(db, media_id), 15), "the schedule stopped at a failed pass"
    assert lib.state["last_scan_at"] is not None


def test_the_bass_screen_outlives_a_database_error(quick_loops, monkeypatch):
    s, db, lib, _ = quick_loops
    s.assess_audio = True
    media_id, _probe_result = _screenable(db)
    monkeypatch.setattr(lib, "_screen", lambda *a: {"schema": assess.ROLLOFF_SCHEMA,
                                                    "verdict": "none"})
    monkeypatch.setattr(db, "x", _failing(db.x, 2))
    lib.start()
    assert _wait(lambda: _rolloff(db, media_id) == {"schema": assess.ROLLOFF_SCHEMA,
                                                    "verdict": "none"}, 15)
    assert _wait(lambda: lib.state["audio"]["done"] == 1, 15)
    assert not lib.state["audio"]["running"], "the screen is over, whatever failed on the way"


def test_a_failing_pass_waits_before_the_next_one(lib_env, monkeypatch):
    s, db, lib, _ = lib_env
    s.scan_on_start, s.watch = False, "off"
    monkeypatch.setattr(library, "LOOP_RETRY_S", 0.2)
    attempts: list[float] = []

    def refused(*args, **kw):
        attempts.append(time.monotonic())
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(lib, "_audio_next", refused)
    lib.start()
    assert _wait(lambda: len(attempts) >= 3, 15), "the loop did not carry on"
    assert all(b - a >= 0.15 for a, b in zip(attempts, attempts[1:], strict=False)), attempts


def test_watched_files_are_still_taken_after_a_failed_pass(quick_loops, monkeypatch):
    s, db, lib, media_dir = quick_loops
    s.watch_settle_seconds = 0
    started: list[list] = []

    def start(force, files):
        started.append(files)
        if len(started) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return True

    monkeypatch.setattr(lib.scanner, "start", start)
    first, second = media_dir / "a.mkv", media_dir / "b.mkv"
    first.touch()
    second.touch()
    lib._note(str(first))
    lib.start()
    assert _wait(lambda: len(started) == 1, 15)
    lib._note(str(second))
    assert _wait(lambda: started[-1] == [second], 15), "the loop stopped at the failed pass"


def test_a_full_reassessment_never_overwrites_a_newer_answer(lib_env):
    """A pass that read a row before it changed must not write its answer after the pass
    that read the change."""
    s, db, lib, _ = lib_env
    media_id = _add_film(db, _t(1, "lpcm", 2, layout="stereo"), title="Nope", year="1999",
                         edition="")
    reading, resume = threading.Event(), threading.Event()
    first_call = [True]

    def designer_info():  # called between a pass's read of the rows and its write
        if first_call[0] and threading.current_thread() is not threading.main_thread():
            first_call[0] = False
            reading.set()
            assert resume.wait(30)
        return {}

    lib.designer_info = designer_info
    stale_pass = threading.Thread(target=lib.reassess, daemon=True)
    stale_pass.start()
    assert reading.wait(30)
    db.x("UPDATE media SET rolloff_json=? WHERE id=?",
         (json.dumps({"schema": assess.ROLLOFF_SCHEMA, "verdict": "clear"}), media_id))
    newer_pass = threading.Thread(target=lib.reassess, args=([media_id],), daemon=True)
    newer_pass.start()
    newer_pass.join(0.3)
    resume.set()
    stale_pass.join(30)
    newer_pass.join(30)
    assert _row(db, media_id)["candidate"] == "rolloff"


def test_excerpts_are_measured_without_windows_across_their_joins():
    silence_then_step = [np.zeros(30_000), np.full(30_000, 0.1)]
    # the DC step at each join leaks to 15 Hz in the window straddling it; within an
    # excerpt (no window crosses) DC stays out of the bass
    joined = charts.measure_stream(silence_then_step)
    separate = charts.measure_stream(silence_then_step, excerpts=True)
    i = int(np.argmin(np.abs(joined.freqs - 15.0)))
    assert joined.peak_db[i] > separate.peak_db[i] + 20


def test_yes_no_settings_read_alike(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "w"))
    for value, charts_on, keep in (("off", False, False), ("on", True, True), ("2", True, False),
                                   ("", True, False), ("NO", False, False)):
        monkeypatch.setenv("BEQ_CHARTS", value)
        monkeypatch.setenv("KEEP_WORK", value)
        s = config.load()
        assert (s.charts, s.keep_work) == (charts_on, keep), value


def test_a_bad_track_name_template_stops_the_service_at_start(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "w"))
    monkeypatch.setenv("TRACK_NAME_TEMPLATE", "BEQ {codex}")
    with pytest.raises(ValueError, match="TRACK_NAME_TEMPLATE"):
        config.load()
    monkeypatch.setenv("TRACK_NAME_TEMPLATE", "BEQ {codec} {orig}")
    assert config.load().track_name_template == "BEQ {codec} {orig}"


def test_watch_finds_a_new_file_and_screens_its_bass(lib_env, sources):
    s, db, lib, media_dir = lib_env
    lib.start()
    assert _wait(lambda: lib.state["watch"] == f"inotify: {media_dir}", 5)
    film = media_dir / "Other Film (1999)"
    film.mkdir()
    time.sleep(0.5)
    shutil.copy(sources["lpcm20"], film / "Other Film (1999).mkv")
    row = _wait(lambda: db.one("SELECT * FROM media WHERE path LIKE '%Other Film%'"))
    assert row is not None, lib.state
    a = json.loads(row["assess_json"])
    assert a["best"]["label"] == "LPCM 2.0" and a["catalogue"] == "none"
    row = _wait(lambda: db.one("SELECT * FROM media WHERE rolloff_json IS NOT NULL"))
    assert row is not None, lib.state
    ro = json.loads(row["rolloff_json"])
    assert ro["verdict"] in ("clear", "some", "none", "unknown") and ro["seconds"] > 10
    assert json.loads(row["assess_json"])["rolloff"]["verdict"] == ro["verdict"]
    # deleting the file forgets it
    (film / "Other Film (1999).mkv").unlink()
    assert _wait(lambda: db.one("SELECT COUNT(*) n FROM media")["n"] == 0, 15)


# -- what belongs to the library -----------------------------------------------------
@pytest.mark.parametrize("path, expected", [
    ("/media/Film (2001)/Film (2001).mkv", True),
    ("/media/Film (2001)/Film (2001).MKV", True),
    ("/media/Film (2001)/poster.jpg", False),
    ("/media/Film (2001)/.Film.beq-tmp-3.mkv", False),
    ("/media/.recycle/user/Film (2001).mkv", False),
    ("/media/Film (2001)/.Trash-1000/files/Film (2001).mkv", False),
    ("/elsewhere/Film.mkv", False),
])
def test_what_belongs_to_the_library(path, expected):
    assert worker.in_library(path, [Path("/media")]) is expected


def test_a_hidden_folder_above_the_media_root_excludes_nothing():
    root = Path("/mnt/.zfs/media")
    assert worker.in_library("/mnt/.zfs/media/Film.mkv", [root])
    assert not worker.in_library("/mnt/.zfs/media/.recycle/Film.mkv", [root])


def _film_in(folder, source):
    folder.mkdir(parents=True)
    shutil.copy(source, folder / "Film (2001).mkv")
    return folder / "Film (2001).mkv"


def test_the_watch_ignores_a_film_moved_to_a_recycle_bin(lib_env, sources):
    s, db, lib, media_dir = lib_env
    s.scan_on_start, s.assess_audio = False, False
    lib.start()
    assert _wait(lambda: lib.state["watch"] == f"inotify: {media_dir}", 5)
    (media_dir / ".recycle").mkdir()
    time.sleep(0.5)
    binned = media_dir / ".recycle" / "Film (2001).mkv"
    shutil.copy(sources["lpcm20"], binned)
    (media_dir / "Kept (2002)").mkdir()
    time.sleep(0.5)
    kept = media_dir / "Kept (2002)" / "Kept (2002).mkv"
    shutil.copy(sources["lpcm20"], kept)
    assert _wait(lambda: db.one("SELECT id FROM media WHERE path=?", (str(kept),))), lib.state
    assert db.one("SELECT id FROM media WHERE path=?", (str(binned),)) is None


def test_a_scan_skips_hidden_folders_and_forgets_what_is_in_them(lib_env, sources):
    s, db, lib, media_dir = lib_env
    kept = _film_in(media_dir / "Kept (2002)", sources["lpcm20"])
    binned = _film_in(media_dir / ".Trash-1000", sources["lpcm20"])
    db.upsert_media(path=str(binned), size=1, mtime=1.0)  # admitted by an older version
    finished = lib.scanner.state["finished_at"]
    assert lib.scanner.start(force=True)
    assert _wait(lambda: lib.scanner.state["finished_at"] != finished, 60)
    assert [r["path"] for r in db.q("SELECT path FROM media")] == [str(kept)]


def test_files_added_during_a_scan_are_queued_not_refused(lib_env, sources, monkeypatch):
    s, db, lib, media_dir = lib_env
    for i in range(3):
        d = media_dir / f"Film {i} (2001)"
        d.mkdir()
        shutil.copy(sources["lpcm20"], d / f"Film {i} (2001).mkv")
    extra = media_dir / "Late (2002)"
    extra.mkdir()
    scanner = lib.scanner
    gate, probing = threading.Event(), threading.Event()
    real_scan_file = worker.scan_file

    def scan_file_held_open(*args, **kw):
        probing.set()
        assert gate.wait(30)
        return real_scan_file(*args, **kw)

    monkeypatch.setattr(worker, "scan_file", scan_file_held_open)
    assert scanner.start(force=True)
    assert probing.wait(30)  # the scan is busy until the gate opens
    late = extra / "Late (2002).mkv"
    shutil.copy(sources["lpcm20"], late)
    assert scanner.start(files=[late])  # busy, but queued
    assert not scanner.start()          # a whole-library scan is refused while busy
    gate.set()
    assert _wait(lambda: not scanner.state["running"] and
                 db.one("SELECT COUNT(*) n FROM media")["n"] == 4, 60)


def test_network_shares_are_left_to_the_scheduled_scan(tmp_path, monkeypatch):
    from beq_encoder import library
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/sda1 / ext4 rw 0 0\n"
        "//nas/media/movies /media/movies cifs rw,vers=3.0 0 0\n"
        "C:\134 /media/local 9p rw,aname=drvfs 0 0\n"
        "/dev/sdb /media/with\040space ext4 rw 0 0\n")
    assert library.mount_fstype("/media/movies/a/b.mkv", str(mounts)) == "cifs"
    assert library.mount_fstype("/media/local", str(mounts)) == "9p"
    assert library.mount_fstype("/media/with space/x", str(mounts)) == "ext4"
    assert library.mount_fstype("/media/moviesX", str(mounts)) == "ext4"  # not a prefix match
    # the watch: inotify on the local root only, the share named as scan-only
    local, share = tmp_path / "local", tmp_path / "share"
    local.mkdir()
    share.mkdir()
    monkeypatch.setattr(library, "mount_fstype",
                        lambda p, m="": "cifs" if str(p).startswith(str(share)) else "ext4")
    watched: list = []
    monkeypatch.setattr(library.Library, "_watch_group",
                        lambda self, roots, polling: watched.append((roots, polling)))
    s = config.Settings(media_roots=[local, share], watch="inotify")
    lib = Library(s, None, None, None, None, designer_info=lambda: {}, dee_outputs=lambda: False)
    lib._watch_loop()
    assert watched == [([str(local)], False)]
    assert lib.state["watch"] == f"inotify: {local} · {share} (cifs): scheduled scan"
    s.watch = "poll"
    watched.clear()
    lib._watch_loop()
    assert watched == [([str(local), str(share)], True)]


# -- a job's last write -------------------------------------------------------------
@pytest.fixture
def job_env(tmp_path, monkeypatch):
    """A real queue over a real database, running a runner the test scripts. The runner's
    log cannot be written, as on a full or unavailable /config."""
    plan: dict = {}

    class FakeRunner:
        def __init__(self, settings, job_id, path, params, progress, log_path, catalogue=None):
            self.src = path

        def run(self):
            return plan["run"]()

        def log(self, msg):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(worker, "JobRunner", FakeRunner)
    s = config.Settings(config_dir=tmp_path, work_dir=tmp_path)
    db = DB(tmp_path / "jobs.sqlite3")
    q = worker.JobQueue(s, db)
    yield q, db, plan
    q.stop()


def _job_status(db, job_id):
    return db.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"]


@pytest.mark.parametrize("error, status", [(Cancelled(), "cancelled"),
                                           (JobFailed("no good"), "failed"),
                                           (RuntimeError("boom"), "failed")])
def test_a_job_whose_log_cannot_be_written_still_ends_and_frees_its_file(job_env, error, status):
    q, db, plan = job_env

    def run():
        raise error

    plan["run"] = run
    job_id = db.create_job(None, "/media/a.mkv", {"phase": "encode"})
    q.start()
    assert _wait(lambda: _job_status(db, job_id) == status and not q.running, 15)
    assert db.create_job(None, "/media/a.mkv", {"phase": "encode"}) is not None


def test_a_claim_the_database_refused_frees_its_slot_and_is_tried_again(job_env, monkeypatch):
    q, db, plan = job_env

    def run():
        raise JobFailed("no good")

    plan["run"] = run
    monkeypatch.setattr(db, "transition", _failing(db.transition, 1))
    job_id = db.create_job(None, "/media/a.mkv", {"phase": "encode"})
    q.start()
    assert _wait(lambda: _job_status(db, job_id) == "failed" and not q.running, 15)


def test_a_job_that_cannot_get_a_thread_goes_back_in_the_queue(job_env, monkeypatch):
    q, db, plan = job_env

    def run():
        raise JobFailed("no good")

    plan["run"] = run
    real_thread, refused = threading.Thread, []

    class Refusing(real_thread):
        def start(self):
            if self.name.startswith("job-") and not refused:
                refused.append(self.name)
                raise RuntimeError("can't start new thread")
            super().start()

    job_id = db.create_job(None, "/media/a.mkv", {"phase": "encode"})
    monkeypatch.setattr(worker.threading, "Thread", Refusing)
    q.start()
    assert _wait(lambda: _job_status(db, job_id) == "failed" and not q.running, 15)
    assert refused == [f"job-{job_id}"]


def test_a_prepared_job_whose_log_cannot_be_written_still_awaits_approval(job_env):
    q, db, plan = job_env
    plan["run"] = lambda: {"phase": "prepare", "outcome": "awaiting", "message": "pick one"}
    job_id = db.create_job(None, "/media/a.mkv", {"phase": "prepare"})
    q.start()
    assert _wait(lambda: _job_status(db, job_id) == "awaiting" and not q.running, 15)


def test_a_final_status_the_database_refused_is_written_when_it_recovers(job_env, monkeypatch):
    q, db, plan = job_env
    monkeypatch.setattr(worker, "FINAL_WRITE_RETRY_S", 0.01)

    def run():
        raise JobFailed("no good")

    plan["run"] = run
    real, refused = db.update_job, []

    def update_job(job_id, **fields):
        if fields.get("status") == "failed" and not refused:
            refused.append(job_id)
            raise sqlite3.OperationalError("disk I/O error")
        real(job_id, **fields)

    monkeypatch.setattr(db, "update_job", update_job)
    job_id = db.create_job(None, "/media/a.mkv", {"phase": "encode"})
    q.start()
    assert _wait(lambda: _job_status(db, job_id) == "failed" and not q.running, 15)
    assert refused == [job_id]


# -- the scanner --------------------------------------------------------------------
def test_a_file_queued_just_as_the_scan_finishes_is_still_scanned(lib_env, sources):
    s, db, lib, media_dir = lib_env
    scanner = lib.scanner
    lock = scanner._qlock
    late = media_dir.parent / "Late (2002).mkv"  # outside the root: only the queue can find it
    shutil.copy(sources["lpcm20"], late)
    accepted: list[bool] = []
    fired = threading.Event()

    class AddsAFileOnceTheScanThreadHasLetGo:
        def __enter__(self):
            return lock.__enter__()

        def __exit__(self, *exc):
            lock.__exit__(*exc)
            scan_thread = threading.current_thread() is not threading.main_thread()
            if scan_thread and not scanner.state["running"] and not fired.is_set():
                fired.set()
                accepted.append(scanner.start(files=[late]))

    scanner._qlock = AddsAFileOnceTheScanThreadHasLetGo()
    assert scanner.start()
    assert _wait(lambda: accepted == [True], 15)
    assert _wait(lambda: db.one("SELECT id FROM media WHERE path=?", (str(late),)), 15)


def test_the_post_scan_callback_gets_the_media_id_and_cannot_fail_the_scan(lib_env, sources):
    s, db, lib, media_dir = lib_env
    film = media_dir / "Film (2001).mkv"
    shutil.copy(sources["lpcm20"], film)
    seen: list[int] = []

    def callback(media_id: int) -> None:
        seen.append(media_id)
        raise RuntimeError("the callback's own bug")

    media_id = worker.scan_file(s, db, film, on_scanned=callback)
    assert media_id is not None and seen == [media_id]


# -- the DEE probe ------------------------------------------------------------------
def test_a_failed_probe_stays_reported_until_a_probe_succeeds(tmp_path, monkeypatch):
    root = tmp_path / "dee"
    root.mkdir()
    (root / "dee_dthd_encoder").touch()
    (tmp_path / "dee_shim").touch()
    d = dee.Dee(root, tmp_path / "dee_shim")
    monkeypatch.setattr(dee, "_PROBES", {})
    monkeypatch.setattr(dee, "PROBE_TTL", 0.0)  # every result is already past its time to live
    refused = "dee_shim ERROR: optional capability preflight: unsupported build"
    answers = [(2, refused), (2, refused), (0, "Dolby TrueHD with Dolby Atmos encoder.")]

    def fake_run(cmd, **kw):
        code, out = answers.pop(0)
        return subprocess.CompletedProcess(cmd, code, out, "")

    monkeypatch.setattr(dee.subprocess, "run", fake_run)
    assert "unsupported build" in d.probe()
    st = d.status()
    assert not st["ok"] and any("unsupported build" in p for p in st["problems"])
    d.reprobe_while_failing()
    assert answers == [] and d.status()["ok"]


# -- what is accepted before anything is decoded ------------------------------------
def test_an_object_track_at_another_rate_is_refused_before_anything_is_decoded():
    p = support.plan(_t(1, "truehd_atmos", 8, layout="7.1", sample_rate=96000))
    assert not p.ok and "96 kHz" in p.reason
    assert support.plan(_t(1, "truehd_atmos", 8, layout="7.1", sample_rate=48000)).ok


SHELF = {"type": "low_shelf", "freq_hz": 20, "gain_db": 1, "q": 1}


@pytest.mark.parametrize("filters, says", [
    ([SHELF] * 11, "over 10 sections"),
    ([{**SHELF, "type": "notch"}], "Unsupported"),
    ([{**SHELF, "freq_hz": 0}], "Invalid"),
    ([{**SHELF, "gain_db": None}], "Invalid"),
])
def test_a_designer_candidate_with_bad_filters_is_refused(filters, says):
    with pytest.raises(designer.DesignerError, match=says):
        designer.validate({"candidates": [{"filters": filters}]})


@pytest.mark.parametrize("response", [{"candidates": []},
                                      {"candidates": None, "decline_reason": None}])
def test_a_designer_response_with_no_answer_is_refused(response):
    with pytest.raises(designer.DesignerError):
        designer.validate(response)


# -- a catalogue download -----------------------------------------------------------
@pytest.mark.parametrize("body", ["[]", '{"records": []}', '[{"renamed": "x"}]', "[1, 2]",
                                  "not json", json.dumps(CAT[:1])],
                         ids=["empty", "object", "reshaped", "numbers", "garbage", "shrunken"])
def test_an_unusable_download_keeps_the_good_copy(cat, tmp_path, body):
    download = tmp_path / "download.json"
    download.write_text(body)
    cat.url = download.as_uri()
    good = cat.path.read_bytes()
    digests = [e.digest for e in cat.entries]
    cat.refresh(force=True)
    assert cat.path.read_bytes() == good and [e.digest for e in cat.entries] == digests
    assert list(cat.path.parent.glob("*.tmp")) == []


def test_a_shrunken_download_is_measured_against_a_cached_copy_not_yet_loaded(cat, tmp_path):
    download = tmp_path / "download.json"
    download.write_text(json.dumps(CAT[:1]))
    starting_up = Catalogue(cat.path, download.as_uri(), -1)
    good = cat.path.read_bytes()
    starting_up.refresh()
    assert cat.path.read_bytes() == good and len(starting_up.entries) == len(CAT)


def test_a_usable_download_replaces_the_cached_copy(cat, tmp_path):
    download = tmp_path / "download.json"
    download.write_text(json.dumps(CAT[:2]))
    cat.url = download.as_uri()
    cat.refresh(force=True)
    assert [e.digest for e in cat.entries] == ["atmos1", "dts51"]


def test_an_empty_cached_file_does_not_count_as_a_loaded_catalogue(cat):
    cat.path.write_text("[]")
    fresh = Catalogue(cat.path, "", 9999)
    fresh.refresh()
    assert fresh.loaded_at is None and fresh.error


@pytest.mark.parametrize("bad", [{"title": None}, {"title": 7}, {"title": ""},
                                 {"author": 7}, {"source": ["Disc"]}, {"edition": {}},
                                 {"digest": ["x"]}, {"content_type": 1}, {"year": ["2001"]},
                                 {"audioTypes": "Atmos"}, {"audioTypes": [1]}])
def test_a_record_with_a_field_of_the_wrong_type_is_skipped_as_malformed(tmp_path, bad):
    filt = {"type": "LowShelf", "freq": 20, "gain": 5, "q": 0.9}
    good = {"title": "Good", "digest": "good", "filters": [filt]}
    p = tmp_path / "database.json"
    p.write_text(json.dumps([good, {**good, "digest": "bad", **bad}]))
    c = Catalogue(p, "", 9999)
    c.refresh()
    assert c.error is None and [e.digest for e in c.entries] == ["good"]
    assert c.for_track(None, "Good", None, None, "truehd", "5.1")


def test_a_year_or_tmdb_id_written_as_a_number_is_read_as_text(tmp_path):
    p = tmp_path / "database.json"
    p.write_text(json.dumps([{"title": "Good", "year": 2001, "theMovieDB": 603, "filters": []}]))
    c = Catalogue(p, "", 9999)
    c.load()
    assert (c.entries[0].year, c.entries[0].tmdb, c.entries[0].mv) == ("2001", "603", "0")


@pytest.mark.parametrize("broken", ["garbage", "unforeseen"])
def test_a_fresh_cached_file_that_cannot_be_loaded_is_replaced_by_a_download(
        cat, tmp_path, monkeypatch, broken):
    good = json.dumps(CAT)
    download = tmp_path / "download.json"
    download.write_text(good)
    if broken == "garbage":
        cat.path.write_text("not json")
    else:  # a defect no one foresaw, while the file is still being read
        real, left = catalogue._parse, [1]

        def parse(raw):
            if left[0]:
                left[0] -= 1
                raise RuntimeError("unforeseen")
            return real(raw)

        monkeypatch.setattr(catalogue, "_parse", parse)
    fresh = Catalogue(cat.path, download.as_uri(), 9999)
    fresh.refresh()
    assert fresh.loaded_at is not None and fresh.error is None
    assert [e.digest for e in fresh.entries] == [r["digest"] for r in CAT]


def test_a_failing_load_callback_is_not_a_failed_load(cat, tmp_path):
    fresh = Catalogue(cat.path, "", 9999)
    fresh.on_loaded = _failing(lambda: None, 1)
    fresh.refresh()
    assert fresh.error is None and fresh.entries


@pytest.mark.parametrize("bad", [{"q": 0}, {"q": -1}, {"freq": 0}, {"freq": float("nan")},
                                 {"gain": float("inf")}, {"count": -1}, {"count": float("inf")}])
def test_a_filter_with_impossible_numbers_is_skipped_as_malformed(tmp_path, bad):
    filt = {"type": "LowShelf", "freq": 20, "gain": 5, "q": 0.9}
    p = tmp_path / "database.json"
    p.write_text(json.dumps([{"title": "Good", "digest": "good", "filters": [filt]},
                             {"title": "Bad", "digest": "bad", "filters": [{**filt, **bad}]}]))
    c = Catalogue(p, "", 9999)
    c.load()
    assert [e.digest for e in c.entries] == ["good"]


# -- bass screen retries ------------------------------------------------------------
def _screenable(db):
    probe = _probe(_t(1, "lpcm", 2, layout="stereo"))
    return db.upsert_media(path="/media/film.mkv", size=1, mtime=1.0, has_audio=1, has_beq=0,
                           probe_json=probe.to_json(), title="Nope", year="1999", edition="",
                           best_json=json.dumps({"track_id": probe.best.track_id})), probe


def _rolloff(db, media_id):
    return json.loads(db.one("SELECT rolloff_json FROM media WHERE id=?",
                             (media_id,))["rolloff_json"] or "null")


def test_a_failed_screen_is_retried_with_a_growing_wait_and_then_succeeds(lib_env, monkeypatch):
    s, db, lib, _ = lib_env
    s.scan_on_start, s.watch = False, "off"
    media_id, _probe_result = _screenable(db)
    outcomes = [RuntimeError("share dropped"), RuntimeError("still down"),
                {"schema": assess.ROLLOFF_SCHEMA, "verdict": "none"}]
    calls: list[str] = []

    def screen(path, probe, best, nice):
        calls.append(path)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(lib, "_screen", screen)

    def age_failure_by(seconds):
        db.x("UPDATE media SET rolloff_at=? WHERE id=?", (time.time() - seconds, media_id))
        lib._audio_wake.set()

    lib.start()
    assert _wait(lambda: len(calls) == 1 and (_rolloff(db, media_id) or {}).get("failures") == 1, 30)
    lib._audio_wake.set()
    time.sleep(0.5)
    assert len(calls) == 1, "an unaged failure must not be screened again"

    age_failure_by(library.SCREEN_RETRY_S + 1)
    assert _wait(lambda: len(calls) == 2 and _rolloff(db, media_id)["failures"] == 2, 30)
    age_failure_by(library.SCREEN_RETRY_S + 1)
    time.sleep(0.5)
    assert len(calls) == 2, "the second failure waits twice as long"

    age_failure_by(2 * library.SCREEN_RETRY_S + 1)
    assert _wait(lambda: _rolloff(db, media_id)["verdict"] == "none", 30)
    assert len(calls) == 3


def test_a_changed_file_is_screened_at_once_even_after_a_failure(lib_env):
    s, db, lib, _ = lib_env
    media_id, probe = _screenable(db)
    key = assess.rolloff_key(1, 1.0, probe.best.track_id)
    failed = json.dumps({"schema": assess.ROLLOFF_SCHEMA, "verdict": "error", "error": "x"})
    db.x("UPDATE media SET rolloff_json=?, rolloff_key=?, rolloff_at=? WHERE id=?",
         (failed, key, time.time(), media_id))
    assert lib._audio_next() == []
    db.x("UPDATE media SET size=2 WHERE id=?", (media_id,))
    assert [t[0]["id"] for t in lib._audio_next()] == [media_id]
