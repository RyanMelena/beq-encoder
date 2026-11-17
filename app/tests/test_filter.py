import os

import numpy as np
import pytest
from scipy.signal import sosfilt

from beq_encoder import caf, workers
from beq_encoder.beqfilter import FilterJob, apply
from beq_encoder.biquad import FilterSpec, design_sos, rbj, sosfilt_channels

from .helpers import make_signal
from .helpers import write_caf

FILTERS = [
    FilterSpec("LowShelf", 40.0, -4.2, 1.0),
    FilterSpec("LowShelf", 22.0, 5.0, 0.9, count=3),
    FilterSpec("PeakingEQ", 28.0, 2.5, 2.0),
]


def test_rbj_matches_catalogue_coefficients():
    # 10 Cloverfield Lane, aron7awol: catalogue biquads at 96 kHz (a stored negated)
    b, a = rbj(FilterSpec("LowShelf", 40.0, -4.2, 1.0), 96000)
    assert b == pytest.approx([0.9996823836277061, -1.997042922306009, 0.9973659126544856], abs=1e-14)
    assert [-a[1], -a[2]] == pytest.approx([1.9970412515096825, -0.997049967078518], abs=1e-14)
    b, a = rbj(FilterSpec("LowShelf", 22.0, 5.0, 0.9), 96000)
    assert b == pytest.approx([1.0002311798591146, -1.998613357621746, 0.9983849406416764], abs=1e-14)


@pytest.mark.parametrize("bits,is_float,le", [
    (24, False, False), (24, False, True), (16, False, False), (32, False, True),
    (32, True, False), (64, True, True)])
