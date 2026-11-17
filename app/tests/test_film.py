"""The film model: what every page says about a film, built from its row, the catalogue,
the stored BEQ Forge filters and the film's jobs."""

import json
import time

import pytest

from beq_encoder import activity, film, store, words
from beq_encoder.catalogue import Catalogue
from beq_encoder.db import DB
from beq_encoder.media import AudioTrack, ProbeResult

BANDS = [{"type": "LowShelf", "freq": 20.0, "gain": 6.0, "q": 0.7, "count": 1}]
CAT = [
    {"title": "Deep", "year": 2020, "theMovieDB": "42", "author": "aron7awol",
     "audioTypes": ["Atmos"], "source": "Disc", "edition": "", "content_type": "film",
     "filters": [{"type": "LowShelf", "freq": 20.0, "gain": 6.0, "q": 0.7, "biquads": {}}],
     "digest": "cat-atmos", "underlying": "", "beqcUrl": "https://beqcatalogue.example/deep"},
    {"title": "Deep", "year": 2020, "theMovieDB": "42", "author": "mobe1969",
     "audioTypes": ["DTS-HD MA 5.1"], "source": "Disc", "edition": "", "content_type": "film",
     "filters": [{"type": "LowShelf", "freq": 25.0, "gain": 4.0, "q": 0.7, "biquads": {}}],
     "digest": "cat-dts", "underlying": ""},
    {"title": "Deep", "year": 2020, "theMovieDB": "42", "author": "nobody",
     "audioTypes": ["Atmos"], "source": "Disc", "edition": "", "content_type": "film",
     "filters": [], "digest": "cat-none", "underlying": ""},
]


def _track(track_id, kind="truehd_atmos", channels=8, layout="7.1", beq=False, **more):
    return AudioTrack(track_id=track_id, audio_index=track_id - 1, codec="TrueHD Atmos",
                      language="eng", name=more.pop("name", ""), default=False,
                      channels=channels, atmos=kind == "truehd_atmos" and not beq, beq=beq,
                      kind=kind, layout=layout, sample_rate=48000, **more)


def _beq(track_id, name="BEQ [-4.5 dB] TrueHD Atmos 7.1", **tags):
    return _track(track_id, beq=True, name=name, beq_tags=tags)


def _probe(*tracks):
    return ProbeResult(path="/media/deep.mkv", duration_s=7200.0, tracks=[],
                       audio=list(tracks))


@pytest.fixture
def env(tmp_path):
    db = DB(tmp_path / "t.sqlite3")
    path = tmp_path / "database.json"
    path.write_text(json.dumps(CAT))
    cat = Catalogue(path, "", 9999)
    cat.load()
    return db, cat


def _film(db, *tracks, rolloff=None, **more):
    fields = dict(path="/media/Deep (2020).mkv", size=10, mtime=5.0, title="Deep", year="2020",
                  tmdb="42", probe_json=_probe(*tracks).to_json(), has_audio=1,
                  rolloff_json=json.dumps(rolloff) if rolloff else None)
    return db.upsert_media(**{**fields, **more})


ROLLOFF = {"verdict": "clear", "corner_hz": 28.0, "depth15_db": 18.0, "slope_db_oct": 24.0,
           "slope_at_hz": 22.0, "plateau_dbfs": -30.0, "seconds": 300}


@pytest.mark.parametrize("verdict, rating", [("clear", "strong"), ("some", "possible"),
                                             ("none", "unlikely"), ("unknown", "unknown"),
                                             ("error", "unknown"), (None, "unknown")])
def test_the_rating_comes_only_from_the_bass_check(env, verdict, rating):
    db, cat = env
    ro = {**ROLLOFF, "verdict": verdict} if verdict else None
    f = film.load(db, cat, _film(db, _track(1), rolloff=ro))
    assert f.rating == rating
    assert len(f.filters) == 2, "a catalogue filter does not change the rating"


def test_a_beq_track_does_not_change_the_rating(env):
    db, cat = env
    before = film.load(db, cat, _film(db, _track(1), rolloff=ROLLOFF)).rating
    after = film.load(db, cat, _film(db, _track(1), _beq(2, digest="cat-atmos",
                                                         source="catalogue"),
                                     rolloff=ROLLOFF)).rating
    assert before == after == "strong"


