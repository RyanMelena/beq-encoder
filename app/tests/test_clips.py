"""Clips: an excerpt of a film with its original track and a BEQ track for each filter,
written to CLIP_DIR at the level the whole film would get, and the film never changed."""

import hashlib
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pytest

from beq_encoder import clips, config, media
from beq_encoder.beqfilter import dbfs, filter_stream
from beq_encoder.biquad import design_sos
from beq_encoder.db import DB
from beq_encoder.pipeline import clip as clipping
from beq_encoder.pipeline.clip import (busiest_start, clip_name, first_packet_fix, free_name,
                                       parse_start, run_clip)
from beq_encoder.pipeline.job import Job
from beq_encoder.pipeline.processes import JobFailed

from .test_pipeline import ENTRY, assets, env  # noqa: F401  (fixtures)
from .test_tracks import sources  # noqa: F401  (fixture)
from .test_web import _wait, app  # noqa: F401  (fixture)

TOOLS = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("mkvmerge")),
                           reason="needs ffmpeg + mkvtoolnix")
SHELF = {**ENTRY, "digest": "shelf", "label": "shelf", "author": "shelf",
         "filters": [{"type": "LowShelf", "freq": 25.0, "gain": 8.0, "q": 0.7, "count": 1}]}


@pytest.mark.parametrize("typed,seconds", [
    ("", None), ("  ", None), ("4872", 4872.0), ("81:12", 4872.0), ("1:21:12", 4872.0),
    ("01:21:12.5", 4872.5), ("0:00:06", 6.0), ("90", 90.0),
])
def test_a_start_is_read_as_people_type_it(typed, seconds):
    assert parse_start(typed) == seconds


@pytest.mark.parametrize("typed", ["1:61", "abc", "1:2:3:4", "-5", "1.5:00", "1:", "1:60:00"])
def test_a_start_that_is_not_a_time_is_refused(typed):
    with pytest.raises(ValueError, match="h:mm:ss"):
        parse_start(typed)


def test_a_clip_is_named_for_the_film_and_where_it_starts(tmp_path):
    film = Path("/media/movies/Pacific Rim (2013)/Pacific Rim (2013).mkv")
    assert clip_name(film, 4872.826) == "Pacific Rim (2013) - BEQ demo 01.21.12.mkv"
    taken = tmp_path / "Pacific Rim (2013) - BEQ demo 01.21.12.mkv"
    assert free_name(tmp_path, taken.name) == taken
    taken.write_bytes(b"")
    (tmp_path / "Pacific Rim (2013) - BEQ demo 01.21.12 (2).mkv").write_bytes(b"")
    assert free_name(tmp_path, taken.name).name == "Pacific Rim (2013) - BEQ demo 01.21.12 (3).mkv"


@pytest.mark.parametrize("pts,fixed", [
    # TrueHD cut at a keyframe (measured): the first access unit carries the keyframe's time
    ([0.0, 0.044, 0.045, 0.046, 0.047, 0.048, 0.048, 0.049], 0.043),
    ([0.0, 0.001, 0.002, 0.003, 0.003, 0.004], None),          # follows on
    ([0.0, 0.032, 0.064, 0.096], None),                        # AC-3 frames
    ([0.0, 0.050, 0.082, 0.114, 0.146], 0.018),                # AC-3, late-stamped
    ([0.0, 0.001], None),                                      # too few to judge
])
def test_a_first_packet_stamped_at_the_keyframe_moves_next_to_the_second(pts, fixed):
    got = first_packet_fix(pts)
    assert got == fixed if fixed is None else got == pytest.approx(fixed)


def _analysis(seconds=600, fs=1000, deep_at=400.0, loud_at=100.0):
    """A 5.1 film at the analysis rate: quiet noise, a loud 250 Hz passage a BEQ filter
    hardly touches at `loud_at`, and a 12 Hz rumble in the LFE at `deep_at`, 30 s each."""
    rng = np.random.default_rng(3)
    t = np.arange(seconds * fs) / fs
    x = 0.001 * rng.standard_normal((len(t), 6))
    loud = (t >= loud_at) & (t < loud_at + 30)
    deep = (t >= deep_at) & (t < deep_at + 30)
    x[loud, :3] += 0.5 * np.sin(2 * np.pi * 250 * t[loud])[:, None]
    x[deep, 3] += 0.1 * np.sin(2 * np.pi * 12 * t[deep])
    return [x[i:i + 65536] for i in range(0, len(x), 65536)]


