"""What the app keeps about a film beside its file (store.py), the job kinds that fill it
(design and measure), and the queue rules that keep one film's activities apart."""

import json
import shutil
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from beq_encoder import analysis, config, media, store, worker
from beq_encoder.db import DB
from beq_encoder.pipeline import FileStamp, JobFailed, source_pin
from beq_encoder.pipeline.prepare import bass_profile

from .test_pipeline import ENTRY, _exe
from .test_tracks import CAT, _encode, _run, cat, env, fake_designer, sources  # noqa: F401

needs_tools = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("mkvmerge")), reason="needs ffmpeg + mkvtoolnix"
)

OLD = {"size": 1000, "mtime_ns": 1_700_000_000_123_456_789}
NEW = {"size": 1400, "mtime_ns": 1_700_000_500_987_654_321}
KEY = "0:pcm:PCM:2:stereo"
FILTER = {"digest": "d1", "label": "beqforge flatten",
          "filters": [{"type": "LowShelf", "freq": 20, "gain": 5, "q": 0.9, "count": 1}]}
LEVEL = {"peak_dbfs": 3.25, "channel": "L"}


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "store.sqlite3")


def _film(db, pin=OLD, path="/media/a.mkv"):
    return db.upsert_media(path=path, size=pin["size"], mtime=pin["mtime_ns"] / 1e9)


def _row(db, media_id):
    return db.one("SELECT * FROM media WHERE id=?", (media_id,))


def _digests(db, media_id):
    return [f["entry"]["digest"] for f in store.forge_filters(db, _row(db, media_id))]