def test_a_film_with_no_usable_track_is_not_supported(env):
    db, cat = env
    f = film.load(db, cat, _film(db, _track(1, kind="pcm", channels=4, layout="quad")))
    assert f.rating == "unsupported" and f.default_track is None
    assert f.cannot and f.cannot[0].why_not


def test_the_film_lists_its_filters_before_any_job_and_says_what_each_was_made_for(env):
    db, cat = env
    f = film.load(db, cat, _film(db, _track(1)))
    made = {x.digest: x for x in f.filters}
    assert made["cat-atmos"].origin == "BEQ Catalogue · aron7awol"
    assert made["cat-atmos"].made_for == "Track 1 · TrueHD Atmos 7.1 · English"
    assert made["cat-atmos"].fits(1) and not made["cat-dts"].fits(1)
    assert "Another mix" in made["cat-dts"].made_for
    assert f.default_filter(1).digest == "cat-atmos"
    assert not f.work, "no job was started"


def test_a_filter_for_another_track_is_never_the_default(env):
    db, cat = env
    f = film.load(db, cat, _film(db, _track(1, kind="pcm", channels=2, layout="stereo")))
    assert f.filters and f.default_filter(1) is None


def test_in_use_is_derived_from_the_beq_track(env):
    db, cat = env
    f = film.load(db, cat, _film(db, _track(1), _beq(2, digest="cat-atmos", source="catalogue",
                                                     author="aron7awol", gain_db="-4.5",
                                                     highpass_hz="15")))
    used = f.filter("cat-atmos")
    assert used.in_use and not f.filter("cat-dts").in_use
    beq = f.beq_tracks[0]
    assert (beq.filter, beq.cutoff, beq.level) == ("BEQ Catalogue · aron7awol (Atmos)", "15 Hz",
                                                   "-4.5 dB")


def test_an_in_use_filter_the_catalogue_dropped_stays_in_the_list(env):
    db, cat = env
    bands = json.dumps(BANDS)
    f = film.load(db, cat, _film(db, _track(1), _beq(2, digest="gone", source="catalogue",
                                                     author="old-author", filters=bands)))
    gone = f.filter("gone")
    assert gone and gone.in_use and gone.labels == ["no longer in the catalogue"]
    assert gone.bands == BANDS and gone.origin == "BEQ Catalogue · old-author"


def _forge(digest="design:1"):
    return {"digest": digest, "origin": "designer", "label": "beqforge 0.4 flatten",
            "author": "beqforge 0.4", "filters": BANDS, "strategy": "flatten"}


def test_a_beq_forge_filter_joins_the_list_while_the_file_is_the_one_it_was_designed_from(env):
    db, cat = env
    probe = _probe(_track(1))
    mid = _film(db, _track(1))
    pin = {"size": 10, "mtime_ns": 5_000_000_000}
    store.save_filters(db, mid, probe.track_key(probe.audio[0]), pin, [_forge()])
    f = film.load(db, cat, mid)
    forge = f.filter("design:1")
    assert forge.origin == "BEQ Forge" and forge.fits(1)
    db.x("UPDATE media SET size=11 WHERE id=?", (mid,))
    assert film.load(db, cat, mid).filter("design:1") is None


def test_levels_are_per_track_and_per_cutoff(env):
    db, cat = env
    probe = _probe(_track(1))
    mid = _film(db, _track(1))
    pin = {"size": 10, "mtime_ns": 5_000_000_000}
    store.save_levels(db, mid, probe.track_key(probe.audio[0]), pin,
                      {"0": {"cat-atmos": {"peak_dbfs": 3.6, "channel": "LFE"}},
                       "15": {"cat-atmos": {"peak_dbfs": 0.2, "channel": "LFE"}}})
    f = film.load(db, cat, mid).filter("cat-atmos")
    assert f.level(1, 0, -1.0) == "-4.6 dB", "in the format of a BEQ track's level"
    assert f.level(1, 15, -1.0) == "-1.2 dB"
    assert f.level(1, 10, -1.0) == "", "not measured at this cutoff"
    assert f.level(2, 0, -1.0) == "", "not measured on this track"


