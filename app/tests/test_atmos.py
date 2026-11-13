"""The TrueHD Atmos check: every sample of every element, and the object positions frame by
frame, against what DEE 6.7 under dee_shim makes of the BEQ-filtered source."""

import json
import time

import numpy as np
import pytest
import yaml
from scipy.signal import butter, sosfilt

from beq_encoder import atmos

from .helpers import FRAME, dee_positions, write_caf, write_damf

FS = 48000
LSB = 1 / 2**23
IDENTITY = np.array([[1.0, 0, 0, 1.0, 0, 0]])
SOS = butter(4, 400, "low", fs=FS, output="sos")
GAIN = 0.9
LEAD = 240                           # frames the encoder put before the programme
ORDER = [3, 0, 2, 1]                 # encoded object k carries source object ORDER[k]
FRAMES = 12 * FS
EVERY = 8 * FRAME                    # the source moves its objects this often


def _programme(frames=FRAMES):
    """The source: a bed LFE and four objects, 24-bit. Object 2 is silent from 3 to 5 s
    and object 4 for the first second, so DEE sends neither's positions meanwhile."""
    rng = np.random.default_rng(1234)
    levels = [0.08, 0.02, 0.12, 0.06, 0.05]
    x = np.stack([v * rng.standard_normal(frames) for v in levels], axis=1)
    x[3 * FS:5 * FS, 2] = 0
    x[:FS, 4] = 0
    return np.rint(x * 2**23) / 2**23


def _position(o, k):
    return [round((o * 7 + k * 3) % 21 / 10 - 1, 4), round((o * 5 + k * 11) % 21 / 10 - 1, 4),
            0.5 * (k % 2)]


