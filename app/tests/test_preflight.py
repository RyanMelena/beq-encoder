"""The gain estimate shown on the review page, against what the filter pass measures."""

import shutil
import subprocess

import numpy as np
import pytest
from scipy.io import wavfile
from scipy.signal import butter, sosfilt

from beq_encoder import analysis, beqfilter, preflight
from beq_encoder.biquad import FilterSpec, design_sos

from .test_tracks import _t

SHELF = {"type": "LowShelf", "freq": 30.0, "gain": 12.0, "q": 0.7}


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_the_estimate_is_the_peak_the_filter_pass_measures(tmp_path, monkeypatch):
    monkeypatch.setattr(analysis, "CHUNK_FRAMES", 4096)  # a film's many blocks, in a short test
    fs = 48000
    t = np.arange(30 * fs) / fs
    mid = sosfilt(butter(4, [200, 2000], "bandpass", fs=fs, output="sos"),
                  np.random.default_rng(3).standard_normal((len(t), 6)), axis=0)
    x = 0.6 * mid / np.abs(mid).max(axis=0)
    # unfiltered, a main is the loudest channel; the shelf lifts the LFE above it
    x[:, 3] = 0.15 * sum(np.sin(2 * np.pi * f * t + f) for f in (15, 20, 25))
    src = tmp_path / "film.wav"
    wavfile.write(src, fs, x.astype(np.float32))
    track = _t(0, "pcm", 6, layout="5.1(side)")
    meta = analysis.extract("ffmpeg", src, track, 30.0, tmp_path, "k", subprocess.Popen,
                            lambda *_: None, lambda _: None, lambda: None)

    got = preflight.predict(analysis.read_channels(tmp_path, meta), meta["channels"],
                            {"shelf": [SHELF]})

    def read(_stage):
        return (x[i:i + beqfilter.CHUNK_FRAMES] for i in range(0, len(x), beqfilter.CHUNK_FRAMES))

    real = beqfilter.filter_stream(read, lambda _y: 0, design_sos([FilterSpec(**SHELF)], fs), 6,
                                   -1.0)
    assert got["shelf"]["channel"] == "LFE"
    assert got["shelf"]["peak_dbfs"] == pytest.approx(beqfilter.dbfs(real.peak_filtered), abs=1.0)


def test_only_filters_the_analysis_can_represent_are_estimated():
    blocks = [np.full((1000, 2), 0.1)]
    got = preflight.predict(blocks, ["L", "R"], {
        "shelf": [SHELF],
        "none": [],
        "too_high": [SHELF, {**SHELF, "type": "PeakingEQ", "freq": 450.0}],
        "unknown_type": [{**SHELF, "type": "Notch"}],
    })
    assert set(got) == {"shelf"}