def test_the_low_end_cutoff_changes_the_curve_and_the_bass_restored(env):
    db, cat = env
    f = film.load(db, cat, _film(db, _track(1))).filter("cat-atmos")
    assert f.curve(0).bass != f.curve(30).bass
    assert f.curve(0).svg != f.curve(30).svg


def test_the_bass_restored_finds_a_peak_below_the_cutoff():
    # Shelves stacked at the cutoff still climb an octave under it, as many catalogue
    # filters do, so the most the filter adds sits below the cutoff, not at it.
    shelves = [{"type": "LowShelf", "freq": 22.7, "gain": 7.6, "q": 0.9, "count": 7}]
    gain, at = words.peak_boost(shelves, 20.0)
    assert 39 < gain < 41 and 14 < at < 18, (gain, at)


def _job(db, mid, phase, status, result=None, message=None, **params):
    job = db.create_job(mid, "/media/Deep (2020).mkv", {"phase": phase, **params})
    db.update_job(job, status=status, started_at=time.time() - 60, finished_at=time.time(),
                  result_json=json.dumps(result) if result else None, message=message)
    return job


def test_work_splits_into_activities_and_history(env):
    db, cat = env
    mid = _film(db, _track(1))
    done = _job(db, mid, "encode", "done", {"track_name": "BEQ", "filter_report": {
        "gain_db": -4.5}}, entry={"digest": "cat-atmos", "author": "aron7awol"})
    older = _job(db, mid, "design", "done", {"design": {"candidates": [_forge()]}})
    db.update_job(older, finished_at=time.time() - 3 * 86400)
    queued = db.create_job(mid, "/media/Deep (2020).mkv", {"phase": "design", "track_id": 1})
    _job(db, mid, "measure", "done", {"levels": {}})
    f = film.load(db, cat, mid)
    assert [w.id for w in f.activities] == [queued]
    assert f.activities[0].what == "Designing a filter"
    assert f.latest.id == done and "4.5 dB quieter" in f.latest.result
    assert [w.id for w in f.history] == [older], "not the lead result, not a measurement"


def test_a_failure_says_what_happened_what_was_kept_and_one_fix(env):
    db, cat = env
    mid = _film(db, _track(1))
    _job(db, mid, "encode", "failed", message="BEQ track still +41.20 ms out of sync")
    w = film.load(db, cat, mid).latest
    assert w.failure.what == "The new track did not stay in time with the original track."
    assert w.failure.kept == "The film file was not changed." and w.failure.fix == "retry"


def test_no_status_waits_for_the_user():
    assert "Waiting" not in " ".join(label for label, _ in activity.STATUS.values())
    assert "Waiting" not in " ".join(label for label, _, _ in activity.OUTCOMES.values())


def test_work_that_ended_says_how_it_ended(env):
    db, cat = env
    mid = _film(db, _track(1))
    _job(db, mid, "design", "done", {"design": {"declined": True}})
    _job(db, mid, "design", "done", {"design": {"candidates": [_forge(), _forge("design:2")]}})
    _job(db, mid, "encode", "done", {"filter_report": {"gain_db": -4.5}})
    _job(db, mid, "design", "cancelled")
    said = {(w.label, w.tone) for w in film.load(db, cat, mid).work}
    assert said == {("Nothing to restore", "plain"), ("2 filters designed", "good"),
                    ("BEQ track added", "good"), ("Cancelled", "plain")}


def test_a_row_says_the_track_the_filter_and_the_cutoff_it_used(env):
    db, cat = env
    mid = _film(db, _track(1))
    _job(db, mid, "encode", "failed", message="Lossless check failed", track_id=1,
         highpass_hz=15.0, entry={"digest": "cat-atmos", "author": "aron7awol",
                                  "audio_types": ["Atmos"]})
    w = film.load(db, cat, mid).latest
    assert w.using == [("Audio track", "Track 1 · TrueHD Atmos 7.1 · English"),
                       ("Filter", "BEQ Catalogue · aron7awol (Atmos)"),
                       ("Low-end cutoff", "15 Hz")]


