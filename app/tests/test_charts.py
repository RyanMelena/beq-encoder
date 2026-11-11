"""Before/after charts: measurement, rendering, embedding in the MKV."""

import io
import json
import shutil
import subprocess

import numpy as np
import pytest

from beq_encoder import analysis, assess, charts, media
from beq_encoder.pipeline import JobRunner

from .test_tracks import ENTRY, _encode, env, sources  # noqa: F401  (fixtures)


def _chunks(x, n=50_000):
    return (x[i:i + n] for i in range(0, len(x), n))


def test_a_sine_reads_its_level_at_its_frequency():
    t = np.arange(120 * charts.FS) / charts.FS
    x = 0.5 * np.sin(2 * np.pi * 30.0 * t)
    m = charts.measure_stream(_chunks(x))
    i = int(np.argmin(np.abs(m.freqs - 30.0)))
    assert m.peak_db[i] == pytest.approx(-6.02, abs=0.3)
    assert m.avg_db[i] == pytest.approx(-6.02, abs=0.3)
    assert m.peak_db[np.argmin(np.abs(m.freqs - 80.0))] < -60
    assert m.duration_s == pytest.approx(120.0)
    assert m.heat_db.shape[0] == len(m.heat_freqs)
    rows = np.abs(m.heat_freqs - 30.0) < 0.5
    assert np.median(m.heat_db[rows].max(axis=0)) == pytest.approx(-6.02, abs=0.5)


def _fake_ffmpeg(tmp_path, exit_code):
    """Writes 4 s of 1 kHz stereo f64le, then exits with `exit_code`."""
    exe = tmp_path / "ffmpeg"
    exe.write_text(f"#!/bin/sh\nhead -c 64000 /dev/zero\nexit {exit_code}\n")
    exe.chmod(0o755)
    return str(exe)


def test_a_decode_that_fails_part_way_is_not_a_whole_film(tmp_path):
    chunks = charts.decode_mono(_fake_ffmpeg(tmp_path, 1), "x", 0, 2, ("L", "R"))
    with pytest.raises(RuntimeError, match="could not decode"):
        for _ in chunks:
            pass


def test_the_bass_screen_stops_on_a_failed_excerpt(tmp_path):
    chunks = assess.sampled_mono(_fake_ffmpeg(tmp_path, 1), "x", 0, 2, ("L", "R"), 7200.0)
    with pytest.raises(RuntimeError, match="could not decode"):
        for _ in chunks:
            pass


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_the_shared_decode_takes_an_excerpt_at_1khz(tmp_path):
    src = tmp_path / "a.mka"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=6:r=48000",
                    "-ac", "2", "-c:a", "pcm_s24le", str(src)], check=True)
    whole = sum(len(b) for b in analysis.decode_blocks("ffmpeg", str(src), 0, 2))
    part = sum(len(b) for b in analysis.decode_blocks("ffmpeg", str(src), 0, 2, start=2.0,
                                                      length=1.5))
    assert abs(whole - 6000) <= 2 and abs(part - 1500) <= 2


def test_a_decode_that_finishes_yields_everything(tmp_path):
    chunks = charts.decode_mono(_fake_ffmpeg(tmp_path, 0), "x", 0, 2, ("L", "R"))
    assert sum(len(c) for c in chunks) == 4000


@pytest.mark.parametrize("frac", [charts.SMOOTH_OCT, 1 / 6])
def test_smoothing_keeps_a_symmetric_peak_centred(frac):
    freqs = np.arange(20.0, 500.0, 0.05)
    octaves_from_peak = np.log2(freqs / 100.0)
    smoothed = charts.smooth(freqs, -(octaves_from_peak / (1 / 24)) ** 2, frac)
    assert abs(octaves_from_peak[np.argmax(smoothed)]) < 1 / 400