def test_a_block_split_over_threads_converts_as_sample_by_sample(bits, is_float, le):
    """A block long enough to be converted in parts (workers.over_rows), with samples
    past full scale: the bytes, the count clipped and the decode are those of one sample
    at a time."""
    frames, ch = 5 * workers.MIN_ROWS + 1, 3
    x = np.random.default_rng(7).uniform(-1.2, 1.2, (frames, ch))
    info = caf.CafInfo("x", 48000.0, ch, bits, is_float, le, 0, frames * ch * bits // 8)
    order = "little" if le else "big"
    if is_float:
        kept = x.astype(f"{'<' if le else '>'}f{bits // 8}")
        want, clipped, back = kept.tobytes(), int((np.abs(x) > 1).sum()), kept.astype(float)
    else:
        fs = 2 ** (bits - 1)
        q = [round(v) for v in (x * fs).reshape(-1).tolist()]  # half to even, as np.rint
        clipped = sum(v < -fs or v > fs - 1 for v in q)
        q = [min(max(v, -fs), fs - 1) for v in q]
        want = b"".join(v.to_bytes(bits // 8, order, signed=True) for v in q)
        back = np.array(q, dtype=float).reshape(-1, ch) / fs
    raw, n = caf.encode(info, x)
    assert (raw, n) == (want, clipped) and clipped
    assert np.array_equal(caf.decode(info, raw), back)
    assert caf.decode(info, b"").shape == (0, ch)
    with pytest.raises(ValueError, match="not whole frames"):
        caf.decode(info, raw[:-1])


def test_the_peak_of_a_block_is_its_largest_magnitude():
    x = np.random.default_rng(8).uniform(-0.5, 0.5, (5 * workers.MIN_ROWS + 1, 3))
    x[-1, 2] = -0.75
    assert workers.abs_peak(x) == 0.75
    assert workers.abs_peak(x[:0]) == 0.0
    x[3, 1] = np.nan  # as np.max: never mistaken for a quiet block
    assert np.isnan(workers.abs_peak(x))


def test_rows_are_split_into_parts_that_cover_them_in_order():
    for rows in (0, 1, workers.MIN_ROWS, 2 * workers.MIN_ROWS + 1, 1 << 18):
        parts = workers.over_rows(lambda a, b: (a, b), rows)
        assert parts[0][0] == 0 and parts[-1][1] == rows
        assert all(p[1] == n[0] for p, n in zip(parts, parts[1:], strict=False))
        assert len(parts) <= max(1, workers.WORKERS)


@pytest.mark.parametrize("ch", [1, 3])
def test_a_channel_that_falls_silent_is_filtered_as_before_and_settles(ch):
    """A second of audio, then silence. Block by block, every 24-bit sample is what one
    plain sosfilt over the whole signal gives; and once the audio has died away the
    filter is at rest (exactly zero), not left working on numbers too small to matter."""
    fs, block = 48000, 1 << 16
    sos = design_sos(FILTERS, fs)
    x = np.zeros((8 * fs, ch))
    x[:fs] = np.rint(0.1 * np.random.default_rng(9).standard_normal((fs, ch)) * 2**23) / 2**23
    state = np.zeros((sos.shape[0], 2, ch))
    blocks = []
    for i in range(0, len(x), block):
        y, state = sosfilt_channels(sos, x[i:i + block], state)
        blocks.append(y)
    y, want = np.concatenate(blocks), sosfilt(sos, x, axis=0)
    assert np.array_equal(np.rint(y * 2**23), np.rint(want * 2**23))
    assert np.abs(y - want).max() < 1e-20
    assert np.abs(y[:fs]).max() > 0.1 and state.shape == (sos.shape[0], 2, ch)
    assert not state.any() and not y[-fs:].any()
    assert want[-fs:].any()  # left alone, the filter never comes to rest


def test_design_expands_count():
    assert design_sos(FILTERS, 48000).shape == (5, 6)


@pytest.mark.parametrize("le", [False, True])
@pytest.mark.parametrize("unknown_size", [False, True])
def test_apply_auto_gain_matches_reference(tmp_path, le, unknown_size):
    x = make_signal()
    p = tmp_path / "a.atmos.audio"
    write_caf(p, x, little_endian=le, unknown_size=unknown_size)
    info = caf.read_info(str(p))
    assert (info.channels, info.bits, info.frames) == (16, 24, x.shape[0])
    with open(p, "rb") as fh:
        fh.seek(info.data_offset)
        src = caf.decode(info, fh.read(info.data_bytes))

    rep = apply(str(p), FilterJob(FILTERS, target_peak_dbfs=-1.0))
    ref = sosfilt(design_sos(FILTERS, 48000), src, axis=0)
    gain = 10 ** (-1.0 / 20) / np.max(np.abs(ref))
    assert rep.gain_db == pytest.approx(20 * np.log10(gain), abs=0.01)
    assert rep.peak_out_dbfs == pytest.approx(-1.0, abs=0.01)
    assert rep.clipped_samples == 0

    with open(p, "rb") as fh:
        fh.seek(info.data_offset)
        out = caf.decode(info, fh.read(info.data_bytes))
    lsb = 1 / 2**23
    assert np.max(np.abs(out - ref * gain)) <= 2.01 * lsb
    assert os.path.getsize(p) == info.data_offset + info.data_bytes  # nothing appended


def test_no_boost_means_no_attenuation(tmp_path):
    x = make_signal() * 0.2
    p = tmp_path / "quiet.audio"
    write_caf(p, x)
    rep = apply(str(p), FilterJob(FILTERS, target_peak_dbfs=-1.0))
    assert rep.gain_db == 0.0


def test_fixed_gain_single_pass_and_clip_count(tmp_path):
    x = make_signal()
    p = tmp_path / "f.audio"
    write_caf(p, x)
    rep = apply(str(p), FilterJob(FILTERS, fixed_gain_db=6.0))
    assert rep.peak_filtered_dbfs is None and rep.gain_db == 6.0
    assert rep.clipped_samples > 0


def test_filter_into_a_new_file_leaves_the_source_as_decoded(tmp_path):
    # the TrueHD Atmos routes keep the decode for their check and encode a filtered copy
    src, dst = tmp_path / "s.caf", tmp_path / "d.caf"
    x = make_signal(frames=48000, ch=4) * 0.5
    write_caf(src, x)
    before = src.read_bytes()
    r = apply(str(src), FilterJob(FILTERS, -1.0), out_path=str(dst))
    assert src.read_bytes() == before
    info_s, info_d = caf.read_info(str(src)), caf.read_info(str(dst))
    assert (info_d.channels, info_d.frames, info_d.data_offset) == \
        (info_s.channels, info_s.frames, info_s.data_offset)
    with open(src, "rb") as fh:
        fh.seek(info_s.data_offset)
        xs = caf.decode(info_s, fh.read(info_s.data_bytes))
    with open(dst, "rb") as fh:
        fh.seek(info_d.data_offset)
        y = caf.decode(info_d, fh.read(info_d.data_bytes))
    exp = sosfilt(design_sos(FILTERS, 48000.0), xs, axis=0) * r.gain
    assert np.abs(y - exp).max() <= 1.5 / 2**23


def test_threaded_filter_is_identical_to_sosfilt():
    from beq_encoder.biquad import sosfilt_channels
    sos = design_sos(FILTERS, 48000.0)
    x = make_signal(frames=1 << 15, ch=7)
    zi = np.zeros((sos.shape[0], 2, 7))
    a1, za = sosfilt(sos, x[:20000], axis=0, zi=zi)
    a2, _ = sosfilt(sos, x[20000:], axis=0, zi=za)
    b1, zb = sosfilt_channels(sos, x[:20000], zi)
    b2, _ = sosfilt_channels(sos, x[20000:], zb)
    assert np.array_equal(a1, b1) and np.array_equal(a2, b2) and np.array_equal(za, zb)


# -- the high-pass floor -------------------------------------------------------------------
@pytest.mark.parametrize("hz,fs", [(15.0, 48000.0), (10.0, 96000.0), (20.0, 1000.0)])
def test_the_floor_is_a_4th_order_butterworth(hz, fs):
    from scipy.signal import butter, sosfreqz

    from beq_encoder.biquad import highpass_rows
    rows = np.asarray(highpass_rows(hz, fs))
    assert rows.shape == (2, 6)
    f = np.array([hz / 4, hz / 2, hz, 2 * hz, 4 * hz, 100.0])
    _, mine = sosfreqz(rows, worN=f, fs=fs)
    _, ref = sosfreqz(butter(4, hz, "highpass", fs=fs, output="sos"), worN=f, fs=fs)
    assert 20 * np.log10(np.abs(mine)) == pytest.approx(20 * np.log10(np.abs(ref)), abs=1e-6)
    # -3 dB at the corner, 24 dB/octave below it, flat above
    db = 20 * np.log10(np.abs(mine))
    assert db[2] == pytest.approx(-3.01, abs=0.01)
    assert db[0] - db[1] == pytest.approx(-24, abs=0.6)
    assert abs(db[5]) < 0.01


def test_the_floor_follows_the_beq_in_one_cascade():
    from beq_encoder.biquad import highpass_rows, response_db
    assert design_sos(FILTERS, 48000.0, 0.0).shape == (5, 6)  # none: the BEQ alone
    sos = design_sos(FILTERS, 48000.0, 15.0)
    assert sos.shape == (7, 6)
    assert np.array_equal(sos[:5], design_sos(FILTERS, 48000.0))
    assert np.array_equal(sos[5:], np.asarray(highpass_rows(15.0, 48000.0)))
    assert FilterJob(FILTERS, highpass_hz=15.0).sos(48000.0).tolist() == sos.tolist()
    f = np.array([5.0, 15.0, 60.0])
    with_floor = response_db(FILTERS, f, 48000.0, 15.0)
    beq = response_db(FILTERS, f, 48000.0)
    assert with_floor[2] == pytest.approx(beq[2], abs=0.01)
    assert with_floor[1] == pytest.approx(beq[1] - 3.01, abs=0.01)
    assert with_floor[0] < beq[0] - 30
    # a floor alone (no BEQ bands with gain) is still the floor
    assert design_sos([], 48000.0, 15.0).shape == (2, 6)


@pytest.mark.parametrize("hz,ok", [(0, True), (0.0, True), (15, True), (40, True), (12.5, True),
                                   (-1, False), (40.1, False), (float("nan"), False),
                                   (float("inf"), False), ("abc", False)])
def test_a_floor_is_0_or_up_to_40_hz(hz, ok):
    from beq_encoder.biquad import check_highpass
    if ok:
        assert check_highpass(hz) == float(hz)
    else:
        with pytest.raises(ValueError):
            check_highpass(hz)


def test_the_filter_pass_applies_the_floor_and_fits_the_target_with_it(tmp_path):
    """Infrasonic content a shelf lifts sets the gain without a floor; with one, the gain
    is set by what is left, so the track is turned down less."""
    fs, n = 48000, 4 * 48000
    t = np.arange(n) / fs
    x = np.stack([0.3 * np.sin(2 * np.pi * 6 * t) + 0.3 * np.sin(2 * np.pi * 50 * t)] * 2, axis=1)
    shelf = [FilterSpec("LowShelf", 20.0, 12.0, 0.7)]
    reports = {}
    for hz in (0.0, 15.0):
        p = tmp_path / f"f{hz:g}.caf"
        write_caf(p, x)
        info = caf.read_info(str(p))
        with open(p, "rb") as fh:
            fh.seek(info.data_offset)
            src = caf.decode(info, fh.read(info.data_bytes))
        reports[hz] = rep = apply(str(p), FilterJob(shelf, -10.0, highpass_hz=hz))
        with open(p, "rb") as fh:
            fh.seek(info.data_offset)
            out = caf.decode(info, fh.read(info.data_bytes))
        exp = sosfilt(design_sos(shelf, 48000.0, hz), src, axis=0) * rep.gain
        assert np.abs(out - exp).max() <= 2.01 / 2**23
        assert rep.peak_out_dbfs == pytest.approx(-10.0, abs=0.01)
    assert reports[15.0].gain_db > reports[0.0].gain_db + 3


def test_a_filter_json_names_its_floor(tmp_path):
    p = tmp_path / "f.json"
    p.write_text('{"filters": [{"type": "LowShelf", "freq": 20, "gain": 6, "q": 0.7}], '
                 '"highpass_hz": 12}')
    assert FilterJob.load(str(p)).highpass_hz == 12.0
    p.write_text('{"filters": []}')
    assert FilterJob.load(str(p)).highpass_hz == 0.0
    p.write_text('{"filters": [], "highpass_hz": 90}')
    with pytest.raises(ValueError):
        FilterJob.load(str(p))
