"""synccheck.compare against a real ffmpeg decode."""

import subprocess

import numpy as np

from beq_encoder import layouts, synccheck

SR = 48000


def _mkv(tmp_path, tracks):
    """One MKV with a 7.1 FLAC track per (frames, 8) array, in order."""
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for i, x in enumerate(tracks):
        raw = tmp_path / f"t{i}.f32"
        raw.write_bytes(x.astype("<f4").tobytes())
        cmd += ["-f", "f32le", "-ar", str(SR), "-ch_layout", "7.1", "-i", str(raw)]
    for i in range(len(tracks)):
        cmd += ["-map", str(i)]
    out = tmp_path / "pair.mkv"
    subprocess.run([*cmd, "-c:a", "flac", str(out)], check=True)
    return out


def test_infrasonic_boost_in_lfe_is_not_a_channel_swap(tmp_path):
    """A BEQ that lifts the LFE's deep bass must not change what the check sees above 300 Hz.
    The LFE has almost nothing up there, so the boosted bass swamped it two ways: leaking
    through a single 2-pole high-pass, and as the filter's response to the step at the
    start of a window cut before it. Either dragged the LFE's correlation with itself down,
    and a real job failed with "LFE↔L"."""
    n = SR * 12
    rng = np.random.default_rng(7)
    t = np.arange(n) / SR
    orig = 0.05 * rng.standard_normal((n, 8))
    lfe = layouts.names(8, "7.1").index("LFE")
    orig[:, lfe] = 0.3 * np.sin(2 * np.pi * 40 * t) + 0.002 * rng.standard_normal(n)
    beq = orig.copy()
    beq[:, lfe] += 0.6 * np.sin(2 * np.pi * 23 * t)  # not 0 at the window start (3.6 s)

    res = synccheck.compare("ffmpeg", str(_mkv(tmp_path, [orig, beq])), 0, 1, 12.0, 8,
                            layouts.names(8, "7.1"))

    assert res.mismatches == []
    assert res.diag["LFE"] > 0.99
    assert abs(res.lag_ms) < 0.2