def _source_events(frames=FRAMES):
    """Each object's (samplePos, pos): moved every EVERY samples, 1112 samples into a DEE
    frame, as on a disc."""
    return {o: [(0, _position(o, 0))] + [(k * EVERY + 1112, _position(o, k))
                                         for k in range(1, frames // EVERY)]
            for o in range(4)}


def _dee(x, events, at_sample=True):
    """What DEE codes from the filtered source: every element as it is (24-bit), the
    objects reordered, padded with 11 silent elements; each object's positions as DEE
    codes them and a decoder reads them (`at_sample`: to the sample, else at the start
    of their frame). Returns (audio, {encoded object k: events})."""
    y = np.rint(sosfilt(SOS, x, axis=0) * GAIN * 2**23) / 2**23
    y = np.column_stack([y[:, 0], y[:, 1:][:, ORDER], np.zeros((len(y), 11))])
    return y, {k: dee_positions(events[o], y[:, 1 + k], at_sample)
               for k, o in enumerate(ORDER)}


def _metadata(base, events):
    meta = atmos.files(base)[2]
    doc = yaml.safe_load(meta.read_text())
    doc["events"] = [e for e in doc["events"] if "pos" not in e] + [
        {"ID": 10 + o, "samplePos": t, "pos": p} for o, evs in events.items() for t, p in evs]
    meta.write_text(yaml.safe_dump(doc, sort_keys=False))


def _write(tmp_path, x, events, y, enc_events, lead=LEAD, bed=("LFE",)):
    write_damf(tmp_path / "src", x)
    _metadata(tmp_path / "src", events)
    write_damf(tmp_path / "out", np.concatenate([np.zeros((lead, y.shape[1])), y]), bed=bed)
    _metadata(tmp_path / "out", {k: [(t + lead, p) for t, p in evs]
                                 for k, evs in enc_events.items()})


def _case(at_sample=True):
    x, events = _programme(), _source_events()
    return (x, events, *_dee(x, events, at_sample))


def _verify(tmp_path, update_times=atmos.SAMPLE, **kw):
    return atmos.verify(tmp_path / "src", tmp_path / "out", SOS, GAIN,
                        update_times=update_times, **kw)


def _audible_frame_update(enc_events, k):
    """An update of encoded object k in a frame where it plays, and its index."""
    return next((i, t) for i, (t, _) in enumerate(enc_events[k]) if i and t > 2 * FS)


@pytest.mark.parametrize("times", [atmos.SAMPLE, atmos.FRAME_ONLY])
def test_an_exact_encode_passes_and_reports_what_it_checked(tmp_path, times):
    x, events, y, enc = _case(at_sample=times == atmos.SAMPLE)
    _write(tmp_path, x, events, y, enc)
    ticks = []
    res = _verify(tmp_path, update_times=times, tick=ticks.append)
    assert res.ok, res.problem
    assert res.offset_frames == LEAD and res.frames == FRAMES
    assert res.objects == 4 and res.out_elements == 16 and res.bed == "LFE"
    assert res.mapping == {0: 0, **{1 + o: 1 + ORDER.index(o) for o in range(4)}}
    assert res.max_err_lsb <= 0.5 + 1e-9 and "LFE" in res.element_err_lsb
    # every frame in which an object plays, and no other
    nf = -(-FRAMES // FRAME)
    playing = sum(bool(np.any(y[f * FRAME:(f + 1) * FRAME, 1 + k]))
                  for f in range(nf) for k in range(4))
    assert res.object_frames == playing < 4 * nf
    assert res.update_times == times and res.max_update_shift == 0
    assert ticks[-1] == pytest.approx(FRAMES)
    report = json.dumps(res.to_dict(), allow_nan=False)  # the job API rejects NaN
    assert '"method": "sample-exact"' in report


def test_a_wrong_position_where_the_object_plays_fails(tmp_path):
    x, events, y, enc = _case()
    i, t = _audible_frame_update(enc, 0)
    p = enc[0][i][1]
    enc[0][i] = (t, [p[0], p[1], 1.0 if p[2] < 0.5 else 0.0])
    _write(tmp_path, x, events, y, enc)
    res = _verify(tmp_path)
    assert not res.ok
    assert f"object {ORDER[0] + 1} (ID {10 + ORDER[0]}) is placed at" in res.problem
    assert "the position metadata does not match" in res.problem


def test_an_update_one_frame_late_fails(tmp_path):
    x, events, y, enc = _case()
    i, t = _audible_frame_update(enc, 2)
    enc[2][i] = (t + FRAME, enc[2][i][1])
    _write(tmp_path, x, events, y, enc)
    res = _verify(tmp_path)
    assert not res.ok and f"at {t // FRAME * FRAME / FS:.3f} s" in res.problem
    assert "the position metadata does not match" in res.problem


def test_updates_at_the_start_of_their_frame_fail_when_read_to_the_sample(tmp_path):
    """DEE's own timing (dee-shim#3): each update moved to the start of its frame. The
    position at each frame's end is right; when the update starts is not."""
    x, events, y, enc = _case(at_sample=False)
    _write(tmp_path, x, events, y, enc)
    res = _verify(tmp_path)
    assert not res.ok and "the position update times do not match" in res.problem
    assert _verify(tmp_path, update_times=atmos.FRAME_ONLY).ok


def test_updates_at_their_sample_fail_when_compared_mid_frame(tmp_path):
    """Read only to the frame, the position is compared mid-frame: an update 1112 samples
    in is not in effect there yet. The check must be told how the decoder reads."""
    x, events, y, enc = _case()
    _write(tmp_path, x, events, y, enc)
    res = _verify(tmp_path, update_times=atmos.FRAME_ONLY)
    assert not res.ok and "the position metadata does not match" in res.problem


def test_read_only_to_the_frame_an_update_near_its_frame_start_passes(tmp_path):
    """truehdd 0.6.2 reads a dee_shim 0.2 update within an access unit (40 samples) of
    its frame's start, either side."""
    x, events, y, enc = _case(at_sample=False)
    enc = {k: [(t - 39 if t else t, p) for t, p in evs] for k, evs in enc.items()}
    _write(tmp_path, x, events, y, enc)
    res = _verify(tmp_path, update_times=atmos.FRAME_ONLY)
    assert res.ok, res.problem


def test_a_move_at_another_objects_update_in_the_frame_passes(tmp_path):
    """dee_shim codes a frame's update at the sample of one of its updates: an object
    whose own update is later in the frame moves at the other one's."""
    x, events, y, enc = _case()
    t0 = events[0][3][0]
    events[1][3] = (t0 + 200, events[1][3][1])  # object 1 moves 200 samples after object 0
    _write(tmp_path, x, events, y, enc)  # the encode moves both at object 0's
    res = _verify(tmp_path)
    assert res.ok, res.problem
    assert res.max_update_shift == 200


def test_a_move_where_the_source_updates_nothing_fails(tmp_path):
    x, events, y, enc = _case()
    i, t = _audible_frame_update(enc, 1)
    enc[1][i] = (t + 1, enc[1][i][1])
    _write(tmp_path, x, events, y, enc)
    res = _verify(tmp_path)
    assert not res.ok
    assert (f"object {ORDER[1] + 1} (ID {10 + ORDER[1]}) moves at sample {t + 1}"
            in res.problem)
    assert "the position update times do not match" in res.problem


@pytest.mark.parametrize("version,times", [
    ("truehdd 0.6.2 (truehd library 0.7.2) Built: 2026-09-15", atmos.FRAME_ONLY),
    ("truehdd 0.6.1", atmos.FRAME_ONLY),
    ("truehdd 0.6.2-13-g77ddc52 (truehd library 0.7.2)", atmos.FRAME_ONLY),
    ("truehdd 0.6.2-19-g0e800fb (truehd library 0.7.2)", atmos.SAMPLE),
    ("truehdd 0.6.3", atmos.SAMPLE),
    ("truehdd 0.7.0 (truehd library 0.8.0)", atmos.SAMPLE),
    ("", atmos.FRAME_ONLY),
])
def test_how_the_decoder_reads_update_times(version, times):
    assert atmos.decoder_update_times(version) == times


def test_where_a_silent_object_sits_does_not_count(tmp_path):
    """Object 2 is silent from 3 to 5 s: a position sent meanwhile is never heard."""
    x, events, y, enc = _case()
    k = ORDER.index(1)
    enc[k].append((4 * FS, [1.0, 1.0, 1.0]))
    enc[k].sort(key=lambda e: e[0])
    _write(tmp_path, x, events, y, enc)
    res = _verify(tmp_path)
    assert res.ok, res.problem


def test_an_object_the_source_never_positions_is_not_judged(tmp_path):
    x, events, y, enc = _case()
    del events[2]
    enc[ORDER.index(2)] = [(0, [0.3, 0.3, 0.3])]
    _write(tmp_path, x, events, y, enc)
    res = _verify(tmp_path)
    assert res.ok, res.problem


@pytest.mark.parametrize("off_lsb,ok", [(0, True), (-1, False), (1, False)])
def test_one_sample_one_lsb_off_fails(tmp_path, off_lsb, ok):
    """One sample of what DEE was given changed by `off_lsb`, away from (1) or towards
    (-1) the unrounded filtered value: only the rounding passes."""
    x, events, y, enc = _case()
    s = 6 * FS
    rounding = y[s, 2] - sosfilt(SOS, x[:, 1 + ORDER[1]])[s] * GAIN
    y[s, 2] += off_lsb * LSB * (1 if rounding >= 0 else -1)
    _write(tmp_path, x, events, y, enc)
    res = _verify(tmp_path)
    assert res.ok == ok, res.problem
    if not ok:
        assert (f"sample {6 * FS} of object {ORDER[1] + 1} (ID {10 + ORDER[1]}) differs from "
                "the filtered source by ") in res.problem
        assert res.max_err_lsb > 0.5


def test_a_dropped_object_fails(tmp_path):
    x, events, y, enc = _case()
    k = ORDER.index(0)  # the quietest: the programme still lines up without it
    y[:, 1 + k] = 0
    del enc[k]
    _write(tmp_path, x, events, y, enc)
    res = _verify(tmp_path)
    assert not res.ok
    assert "object 1 (ID 10) is not carried by any one element" in res.problem


@pytest.mark.parametrize("stray_lsb,ok", [(0, True), (1, False)])
def test_a_silent_source_object_must_arrive_silent(tmp_path, stray_lsb, ok):
    """The master is not dithered, so a silent object reaches DEE as digital silence; an
    encoded element carrying even 1 LSB carries audio from nowhere."""
    x, events, y, enc = _case()
    x = np.column_stack([x, np.zeros(len(x))])
    events[4] = [(0, [0.0, 0.0, 0.0])]
    y[:, 5] = np.random.default_rng(3).integers(-stray_lsb, stray_lsb + 1, len(y)) * LSB
    _write(tmp_path, x, events, y, enc)
    res = _verify(tmp_path)
    assert res.ok == ok, res.problem
    if not ok:
        assert "element 6 carries audio that belongs to no source element" in res.problem


def _bed_differs(x, events, y, enc, tmp_path):
    _write(tmp_path, x, events, y, enc, bed=("L",))


def _too_many_elements(x, events, y, enc, tmp_path):
    _write(tmp_path, x, events, np.column_stack([y, np.zeros(len(y))]), enc)


def _channel_count(x, events, y, enc, tmp_path):
    _write(tmp_path, x, events, y, enc)
    write_caf(atmos.files(tmp_path / "out")[1], y[:, :-1])


def _no_alignment(x, events, y, enc, tmp_path):
    _write(tmp_path, x, events, 0.05 * np.random.default_rng(5).standard_normal(y.shape), enc)


def _starts_early(x, events, y, enc, tmp_path):
    x[:FS] = 0
    _write(tmp_path, x, events, _dee(x, events)[0][120:], enc, lead=0)


def _ends_early(x, events, y, enc, tmp_path):
    x, events = _programme(24 * FS), _source_events(24 * FS)  # longer than alignment reads
    y, enc = _dee(x, events)
    _write(tmp_path, x, events, y[:-FS], enc)


def _trailing_frames(x, events, y, enc, tmp_path):
    _write(tmp_path, x, events, np.concatenate([y, y[:FS]]), enc)


def _loud_lead(x, events, y, enc, tmp_path):
    _write(tmp_path, x, events, y, enc)
    noisy = np.concatenate([np.full((LEAD, y.shape[1]), 2 * LSB), y])
    write_caf(atmos.files(tmp_path / "out")[1], noisy)


def _never_positioned(x, events, y, enc, tmp_path):
    del enc[1]
    _write(tmp_path, x, events, y, enc)


BROKEN = {
    "bed_differs": (_bed_differs, "bed channels differ"),
    "too_many_elements": (_too_many_elements, "the encoded track has 17 elements"),
    "channel_count": (_channel_count, "encoded DAMF has 15 channels, its .atmos lists 16"),
    "no_alignment": (_no_alignment, "does not line up with the filtered source"),
    "starts_early": (_starts_early, "starts 120 samples early"),
    "ends_early": (_ends_early, "ends early"),
    "trailing_frames": (_trailing_frames, "unexpected frames after the source ends"),
    "loud_lead": (_loud_lead, f"starts with {LEAD} frames that are not in the source"),
    "never_positioned": (_never_positioned, "has audio but the encode never positions it"),
}


@pytest.mark.parametrize("name", BROKEN)
def test_a_broken_encode_fails_and_says_why(tmp_path, name):
    build, says = BROKEN[name]
    build(*_case(), tmp_path)
    res = _verify(tmp_path)
    assert not res.ok and says in res.problem


# -- one pass over the programme where the encode allows it -----------------------------

NAMES = ["LFE"] + [f"object {o + 1} (ID {10 + o})" for o in range(4)]
MAPPING = {0: 0, **{1 + o: 1 + ORDER.index(o) for o in range(4)}}


def _errors_lsb(x, y):
    """Each source element's largest distance from the encoded element that carries it,
    worked out here from the filter alone."""
    want = sosfilt(SOS, x, axis=0) * GAIN
    carried = y[:, [MAPPING[c] for c in range(5)]]
    return dict(zip(NAMES, np.abs(want - carried).max(axis=0) / LSB, strict=True))


def _playing(y):
    """The (object, frame) pairs in which an encoded object has audio."""
    return sum(bool(np.any(y[f * FRAME:(f + 1) * FRAME, 1 + k]))
               for f in range(-(-FRAMES // FRAME)) for k in range(4))


def _passes(ticks, block):
    """How many times the check read the programme: it ticks once a block of each pass."""
    return len(ticks) / -(-FRAMES // block)


@pytest.mark.parametrize("block", [1 << 17, 1 << 14])
def test_an_exact_encode_is_checked_in_one_pass(tmp_path, block):
    """Reordered objects, one that starts a second in (blocks into the programme at the
    smaller size) and one that falls silent for a while: one read of the programme finds
    which element carries each and every sample's error."""
    x, events, y, enc = _case()
    _write(tmp_path, x, events, y, enc)
    ticks = []
    res = _verify(tmp_path, block=block, tick=ticks.append)
    assert res.ok, res.problem
    assert _passes(ticks, block) == 1
    assert ticks == sorted(ticks) and ticks[-1] == pytest.approx(FRAMES)
    assert res.mapping == MAPPING
    assert res.element_err_lsb == pytest.approx(_errors_lsb(x, y), abs=1e-6)
    assert res.object_frames == _playing(y) and res.max_update_shift == 0


def test_objects_that_start_alike_are_told_apart_in_a_second_pass(tmp_path):
    """Objects 1 and 4 open alike, and the first free encoded element carries object 4.
    It matches object 1 only until they part, so the samples are compared again once the
    whole programme has settled which element carries which. The encode is exact."""
    block = 1 << 14
    x, events = _programme(), _source_events()
    x[:FS, 4] = x[:FS, 1]
    y, enc = _dee(x, events)
    _write(tmp_path, x, events, y, enc)
    ticks = []
    res = _verify(tmp_path, block=block, tick=ticks.append)
    assert res.ok, res.problem
    assert _passes(ticks, block) == 2 and ticks[-1] == pytest.approx(FRAMES)
    assert res.mapping == MAPPING
    assert res.element_err_lsb == pytest.approx(_errors_lsb(x, y), abs=1e-6)
    assert res.object_frames == _playing(y)


def test_audio_in_an_element_before_its_object_starts_fails(tmp_path):
    """Object 4 is silent for the first second; the element that carries it is not (1 LSB,
    once, blocks before the object starts)."""
    x, events, y, enc = _case()
    y[100, 1 + ORDER.index(3)] = LSB
    _write(tmp_path, x, events, y, enc)
    res = _verify(tmp_path, block=1 << 14)
    assert not res.ok
    assert "sample 100 of object 4 (ID 13) differs from the filtered source by 1.0 LSB" \
        in res.problem


# -- the stages, each alone ------------------------------------------------------------
def test_position_events_hold_each_event_until_the_next(tmp_path):
    meta = tmp_path / "m.atmos.metadata"
    meta.write_text(yaml.safe_dump({"events": [
        {"ID": 1, "samplePos": 30, "pos": [3, 3, 3]},
        {"ID": 1, "samplePos": 10, "pos": [1, 1, 1]},
        {"ID": 1, "samplePos": 10, "pos": [2, 2, 2]},  # same time as the one before: it wins
        {"ID": 1, "samplePos": 99, "pos": [9, 9, 9]},
    ]}))
    p = atmos.position_events(meta, [1, 2])
    assert 2 not in p
    at = p[1].at(np.array([0, 9, 10, 29, 30, 98, 1000]))
    assert at.tolist() == [[1, 1, 1]] * 2 + [[2, 2, 2]] * 2 + [[3, 3, 3]] * 2 + [[9, 9, 9]]


def test_position_events_read_compact_events(tmp_path):
    """Cavern writes one samplePos per block; the rest of the block inherits it."""
    meta = tmp_path / "m.atmos.metadata"
    meta.write_text(
        "sampleRate: 48000\nevents:\n"
        "  - ID: 10\n    samplePos: 0\n    pos: [0, 0, 0]\n"
        "  - ID: 11\n    pos: [1, 0, 0]\n"
        "  - ID: 10\n    samplePos: 9600\n    pos: [0.5, 0.5, 0]\n"
        "  - ID: 11\n    pos: [-1, 0, 0]\n"
        "  - ID: 10\n    gain: -3\n")
    p = atmos.position_events(meta, [10, 11])
    assert p[10].times.tolist() == [0, 9600] and p[11].times.tolist() == [0, 9600]
    assert p[11].pos.tolist() == [[1, 0, 0], [-1, 0, 0]]


def test_position_events_stay_fast_for_a_film(tmp_path):
    meta = tmp_path / "m.atmos.metadata"
    meta.write_text("events:\n" + "".join(
        f"  - {{ID: {10 + k % 16}, samplePos: {k * 384}, pos: [0, 0, 0]}}\n"
        for k in range(40_000)))
    started = time.perf_counter()
    atmos.position_events(meta, list(range(10, 26)))
    assert time.perf_counter() - started < 5.0


def test_alignment_reads_a_quiet_opening_a_block_at_a_time():
    """The encode's leading frames are found after minutes of silence, without holding
    those minutes in memory."""
    block, quiet_blocks, lead = 1 << 16, 30, 1000
    rng = np.random.default_rng(3)
    loud = 0.2 * rng.standard_normal((FS * 25, 2))
    programme = np.concatenate([np.zeros((quiet_blocks * block, 2)), loud])
    read_sizes: list[int] = []

    class Stream:
        ch = 2

        def __init__(self, x):
            self.x, self.pos = x, 0

        def read(self, n):
            n = int(n)
            read_sizes.append(n)
            out = self.x[self.pos: self.pos + n]
            self.pos += len(out)
            return out

        def close(self):
            pass

    class Damf:
        def __init__(self, x):
            self.x = x

        def open(self):
            return Stream(self.x)

    encoded = np.concatenate([np.zeros((lead, 2)), programme])
    streams = atmos._Streams(Damf(programme), Damf(encoded), IDENTITY, 1.0, block)
    assert atmos._find_offset(streams, FS) == lead
    assert max(read_sizes) <= block


def _energies(cross, source, encoded, out_peak=None):
    source, encoded = np.array(source, float), np.array(encoded, float)
    out_peak = np.zeros(len(encoded)) if out_peak is None else np.array(out_peak, float)
    return atmos.Energies(FS, np.array(cross, float), source, encoded, np.ones(len(source)),
                          out_peak)


LAYOUT = atmos.Layout([("LFE", 3)], [10, 11])


def test_map_elements_gives_each_object_its_best_element():
    # encoded objects (columns 1, 2) carry source objects 2 and 1
    energies = _energies([[4, 0, 0], [0, 0, 4], [0, 4, 0]], [4, 4, 4], [4, 4, 4])
    result = atmos.map_elements(energies, LAYOUT, 0.9)
    assert result.mapping == {0: 0, 1: 2, 2: 1} and result.min_corr == 1 and not result.problem


def test_map_elements_stops_at_the_first_object_no_element_carries():
    energies = _energies([[4, 0, 0], [0, 1, 0], [0, 0, 4]], [4, 4, 4], [4, 4, 4])
    result = atmos.map_elements(energies, LAYOUT, 0.9)
    assert "object 1 (ID 10) is not carried" in result.problem and result.min_corr == 0.25
    assert 0 in result.mapping and 1 not in result.mapping


def test_map_elements_refuses_audio_that_belongs_to_nothing():
    energies = _energies([[4, 0], [0, 4], [0, 0]], [4, 4], [4, 4, 4], [0.5, 0.5, 3 * LSB])
    result = atmos.map_elements(energies, atmos.Layout([("LFE", 3)], [10]), 0.9)
    assert "element 3 carries audio that belongs to no source element" in result.problem