def test_both_charts_render_self_contained_pngs():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(200 * charts.FS) * 0.01
    b = charts.measure_stream(_chunks(x))
    a = b.shifted(+3.0)
    info = charts.ChartInfo(title="Test Movie (2020)", source_track="DTS-HD MA 5.1 · eng · track 1",
                            beq_track="BEQ FLAC 5.1 (tester)", beq_source="BEQ Catalogue · tester",
                            filters=ENTRY["filters"], gain_db=-2.0, peak_in_dbfs=-3.0,
                            peak_out_dbfs=-1.0, mv_db=-1.5, encoder="beq-encoder test",
                            date="2026-01-01", facts=[("Lossless check", "passed")])
    for render in (charts.render_spectrum, charts.render_heatmap):
        buf = io.BytesIO()
        render(b, a, info, buf)
        png = buf.getvalue()
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 50_000
    d = charts.summary(b, a, info)
    assert d["gain_db"] == -2.0 and len(d["spectrum"]["original"]["freqs_hz"]) > 500


def _attachments(path):
    return media.mkvmerge_identify("mkvmerge", str(path)).get("attachments", [])


def _run(s, path, params, log):
    return JobRunner(s, 1, str(path), params, lambda *a: None, log).run()


def test_every_encode_embeds_charts_and_report(env):
    s, place, tmp = env
    path = place("dts51")
    # a file with an attachment of its own (a subtitle font) keeps it
    font = tmp / "font.ttf"
    font.write_bytes(b"\0" * 64)
    withfont = path.with_name("Test Movie (2020) font.mkv")
    subprocess.run(["mkvmerge", "-q", "-o", str(withfont), "--attachment-mime-type",
                    "font/ttf", "--attach-file", str(font), str(path)], check=True)
    pr = media.probe(str(withfont))
    result = _run(s, withfont, _encode(pr), tmp / "job.log")
    art = result["charts"]
    names = art["names"]
    got = {a["file_name"]: a for a in _attachments(withfont)}
    assert {"font.ttf", names["spectrum"], names["heatmap"], names["report"]} <= set(got)
    assert got[names["spectrum"]]["content_type"] == "image/png"
    assert got[names["report"]]["content_type"] == "application/json"
    # the track's tags point at its attachments
    tags = subprocess.run(["mkvextract", str(withfont), "tags", "-"], capture_output=True,
                          text=True, check=True).stdout
    assert names["spectrum"] in tags and names["report"] in tags
    # the report inside the file is complete
    rep_id = got[names["report"]]["id"]
    out = tmp / "rep.json"
    subprocess.run(["mkvextract", str(withfont), "attachments", f"{rep_id}:{out}"], check=True,
                   capture_output=True)
    rep = json.loads(out.read_text())
    assert rep["fidelity"]["ok"] and rep["sync"] is not None
    assert rep["filter"]["filters"] == ENTRY["filters"] and rep["beq_track"]["name"]
    assert (s.charts_dir / "job-1" / "spectrum.png").is_file()

    # a re-run replaces the BEQ track and its charts, nothing else
    pr = media.probe(str(withfont))
    _run(s, withfont, _encode(pr), tmp / "job2.log")
    files = [a["file_name"] for a in _attachments(withfont)]
    assert files.count("font.ttf") == 1
    assert sorted(f for f in files if f.startswith("beq-")) == sorted(names.values())


def test_a_chart_failure_never_fails_the_beq_track(env, monkeypatch):
    s, place, tmp = env
    path = place("lpcm20")

    def boom(*a, **k):
        raise RuntimeError("no fonts today")

    monkeypatch.setattr(charts, "render_spectrum", boom)
    pr = media.probe(str(path))
    result = _run(s, path, _encode(pr), tmp / "job.log")
    assert result["charts"] is None and result["track_name"]
    assert "no charts for this BEQ track: no fonts today" in (tmp / "job.log").read_text()
    assert not [a for a in _attachments(path) if a["file_name"].startswith("beq-")]
