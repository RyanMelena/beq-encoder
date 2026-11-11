"""pcmio: bit-exact decode, start offset split, and the sample-by-sample check."""

import shutil
import subprocess
import time

import numpy as np
import pytest
from scipy.signal import sosfilt

from beq_encoder import pcmio
from beq_encoder.biquad import FilterSpec, design_sos

LSB = pcmio.LSB24


class FakeStream:
    def __init__(self, x: np.ndarray):
        self.x, self.ch, self.frames, self.pos = x, x.shape[1], 0, 0

    def read(self, n):
        y = self.x[self.pos: self.pos + n]
        self.pos += len(y)
        self.frames += len(y)
        return y


SOS = design_sos([FilterSpec.from_catalogue(
    {"type": "LowShelf", "freq": 30, "q": 0.7, "gain": 6.0})], 48000.0)


def _signal(n=200_000, ch=2, seed=1):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((n, ch)) * 0.05
    x[:5000] = 0.0  # a silent opening, like most films
    return x


def _encoded(x, gain=0.5, pad=0, lead=0, tail=0):
    y = sosfilt(SOS, x, axis=0) * gain
    y += (np.random.default_rng(2).random(y.shape) - np.random.default_rng(3).random(y.shape)) * LSB
    q = np.clip(np.rint(y * 2**23), -(2**23), 2**23 - 1) / 2**23
    z = np.zeros((pad + lead, x.shape[1]))
    return np.concatenate([z, q, np.zeros((tail, x.shape[1]))])


def test_exact_output_passes():
    x = _signal()
    r = pcmio.verify(FakeStream(x), FakeStream(_encoded(x)), SOS, 0.5, 0, block=30_000)
    assert r.ok and r.frames == len(x) and r.max_err_lsb <= 1.5 and r.offset_frames == 0


def test_pad_lead_and_tail_are_accounted_for():
    x = _signal()
    r = pcmio.verify(FakeStream(x), FakeStream(_encoded(x, pad=12, lead=960, tail=40)),
                     SOS, 0.5, 12, block=30_000)
    assert r.ok and r.offset_frames == 960 and r.trailing_frames == 40 and r.pad_frames == 12


@pytest.mark.parametrize("damage,msg", [
    (lambda y: y.__setitem__((150_000, 1), y[150_000, 1] + 4 * LSB), "sample 150000 differs"),
    (lambda y: None, "ends early"),
])
def test_any_changed_or_missing_sample_fails(damage, msg):
    x = _signal()
    y = _encoded(x)
    damage(y)
    if msg == "ends early":
        y = y[:-100]
    r = pcmio.verify(FakeStream(x), FakeStream(y), SOS, 0.5, 0, block=30_000)
    assert not r.ok and msg in r.problem


def test_missing_leading_samples_fail():
    x = _signal()
    y = _encoded(x)[5010:]  # lost the first 10 non-silent frames
    r = pcmio.verify(FakeStream(x), FakeStream(y), SOS, 0.5, 0, block=30_000)
    assert not r.ok


def test_lead_splits_whole_ms_and_padding():
    assert pcmio.lead(0.0, 48000) == (0, 0)
    assert pcmio.lead(0.25, 48000) == (250, 0)
    assert pcmio.lead(0.0125, 48000) == (12, 24)
    assert pcmio.lead(-0.005, 48000) == (0, 0)
    assert pcmio.lead(0.0129999, 48000) == (13, 0)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_decode_is_bit_exact(tmp_path):
    # odd packet timing (DTS: 512-sample frames at ms-rounded timestamps) must not
    # make the decode stretch, pad or drop anything
    src = tmp_path / "a.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "anoisesrc=color=pink:duration=5:sample_rate=48000",
                    "-c:a", "pcm_s24le", "-frame_size", "512", str(src)], check=True)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-f", "s32le", "-"],
                         capture_output=True, check=True).stdout
    ref = np.frombuffer(raw, "<i4").astype(np.float64)[:, None] / 2**31
    with pcmio.PCMStream(pcmio.decode_cmd("ffmpeg", str(src)), 1) as s:
        got = np.concatenate([s.read(10_000) for _ in range(30)])
    assert len(got) == len(ref) == 5 * 48000
    assert np.array_equal(got, ref)