def _wait(fn, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return None


# -- the store ----------------------------------------------------------------------
def test_a_saved_filter_is_listed_with_its_track_while_the_file_is_the_one_it_was_made_from(db):
    mid = _film(db)
    store.save_filters(db, mid, KEY, OLD, [FILTER])
    found = store.forge_filters(db, _row(db, mid))
    assert [(f["track_key"], f["entry"]) for f in found] == [(KEY, FILTER)]


@pytest.mark.parametrize("change", [{"size": OLD["size"] + 1},
                                    {"mtime": OLD["mtime_ns"] / 1e9 + 1.0}])
def test_a_filter_a_level_and_a_profile_stop_holding_when_the_file_changes(db, change):
    mid = _film(db)
    store.save_filters(db, mid, KEY, OLD, [FILTER])
    store.save_levels(db, mid, KEY, OLD, {"0": {"d1": LEVEL}})
    store.save_profile(db, mid, KEY, OLD, {"bucket_s": 10.0, "db": [-30.0]})
    assert store.levels(db, _row(db, mid), KEY) and store.profile(db, _row(db, mid), KEY)
    db.upsert_media(path="/media/a.mkv", **change)
    m = _row(db, mid)
    assert store.forge_filters(db, m) == []
    assert store.levels(db, m, KEY) == {}
    assert store.profile(db, m, KEY) is None


def test_what_was_saved_without_a_file_pin_never_holds(db):
    mid = _film(db)
    store.save_filters(db, mid, KEY, None, [FILTER])
    store.save_levels(db, mid, KEY, None, {"0": {"d1": LEVEL}})
    assert store.forge_filters(db, _row(db, mid)) == []
    assert store.levels(db, _row(db, mid), KEY) == {}


def test_a_filter_designed_again_for_the_changed_file_is_listed_once(db):
    mid = _film(db)
    store.save_filters(db, mid, KEY, OLD, [FILTER])
    _film(db, NEW)
    again = {**FILTER, "label": "beqforge flatten, again"}
    store.save_filters(db, mid, KEY, NEW, [again])
    assert [f["entry"] for f in store.forge_filters(db, _row(db, mid))] == [again]


def test_carry_over_makes_what_held_for_the_old_file_hold_for_the_new_one(db):
    mid = _film(db)
    older = {"size": 900, "mtime_ns": 1_600_000_000_000_000_000}
    store.save_filters(db, mid, KEY, older, [{**FILTER, "digest": "outdated"}])
    store.save_filters(db, mid, KEY, OLD, [FILTER])
    store.save_levels(db, mid, KEY, OLD, {"0": {"d1": LEVEL}})
    store.save_profile(db, mid, KEY, OLD, {"bucket_s": 10.0, "db": [-30.0, -12.5]})
    store.carry_over(db, mid, OLD, NEW)
    _film(db, NEW)
    m = _row(db, mid)
    assert _digests(db, mid) == ["d1"]  # what was already outdated stays outdated
    assert store.levels(db, m, KEY) == {"d1": {0.0: LEVEL}}
    assert store.profile(db, m, KEY) == {"bucket_s": 10.0, "db": [-30.0, -12.5]}


def test_carry_over_leaves_another_films_rows_alone(db):
    mid, other = _film(db), _film(db, path="/media/b.mkv")
    store.save_filters(db, other, KEY, OLD, [FILTER])
    store.carry_over(db, mid, OLD, NEW)
    assert _digests(db, other) == ["d1"]


def test_levels_come_back_by_filter_then_cutoff_for_the_track_asked_for(db):
    mid = _film(db)
    quieter = {"peak_dbfs": 1.5, "channel": "LFE"}
    store.save_levels(db, mid, KEY, OLD, {"0": {"d1": LEVEL, "d2": LEVEL},
                                          "12.5": {"d1": quieter}})
    store.save_levels(db, mid, "1:ac3:AC-3:6:5.1(side)", OLD, {"0": {"d9": LEVEL}})
    assert store.levels(db, _row(db, mid), KEY) == {
        "d1": {0.0: LEVEL, 12.5: quieter}, "d2": {0.0: LEVEL}}


def test_a_level_measured_again_replaces_the_earlier_figure(db):
    mid = _film(db)
    store.save_levels(db, mid, KEY, OLD, {"10": {"d1": LEVEL}})
    store.save_levels(db, mid, KEY, OLD, {"10": {"d1": {"peak_dbfs": -0.5, "channel": "R"}}})
    assert store.levels(db, _row(db, mid), KEY) == {
        "d1": {10.0: {"peak_dbfs": -0.5, "channel": "R"}}}


def test_a_draft_round_trips_is_replaced_and_is_cleared(db):
    mid = _film(db)
    assert store.draft(db, mid) == {}
    store.save_draft(db, mid, {"track": KEY, "digest": "d1", "cutoff": 10})
    store.save_draft(db, mid, {"track": KEY, "digest": "d2", "options": {"default": True}})
    assert store.draft(db, mid) == {"track": KEY, "digest": "d2", "options": {"default": True}}
    store.clear_draft(db, mid)
    assert store.draft(db, mid) == {}


def test_a_setting_round_trips_and_an_unset_one_is_none(db):
    assert store.setting(db, "highpass_hz") is None
    store.set_setting(db, "highpass_hz", "10")
    store.set_setting(db, "highpass_hz", "12")
    assert store.setting(db, "highpass_hz") == "12"
    assert store.setting(db, "something_else") is None


def test_the_cutoff_ladder_is_the_presets_the_default_and_a_custom_cutoff_in_order():
    presets = [0.0, 5.0, 10.0, 15.0, 20.0, 25.0]
    assert store.cutoff_ladder(10) == presets
    assert store.cutoff_ladder(18) == [0.0, 5.0, 10.0, 15.0, 18.0, 20.0, 25.0]
    assert store.cutoff_ladder(0, 12.5) == [0.0, 5.0, 10.0, 12.5, 15.0, 20.0, 25.0]


def test_record_keeps_a_design_results_filters_and_levels(db):
    mid = _film(db)
    second = {**FILTER, "digest": "d2"}
    store.record(db, mid, 7, {
        "phase": "design", "track_key": KEY, "source": OLD,
        "design": {"declined": False, "candidates": [FILTER, second]},
        "levels": {"0": {"d1": LEVEL, "d2": LEVEL}, "10": {"d1": LEVEL}}})
    m = _row(db, mid)
    assert [(f["track_key"], f["entry"]) for f in store.forge_filters(db, m)] == [
        (KEY, FILTER), (KEY, second)]
    assert store.levels(db, m, KEY) == {"d1": {0.0: LEVEL, 10.0: LEVEL}, "d2": {0.0: LEVEL}}
    assert store.profile(db, m, KEY) is None


def test_record_keeps_a_measure_results_levels_and_profile(db):
    mid = _film(db)
    profile = {"bucket_s": 10.0, "db": [-40.0, -18.2, -33.1]}
    store.record(db, mid, 8, {"phase": "measure", "track_key": KEY, "source": OLD,
                              "levels": {"15": {"atmos1": LEVEL}}, "profile": profile})
    m = _row(db, mid)
    assert store.levels(db, m, KEY) == {"atmos1": {15.0: LEVEL}}
    assert store.profile(db, m, KEY) == profile
    assert store.forge_filters(db, m) == []


def test_record_keeps_nothing_of_a_design_that_was_declined(db):
    mid = _film(db)
    store.record(db, mid, 9, {"phase": "design", "track_key": KEY, "source": OLD, "levels": {},
                              "design": {"declined": True, "candidates": []}})
    assert store.forge_filters(db, _row(db, mid)) == []


# -- one active job of each kind ----------------------------------------------------
def test_a_film_takes_one_queued_job_of_each_kind(db):
    first = db.create_job(None, "/media/a.mkv", {"phase": "design"})
    assert first is not None
    assert db.create_job(None, "/media/a.mkv", {"phase": "design"}) is None
    assert db.create_job(None, "/media/a.mkv", {"phase": "measure"}) is not None
    assert db.create_job(None, "/media/a.mkv", {"phase": "encode"}) is not None
    assert db.create_job(None, "/media/b.mkv", {"phase": "design"}) is not None
    db.update_job(first, status="done")
    assert db.create_job(None, "/media/a.mkv", {"phase": "design"}) is not None


# -- a track's key ------------------------------------------------------------------
def _t(i, kind, ch, **kw):
    base = dict(track_id=i, audio_index=i - 1, codec=kind, language="en", name="",
                default=False, channels=ch, atmos=kind == "truehd_atmos", beq=False, kind=kind,
                layout={8: "7.1", 6: "5.1(side)", 2: "stereo"}[ch])
    return media.AudioTrack(**{**base, **kw})


def _probe(*audio):
    return media.ProbeResult(path="/media/a.mkv", duration_s=7200.0, tracks=[], audio=list(audio))


def test_a_tracks_key_survives_a_beq_track_inserted_before_it():
    before = _probe(_t(1, "truehd_atmos", 8), _t(2, "ac3", 6))
    after = _probe(_t(1, "truehd_atmos", 8), _t(2, "truehd_atmos", 8, beq=True, name="BEQ"),
                   _t(3, "ac3", 6))
    for was, now in ((1, 1), (2, 3)):
        key = before.track_key(before.track(was))
        assert after.track_key(after.track(now)) == key
        assert after.track_by_key(key).track_id == now
        assert before.track_by_key(key).track_id == was


def test_two_tracks_of_one_format_have_different_keys_and_a_beq_track_is_never_found():
    pr = _probe(_t(1, "truehd_atmos", 8), _t(2, "truehd_atmos", 8, beq=True),
                _t(3, "truehd_atmos", 8))
    first, second = pr.track_key(pr.track(1)), pr.track_key(pr.track(3))
    assert first != second
    assert [pr.track_by_key(first).track_id, pr.track_by_key(second).track_id] == [1, 3]
    assert pr.track_by_key("7:ac3:ac3:6:5.1(side)") is None


# -- the bass level over the running time --------------------------------------------
def _programme(seconds=60, loud=(32, 38), amplitude=0.5):
    t = np.arange(seconds * analysis.FS) / analysis.FS
    x = np.zeros((len(t), 2))
    x[:, 0] = 1e-4 * np.sin(2 * np.pi * 30 * t)
    on = slice(loud[0] * analysis.FS, loud[1] * analysis.FS)
    x[on, 0] = amplitude * np.sin(2 * np.pi * 30 * t[on])
    return x


def _blocks(x, sizes):
    """`x` cut into blocks of `sizes` in turn, over and over."""
    out, at, i = [], 0, 0
    while at < len(x):
        out.append(x[at:at + sizes[i % len(sizes)]])
        at += sizes[i % len(sizes)]
        i += 1
    return out


@pytest.mark.parametrize("sizes", [(60_000,), (10_000,), (7_333,), (25_000,), (4_999, 12_001, 1)])
def test_the_bass_profile_is_loudest_in_the_bucket_that_holds_the_loud_stretch(sizes):
    x = _programme()
    p = bass_profile(_blocks(x, sizes), ["L", "R"])
    assert p["bucket_s"] == 10.0 and len(p["db"]) == 6
    assert int(np.argmax(p["db"])) == 3
    # a 0.5 sine on L for 6 of the bucket's 10 seconds, at the mono mix's gain for a main
    expected = 10 * np.log10((0.5 * analysis.MAIN_GAIN) ** 2 / 2 * 0.6)
    assert p["db"][3] == pytest.approx(expected, abs=0.1)
    assert p["db"] == pytest.approx(bass_profile([x], ["L", "R"])["db"], abs=0.11)
    assert max(v for i, v in enumerate(p["db"]) if i != 3) < expected - 40


def test_the_bass_profile_weights_the_lfe_above_a_main_channel():
    x = _programme()
    main = bass_profile([x], ["L", "LFE"])["db"][3]
    lfe = bass_profile([x[:, ::-1]], ["L", "LFE"])["db"][3]
    assert lfe - main == pytest.approx(10.0, abs=0.11)


# -- the queue keeps a film's activities apart ---------------------------------------
@pytest.fixture
def queue(tmp_path, monkeypatch):
    """A real queue over a real database. Its runner ends a job at once with what
    `results` holds for the job's path, except while the path is in `held`."""
    held: set[str] = set()
    results: dict[str, dict] = {}
    released = threading.Event()

    class FakeRunner:
        def __init__(self, settings, job_id, path, params, progress, log_path, catalogue=None):
            self.src = path
            self.job = type("J", (), {"params": params})()

        def run(self):
            while self.src in held and not released.wait(0.02):
                pass
            return results.get(self.src) or {"phase": "measure", "levels": {}}

        def log(self, msg):
            pass

        def cancel(self):
            released.set()

    monkeypatch.setattr(worker, "JobRunner", FakeRunner)
    s = config.Settings(config_dir=tmp_path, work_dir=tmp_path)
    s.max_concurrent_jobs = 4
    database = DB(tmp_path / "jobs.sqlite3")
    q = worker.JobQueue(s, database)
    yield q, database, held, results
    released.set()
    q.stop()


def _status(db, job_id):
    return db.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"]


def test_a_films_second_job_waits_for_its_first_while_another_films_job_runs(queue):
    q, db, held, _ = queue
    held.add("/media/a.mkv")
    design = db.create_job(None, "/media/a.mkv", {"phase": "design"})
    encode = db.create_job(None, "/media/a.mkv", {"phase": "encode"})
    other = db.create_job(None, "/media/b.mkv", {"phase": "design"})
    q.start()
    assert _wait(lambda: _status(db, other) == "done")
    assert (_status(db, design), _status(db, encode)) == ("running", "queued")
    q.kick()
    time.sleep(0.5)
    assert _status(db, encode) == "queued"
    held.clear()
    assert _wait(lambda: _status(db, design) == "done" and _status(db, encode) == "done")


def test_a_beq_track_is_not_added_while_a_clip_is_cut_from_the_film(queue):
    q, db, _, _ = queue
    clip = db.insert(
        "INSERT INTO clips (path, status, params_json, created_at) VALUES (?, 'running', '{}', ?)",
        ("/media/a.mkv", time.time()))
    encode = db.create_job(None, "/media/a.mkv", {"phase": "encode"})
    measure = db.create_job(None, "/media/a.mkv", {"phase": "measure"})
    elsewhere = db.create_job(None, "/media/b.mkv", {"phase": "encode"})
    q.start()
    assert _wait(lambda: _status(db, measure) == "done" and _status(db, elsewhere) == "done")
    q.kick()
    time.sleep(0.5)
    assert _status(db, encode) == "queued"
    db.x("UPDATE clips SET status='done' WHERE id=?", (clip,))
    q.kick()
    assert _wait(lambda: _status(db, encode) == "done")


def test_a_finished_design_jobs_filters_and_levels_are_the_films_when_the_job_says_done(
        queue, tmp_path):
    q, db, _, results = queue
    film = tmp_path / "Film (2020).mkv"
    film.write_bytes(b"not a real film")
    st = film.stat()
    mid = db.upsert_media(path=str(film), size=st.st_size, mtime=st.st_mtime, probe_json="{}")
    pin = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    results[str(film)] = {
        "phase": "design", "track_key": KEY, "source": pin, "levels": {"0": {"d1": LEVEL}},
        "design": {"declined": False, "candidates": [FILTER], "decline_message": None,
                   "decline_reason": None}}
    job = db.create_job(mid, str(film), {"phase": "design"})
    q.start()
    assert _wait(lambda: _status(db, job) == "done")
    assert _digests(db, mid) == ["d1"]
    assert store.levels(db, _row(db, mid), KEY) == {"d1": {0.0: LEVEL}}
    assert db.one("SELECT message FROM jobs WHERE id=?", (job,))["message"] == \
        "BEQ Forge designed 1 filters"


# -- design and measure through the real pipeline -------------------------------------
def _track(path):
    pr = media.probe(str(path))
    return pr, pr.best


@needs_tools
def test_a_design_job_returns_its_filters_measured_at_every_cutoff_asked_for(
        env, cat, fake_designer):  # noqa: F811
    s, place, tmp = env
    path = place("lpcm20")
    pr, track = _track(path)
    r, _ = _run(s, path, {"phase": "design", "track_id": track.track_id,
                          "cutoffs": [0.0, 10.0, 20.0]}, tmp, cat)
    assert r["phase"] == "design" and len(r["design"]["candidates"]) == 1
    digest = r["design"]["candidates"][0]["digest"]
    assert r["track_key"] == pr.track_key(track)
    assert r["source"] == source_pin(FileStamp.of(path), track)
    assert set(r["levels"]) == {"0", "10", "20"}
    for found in r["levels"].values():
        assert set(found) == {digest} and found[digest]["channel"] in ("L", "R")
    # the candidate boosts below 24 Hz, so a 20 Hz floor takes level away
    assert r["levels"]["20"][digest]["peak_dbfs"] < r["levels"]["0"][digest]["peak_dbfs"] - 1


@needs_tools
def test_a_declined_design_is_a_result_with_no_filters_and_no_levels(
        env, cat, fake_designer):  # noqa: F811
    s, place, tmp = env
    fake_designer.mode = "decline"
    path = place("lpcm20")
    r, _ = _run(s, path, {"phase": "design", "track_id": _track(path)[1].track_id,
                          "cutoffs": [0.0, 10.0]}, tmp, cat)
    assert r["design"]["declined"] and not r["design"]["candidates"]
    assert r["levels"] == {}
    assert worker._found(r).startswith("BEQ Forge designed no filter")


@needs_tools
def test_a_measuring_job_returns_a_level_for_each_filter_and_cutoff_and_a_profile(
        env, cat):  # noqa: F811
    s, place, tmp = env
    path = place("lpcm20")
    pr, track = _track(path)
    r, _ = _run(s, path, {"phase": "measure", "track_id": track.track_id,
                          "filters": [CAT[0], CAT[2]], "cutoffs": [0.0, 15.0]}, tmp, cat)
    assert r["phase"] == "measure" and r["track_key"] == pr.track_key(track)
    assert r["source"] == source_pin(FileStamp.of(path), track)
    assert set(r["levels"]) == {"0", "15"}
    for found in r["levels"].values():
        assert set(found) == {"atmos1", "ddpa"}
        assert all(isinstance(p["peak_dbfs"], float) for p in found.values())
    # CAT[0] is two 5 dB shelves at 20 Hz: more boost than CAT[2]'s one of 6 dB at 18 Hz
    assert r["levels"]["0"]["atmos1"]["peak_dbfs"] > r["levels"]["0"]["ddpa"]["peak_dbfs"]
    assert r["profile"]["bucket_s"] == 10.0
    assert len(r["profile"]["db"]) == 2  # a 16 second film
    assert all(-60 < v < 0 for v in r["profile"]["db"])
    assert worker._found(r) == "Measured 2 filters"


@needs_tools
@pytest.mark.parametrize("phase", ["design", "measure"])
def test_a_job_for_a_beq_track_fails(env, cat, fake_designer, phase):  # noqa: F811
    s, place, tmp = env
    path = place("lpcm20")
    _run(s, path, _encode(media.probe(str(path))), tmp)
    beq = media.probe(str(path)).beq_tracks[0]
    with pytest.raises(JobFailed):
        _run(s, path, {"phase": phase, "track_id": beq.track_id, "filters": [CAT[0]],
                       "cutoffs": [0.0]}, tmp, cat)
    assert not fake_designer.requests


@needs_tools
@pytest.mark.parametrize("phase", ["design", "measure"])
def test_a_job_for_a_track_the_app_cannot_encode_fails(env, cat, fake_designer, monkeypatch,
                                                       phase):  # noqa: F811
    s, place, tmp = env
    path = place("dts51")
    track_id = _track(path)[1].track_id
    monkeypatch.setattr(media, "classify", lambda *a: "dtsx" if "dts" in a[1] else "ac3")
    with pytest.raises(JobFailed, match="Not supported"):
        _run(s, path, {"phase": phase, "track_id": track_id, "filters": [CAT[0]],
                       "cutoffs": [0.0]}, tmp, cat)
    assert not fake_designer.requests


@needs_tools
def test_a_measuring_job_fails_when_the_track_cannot_be_decoded(env, cat, tmp_path):  # noqa: F811
    s, place, tmp = env
    path = place("lpcm20")
    track_id = _track(path)[1].track_id
    s.ffmpeg = str(_exe(tmp_path / "ffmpeg", "#!/bin/sh\necho 'no decoder' >&2\nexit 1\n"))
    with pytest.raises(JobFailed):
        _run(s, path, {"phase": "measure", "track_id": track_id, "filters": [CAT[0]],
                       "cutoffs": [0.0]}, tmp, cat)


@needs_tools
def test_a_designed_filter_still_holds_after_the_app_adds_a_beq_track_to_the_file(
        env, cat, fake_designer):  # noqa: F811
    s, place, tmp = env
    path = place("lpcm20")
    db = DB(tmp / "jobs.sqlite3")
    mid = worker.scan_file(s, db, path)
    pr, track = _track(path)
    before = FileStamp.of(path)
    design = db.create_job(mid, str(path), {"phase": "design", "track_id": track.track_id,
                                            "cutoffs": [0.0, 10.0]})
    encode = db.create_job(mid, str(path), {**_encode(pr), "entry": ENTRY,
                                            "source": source_pin(before, track)})
    q = worker.JobQueue(s, db, cat)
    q.start()
    try:
        assert _wait(lambda: _status(db, encode) in ("done", "failed"), 180)
    finally:
        q.stop()
    assert (_status(db, design), _status(db, encode)) == ("done", "done"), [
        r["message"] for r in db.q("SELECT message FROM jobs")]
    assert FileStamp.of(path) != before
    assert _wait(lambda: _row(db, mid)["size"] == path.stat().st_size, 30)
    m = _row(db, mid)
    after = media.probe(str(path))
    assert after.beq_tracks
    key = pr.track_key(track)
    assert after.track_by_key(key).track_id == track.track_id
    found = store.forge_filters(db, m)
    assert [f["track_key"] for f in found] == [key]
    assert set(store.levels(db, m, key)[found[0]["entry"]["digest"]]) == {0.0, 10.0}
    assert json.loads(db.one("SELECT result_json FROM jobs WHERE id=?",
                             (design,))["result_json"])["track_key"] == key