def test_a_running_row_shows_progress_and_time_left_and_keeps_the_stage_for_details(env):
    db, cat = env
    mid = _film(db, _track(1))
    job = db.create_job(mid, "/media/Deep (2020).mkv", {"phase": "encode"})
    db.update_job(job, status="running", started_at=time.time() - 600, percent=50.0,
                  stage="Encoding TrueHD (DEE: step 2)")
    w = film.load(db, cat, mid).activities[0]
    assert w.progress_label.startswith("About 54% done · roughly ")
    assert "DEE" not in w.progress_label and "DEE" in w.stage
    clip = activity.clip_progress("BEQ track 2 of 2: Encoding TrueHD", 50.0)
    assert activity.clip_progress("BEQ track 1 of 2: Encoding TrueHD", 50.0) < clip < 88
    assert activity.clip_progress("a stage the app does not write", 38.0) is None


def test_a_failure_stands_until_newer_work_for_the_film_ends(env):
    db, cat = env
    mid = _film(db, _track(1))
    failed = _job(db, mid, "encode", "failed", message="x")
    assert activity.counts(db)["failed"] == 1
    assert list(activity.standing_failures(db)) == [mid]
    assert film.load(db, cat, mid).latest.id == failed, "the film page leads with the same one"
    _job(db, mid, "measure", "done", {"levels": {}})
    assert activity.counts(db)["failed"] == 1, "a measurement does not end it"
    done = _job(db, mid, "encode", "done", {"filter_report": {"gain_db": -4.5}})
    db.update_job(done, finished_at=time.time() + 1)
    assert activity.counts(db)["failed"] == 0 and not activity.standing_failures(db)
    assert activity.get(db, f"job-{failed}").superseded


def test_an_unknown_rating_says_which_unknown_it_is(env):
    db, cat = env
    cases = [({"rolloff": None}, "Unknown: not checked"),
             ({"rolloff": {**ROLLOFF, "verdict": "unknown"}}, "Unknown: too little bass"),
             ({"rolloff": {**ROLLOFF, "verdict": "error"}}, "Unknown: bass check failed"),
             ({"probe_json": None, "error": "mkvmerge -J failed"}, "Unknown: file can't be read")]
    for i, (more, label) in enumerate(cases):
        f = film.load(db, cat, _film(db, _track(1), path=f"/media/{i}.mkv", **more))
        assert f.rating_label == label
        row = db.one("SELECT * FROM media WHERE id=?", (f.id,))
        assert film.row_rating(row) == ("unknown", label), "the Library row says the same"


def test_a_commentary_track_is_not_offered_when_the_main_track_cannot_get_a_beq_track(env):
    db, cat = env
    f = film.load(db, cat, _film(db, _track(1, kind="pcm", channels=4, layout="quad"),
                                 _track(2, kind="ac3", channels=2, layout="stereo",
                                        commentary=True)))
    assert f.rating == "unsupported" and not f.sources and len(f.cannot) == 2
    assert not f.add.can


def test_a_filter_in_use_shows_the_level_of_its_beq_track(env):
    db, cat = env
    f = film.load(db, cat, _film(db, _track(1), _beq(2, digest="cat-atmos", source="catalogue",
                                                     author="aron7awol", gain_db="-4.5",
                                                     highpass_hz="15",
                                                     source_track="TrueHD Atmos 7.1 (TrueHD)")))
    used = f.filter("cat-atmos")
    assert used.level(1, 15, -1.0) == "-4.5 dB", "never unmeasured beside its BEQ track"
    assert used.level(1, 0, -1.0) == "", "a figure is for one cutoff"
    assert f.level_said(used, 1, 0, -1.0) == "Not measured yet", "one format in the column"
    assert f.level_shown(used, 1, 0, -1.0) == ("-4.5 dB", "at a low-end cutoff of 15 Hz"), \
        "the film page shows the level its BEQ track plays at, with that track's cutoff"
    assert "4.5 dB quieter" in f.track(2).volume, "the track says what to do with the volume"
    assert f.track(1).beqs == [2] and f.track(2).source == f.track(1).name
    assert f.track(2).filter == used.name == "BEQ Catalogue · aron7awol (Atmos)"
    made, _ = f.filters_for(1)
    assert made == [used] and f.add.can and not f.add.main and f.add.why == film.ALL_IN_USE,         "with every filter for the track in use, Add BEQ track stays but is not the main action"