def test_gap_filled_inserts_silence_where_told():
    x = np.arange(1, 21, dtype=float).reshape(10, 2)
    g = pcmio.GapFilled(FakeStream(x), [(0, 2), (4, 3)])
    got = np.concatenate([g.read(3) for _ in range(6)])
    assert len(got) == 15 and g.frames == 15
    assert np.array_equal(got[:2], np.zeros((2, 2)))
    assert np.array_equal(got[2:6], x[:4])
    assert np.array_equal(got[6:9], np.zeros((3, 2)))
    assert np.array_equal(got[9:], x[4:])


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("mkvmerge")),
                    reason="needs ffmpeg + mkvtoolnix")
def test_find_gaps_sees_a_hole_in_the_timeline(tmp_path):
    a, b, out = tmp_path / "a.mka", tmp_path / "b.mka", tmp_path / "gap.mkv"
    for p, d in ((a, 2), (b, 3)):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        f"anoisesrc=d={d}:r=48000", "-c:a", "pcm_s24le", str(p)], check=True)
    subprocess.run(["mkvmerge", "-q", "-o", str(out), str(a), "+", "--sync", "0:50", str(b)],
                   check=True)
    m = pcmio.find_gaps("ffprobe", str(out), 48000)
    assert m.first_pts == 0.0 and m.frames == 5 * 48000
    assert len(m.gaps) == 1 and m.gaps[0][0] == 2 * 48000
    assert abs(m.gaps[0][1] - 2400) <= 2  # 50 ms
    plain = pcmio.find_gaps("ffprobe", str(a), 48000)
    assert plain.gaps == [] and plain.frames == 2 * 48000


def test_decodes_keep_the_full_dynamic_range():
    # ffmpeg's AC-3 / E-AC-3 decoders apply DRC unless told not to (drc_scale 0)
    cmd = pcmio.decode_cmd("ffmpeg", "x.mkv", 1)
    i = cmd.index("-i")
    assert cmd[i - 2: i] == ["-drc_scale", "0"]


def test_find_gaps_maps_every_dts_frame(tmp_path):
    # ffprobe lists DTS frames as "pts,nb_samples," (a trailing empty field); the map
    # once skipped every line and so checked nothing
    p = tmp_path / "dts.mka"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=4:r=48000",
                    "-ac", "6", "-c:a", "dca", "-strict", "-2", str(p)], check=True)
    m = pcmio.find_gaps("ffprobe", str(p), 48000)
    assert m.first_pts == 0.0 and m.gaps == []
    assert m.frames == 4 * 48000


def _fake_ffprobe(tmp_path, script: str) -> str:
    exe = tmp_path / "ffprobe"
    exe.write_text(script)
    exe.chmod(0o755)
    return str(exe)


def test_find_gaps_fails_when_ffprobe_exits_non_zero(tmp_path):
    exe = _fake_ffprobe(tmp_path, "#!/bin/sh\necho 0.0,40\nexit 3\n")
    with pytest.raises(RuntimeError, match="code 3"):
        pcmio.find_gaps(exe, "x.mka", 48000)


def test_find_gaps_times_out_across_the_read(tmp_path):
    exe = _fake_ffprobe(tmp_path, "#!/bin/sh\necho 0.0,40\nexec sleep 300\n")
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        pcmio.find_gaps(exe, "x.mka", 48000, timeout=0.5)
    assert time.monotonic() - started < 30


def test_long_silent_opening_is_not_mistaken_for_missing_samples():
    x = _signal(n=300_000)
    x[:120_000] = 0.0  # 2.5 s of digital silence before the film starts
    r = pcmio.verify(FakeStream(x), FakeStream(_encoded(x)), SOS, 0.5, 0, block=30_000)
    assert r.ok, r.problem
    r = pcmio.verify(FakeStream(x), FakeStream(_encoded(x, lead=480)), SOS, 0.5, 0, block=30_000)
    assert r.ok and r.offset_frames == 480