CH51 = ["L", "R", "C", "LFE", "Ls", "Rs"]


def test_the_start_is_picked_where_the_filters_add_the_most_bass():
    start = busiest_start(_analysis(), CH51, [SHELF["filters"]], 30)
    assert 395 <= start <= 405  # not the loud passage: the filter does not change it
    # two filters: summed, still the rumble
    assert 395 <= busiest_start(_analysis(), CH51, [SHELF["filters"], ENTRY["filters"]], 30) <= 405


def test_without_a_filter_that_changes_anything_the_loudest_window_is_picked():
    assert 95 <= busiest_start(_analysis(), CH51, [[]], 30) <= 105
    assert busiest_start(_analysis(seconds=20), CH51, [SHELF["filters"]], 30) == 0.0


@pytest.mark.parametrize("programme,gain", [(6.0, -7.0), (-20.0, None)])
def test_an_excerpt_gets_the_gain_of_the_whole_programme(programme, gain):
    """The film peaks at `programme` dBFS once filtered: an excerpt quieter than that gets the
    film's gain; one louder gets its own, so it never clips."""
    x = 0.95 * np.sin(np.linspace(0, 200, 48000))[:, None]
    out = []

    def write(y):
        out.append(y.copy())
        return 0

    sos = design_sos([], 48000.0)
    res = filter_stream(lambda _stage: [x], write, sos, 1, -1.0, programme_peak_dbfs=programme)
    expected = gain if gain is not None else -1.0 - dbfs(0.95)
    assert res.gain_db == pytest.approx(expected, abs=1e-3)
    assert dbfs(res.peak_filtered) == pytest.approx(dbfs(0.95), abs=1e-3)  # the excerpt's own
    assert np.abs(np.concatenate(out)).max() <= 10 ** (-1.0 / 20) + 1e-9  # at most the target


# -- the clip, made from a film --------------------------------------------------------

def _make_film(path: Path, seconds: int = 20, codec: tuple = ("-c:a", "pcm_s24le")) -> None:
    """A film with a keyframe every second and a 5.1 track of pink noise and a 20 Hz tone."""
    noise = [a for k in range(6) for a in (
        "-f", "lavfi", "-i", f"anoisesrc=color=pink:duration={seconds}:sample_rate=48000:"
                             f"seed={k + 3}:amplitude=0.3")]
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         f"testsrc=size=160x90:rate=24:duration={seconds}", *noise, "-filter_complex",
         "".join(f"[{k + 1}:a]" for k in range(6)) + "join=inputs=6:channel_layout=5.1(side)[a]",
         "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-g", "24",
         *codec, "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=Surround 5.1",
         str(path)],
        check=True)


@pytest.fixture(scope="module")
def film_src(tmp_path_factory):
    d = tmp_path_factory.mktemp("film")
    p = d / "Clip Test (2021) {tmdb-42}.mkv"
    _make_film(p)
    return p


def _snapshot(folder: Path) -> dict:
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns,
                     hashlib.sha256(p.read_bytes()).hexdigest()) for p in folder.iterdir()}


def _packets(path: Path, stream: str, n: int = 3) -> list[float]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
                        "packet=pts_time", "-read_intervals", f"%+#{n}", "-of", "csv=p=0",
                        str(path)], capture_output=True, text=True, check=True)
    return [float(v) for v in r.stdout.split()]


