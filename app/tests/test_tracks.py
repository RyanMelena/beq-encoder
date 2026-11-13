"""Non-Atmos sources (PCM route), best-track picking, catalogue matching and the
prepare phase against a fake beqforge designer.

ffmpeg can only write a DTS core stream, so the DTS-HD MA case patches the probe's
classification of that DTS track; everything after classification is real.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from beq_encoder import analysis, catalogue, config, media, synccheck, worker
from beq_encoder.db import DB
from beq_encoder.pipeline import JobFailed, JobRunner

from .test_pipeline import (ENTRY, FAKE_TRUEHDD, _dee_args, _exe, _pairs, beq_name,
                            make_dee)

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("mkvmerge")), reason="needs ffmpeg + mkvtoolnix"
)

SOURCES = {
    # name: (codec args, layout, pan expression for the noise channels)
    "dts51": (["-c:a:0", "dca", "-strict", "-2", "-b:a:0", "1509k"], "5.1(side)", 6),
    "lpcm20": (["-c:a:0", "pcm_s24le"], "stereo", 2),
    "lpcm71": (["-c:a:0", "pcm_s24le"], "7.1", 8),
    "eac351": (["-c:a:0", "eac3", "-b:a:0", "640k"], "5.1(side)", 6),
    "ac351": (["-c:a:0", "ac3", "-b:a:0", "640k", "-dialnorm", "-27"], "5.1(side)", 6),
}


def _make(path: Path, codec: list[str], layout: str, ch: int, seconds: int = 16) -> None:
    ins = [a for k in range(ch) for a in (
        "-f", "lavfi", "-i", f"anoisesrc=color=pink:duration={seconds}:sample_rate=48000:seed={k + 7}")]
    joins = "".join(f"[{k + 1}:a]" for k in range(ch))
    fc = (f"{joins}join=inputs={ch}:channel_layout={layout}[a];"
          f"[1:a]anull[c]")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         f"testsrc=size=160x90:rate=24:duration={seconds}", *ins, "-filter_complex", fc,
         "-map", "0:v", "-map", "[a]", "-map", "[c]", "-c:v", "libx264", "-preset", "ultrafast",
         *codec, "-c:a:1", "ac3", "-ac:a:1", "2", "-metadata:s:a:0", "language=eng",
         "-metadata:s:a:1", "title=Director's Commentary", str(path)],
        check=True,
    )


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    d = tmp_path_factory.mktemp("srcs")
    out = {}
    for name, (codec, layout, ch) in SOURCES.items():
        p = d / f"{name}.mkv"
        _make(p, codec, layout, ch)
        out[name] = p
    out["dee"], out["shim"] = make_dee(d / "dee")
    here = str(Path(__file__).parent)
    out["cavern"] = _exe(d / "cavern-dump", f"#!{sys.executable}\nimport sys\n"
                         f"sys.path.insert(0, {here!r})\nfrom helpers import fake_cavern_dump\n"
                         "sys.exit(fake_cavern_dump(sys.argv[1:]))\n")
    out["truehdd"] = _exe(d / "truehdd", FAKE_TRUEHDD.format(py=sys.executable, here=here))
    return out


@pytest.fixture
def env(tmp_path, sources, monkeypatch):
    monkeypatch.setenv("ALLOW_TEST_FORMATS", "true")
    monkeypatch.setenv("MEDIA_ROOTS", str(tmp_path / "media"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("DEE_PATH", str(sources["dee"]))
    monkeypatch.setenv("DEE_SHIM", str(sources["shim"]))
    monkeypatch.setenv("FAKE_DEE_ARGS", str(tmp_path / "dee-args.txt"))
    monkeypatch.setenv("FAKE_DEE_SOURCE", "/nonexistent")
    for k in ("FAKE_DEE_DELAY_MS", "FAKE_DEE_FAIL", "FAKE_DEE_PAN", "FAKE_DEE_ATMOS",
              "FAKE_CAVERN_LAG", "FAKE_CAVERN_EXTRA"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CAVERN_DUMP", str(tmp_path / "no-cavern-dump"))  # see joc()
    monkeypatch.setenv("TRUEHDD_PATH", str(sources["truehdd"]))
    real = media.classify

    def classify(*a):  # ffmpeg writes DTS core only: call it DTS-HD MA
        k = real(*a)
        return "dtshd_ma" if k == "dts" else k

    monkeypatch.setattr(media, "classify", classify)
    s = config.load()

    def place(name: str) -> Path:
        movie = tmp_path / "media" / "Test Movie (2020) {tmdb-333371}"
        movie.mkdir(parents=True, exist_ok=True)
        dst = movie / f"Test Movie (2020) {name}.mkv"
        shutil.copy(sources[name], dst)
        return dst

    return s, place, tmp_path


def _run(s, path, params, tmp, cat=None):
    stages = []
    r = JobRunner(s, 1, str(path), params, lambda st, p: stages.append((st, p)),
                  tmp / "job.log", catalogue=cat)
    return r.run(), stages


def _encode(pr, **kw):
    best = pr.best
    return {"phase": "encode", "entry": ENTRY, "track_id": best.track_id, "format": "flac",
            "target_peak_dbfs": -1.0, **kw}


# -- best track ---------------------------------------------------------------------
def _t(i, kind, ch, **kw):
    base = dict(track_id=i, audio_index=i, codec=kind, language="en", name="", default=False,
                channels=ch, atmos=kind == "truehd_atmos", beq=False, kind=kind)
    return media.AudioTrack(**{**base, **kw})


def test_pick_best_tiers_and_tiebreaks():
    tracks = [_t(0, "eac3_joc", 6), _t(1, "dtshd_ma", 6), _t(2, "ac3", 6),
              _t(3, "pcm", 2), _t(4, "dtshd_ma", 8)]
    assert media.pick_best(tracks).track_id == 4  # lossless, more channels
    tracks.append(_t(5, "dtsx", 8))
    assert media.pick_best(tracks).track_id == 5  # lossless with objects
    tracks.append(_t(6, "truehd_atmos", 8, commentary=True))
    assert media.pick_best(tracks).track_id == 5  # commentary is never picked
    a, b = _t(7, "flac", 6), _t(8, "flac", 6, original=True)
    assert media.pick_best([a, b]).track_id == 8  # original language beats file order
    c = _t(9, "flac", 6, default=True)
    assert media.pick_best([a, c]).track_id == 9  # then the default flag
    beq = _t(10, "truehd_atmos", 8)
    beq.beq = True
    assert media.pick_best([beq, a]).track_id == 7  # an existing BEQ track is not a source


@pytest.mark.parametrize("name,kind,label", [
    ("dts51", "dtshd_ma", "5.1"), ("lpcm20", "pcm", "2.0"), ("ac351", "ac3", "5.1")])
def test_probe_classifies_and_skips_commentary(env, name, kind, label):
    s, place, _ = env
    pr = media.probe(str(place(name)))
    assert pr.audio[1].commentary and not pr.audio[1].pickable
    best = pr.best
    assert best.audio_index == 0 and best.kind == kind and best.layout_label == label


@pytest.mark.parametrize("script", [
    "#!/bin/sh\nexit 1\n",                                       # could not read the file
    '#!/bin/sh\necho \'{"streams": [{"codec_name": "dts"}]}\'\n',  # one of the two audio tracks
])
def test_a_file_ffprobe_cannot_describe_is_a_scan_error_not_a_guess(env, script):
    s, place, tmp = env
    path = place("dts51")
    s.ffprobe = str(_exe(tmp / "ffprobe", script))
    with pytest.raises(RuntimeError):
        media.probe(str(path), s.mkvmerge, s.ffprobe)
    db = DB(s.db_path)
    row = db.one("SELECT * FROM media WHERE id=?", (worker.scan_file(s, db, path),))
    assert row["error"] and row["probe_json"] is None


# -- the PCM route: any track -> FLAC (or TrueHD through DEE) --------------------------
@pytest.mark.parametrize("name,expect", [
    ("dts51", "FLAC 5.1"),
    ("lpcm20", "FLAC 2.0"),
    ("ac351", "FLAC 5.1"),
])
def test_non_atmos_to_flac(env, name, expect):
    s, place, tmp = env
    path = place(name)
    before = media.probe(str(path))
    result, stages = _run(s, path, _encode(before), tmp)
    after = media.probe(str(path))
    assert [a.name for a in after.audio][:2] == [before.audio[0].name,
                                                beq_name(expect, "tester", result)]
    beq = after.audio[1]
    assert beq.kind == "flac" and beq.channels == before.audio[0].channels
    assert beq.layout == before.audio[0].layout and not beq.default
    assert result["route"] == "pcm" and result["filter_report"]["gain_db"] <= 0
    sync = result["sync"]
    assert abs(sync["lag_ms"]) <= 1.0 and sync["confidence"] > 0.5
    want = {"5.1": {"L", "R", "C", "Ls", "Rs"}, "2.0": {"L", "R"}}[before.audio[0].layout_label]
    assert set(sync["channel_corr"]) >= want
    assert min(sync["channel_corr"].values()) > 0.9
    tags = subprocess.run(["mkvextract", str(path), "tags", "-"], capture_output=True,
                          text=True).stdout
    assert "BEQ_LABEL" in tags and "tester" in tags
    assert "BEQ filter: writing FLAC" in {st for st, _ in stages}
    fid = result["filter_report"]["fidelity"]
    assert fid["ok"] and fid["offset_frames"] == 0 and fid["max_err_lsb"] <= 1.5
    # every frame of the source's own decode (ffmpeg's DTS encoder sometimes ends the
    # 16 s test source a frame or two early)
    rep = result["filter_report"]
    assert fid["frames"] == rep["frames"] == rep["source_frames"] + rep["gap_frames"]
    assert abs(fid["frames"] - 16 * 48000) <= 2048


def test_71_reaches_dee_in_dolby_order_and_comes_back_exact(env):
    # DEE reads 7.1 by position as L R C LFE Ls Rs Lrs Rrs; ffmpeg's order puts the back
    # pair first. Without the remap the side and rear surrounds swap: the fake DEE reads
    # by position, as DEE does.
    s, place, tmp = env
    path = place("lpcm71")
    pr = media.probe(str(path))
    assert pr.best.layout == "7.1"
    result, _ = _run(s, path, _encode(pr, format="truehd"), tmp)
    args = _dee_args(tmp)
    assert ("--presentation", "8ch") in _pairs(args)
    fid = result["filter_report"]["fidelity"]
    assert fid["ok"] and fid["max_err_lsb"] <= 1.5
    corr = result["sync"]["channel_corr"]
    assert set(corr) == {"L", "R", "C", "LFE", "Lb", "Rb", "Ls", "Rs"}
    assert min(corr.values()) > 0.9
    assert "Dolby channel order" in (tmp / "job.log").read_text()


def test_dts_to_channel_based_truehd_via_dee(env):
    s, place, tmp = env
    path = place("dts51")
    pr = media.probe(str(path))
    result, _ = _run(s, path, _encode(pr, format="truehd"), tmp)
    after = media.probe(str(path))
    assert after.audio[1].name == beq_name("TrueHD 5.1", "tester", result)
    assert "TrueHD" in after.audio[1].codec
    args = _dee_args(tmp)
    shim, dee_args = _pairs(args[:args.index("--")]), _pairs(args[args.index("--") + 1:])
    assert {("--as-is", "on"), ("--drc", "none")} <= shim and "--oamd-as-is" not in args
    assert {("--presentation", "2ch:drc_default_on=0"), ("--presentation", "6ch"),
            ("--input-format", "wav")} <= dee_args
    assert ("-i", str(s.work_dir / "job-1" / "beq.wav")) in dee_args
    assert ("--loudness-management", "skip:dialnorm=-31") in dee_args  # DTS carries none
    # the peak at the target, above 0.75 FS: --as-is codes those samples unchanged too
    rep = result["filter_report"]
    assert rep["gain_limited_by"] == "target" and rep["peak_target_dbfs"] == -1.0
    assert -1.0 >= rep["peak_out_dbfs"] > -2.5
    assert abs(result["sync"]["lag_ms"]) <= 1.0
    fid = result["filter_report"]["fidelity"]
    rep = result["filter_report"]
    assert fid["ok"] and fid["max_err_lsb"] <= 1.5
    assert fid["frames"] == rep["source_frames"] + rep["gap_frames"]
    assert abs(fid["frames"] - 16 * 48000) <= 2048


def test_encoder_that_alters_samples_fails_the_fidelity_check(env, monkeypatch):
    s, place, tmp = env
    path = place("dts51")
    before = path.read_bytes()
    monkeypatch.setenv("FAKE_DEE_PAN", "volume=0.999")
    pr = media.probe(str(path))
    with pytest.raises(JobFailed, match="not the lossless BEQ"):
        _run(s, path, _encode(pr, format="truehd"), tmp)
    assert path.read_bytes() == before


def test_encoder_delay_is_found_and_corrected(env, monkeypatch):
    s, place, tmp = env
    path = place("lpcm20")
    monkeypatch.setenv("FAKE_DEE_DELAY_MS", "20")
    pr = media.probe(str(path))
    result, _ = _run(s, path, _encode(pr, format="truehd"), tmp)
    fid = result["filter_report"]["fidelity"]
    assert fid["ok"] and fid["offset_frames"] == 960
    assert abs(result["sync"]["lag_ms"]) <= 1.0


def test_a_measured_encoder_delay_goes_into_the_first_remux(env, monkeypatch):
    s, place, tmp = env
    s.charts = False
    path = place("lpcm20")
    monkeypatch.setenv("FAKE_DEE_DELAY_MS", "20")
    monkeypatch.setattr(synccheck, "compare",
                        lambda *a, **k: synccheck.CheckResult(0.0, 0.1, 6.0, channels=2))
    result, _ = _run(s, path, _encode(media.probe(str(path)), format="truehd"), tmp)
    assert result["filter_report"]["fidelity"]["offset_frames"] == 960
    assert result["sync"]["applied_ms"] == pytest.approx(-20)
    log = (tmp / "job.log").read_text()
    assert "--sync 0:-20" in log and "Correcting" not in log


def test_keep_work_keeps_the_wav_on_the_pcm_route(env):
    s, place, tmp = env
    s.keep_work = True
    path = place("lpcm20")
    _run(s, path, _encode(media.probe(str(path)), format="truehd"), tmp)
    assert (s.work_dir / "job-1" / "beq.wav").is_file()


def test_gap_in_the_source_timeline_is_filled_and_stays_in_sync(env):
    s, place, tmp = env
    src = place("lpcm20")
    head, tail = src.with_name("head.mkv"), src.with_name("tail.mkv")
    path = src.with_name("Test Movie (2020) gap.mkv")
    subprocess.run(["mkvmerge", "-q", "-o", str(head), "--split", "parts:00:00:00-00:00:03",
                    str(src)], check=True)
    subprocess.run(["mkvmerge", "-q", "-o", str(tail), "--split", "parts:00:00:03-00:00:16",
                    str(src)], check=True)
    # the second part's audio 40 ms later than where the first part ends
    subprocess.run(["mkvmerge", "-q", "-o", str(path), str(head), "+", "--sync", "1:40",
                    str(tail)], check=True)
    head.unlink()
    tail.unlink()
    pr = media.probe(str(path))
    result, _ = _run(s, path, _encode(pr), tmp)
    rep = result["filter_report"]
    assert rep["gaps"] >= 1 and rep["gap_frames"] >= 1900  # >= the 40 ms added
    assert rep["fidelity"]["ok"]
    log = (tmp / "job.log").read_text()
    assert "filled with digital silence" in log and "Correcting" not in log
    assert abs(result["sync"]["lag_ms"]) <= 1.0


def test_late_starting_track_keeps_its_place(env, tmp_path):
    s, place, tmp = env
    src = place("lpcm20")
    path = src.with_name("Test Movie (2020) late.mkv")
    subprocess.run(["mkvmerge", "-q", "-o", str(path), "--sync", "1:250", str(src)], check=True)
    pr = media.probe(str(path))
    result, _ = _run(s, path, _encode(pr), tmp)
    assert "--sync 250 ms" in (tmp / "job.log").read_text()
    assert result["filter_report"]["fidelity"]["ok"]
    assert abs(result["sync"]["lag_ms"]) <= 1.0


@pytest.mark.parametrize("kind", ["dtsx", "eac3_joc"])
@pytest.mark.parametrize("fmt", ["truehd", "flac"])
def test_object_tracks_are_refused_never_reduced_to_their_bed(env, monkeypatch, kind, fmt):
    s, place, tmp = env
    path = place("dts51")
    before = path.read_bytes()
    monkeypatch.setattr(media, "classify", lambda *a: kind if "dts" in a[1] else "ac3")
    pr = media.probe(str(path))
    assert pr.best.kind == kind and pr.best.objects
    with pytest.raises(JobFailed, match="lossy conversions are currently out of scope"):
        _run(s, path, _encode(pr, format=fmt), tmp)
    assert path.read_bytes() == before


@pytest.fixture
def joc(env, sources, monkeypatch):
    """A DD+ Atmos source (an E-AC-3 5.1 classified as JOC) and a fake cavern-dump."""
    s, place, tmp = env
    monkeypatch.setenv("CAVERN_DUMP", str(sources["cavern"]))
    s.cavern_dump = str(sources["cavern"])
    real = media.classify
    monkeypatch.setattr(media, "classify",
                        lambda *a: "eac3_joc" if real(*a) == "eac3" else real(*a))
    path = place("eac351")
    monkeypatch.setenv("FAKE_DEE_SOURCE", str(path))  # what the fake DEE "encodes"
    return s, path, tmp


def test_ddp_atmos_to_truehd_atmos_via_cavern(joc):
    s, path, tmp = joc
    pr = media.probe(str(path))
    assert pr.best.kind == "eac3_joc" and support_plan(pr.best).route == "joc"
    result, stages = _run(s, path, _encode(pr, format="truehd"), tmp)
    after = media.probe(str(path))
    assert after.audio[1].name == beq_name("TrueHD Atmos", "tester", result)
    assert after.audio[1].beq
    fid = result["filter_report"]["fidelity"]
    assert fid["ok"] and fid["method"] == ("objects decoded by Cavern, encoded sample-exact "
                                           "from that decode")
    assert fid["max_err_lsb"] <= 0.5 and fid["object_frames"] > 0
    assert fid["objects"] == 5 and fid["out_elements"] == 16 and fid["bed"] == "LFE"
    assert fid["limits"]["min_object_corr"] == 0.98
    dec = fid["decoder"]  # Cavern's decode against the plain E-AC-3 decode
    assert dec["lfe_corr"] > 0.9999 and dec["objects_corr"] > 0.9999
    assert dec["damf_frames"] == dec["core_frames"] == dec["frames"] == fid["frames"]
    assert abs(result["sync"]["lag_ms"]) <= 1.0
    assert ("--input-format", "atmos_mezz") in _pairs(_dee_args(tmp))
    assert "Decoding DD+ Atmos objects (Cavern)" in {st for st, _ in stages}


@pytest.mark.parametrize("var,value,match", [
    ("FAKE_CAVERN_LAG", "1200", "does not line up with the plain E-AC-3 decode"),
    ("FAKE_CAVERN_EXTRA", "0", "keep the objects' last samples"),
])
def test_cavern_with_other_timing_or_no_flush_fails(joc, monkeypatch, var, value, match):
    s, path, tmp = joc
    before = path.read_bytes()
    monkeypatch.setenv(var, value)
    with pytest.raises(JobFailed, match=match):
        _run(s, path, _encode(media.probe(str(path)), format="truehd"), tmp)
    assert path.read_bytes() == before


def test_cavern_alignment_moves_objects_and_their_metadata(tmp_path):
    from beq_encoder import atmos
    from .helpers import read_caf, write_damf
    n, lag = 48000, 577
    x = np.zeros((n + 1536, 3))
    x[:n, 0] = np.linspace(-0.5, 0.5, n)            # bed (LFE)
    x[lag:lag + n, 1] = np.linspace(0.5, -0.5, n)   # an object, late by `lag`
    base = tmp_path / "c"
    write_damf(base, x)
    meta = atmos.files(base)[2]
    meta.write_text(meta.read_text().replace("samplePos: 0", "samplePos: 1000"))
    atmos.align_joc(base, n)
    y = read_caf(atmos.files(base)[1])
    assert y.shape == (n, 3)
    assert np.allclose(y[:, 0], x[:n, 0], atol=2 / 2**23)
    assert np.allclose(y[:, 1], x[lag:lag + n, 1], atol=2 / 2**23)
    text = meta.read_text()
    assert "samplePos: 1000" in text and f"samplePos: {1000 - lag}" in text  # bed stays


def test_an_unreadable_dialogue_level_stops_before_the_filter_passes(env, monkeypatch):
    s, place, tmp = env
    path = place("ac351")
    monkeypatch.setattr(media, "dialnorm", lambda *a: None)
    stages = []
    params = _encode(media.probe(str(path)), format="truehd")
    runner = JobRunner(s, 1, str(path), params, lambda st, p: stages.append(st), tmp / "job.log")
    with pytest.raises(JobFailed, match="dialogue level"):
        runner.run()
    assert not any(st.startswith("BEQ filter") for st in stages)


def test_ac3_dialogue_level_is_carried_into_truehd(env):
    s, place, tmp = env
    path = place("ac351")
    pr = media.probe(str(path))
    result, _ = _run(s, path, _encode(pr, format="truehd"), tmp)
    assert ("--loudness-management", "skip:dialnorm=-27") in _pairs(_dee_args(tmp))
    assert result["filter_report"]["fidelity"]["ok"]


def test_lossless_pcm_truehd_refuses_unsupported_channel_count(env, monkeypatch):
    s, place, tmp = env
    path = place("lpcm20")
    pr = media.probe(str(path))
    t = pr.best
    monkeypatch.setattr(media.ProbeResult, "track", lambda self, tid: media.AudioTrack(
        **{**{f: getattr(t, f) for f in t.__slots__}, "channels": 3, "layout": "2.1"}))
    with pytest.raises(JobFailed, match="2.0, 5.1 or 7.1"):
        _run(s, path, _encode(pr, format="truehd"), tmp)


# -- catalogue matching by track ---------------------------------------------------------
CAT = [
    {"title": "Test Movie", "year": 2020, "theMovieDB": "333371", "audioTypes": ["Atmos"],
     "author": "a1", "source": "Disc", "digest": "atmos1", "content_type": "film",
     "created_at": 1543898641, "updated_at": 1611984156,
     "filters": [{"type": "LowShelf", "freq": 20, "gain": 5, "q": 0.9, "count": 2}]},
    {"title": "Test Movie", "year": 2020, "theMovieDB": "333371",
     "audioTypes": ["DTS-HD MA 5.1", "DD+ 5.1"], "author": "a2", "source": "Disc",
     "digest": "dts51", "content_type": "film", "filters": [], "created_at": "soon"},
    {"title": "Test Movie", "year": 2020, "theMovieDB": "333371", "audioTypes": ["DD+ Atmos"],
     "author": "a3", "source": "Streaming", "digest": "ddpa", "content_type": "film",
     "filters": [{"type": "LowShelf", "freq": 18, "gain": 6, "q": 0.9}]},
]


@pytest.fixture
def cat(tmp_path):
    p = tmp_path / "database.json"
    p.write_text(json.dumps(CAT))
    c = catalogue.Catalogue(p, "", 9999)
    c.load()
    return c


def test_catalogue_keeps_no_filter_entries_and_matches_the_track(cat):
    assert cat.get("dts51") is not None and cat.get("dts51").no_filters
    got = cat.for_track("333371", "Test Movie", "2020", None, "dtshd_ma", "5.1")
    assert got[0] == ("exact", cat.get("dts51"))
    assert {m for m, _ in got[1:]} == {"other"}
    got = cat.for_track("333371", None, None, None, "truehd_atmos", "7.1")
    assert got[0][0] == "exact" and got[0][1].digest == "atmos1"
    got = cat.for_track("333371", None, None, None, "eac3", "5.1")
    assert got[0][1].digest == "dts51"  # the DD+ 5.1 half of a combined entry
    got = cat.for_track("333371", None, None, None, "flac", "5.1")
    assert got[0][0] == "related"  # a FLAC rip may be of the DTS-HD MA track


def test_catalogue_entries_carry_their_dates(cat):
    e = cat.get("atmos1")
    assert (e.created, e.updated) == (1543898641, 1611984156)
    assert e.to_dict()["created_at"] == 1543898641
    assert cat.get("dts51").created is None  # a bad date is dropped, not the entry
    assert cat.get("ddpa").updated is None


def test_refresh_reloads_only_when_the_file_changed(cat):
    loads = []
    cat.on_loaded = lambda: loads.append(cat.loaded_at)
    first = cat.loaded_at
    cat.refresh()
    assert cat.loaded_at == first and loads == []
    cat.path.write_text(json.dumps(CAT[:1]))
    cat.refresh()
    assert cat.loaded_at != first and loads == [cat.loaded_at]
    assert [e.digest for e in cat.entries] == ["atmos1"]


def test_a_failed_download_is_an_error_only_without_a_cached_copy(cat, tmp_path):
    cat.refresh(force=True)  # the empty URL cannot be fetched
    assert cat.error is None and cat.entries
    empty = catalogue.Catalogue(tmp_path / "none.json", "", 9999)
    empty.refresh()
    assert empty.error and empty.loaded_at is None


def test_title_match_is_normalised_year_tolerant_and_films_only(tmp_path):
    def entry(title, year, digest, kind="film"):
        return {"title": title, "year": year, "digest": digest, "content_type": kind, "filters": []}

    p = tmp_path / "database.json"
    p.write_text(json.dumps([entry("The Movie!", 2001, "next-year"), entry("The  MOVIE", 2003, "off"),
                             entry("The Movie", 2000, "tv", "TV"), entry("Other", 2000, "other")]))
    c = catalogue.Catalogue(p, "", 9999)
    c.load()
    assert [e.digest for e in c.candidates(None, "the movie", "2000", None)] == ["next-year"]


def test_a_malformed_catalogue_entry_is_skipped_not_fatal(tmp_path):
    good = {"title": "Good", "year": 2000, "digest": "good", "filters": []}
    p = tmp_path / "database.json"
    p.write_text(json.dumps([
        {"title": "Null filters", "digest": "bad1", "filters": None},
        {"title": "Odd value", "digest": "bad2",
         "filters": [{"type": "PeakingEQ", "freq": "?", "gain": 1, "q": 1}]},
        "not a record",
        good,
    ]))
    c = catalogue.Catalogue(p, "", 9999)
    c.load()
    assert [e.digest for e in c.entries] == ["good"]


def test_an_unreadable_catalogue_is_reported(tmp_path):
    p = tmp_path / "database.json"
    p.write_text("{ not json")
    c = catalogue.Catalogue(p, "", 9999)
    c.refresh()
    assert "load failed" in c.error and c.loaded_at is None


def test_title_matching_stays_fast_on_a_large_catalogue(tmp_path):
    """Most of a library has no tmdb hit, so every film takes the title path."""
    p = tmp_path / "database.json"
    p.write_text(json.dumps([{"title": f"Film Number {i}", "year": 2000, "digest": str(i),
                              "content_type": "film", "filters": []} for i in range(100_000)]))
    c = catalogue.Catalogue(p, "", 9999)
    c.load()
    started = time.perf_counter()
    for i in range(300):
        assert [e.digest for e in c.candidates(None, f"Film Number {i}", "2000", None)] == [str(i)]
    assert time.perf_counter() - started < 2.0


# -- prepare: catalogue, then the designer -------------------------------------------------
class FakeDesigner:
    def __init__(self, root: Path):
        self.root = root
        self.mode = "design"
        self.requests: list[dict] = []
        self.checked: dict[str, np.ndarray] = {}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                self._send(200, {"status": "ok", "contract_version": "1.6", "shared_root": True,
                                 "designer_release": "9.9.9", "design_version": 7})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                for name, arr in [("mono_mix", body["mono_mix"]), *body["channels"].items()]:
                    rate, data = wavfile.read(fake.root / arr["file"]["path"])
                    col = data[:, arr["file"]["channel"]].astype("<f8")
                    assert rate == body["fs"] and len(col) == arr["shape"][0]
                    assert hashlib.sha256(col.tobytes()).hexdigest() == arr["sha256"], name
                    fake.checked[name] = col
                if fake.mode == "decline":
                    return self._send(200, {"contract_version": body["contract_version"],
                                            "decline_reason": "within_goal_tolerance",
                                            "decline_message": "nothing worth correcting"})
                cand = {"filters": [{"type": "low_shelf", "freq_hz": 24.19, "gain_db": 15.6,
                                     "q": 1.972},
                                    {"type": "low_shelf", "freq_hz": 13.89, "gain_db": 22.32,
                                     "q": 0.423}],
                        "confidence": 0.95, "mv_adjust_db": 37.5, "method": "non_parametric",
                        "gain_reduction_db": -5.9, "commentary": {"strategy": "flatten"}}
                rej = {**cand, "confidence": 0.9, "commentary": {"strategy": "counterfactual"},
                       "rejection_reasons": ["introduces a cliff"]}
                self._send(200, {"contract_version": body["contract_version"],
                                 "candidates": [cand], "rejected": [rej], "design_version": 7,
                                 "design_settings": "abc", "designer_build": "deadbeef"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake_designer(env, monkeypatch):
    s, _, tmp = env
    fd = FakeDesigner(s.work_dir)
    s.designer_url = fd.url
    yield fd
    fd.close()


def test_prepare_uses_catalogue_and_ends_no_beq_for_a_no_filter_entry(env, cat, fake_designer):
    s, place, tmp = env
    path = place("dts51")
    r, _ = _run(s, path, {"phase": "prepare"}, tmp, cat)
    assert r["outcome"] == "no_beq" and "does not need BEQ" in r["message"]
    assert r["track"]["kind"] == "dtshd_ma" and r["design"] is None
    assert not fake_designer.requests


def test_prepare_designs_by_reference_when_the_track_has_no_entry(env, cat, fake_designer):
    s, place, tmp = env
    path = place("lpcm20")
    r, stages = _run(s, path, {"phase": "prepare"}, tmp, cat)
    assert r["outcome"] == "awaiting" and r["design"]["candidates"]
    c = r["design"]["candidates"][0]
    assert c["label"] == "beqforge 9.9.9 flatten" and c["gain_reduction_db"] == -5.9
    assert c["filters"][0] == {"type": "LowShelf", "freq": 24.19, "gain": 15.6, "q": 1.972,
                               "count": 1}
    assert r["design"]["rejected"][0]["rejection_reasons"] == ["introduces a cliff"]
    body = fake_designer.requests[0]
    assert body["contract_version"] == "1.2" and body["coverage"] == "complete_programme"
    assert list(body["channels"]) == ["L", "R"] and body["fs"] == 1000
    assert body["bass_management"]["lpf_fs"] == 80.0
    ch = fake_designer.checked
    mono = ch["L"] * analysis.MAIN_GAIN + ch["R"] * analysis.MAIN_GAIN
    assert np.allclose(ch["mono_mix"], mono)
    assert abs(len(ch["L"]) - 16000) < 100
    # cached: a second prepare does not decode again
    _run(s, path, {"phase": "prepare"}, tmp, cat)
    assert "Analysis: cached" in (tmp / "job.log").read_text()
    assert len(fake_designer.requests) == 2


def test_prepare_lfe_weighting_in_51(env, cat, fake_designer):
    s, place, tmp = env
    path = place("ac351")
    # the AC-3 track's only exact entry is the no-filters one, so force the designer
    r, _ = _run(s, path, {"phase": "prepare", "force_designer": True}, tmp, cat)
    assert r["outcome"] == "awaiting"
    body = fake_designer.requests[0]
    assert list(body["channels"]) == ["L", "R", "C", "LFE", "Ls", "Rs"]
    ch = fake_designer.checked
    mono = sum(ch[n] * (analysis.LFE_GAIN if n == "LFE" else analysis.MAIN_GAIN)
               for n in body["channels"])
    assert np.allclose(ch["mono_mix"], mono)


def test_prepare_refuses_to_guess_when_the_catalogue_has_not_loaded(env, fake_designer, tmp_path):
    s, place, tmp = env
    unloaded = catalogue.Catalogue(tmp_path / "none.json", "", 9999)
    with pytest.raises(JobFailed, match="catalogue is not loaded"):
        _run(s, place("lpcm20"), {"phase": "prepare"}, tmp, unloaded)
    assert not fake_designer.requests
    r, _ = _run(s, place("lpcm20"), {"phase": "prepare", "force_designer": True}, tmp, unloaded)
    assert r["outcome"] == "awaiting" and len(fake_designer.requests) == 1


def test_prepare_estimates_every_catalogue_filter_and_never_fails_for_it(env, fake_designer,
                                                                          tmp_path):
    s, place, tmp = env
    lpcm = {**CAT[0], "audioTypes": ["LPCM 2.0"], "author": "a4", "digest": "lpcm20"}
    p = tmp_path / "with-lpcm.json"
    p.write_text(json.dumps([*CAT, lpcm]))
    cat = catalogue.Catalogue(p, "", 9999)
    cat.load()
    path = place("lpcm20")
    ffmpeg = s.ffmpeg
    s.ffmpeg = str(_exe(tmp_path / "ffmpeg", "#!/bin/sh\necho 'no decoder' >&2\nexit 1\n"))
    r, _ = _run(s, path, {"phase": "prepare"}, tmp, cat)
    assert r["outcome"] == "awaiting" and r["predicted"] == {}
    assert "Gain estimate: not made" in (tmp / "job.log").read_text()
    s.ffmpeg = ffmpeg
    r, stages = _run(s, path, {"phase": "prepare"}, tmp, cat)
    assert r["outcome"] == "awaiting" and r["design"] is None and not fake_designer.requests
    assert set(r["predicted"]) == {"lpcm20", "atmos1", "ddpa"}  # exact, related and other
    assert r["predicted"]["lpcm20"]["channel"] in ("L", "R")
    assert ("Estimating the gain of each filter", 100.0) in stages


def _cached_analysis(root, key, mtime):
    d = root / "analysis" / key
    d.mkdir(parents=True)
    (d / "analysis.wav").write_bytes(b"x" * 1000)
    (d / "meta.json").write_text(json.dumps({"schema": analysis.SCHEMA, "wav_bytes": 1000,
                                             "wav": f"analysis/{key}/analysis.wav"}))
    os.utime(d, (mtime, mtime))


def test_analysis_eviction_goes_by_last_use_and_spares_what_a_job_claims(tmp_path):
    _cached_analysis(tmp_path, "used-lately", 100)
    _cached_analysis(tmp_path, "claimed", 200)
    _cached_analysis(tmp_path, "stale", 300)
    assert analysis.load_cached(tmp_path, "used-lately") is not None
    with analysis.in_use("claimed"):
        analysis.prune(tmp_path, 2500)  # room for two of the three
    left = {d.name for d in (tmp_path / "analysis").iterdir()}
    assert left == {"used-lately", "claimed"}


def test_designer_decline_is_no_beq_not_failure(env, cat, fake_designer):
    s, place, tmp = env
    fake_designer.mode = "decline"
    r, _ = _run(s, place("lpcm20"), {"phase": "prepare"}, tmp, cat)
    assert r["outcome"] == "no_beq" and "within_goal_tolerance" in r["message"]


def test_designer_unreachable_fails_the_prepare(env, cat):
    s, place, tmp = env
    s.designer_url = "http://127.0.0.1:9"
    with pytest.raises(JobFailed, match="not reachable"):
        _run(s, place("lpcm20"), {"phase": "prepare"}, tmp, cat)


# -- the support rules ---------------------------------------------------------------
def support_plan(t):
    from beq_encoder import support
    return support.plan(t)


def test_support_plan_rules(monkeypatch, tmp_path):
    from beq_encoder import support
    monkeypatch.setenv("CAVERN_DUMP", str(tmp_path / "none"))
    assert support.plan(_t(0, "truehd_atmos", 8)).label == "TrueHD Atmos"
    for kind in ("dtsx", "eac3_joc"):
        p = support.plan(_t(0, kind, 8))
        assert not p.ok and "objects" in p.reason
        assert "lossy conversions are currently out of scope" in p.reason
        assert support.formats(_t(0, kind, 8), True) == []  # not even a test FLAC of the bed
    p = support.plan(_t(0, "dtshd_ma", 8, layout="7.1", sample_rate=48000))
    assert p.ok and p.route == "pcm" and p.label == "TrueHD 7.1" and not p.native
    assert support.plan(_t(0, "truehd", 6, layout="5.1(side)")).native
    assert not support.plan(_t(0, "dtshd_ma", 7, layout="6.1")).ok  # TrueHD 2.0/5.1/7.1 only
    assert not support.plan(_t(0, "dtshd_ma", 1, layout="mono")).ok
    for ch in (6, 8):
        p = support.plan(_t(0, "dtshd_ma", ch))  # ffprobe named no layout: it can't be assumed
        assert not p.ok and "layout is unknown" in p.reason
    assert not support.plan(_t(0, "truehd", 6, layout="made-up")).ok
    p = support.plan(_t(0, "pcm", 6, layout="5.1", layout_assumed=True, sample_rate=48000))
    assert p.ok and p.label == "TrueHD 5.1"  # PCM's layout is established by its channel count
    p = support.plan(_t(0, "pcm", 8, layout="7.1(wide)", sample_rate=48000))
    assert not p.ok and "relabelled" in p.reason  # not a layout DEE's template takes
    assert support.plan(_t(0, "pcm", 8, layout="7.1", sample_rate=48000)).ok
    p = support.plan(_t(0, "pcm", 6, layout="5.1", sample_rate=96000))
    assert not p.ok and "96 kHz" in p.reason
    # production offers TrueHD only; FLAC/DD+ are test-only
    assert support.formats(_t(0, "truehd_atmos", 8), False) == ["truehd"]
    assert support.formats(_t(0, "ac3", 6, layout="5.1(side)"), False) == ["truehd"]
    assert support.formats(_t(0, "dtsx", 8), False) == []
    # DD+ Atmos: objects decoded by Cavern when cavern-dump is there; DTS:X never
    exe = tmp_path / "cavern-dump"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("CAVERN_DUMP", str(exe))
    p = support.plan(_t(0, "eac3_joc", 6))
    assert p.ok and p.route == "joc" and p.label == "TrueHD Atmos" and not p.native
    assert support.formats(_t(0, "eac3_joc", 6), True) == ["truehd"]  # no FLAC of the bed
    assert not support.plan(_t(0, "dtsx", 8)).ok
    assert support.formats(_t(0, "truehd_atmos", 8), True) == ["truehd"]  # FLAC = bed only
    assert support.formats(_t(0, "ac3", 6, layout="5.1(side)"), True) == ["truehd", "flac"]


def test_prepare_ends_unsupported_for_object_tracks(env, cat, fake_designer, monkeypatch):
    s, place, tmp = env
    path = place("dts51")
    monkeypatch.setattr(media, "classify", lambda *a: "dtsx" if "dts" in a[1] else "ac3")
    r, _ = _run(s, path, {"phase": "prepare"}, tmp, cat)
    assert r["outcome"] == "unsupported" and "objects" in r["message"]
    assert not fake_designer.requests


def test_encode_refuses_flac_in_production(env):
    s, place, tmp = env
    s.allow_test_formats = False
    path = place("ac351")
    with pytest.raises(JobFailed, match="lossless TrueHD only"):
        _run(s, path, _encode(media.probe(str(path))), tmp)