def test_a_revised_filter_points_at_its_replacement_and_is_not_offered_again(env):
    db, cat = env
    f = film.load(db, cat, _film(db, _track(1), _beq(2, digest="old", source="catalogue",
                                                     author="aron7awol",
                                                     filters=json.dumps(BANDS))))
    old = f.filter("old")
    assert old.labels == ["revised since"] and old.newer == "cat-atmos"
    assert old.name == "BEQ Catalogue · aron7awol (revised since)"
    assert old not in f.offered and f.default_filter(1).digest == "cat-atmos"


def test_a_film_with_no_filter_says_why_once_and_the_same_way(env):
    db, cat = env
    mid = _film(db, _track(1, kind="pcm", channels=2, layout="stereo"), tmdb="7",
                title="Other")
    f = film.load(db, cat, mid)
    assert f.gap == "open" and f.add.main
    assert "start by having BEQ Forge design one, which takes 4 min" in f.gap_said("4 min")[0], \
        "what Add BEQ track leads to, and how long the design takes"
    _job(db, mid, "design", "done", {"design": {"declined": True}})
    f = film.load(db, cat, mid)
    assert f.gap == "declined" and not f.add.main
    assert f.gap_said()[0].startswith(film.NO_FILTER)
    assert "BEQ Forge, which can try a design again" in f.gap_said()[0], \
        "what the user can still do"
    assert f.latest.outcome == "nothing" and "designed no filter" in f.latest.result
    assert f.gap_said()[1] == "None.", "the result that leads the page says why, once"
    db.x("UPDATE jobs SET finished_at=? WHERE media_id=?", (time.time() - 2 * film.RECENT_S, mid))
    f = film.load(db, cat, mid)
    assert f.latest is None and "designed none" in f.gap_said()[1], \
        "Filters says why once the result no longer leads"
    db.create_job(mid, "/media/Deep (2020).mkv", {"phase": "design", "track_id": 1})
    assert film.load(db, cat, mid).gap == "designing"


def test_two_filters_of_one_origin_have_names_that_tell_them_apart():
    from beq_encoder.words import filter_name
    forge = film.Filter({**_forge(), "digest": "a"}, matches={1: "exact"})
    gentle = film.Filter({**_forge(), "digest": "b", "filters": [{**BANDS[0], "gain": 3.0}]},
                         matches={1: "exact"})
    assert filter_name(forge.entry) == "BEQ Forge", "one design alone needs no tell"
    film._tell_forge([forge, gentle])
    assert filter_name(forge.entry) == "BEQ Forge (fuller)"
    assert filter_name(gentle.entry) == "BEQ Forge (gentler)"


def test_the_summary_counts_what_the_library_shows(env):
    db, cat = env
    mid = _film(db, _track(1), _beq(2, digest="cat-atmos", source="catalogue"), rolloff=ROLLOFF)
    row = db.one("SELECT * FROM media WHERE id=?", (mid,))
    assert film.summary(db, cat, row) == ("strong", 2, 1)


def test_a_catalogue_entry_with_no_filter_bands_is_left_out(env):
    db, cat = env
    f = film.load(db, cat, _film(db, _track(1)))
    assert cat.get("cat-none") is not None and f.filter("cat-none") is None


def test_a_queued_job_says_queued_with_its_place_in_the_queue(env):
    db, cat = env
    mid = _film(db, _track(1))
    db.create_job(mid, "/media/Deep (2020).mkv", {"phase": "measure", "track_id": 1})
    w = film.load(db, cat, mid).activities[0]
    assert (w.label, w.status_line) == ("Queued", "First in the queue")


def test_a_filter_says_where_its_measurement_stands(env):
    db, cat = env
    mid = _film(db, _track(1))

    def said():
        f = film.load(db, cat, mid)
        return f.level_said(f.filter("cat-atmos"), 1, 0, -1.0)

    def next_step():
        return film.load(db, cat, mid).measures_next(1, 0, -1.0)

    assert said() == "Not measured yet"
    assert next_step().startswith("Opening Add BEQ track measures"), "the next step, said once"
    job = db.create_job(mid, "/media/Deep (2020).mkv", {"phase": "measure", "track_id": 1})
    assert said() == "Queued for measuring" and not next_step()
    db.update_job(job, status="running", started_at=time.time())
    assert said() == "Measuring"