def _settings(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("ALLOW_TEST_FORMATS", "true")
    monkeypatch.setenv("MEDIA_ROOTS", str(tmp_path / "media"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("CLIP_DIR", str(tmp_path / "clips"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return config.load()


def _clip_job(s, path: Path, params: dict, clip_id: int = 1) -> Job:
    return Job(s, clip_id, str(path), params, lambda *_: None, s.log_dir / f"clip-{clip_id}.log",
               work=s.work_dir / f"clip-{clip_id}", tmp_mkv=clips.temp_path(s, clip_id))


@TOOLS
def test_a_clip_holds_the_original_and_a_beq_track_per_filter_and_leaves_the_film_alone(
        tmp_path, monkeypatch, film_src):
    s = _settings(tmp_path, monkeypatch)
    movie = tmp_path / "media" / "Clip Test (2021)"
    movie.mkdir(parents=True)
    path = movie / film_src.name
    shutil.copy(film_src, path)
    before = _snapshot(movie)
    probe = media.probe(str(path))
    params = {"track_id": probe.audio[0].track_id, "format": "flac", "target_peak_dbfs": -1.0,
              "fixed_gain_db": None, "start_s": 6.4, "length_s": 5.0,
              "filters": [{"entry": SHELF, "peak_dbfs": 6.0},
                          {"entry": ENTRY, "peak_dbfs": None}]}  # estimated by the clip
    result = run_clip(_clip_job(s, path, params))

    assert _snapshot(movie) == before  # the film is only read
    out = Path(result["path"])
    assert out.parent == s.clip_dir and out.name == "Clip Test (2021) - BEQ demo 00.00.06.mkv"
    assert not clips.temp_path(s, 1).exists()
    assert result["start_s"] == pytest.approx(6.0, abs=0.05)  # the keyframe before 6.4 s
    clip = media.probe(str(out))
    assert [t["type"] for t in clip.tracks] == ["video", "audio", "audio", "audio"]
    names = [a.name for a in clip.audio]
    assert names[0] == "Surround 5.1"
    assert names[1] == "BEQ [-7.0 dB] FLAC 5.1"  # the film's level, not the clip's
    assert names[2].startswith("BEQ [") and names[2].endswith("FLAC 5.1")
    assert [t["gain_from"] for t in result["tracks"]][0] == "film"
    assert [a.default for a in clip.audio] == [True, False, False]
    assert 4.5 < clip.duration_s < 7
    # every audio track starts together, at the start of the video
    firsts = [_packets(out, f"a:{i}") for i in range(3)]
    assert all(abs(f[0] - firsts[0][0]) <= 0.001 for f in firsts)
    assert all(abs(t["lag_ms"]) <= 1.0 for t in result["tracks"])
    assert result["first_packets"]["a:0"][0] == pytest.approx(firsts[0][0])


@TOOLS
def test_a_clip_plays_the_high_pass_floor_the_encode_would(tmp_path, monkeypatch, film_src):
    """The clip runs encode.filter_job, so the floor is in its BEQ track and in the gain
    estimate the clip makes for a filter the review did not estimate."""
    s = _settings(tmp_path, monkeypatch)
    movie = tmp_path / "media" / "Clip Test (2021)"
    movie.mkdir(parents=True)
    path = movie / film_src.name
    shutil.copy(film_src, path)
    probe = media.probe(str(path))
    params = {"track_id": probe.audio[0].track_id, "format": "flac", "target_peak_dbfs": -1.0,
              "fixed_gain_db": None, "highpass_hz": 15.0, "start_s": 6.4, "length_s": 5.0,
              "filters": [{"entry": ENTRY, "peak_dbfs": None}]}
    result = run_clip(_clip_job(s, path, params))
    clip = media.probe(result["path"])
    assert clip.audio[1].name.endswith("FLAC 5.1")  # the floor is in the tags
    assert clip.audio[1].beq_tags.get("highpass_hz") == "15"
    assert result["tracks"][0]["gain_from"] in ("film", "clip_peak")  # estimated with the floor


@TOOLS
def test_a_clip_without_the_whole_tracks_level_is_not_made(tmp_path, monkeypatch, film_src):
    """A clip at its own level would play louder or quieter than the BEQ track it previews."""
    s = _settings(tmp_path, monkeypatch)
    movie = tmp_path / "media" / "Clip Test (2021)"
    movie.mkdir(parents=True)
    path = movie / film_src.name
    shutil.copy(film_src, path)
    probe = media.probe(str(path))
    monkeypatch.setattr(clipping.preflight, "predict", lambda *a, **k: {})
    params = {"track_id": probe.audio[0].track_id, "format": "flac", "target_peak_dbfs": -1.0,
              "fixed_gain_db": None, "start_s": 6.4, "length_s": 5.0,
              "filters": [{"entry": ENTRY, "peak_dbfs": None}]}
    with pytest.raises(JobFailed, match="could not be measured"):
        run_clip(_clip_job(s, path, params))
    assert not list(s.clip_dir.glob("*.mkv"))


@TOOLS
def test_a_second_clip_at_the_same_start_never_replaces_the_first(tmp_path, monkeypatch,
                                                                  film_src):
    s = _settings(tmp_path, monkeypatch)
    movie = tmp_path / "media" / "Clip Test (2021)"
    movie.mkdir(parents=True)
    path = movie / film_src.name
    shutil.copy(film_src, path)
    probe = media.probe(str(path))
    params = {"track_id": probe.audio[0].track_id, "format": "flac", "target_peak_dbfs": -1.0,
              "fixed_gain_db": -12.0, "start_s": 2.0, "length_s": 5.0,
              "filters": [{"entry": SHELF, "peak_dbfs": None}]}
    first = run_clip(_clip_job(s, path, params, 1))
    second = run_clip(_clip_job(s, path, params, 2))
    assert first["file"] == "Clip Test (2021) - BEQ demo 00.00.02.mkv"
    assert second["file"] == "Clip Test (2021) - BEQ demo 00.00.02 (2).mkv"
    assert second["tracks"][0]["gain_db"] == -12.0 and second["tracks"][0]["gain_from"] == "fixed"


@TOOLS
def test_a_track_whose_first_packet_the_cut_stamped_early_follows_on_without_a_gap(
        tmp_path, monkeypatch, film_src):
    """As ffmpeg and mkvmerge cut TrueHD: the first packet carries the keyframe's time."""
    s = _settings(tmp_path, monkeypatch)
    job = _clip_job(s, film_src, {})
    job.work.mkdir(parents=True)
    early = job.work / "early.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(film_src), "-map", "0:a:0", "-t", "3",
                    "-c", "copy", "-bsf:a", r"setts=ts=if(eq(N\,0)\,TS-40\,TS)", str(early)],
                   check=True)
    pts = _packets(early, "a:0", 4)
    assert pts[1] - pts[0] > 0.05  # the gap the cut leaves
    fixed = clipping._track_on_its_time(job, early, "a:0")
    got = _packets(fixed, "a:0", 4)
    steps = np.diff(got)
    assert got[1:] == pts[1:] and steps[0] == pytest.approx(steps[1], abs=0.0011)


@TOOLS
def test_an_atmos_clip_runs_the_atmos_route_and_its_checks(env, monkeypatch):  # noqa: F811
    s, path, tmp = env
    monkeypatch.setenv("CLIP_DIR", str(tmp / "clips"))
    s = config.load()
    # the fake DEE re-encodes this file's audio: the clip's own original track
    monkeypatch.setenv("FAKE_DEE_SOURCE", str(s.work_dir / "clip-1" / "original.mka"))
    before = _snapshot(path.parent)
    probe = media.probe(str(path))
    assert probe.audio[0].atmos  # the test's stand-in for TrueHD Atmos
    params = {"track_id": probe.audio[0].track_id, "format": "truehd", "target_peak_dbfs": -1.0,
              "fixed_gain_db": None, "start_s": 12.0, "length_s": 5.0,
              "filters": [{"entry": ENTRY, "peak_dbfs": 12.0}]}
    result = run_clip(_clip_job(s, path, params))
    assert _snapshot(path.parent) == before
    clip = media.probe(result["path"])
    assert [a.name for a in clip.audio] == ["TrueHD Atmos 7.1", "BEQ [-13.0 dB] TrueHD Atmos"]
    assert result["tracks"][0]["check"] == "sample-exact"
    log = (s.log_dir / "clip-1.log").read_text()
    assert "TrueHD Atmos check passed" in log and "Sync check at" in log


def test_clips_inside_a_media_root_are_refused_as_the_library_would_scan_them(tmp_path,
                                                                             monkeypatch):
    s = _settings(tmp_path, monkeypatch, CLIP_DIR=str(tmp_path / "media" / "clips"))
    assert "inside a media root" in clips.problem(s)
    s = _settings(tmp_path, monkeypatch, CLIP_DIR=str(tmp_path / "elsewhere"))
    assert clips.problem(s) == "" and (tmp_path / "elsewhere").is_dir()


def test_clip_settings_have_defaults_and_bounds(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    monkeypatch.delenv("CLIP_DIR")
    s = config.load()
    assert s.clip_dir == tmp_path / "work" / "clips" and s.clip_seconds == 30.0
    monkeypatch.setenv("CLIP_SECONDS", "2")
    assert config.load().clip_seconds == 5.0
    monkeypatch.setenv("CLIP_SECONDS", "45")
    assert config.load().clip_seconds == 45.0


def test_a_queued_clip_is_cancelled_and_an_interrupted_one_fails(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    db = DB(s.db_path)
    q = clips.ClipQueue(s, db)  # not started: nothing takes the rows
    queued = clips.create(db, None, None, "/media/film.mkv", {"length_s": 30})
    assert q.cancel(queued) and clips.get(db, queued)["status"] == "cancelled"
    running = clips.create(db, None, None, "/media/film.mkv", {"length_s": 30})
    db.x("UPDATE clips SET status='running' WHERE id=?", (running,))
    s.clip_dir.mkdir(parents=True)
    clips.temp_path(s, running).write_bytes(b"half a clip")
    (s.work_dir / f"clip-{running}").mkdir(parents=True)
    q._recover_interrupted()
    c = clips.get(db, running)
    assert c["status"] == "failed" and c["message"] == "Interrupted by restart"
    assert not clips.temp_path(s, running).exists() and not (s.work_dir / f"clip-{running}").exists()


# -- the clip from the page where the filter is chosen -----------------------------------

def _clip_wait(web, clip_id, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        c = web.clips.get(web.db, clip_id)
        if c["status"] not in ("queued", "running"):
            return c
        time.sleep(0.2)
    raise AssertionError(f"clip {clip_id} stuck at {c['stage']}")


@TOOLS
def test_a_clip_from_the_wizard_plays_each_filter_at_the_full_tracks_level(app, film_src):  # noqa: F811
    c, web, d = app
    path = d.parent / "Clip Test (2021)" / film_src.name
    path.parent.mkdir()
    shutil.copy(film_src, path)
    mid = web.scan_file(web.S, web.db, path)
    film = web.films.load(web.db, web.catalogue, mid)
    r = c.post(f"/films/{mid}/design", data={"track": film.default_track.id, "output": "clip"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/films/{mid}/clip")
    design = web.db.one("SELECT id FROM jobs WHERE media_id=? AND phase='design'", (mid,))["id"]
    assert _wait(web, design)["status"] == "done"
    film = web.films.load(web.db, web.catalogue, mid)
    forge = next(f for f in film.filters if f.forge)
    peak = forge.levels[film.default_track.id][0.0]["peak_dbfs"]

    form = {"options": "1", "filters": forge.digest, "start": "", "seconds": "5",
            "format": "flac", "cutoff": "0"}
    for bad in ({"filters": ""}, {"start": "1:99"}, {"seconds": "2"}, {"filters": "nope"}):
        web.store.clear_draft(web.db, mid)
        assert c.post(f"/films/{mid}/clip", data={**form, **bad},
                      follow_redirects=False).status_code == 400, bad
    r = c.post(f"/films/{mid}/clip", data=form, follow_redirects=False)
    assert r.status_code == 303 and "#clip-" in r.headers["location"]
    clip_id = int(r.headers["location"].rsplit("-", 1)[1])
    done = _clip_wait(web, clip_id)
    assert done["status"] == "done", done["message"]
    res = done["result"]
    assert res["picked"] and res["tracks"][0]["gain_from"] in ("film", "clip_peak")
    if res["tracks"][0]["gain_from"] == "film":
        assert res["tracks"][0]["gain_db"] == pytest.approx(min(0.0, -1.0 - peak), abs=0.01)
    assert not list(web.S.work_dir.glob("clip-*"))  # the queue cleans up after itself
    got = c.get(f"/clips/{clip_id}/file")
    assert got.status_code == 200 and len(got.content) == res["bytes"]
    assert quote(res["file"]) in got.headers["content-disposition"]
    page = c.get(f"/films/{mid}").text
    assert "BEQ clip made" in page and res["file"] in page
    assert c.get(f"/api/clips/{clip_id}").json()["result"]["file"] == res["file"]

    # the folder must be one the library does not scan
    web.S.clip_dir = d / "clips"
    assert c.post(f"/films/{mid}/clip", data=form, follow_redirects=False).status_code == 409
    assert "BEQ clips are off" in c.get(f"/films/{mid}/clip?step=options").text
