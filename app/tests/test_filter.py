import os

import numpy as np
import pytest
from scipy.signal import sosfilt

from beq_encoder import caf, workers
from beq_encoder.beqfilter import FilterJob, apply
from beq_encoder.biquad import FilterSpec, design_sos, rbj

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