def test_time_left_does_not_depend_on_when_the_page_renders(env):
    db, cat = env
    mid = _film(db, _track(1))
    done = _job(db, mid, "encode", "done", {"filter_report": {"gain_db": -4.5}})
    db.update_job(done, started_at=time.time() - 600)  # an encode takes ten minutes here
    job = db.create_job(mid, "/media/Deep (2020).mkv", {"phase": "encode"})
    said = set()
    for ago in (120, 900):  # the same progress, seen two minutes and fifteen minutes in
        db.update_job(job, status="running", started_at=time.time() - ago, percent=50.0,
                      stage="Encoding TrueHD (DEE: step 2)")
        said.add(film.load(db, cat, mid).activities[0].progress_label)
    assert said == {"About 54% done · roughly 5 min left"}


def test_a_failures_advice_names_only_what_the_film_can_change(env):
    db, cat = env
    mid = _film(db, _track(1))
    _job(db, mid, "encode", "failed", message="Lossless check failed")
    for filters, choice in ((1, "change the low-end cutoff"),
                            (2, "change the filter or the low-end cutoff")):
        db.x("UPDATE media SET filter_count=? WHERE id=?", (filters, mid))
        _, words, href, _ = film.load(db, cat, mid).latest.failure.advice
        assert words == choice
        assert href.startswith(f"/films/{mid}/add?step=filter"), \
            "the advice links to the step that changes it"


def test_a_failures_fix_is_the_pages_main_action_until_its_work_runs_again(env):
    db, cat = env
    mid = _film(db, _track(1))
    assert film.load(db, cat, mid).add.main
    _job(db, mid, "encode", "failed", message="x")
    f = film.load(db, cat, mid)
    assert f.fix == "retry" and f.add.can and not f.add.main
    db.create_job(mid, "/media/Deep (2020).mkv", {"phase": "encode"})
    f = film.load(db, cat, mid)
    assert not f.fix and f.retrying.what == "Adding a BEQ track"


def test_a_revised_filter_has_the_rows_of_any_filter_and_one_label(env):
    db, cat = env
    stale = {"beq": [{"name": "BEQ [-4.5 dB] TrueHD Atmos 7.1",
                      "reasons": ["the catalogue entry has been updated"]}]}
    f = film.load(db, cat, _film(
        db, _track(1), _beq(2, digest="old", source="catalogue", author="aron7awol",
                            filters=json.dumps(BANDS), source_track="TrueHD Atmos 7.1 (TrueHD)"),
        assess_json=json.dumps(stale)))
    old, beq = f.filter("old"), f.track(2)
    assert old.made_for == f.track(1).name, "by the track's one name, as on every filter"
    assert old.dated == ("Last updated", "Not recorded")
    assert beq.filter == old.name and beq.notes == [], "the name carries the label once"
    groups = f.filter_groups
    assert [heading for heading, _, _ in groups] == [
        f"Made for {f.track(1).name}", "Made for another track or another mix of this film"]
    assert old in groups[0][2] and f.filter("cat-dts") in groups[1][2]


def test_a_beq_forge_filter_shows_a_fact_to_compare_it_on():
    forge = film.Filter({**_forge(), "confidence": 0.91})
    assert forge.sure == "91 of 100"
    assert "Confidence" not in dict(forge.details), "said once, as a row"
    assert film.Filter(CAT[0]).sure == ""


def test_a_finished_clip_marks_its_filters_by_their_audio_track(env):
    db, cat = env
    mid = _film(db, _track(1))
    db.x("INSERT INTO clips (media_id, path, status, params_json, result_json, created_at, "
         "finished_at) VALUES (?, ?, 'done', ?, ?, ?, ?)",
         (mid, "/media/Deep (2020).mkv",
          json.dumps({"track_id": 1, "filters": [{"entry": {"digest": "cat-dts"}},
                                                 {"entry": {"digest": "cat-atmos"}}]}),
          json.dumps({"path": "/clips/Deep.mkv"}), time.time(), time.time()))
    f = film.load(db, cat, mid)
    assert f.clip_marks == {"cat-dts": 2, "cat-atmos": 3}
    assert "track=1" in f.add_href(f.filter("cat-atmos")) and "cutoff=0" in f.add_href(
        f.filter("cat-atmos")), "a clip's filter is added with the clip's track and cutoff"
