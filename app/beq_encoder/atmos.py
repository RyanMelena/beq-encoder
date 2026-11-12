"""TrueHD Atmos as truehdd decodes it (a DAMF), and the check of a TrueHD Atmos encode.

A DAMF is three files: `<base>.atmos` (YAML: the bed channels and objects, in the
order of the essence's channels), `<base>.atmos.audio` (CAF: one channel per bed
channel and object, 24-bit 48 kHz) and `<base>.atmos.metadata` (YAML: each
element's events: position, size, gain, ... at a sample position).

THE CHECK (see README, "How the TrueHD is made")
------------------------------------------------
DEE 6.7's TrueHD encoder under dee_shim (`--as-is on --oamd-as-is on`) codes every
element of the master as it is: no re-clustering, no level change, no added frames
(measured on Edge of Tomorrow and on a Cavern decode of Klaus: every element 0 LSB
off). Encoded element IDs and order are a permutation of the source's. So the encode
is checked like every other route, sample by sample, and fails loudly otherwise:

* the same bed, at most 16 elements, the full length (frames the encoder put in
  front of or after the programme must be digital silence)
* every source element with audio carried by one encoded element (correlation over
  the whole programme >= the route's minimum), and every sample of it within
  TOLERANCE_LSB of sosfilt(source) x gain: the rounding of the master DEE is given,
  which is not dithered and which DEE codes as it is; every other encoded element
  digitally silent
* the object metadata, frame by frame: DEE codes each position update at the start
  of its 1536-sample frame (frames counted from the programme's first sample; the
  last update inside a frame wins), and sends none while an object is digitally
  silent. So in every frame in which an encoded object has audio, its position
  mid-frame must equal the source position in effect at the frame's last sample
  (a track's first event is its initial state), within POSITION_TOLERANCE.

Both are judged in one pass over the programme where the encode allows it (`_Guess`):
the element carrying each source element is settled over the whole programme, so it is
guessed as each element starts and the samples compared under the guess; if the guess
does not hold, the samples are compared again in a second pass.

Reported: each element's largest error in LSB, the object-frames whose position was
checked, and the largest lead DEE gave an update by moving it to its frame's start.
"""

from __future__ import annotations

import shutil
import struct
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from .biquad import sosfilt_channels
from .caf import CafStream, read_info
from .workers import over_rows

try:  # the C loader is ~10x faster on a film's metadata
    _Loader = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    _Loader = yaml.SafeLoader

LSB24 = 1.0 / 2**23
# The master is the filtered source rounded to 24 bits, without dither: DEE 6.7 under
# dee_shim (with the quiet-weight patch, see the Dockerfile) codes every sample of it as
# it is, so the only error left is that rounding
TOLERANCE_LSB = 0.5 + 1e-6
FS = 48000
MAX_ELEMENTS = 16
MIN_OBJECT_CORR = 0.99      # each source element vs the encoded element carrying it
# Below this peak an element's expected signal rounds to digital silence, so no
# element is asked to carry it
ACTIVE_PEAK = 0.5 * LSB24
FRAME = 1536                # DEE codes object positions once per frame of this many samples
POSITION_TOLERANCE = 1 / 62  # room units: half a position step of the coded metadata
METHOD = "sample-exact"


@dataclass(slots=True)
class Layout:
    """The elements of a DAMF, in essence (CAF channel) order."""
    bed: list[tuple[str, int]]          # (speaker, ID) of each bed channel
    objects: list[int]                  # object IDs

    @property
    def ids(self) -> list[int]:
        return [i for _, i in self.bed] + list(self.objects)

    @property
    def channels(self) -> int:
        return len(self.bed) + len(self.objects)

    def name(self, c: int) -> str:
        nb = len(self.bed)
        return self.bed[c][0] if c < nb else f"object {c - nb + 1} (ID {self.objects[c - nb]})"

    def describe(self) -> str:
        bed = ", ".join(n for n, _ in self.bed) or "none"
        return f"bed [{bed}] + {len(self.objects)} objects"


def files(base: Path | str) -> tuple[Path, Path, Path]:
    b = str(base)
    return Path(f"{b}.atmos"), Path(f"{b}.atmos.audio"), Path(f"{b}.atmos.metadata")


def read_layout(atmos: Path | str) -> Layout:
    doc = yaml.load(Path(atmos).read_text(encoding="utf-8"), Loader=_Loader) or {}
    pres = (doc.get("presentations") or [{}])[0]
    bed = [(str(ch.get("channel")), int(ch.get("ID")))
           for inst in pres.get("bedInstances") or [] for ch in inst.get("channels") or []]
    return Layout(bed, [int(o.get("ID")) for o in pres.get("objects") or []])


@dataclass(frozen=True, slots=True)
class Positions:
    """One element's position events, in time order (file order at a tie)."""
    times: np.ndarray      # the sample of each event
    pos: np.ndarray        # (events, 3)

    def at(self, t: np.ndarray) -> np.ndarray:
        """The position in effect at each sample of `t` (before the first event, that
        event's: a track's first event is its initial state)."""
        k = np.searchsorted(self.times, t, side="right") - 1
        return self.pos[np.maximum(k, 0)]


def position_events(metadata: Path | str, ids: list[int]) -> dict[int, Positions]:
    """The position events of each element in `ids` that has any.

    An event without `samplePos` happens at the previous event's time: Cavern (like
    Dolby's own writers) lists one `samplePos` per block of simultaneous updates."""
    doc = yaml.load(Path(metadata).read_text(encoding="utf-8"), Loader=_Loader) or {}
    timed: dict[int, list[tuple[int, list]]] = {i: [] for i in ids}
    t = 0
    for ev in doc.get("events") or []:
        t = int(ev.get("samplePos", t))
        i = int(ev.get("ID", -1))
        if i in timed and ev.get("pos") is not None:
            timed[i].append((t, ev["pos"]))
    out = {}
    for i, evs in timed.items():
        if evs:
            evs.sort(key=lambda e: e[0])  # stable: file order at a tie
            out[i] = Positions(np.array([e[0] for e in evs], dtype=np.int64),
                               np.array([e[1] for e in evs], dtype=float).reshape(-1, 3))
    return out


# ------------------------------------------------------------------ the check

@dataclass(slots=True)
class AtmosCheck:
    """The check of a TrueHD Atmos encode against the BEQ-filtered source."""
    ok: bool
    method: str = METHOD
    frames: int = 0
    elements: int = 0                  # source elements
    objects: int = 0                   # source objects with audio
    out_elements: int = 0
    bed: str = ""
    mapping: dict[int, int] = field(default_factory=dict)    # source element -> encoded
    offset_frames: int = 0
    min_object_corr: float = 1.0
    max_err_lsb: float = 0.0
    element_err_lsb: dict[str, float] = field(default_factory=dict)
    object_frames: int = 0             # (object, frame) pairs whose position was checked
    max_update_lead: int = 0           # samples DEE moved an update earlier, at most
    problem: str = ""
    min_corr_limit: float = MIN_OBJECT_CORR
    decoder: dict = field(default_factory=dict)                    # e.g. Cavern's alignment

    def to_dict(self) -> dict:
        return {"ok": self.ok, "method": self.method, "frames": self.frames,
                "elements": self.elements, "objects": self.objects,
                "out_elements": self.out_elements, "bed": self.bed,
                "mapping": {str(k): v for k, v in self.mapping.items()},
                "offset_frames": self.offset_frames,
                "min_object_corr": round(self.min_object_corr, 5),
                "max_err_lsb": round(self.max_err_lsb, 3),
                "element_err_lsb": {k: round(v, 3) for k, v in self.element_err_lsb.items()},
                "object_frames": self.object_frames, "max_update_lead": self.max_update_lead,
                "problem": self.problem, "decoder": self.decoder,
                "limits": {"min_object_corr": self.min_corr_limit,
                           "max_err_lsb": round(TOLERANCE_LSB, 3),
                           "position_tolerance": round(POSITION_TOLERANCE, 5),
                           "max_elements": MAX_ELEMENTS}}


def _expected(src, sos, gain, block):
    """sosfilt(source) x gain, block by block (float64)."""
    zi = np.zeros((sos.shape[0], 2, src.ch))
    while True:
        x = src.read(block)
        if len(x) == 0:
            return
        y, zi = sosfilt_channels(sos, x, zi)
        yield y * gain


@dataclass(frozen=True, slots=True)
class _Damf:
    """A decoded programme on disk (`what` names it in errors)."""
    layout: Layout
    audio: Path
    metadata: Path
    what: str

    @classmethod
    def at(cls, base: Path | str, what: str) -> _Damf:
        atmos_file, audio, metadata = files(base)
        return cls(read_layout(atmos_file), audio, metadata, what)

    def open(self) -> CafStream:
        s = CafStream(str(self.audio), self.what)
        if s.ch != self.layout.channels:
            s.close()
            raise ValueError(f"{self.what} has {s.ch} channels, its .atmos lists "
                             f"{self.layout.channels} elements")
        return s


@dataclass(frozen=True, slots=True)
class _Streams:
    """The encode, and the source BEQ-filtered as it should have come out."""
    source: _Damf
    encoded: _Damf
    sos: np.ndarray
    gain: float
    block: int

    @contextmanager
    def reading(self, skip: int):
        """(the expected signal block by block, the encode past `skip` leading frames,
        the source's length in frames)."""
        src, out = self.source.open(), self.encoded.open()
        try:
            out.read(skip)
            yield _expected(src, self.sos, self.gain, self.block), out, src.info.frames
        finally:
            src.close()
            out.close()


def _layout_problem(la: Layout, lb: Layout) -> str:
    """Same bed, and no more elements than TrueHD Atmos carries."""
    if [n for n, _ in la.bed] != [n for n, _ in lb.bed]:
        return f"bed channels differ: source {la.describe()}, encoded {lb.describe()}"
    if lb.channels > MAX_ELEMENTS:
        return f"the encoded track has {lb.channels} elements (TrueHD Atmos carries 16)"
    return ""


def _summed(stream, first: int, stop: int, block: int = 1 << 16) -> np.ndarray:
    """The sum of all elements over frames [first, stop), read a block at a time so that
    memory follows the length of the window, not how far into the programme it is."""
    kept: list[np.ndarray] = []
    pos = 0
    while pos < stop:
        x = stream.read(min(block, stop - pos))
        if len(x) == 0:
            break
        if pos + len(x) > first:
            kept.append(x[max(0, first - pos):].sum(axis=1))
        pos += len(x)
    return np.concatenate(kept) if kept else np.zeros(0)


def _find_offset(streams: _Streams, max_offset: int) -> int | None:
    """Leading frames the encoder added: the lag that best aligns the sums of all
    elements around the first loud passage (None: they don't line up)."""
    need = FS * 20
    src = streams.source.open()
    try:
        parts: list[np.ndarray] = []
        base = 0  # the frame parts[0] starts at
        start = None
        pos = 0
        for y in _expected(src, streams.sos, streams.gain, 1 << 16):
            s = y.sum(axis=1)
            if start is None:
                loud = np.nonzero(np.abs(s) > 1e-3)[0]
                if len(loud):
                    start = pos + int(loud[0])
                else:  # still quiet: only this block can hold the passage's lead-in
                    parts, base = [], pos
            parts.append(s)
            pos += len(y)
            if start is not None and pos >= start + need + max_offset:
                break
    finally:
        src.close()
    if start is None:
        return 0  # a silent programme: nothing to align
    from scipy.signal import correlate
    lo = max(0, start - 4800)
    seg = np.concatenate(parts)[lo - base: lo - base + need]
    k0 = min(max_offset, lo)
    out = streams.encoded.open()
    try:
        win = _summed(out, lo - k0, lo + len(seg) + max_offset)
    finally:
        out.close()
    if len(win) < len(seg):
        return None
    xc = correlate(win, seg, mode="valid", method="fft")  # xc[i]: lag i - k0
    at = int(np.argmax(xc)) - k0
    o = win[at + k0: at + k0 + len(seg)]
    den = float(np.sqrt((o @ o) * (seg @ seg)))
    best = float(o @ seg) / den if den else 0.0
    return at if best >= 0.95 else None  # alignment only: the checks that follow judge


def _align(streams: _Streams, max_offset: int) -> tuple[int, str]:
    """(the encode's leading frames, why they make it unacceptable)."""
    try:
        offset = _find_offset(streams, max_offset)
    except ValueError as exc:
        return 0, str(exc)
    if offset is None:
        return 0, "the encoded track does not line up with the filtered source"
    if offset < 0:
        return 0, f"the encoded track starts {-offset} samples early: source audio missing"
    return offset, ""


def _lead_problem(encoded: _Damf, offset: int) -> str:
    """The frames the encoder put before the programme must be digital silence:
    anything there is audio that is in no rendering of the source."""
    if not offset:
        return ""
    out = encoded.open()
    try:
        lead = out.read(offset)
    finally:
        out.close()
    peak = float(np.abs(lead).max(initial=0.0))
    if peak:
        return (f"the encoded track starts with {offset} frames that are not in the "
                f"source and not silent (peak {20 * np.log10(peak):.1f} dBFS)")
    return ""


@dataclass(frozen=True, slots=True)
class Energies:
    """The encode against the expected signal, element by element, over the
    whole programme."""
    frames: int
    cross: np.ndarray     # (encoded, source): inner products
    source: np.ndarray    # each source element's energy
    encoded: np.ndarray   # each encoded element's energy
    peak: np.ndarray      # each source element's expected peak
    out_peak: np.ndarray  # each encoded element's peak

    @property
    def active(self) -> list[int]:
        """The source elements that carry audio."""
        return [c for c in range(len(self.peak)) if self.peak[c] > ACTIVE_PEAK]

    def objects(self, nb: int) -> list[int]:
        return [c for c in self.active if c >= nb]


def _column_sums(fn, x: np.ndarray, width: int) -> np.ndarray:
    """`fn(first, stop)` gives one row of `width` per part of `x`'s rows: their sum."""
    return np.sum(over_rows(fn, len(x)), axis=0) if len(x) else np.zeros(width)


def _column_peaks(x: np.ndarray) -> np.ndarray:
    """The largest magnitude in each column of `x`."""
    if not len(x):
        return np.zeros(x.shape[1])
    return np.max(over_rows(lambda a, b: np.abs(x[a:b]).max(axis=0), len(x)), axis=0)


def _correlate(streams: _Streams, offset: int, max_offset: int,
               tick) -> tuple[Energies | None, _Guess | None, str]:
    """(the energies, the samples as compared along the way, why the encode's length
    is wrong)."""
    nc, nk = streams.source.layout.channels, streams.encoded.layout.channels
    cross, e_src, e_out = np.zeros((nk, nc)), np.zeros(nc), np.zeros(nk)
    peak, out_peak = np.zeros(nc), np.zeros(nk)
    total = 0
    with streams.reading(offset) as (expected, out, frames):
        guess = _Guess.over(frames, nc, nk, len(streams.source.layout.bed))
        for y in expected:
            b = out.read(len(y))
            if len(b) < len(y):
                return None, None, (f"the encoded track ends early: {total + len(b)} of "
                                    "the source's frames")
            cross += b.T @ y
            e_src += _column_sums(lambda i, j: (y[i:j] * y[i:j]).sum(axis=0), y, nc)
            e_out += _column_sums(lambda i, j: (b[i:j] * b[i:j]).sum(axis=0), b, nk)
            block_peak, block_out_peak = _column_peaks(y), _column_peaks(b)
            guess.block(y, b, total, block_peak, peak, out_peak)
            peak = np.maximum(peak, block_peak)
            out_peak = np.maximum(out_peak, block_out_peak)
            total += len(y)
            if tick:  # the samples still have to be compared if the guess has failed
                tick(total if guess.holds else total / 2)
        tail = out.read(max_offset + 1)
    if len(tail) > max_offset or np.abs(tail).max(initial=0.0):
        return None, None, (f"the encoded track has {len(tail)} unexpected frames after "
                            "the source ends")
    return Energies(total, cross, e_src, e_out, peak, out_peak), guess, ""


@dataclass(frozen=True, slots=True)
class ElementMap:
    """Which encoded element carries each source element with audio."""
    mapping: dict[int, int]
    min_corr: float = 1.0
    problem: str = ""


def map_elements(energies: Energies, layout: Layout, min_object_corr: float) -> ElementMap:
    """Loudest first: a bed channel stays that bed channel; an object goes to one unused
    element. Encoded elements left over must be silent within the tolerance."""
    nb, nk = len(layout.bed), len(energies.encoded)
    mapping: dict[int, int] = {}
    taken: set[int] = set()
    min_corr = 1.0
    for c in sorted(energies.active, key=lambda c: -energies.source[c]):
        cands = [c] if c < nb else [k for k in range(nb, nk) if k not in taken]
        best, bk = -1.0, None
        for k in cands:
            den = float(np.sqrt(energies.source[c] * energies.encoded[k]))
            r = float(energies.cross[k, c]) / den if den else 0.0
            if r > best:
                best, bk = r, k
        min_corr = min(min_corr, best)
        if bk is None or best < min_object_corr:
            return ElementMap(mapping, min_corr, (
                f"{layout.name(c)} is not carried by any one element of the encoded "
                f"track (best correlation {best:.4f} < {min_object_corr})"))
        mapping[c] = bk
        taken.add(bk)
    for k in range(nk):
        lsb = energies.out_peak[k] / LSB24
        if k not in taken and lsb > TOLERANCE_LSB:
            return ElementMap(mapping, min_corr, (
                f"encoded element {k + 1} carries audio that belongs to no source element "
                f"(peak {lsb:.0f} LSB)"))
    return ElementMap(mapping, min_corr)


@dataclass(frozen=True, slots=True)
class _Samples:
    """Every sample of the mapped elements against the expected signal."""
    idx: np.ndarray            # the mapped source elements
    err_lsb: np.ndarray        # the largest error of each, so far
    audible: np.ndarray        # (frames of FRAME samples, mapped elements): any audio
    problem: str = ""


_TOLERANCE = TOLERANCE_LSB * LSB24


def _errors(y: np.ndarray, b: np.ndarray, idx: np.ndarray, ks: np.ndarray) -> np.ndarray:
    """|expected - encoded| of source elements `idx` as carried by encoded elements `ks`,
    for a block of each: (frames, len(idx))."""
    d = np.empty((len(y), len(idx)))

    def part(i: int, j: int) -> None:
        np.clip(y[i:j, idx], -1.0, 1.0 - LSB24, out=d[i:j])
        d[i:j] -= b[i:j, ks]
        np.abs(d[i:j], out=d[i:j])

    over_rows(part, len(y))
    return d


def _mark_audible(audible: np.ndarray, cols: np.ndarray, b: np.ndarray, ks: np.ndarray,
                  done: int) -> None:
    """Set `audible[frame, cols]` for each frame of FRAME samples in which the encoded
    elements `ks` of block `b` (which starts at sample `done`) are not digitally silent."""
    ids = (done + np.arange(len(b))) // FRAME
    starts = np.flatnonzero(np.diff(ids, prepend=-1))
    audible[ids[starts][:, None], cols] |= np.logical_or.reduceat(b[:, ks] != 0, starts, axis=0)


@dataclass(slots=True)
class _Guess:
    """The samples compared during the pass that measures the energies, before the
    mapping that pass ends with is known.

    A source element is given the encoded element that carries it when its expected
    signal first leaves digital silence: the first free one (its own, for a bed channel)
    that matches it within the tolerance over that block and has been digitally silent
    until then, so that every earlier sample of the pair is accounted for (the error
    there is the expected signal's own magnitude). From then on the pair is compared
    block by block as `_compare` does. Anything that does not fit ends the guess:
    `samples` then gives None, and `_compare` judges (and words the failure) in a
    second pass. So what is accepted here is exactly what `_compare` would find."""
    nb: int                    # bed channels
    nk: int                    # encoded elements
    err: np.ndarray            # the largest error of each source element, so far
    audible: np.ndarray        # (frames of FRAME samples, source elements): any audio
    to: dict[int, int] = field(default_factory=dict)   # source element -> encoded
    holds: bool = True

    @classmethod
    def over(cls, frames: int, nc: int, nk: int, nb: int) -> _Guess:
        return cls(nb, nk, np.zeros(nc), np.zeros((-(-frames // FRAME), nc), dtype=bool))

    def _carrier(self, c: int, y: np.ndarray, b: np.ndarray, out_peak: np.ndarray) -> int | None:
        """The encoded element that carries source element `c` in this block."""
        taken = set(self.to.values())
        cands = [c] if c < self.nb else [k for k in range(self.nb, self.nk) if k not in taken]
        want = np.clip(y[:, c], -1.0, 1.0 - LSB24)
        for k in cands:
            if k < self.nk and out_peak[k] == 0 and \
                    np.abs(want - b[:, k]).max(initial=0.0) <= _TOLERANCE:
                return k
        return None

    def block(self, y: np.ndarray, b: np.ndarray, done: int, block_peak: np.ndarray,
              peak: np.ndarray, out_peak: np.ndarray) -> None:
        """Take in the block that starts at sample `done`. `block_peak` is each expected
        element's peak in it; `peak` and `out_peak` are the expected and the encoded
        elements' peaks over everything before it."""
        if not self.holds:
            return
        for c in np.flatnonzero(block_peak > _TOLERANCE):
            c = int(c)
            if c in self.to:
                continue
            k = self._carrier(c, y, b, out_peak)
            # every earlier block left it below the tolerance, unless one held a NaN
            if k is None or not peak[c] <= _TOLERANCE:
                self.holds = False
                return
            self.to[c] = k
            self.err[c] = peak[c]
        if not self.to:
            return
        idx = np.array(sorted(self.to), dtype=int)
        ks = np.array([self.to[c] for c in idx], dtype=int)
        d = _errors(y, b, idx, ks)
        worst = d.max(axis=0, initial=0.0)
        if not (worst <= _TOLERANCE).all():  # a NaN compares false: never a pass
            self.holds = False
            return
        self.err[idx] = np.maximum(self.err[idx], worst)
        _mark_audible(self.audible, idx, b, ks, done)

    def samples(self, emap: ElementMap) -> _Samples | None:
        """What `_compare` would give for `emap`, if the guess held and is that mapping."""
        if not self.holds or self.to != emap.mapping:
            return None
        idx = np.array(sorted(self.to), dtype=int)
        return _Samples(idx, self.err[idx] / LSB24, self.audible[:, idx])


def _compare(streams: _Streams, energies: Energies, offset: int, emap: ElementMap,
             tick) -> _Samples:
    """A pass of its own over the programme: every sample of the mapped elements."""
    idx = np.array(sorted(emap.mapping), dtype=int)
    ks = np.array([emap.mapping[c] for c in idx], dtype=int)
    cols = np.arange(len(idx))
    err = np.zeros(len(idx))
    audible = np.zeros((-(-energies.frames // FRAME), len(idx)), dtype=bool)
    done = 0
    with streams.reading(offset) as (expected, out, _frames):
        for y in expected:
            b = out.read(len(y))
            d = _errors(y, b, idx, ks)
            err = np.maximum(err, d.max(axis=0, initial=0.0))
            bad = ~(d <= _TOLERANCE)  # a NaN compares false against everything: never a pass
            if bad.any():
                at, j = np.argwhere(bad)[0]
                name = streams.source.layout.name(int(idx[j]))
                return _Samples(idx, err / LSB24, audible, (
                    f"sample {done + int(at)} of {name} differs from the filtered source "
                    f"by {d[at, j] / LSB24:.1f} LSB"))
            _mark_audible(audible, cols, b, ks, done)
            done += len(y)
            if tick:
                tick(energies.frames / 2 + done / 2)
    return _Samples(idx, err / LSB24, audible)


@dataclass(frozen=True, slots=True)
class _Gate:
    """The object metadata against the source's, frame by frame."""
    checked: int = 0
    max_lead: int = 0
    problem: str = ""


def _positions(source: _Damf, encoded: _Damf, mapping: dict[int, int], offset: int,
               samples: _Samples) -> _Gate:
    """In every frame in which an encoded object has audio, its position mid-frame must
    be the source's in effect at the frame's last sample (module docstring)."""
    la, lb = source.layout, encoded.layout
    src = position_events(source.metadata, la.ids)
    enc = position_events(encoded.metadata, lb.ids)
    checked = max_lead = 0
    for j, c in enumerate(int(c) for c in samples.idx):
        frames = np.flatnonzero(samples.audible[:, j])
        if c < len(la.bed) or not len(frames) or la.ids[c] not in src:
            continue  # a bed channel, a silent object, or one the source never positions
        s, e = src[la.ids[c]], enc.get(lb.ids[mapping[c]])
        if e is None:
            return _Gate(checked, max_lead,
                         f"{la.name(c)} has audio but the encode never positions it")
        starts = frames * FRAME
        want = s.at(starts + FRAME - 1)
        got = e.at(starts + FRAME // 2 + offset)
        off = np.abs(want - got).max(axis=1) > POSITION_TOLERANCE
        checked += len(frames)
        if off.any():
            f = int(np.flatnonzero(off)[0])
            return _Gate(checked, max_lead, (
                f"{la.name(c)} is placed at {_rounded(got[f])} at {starts[f] / FS:.3f} s, "
                f"where the source has it at {_rounded(want[f])}: the position metadata "
                "does not match"))
        # each encoded update against the source's last update inside its frame
        t = e.times - offset
        last = np.searchsorted(s.times, t + FRAME, side="left") - 1
        found = last >= 0
        last = np.maximum(last, 0)
        same = found & (s.times[last] >= t) & (
            np.abs(s.pos[last] - e.pos).max(axis=1) <= POSITION_TOLERANCE)
        if same.any():
            max_lead = max(max_lead, int((s.times[last] - t)[same].max()))
    return _Gate(checked, max_lead)


def _rounded(p: np.ndarray) -> list[float]:
    return [round(float(v), 3) for v in p]


def verify(src_base: Path | str, out_base: Path | str, sos: np.ndarray, gain: float,
           max_offset: int = 4800, block: int = 1 << 17, tick=None,
           min_object_corr: float = MIN_OBJECT_CORR, method: str = METHOD) -> AtmosCheck:
    """Check a TrueHD Atmos encode (decoded back to `out_base`) against the source's
    decode kept from before the filter (`src_base`, BEQ-filtered here with `sos` and
    `gain`): see the module docstring. A failure's `problem` says at which step it
    stopped; the fields measured up to there are filled in."""
    source, encoded = _Damf.at(src_base, "source DAMF"), _Damf.at(out_base, "encoded DAMF")
    res = AtmosCheck(ok=False, elements=source.layout.channels,
                     out_elements=encoded.layout.channels,
                     bed=", ".join(n for n, _ in source.layout.bed), method=method,
                     min_corr_limit=min_object_corr)
    res.problem = _layout_problem(source.layout, encoded.layout)
    if res.problem:
        return res
    streams = _Streams(source, encoded, sos, gain, block)
    res.offset_frames, res.problem = _align(streams, max_offset)
    if res.problem:
        return res
    res.problem = _lead_problem(encoded, res.offset_frames)
    if res.problem:
        return res
    energies, guess, res.problem = _correlate(streams, res.offset_frames, max_offset, tick)
    if res.problem:
        return res
    nb = len(source.layout.bed)
    res.frames, res.objects = energies.frames, len(energies.objects(nb))
    emap = map_elements(energies, source.layout, min_object_corr)
    res.mapping, res.min_object_corr, res.problem = emap.mapping, emap.min_corr, emap.problem
    if res.problem:
        return res
    samples = guess.samples(emap) or _compare(streams, energies, res.offset_frames, emap, tick)
    res.element_err_lsb = {source.layout.name(int(c)): float(e)
                           for c, e in zip(samples.idx, samples.err_lsb, strict=True)}
    res.max_err_lsb = float(samples.err_lsb.max(initial=0.0))
    res.problem = samples.problem
    if res.problem:
        return res
    gate = _positions(source, encoded, emap.mapping, res.offset_frames, samples)
    res.object_frames, res.max_update_lead = gate.checked, gate.max_lead
    res.problem = gate.problem
    res.ok = not res.problem
    return res


# ------------------------------------------------------------------ DD+ Atmos (Cavern)

# Cavern (cavern-dump) decodes DD+ Atmos into a DAMF whose bed (the LFE) is on the
# timeline of an ordinary E-AC-3 decode (ffmpeg's: verified, correlation 1.0) and
# whose objects lag it by the JOC (QMF) filter bank's delay, 577 samples (measured
# against Dolby's own decode: objects 833, LFE 256 samples behind it; ffmpeg's decode
# is 256 behind). cavern-dump renders CAVERN_FLUSH samples past the end of the stream
# so that the objects' last samples come out of the delay lines; align_joc trims the
# lead and keeps exactly the stream's length.
JOC_OBJECT_LAG = 577
CAVERN_FLUSH = 1536
JOC_MIN_OBJECT_CORR = 0.98   # MIN_OBJECT_CORR for a Cavern decode's elements
MIN_ALIGN_CORR = 0.99
MAX_JOC_LFE_GAIN_DB = 0.1    # the LFE is bit-identical to the plain decode (measured);
#                              correlation alone would accept a level change


def align_joc(base: Path | str, frames: int, object_lag: int = JOC_OBJECT_LAG,
              block: int = 1 << 16) -> None:
    """In place: move every object `object_lag` samples earlier (the bed stays), keep
    `frames` samples, and shift the objects' metadata events by the same amount."""
    damf, audio, meta = files(base)
    lay = read_layout(damf)
    nb = len(lay.bed)
    info = read_info(str(audio))
    if info.channels != lay.channels:
        raise ValueError(f"the essence has {info.channels} channels, the .atmos lists "
                         f"{lay.channels}")
    if info.frames < frames + object_lag:
        raise ValueError(f"the decoder wrote {info.frames} samples; {frames + object_lag} "
                         "are needed to keep the objects' last samples")
    fb, bps, ch = info.frame_bytes, info.bytes_per_sample, info.channels
    with open(audio, "r+b") as fh:
        pos = 0
        while pos < frames:
            n = min(block, frames - pos)
            fh.seek(info.data_offset + pos * fb)
            bed = np.frombuffer(fh.read(n * fb), np.uint8).reshape(n, ch, bps)
            fh.seek(info.data_offset + (pos + object_lag) * fb)
            out = np.frombuffer(fh.read(n * fb), np.uint8).reshape(n, ch, bps).copy()
            out[:, :nb] = bed[:, :nb]
            fh.seek(info.data_offset + pos * fb)
            fh.write(out.tobytes())
            pos += n
        fh.truncate(info.data_offset + frames * fb)
        fh.seek(info.data_offset - 12)  # the data chunk's size: edit count + samples
        fh.write(struct.pack(">q", frames * fb + 4))
    beds = {i for _, i in lay.bed}
    # Only explicit samplePos lines move; events without one inherit the shifted time
    # of the block they sit in. Cavern leads every timed block with an object event.
    lines = Path(meta).read_text(encoding="utf-8").splitlines(keepends=True)
    cur = None
    for k, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("- ID:"):
            cur = int(s.split(":", 1)[1])
        elif s.startswith("samplePos:") and cur is not None:
            v = int(s.split(":", 1)[1])
            v = min(frames - 1, max(0, v - (0 if cur in beds else object_lag)))
            lines[k] = ln[: len(ln) - len(ln.lstrip())] + f"samplePos: {v}\n"
    Path(meta).write_text("".join(lines), encoding="utf-8")


def joc_alignment(base: Path | str, core) -> dict:
    """After align_joc: the bed must equal the plain E-AC-3 decode's LFE (`core`: an
    ffmpeg 5.1 decode stream, L R C LFE Ls Rs, `read(n)`), and the objects summed must
    track the core's mains summed, both at lag 0. Returns correlations and lengths."""
    lay = read_layout(files(base)[0])
    nb = len(lay.bed)
    lfe_i = next((i for i, (n, _) in enumerate(lay.bed) if n == "LFE"), None)
    src = CafStream(str(files(base)[1]), "Cavern DAMF")
    acc = np.zeros(6)  # lfe: xy xx yy; objects: xy xx yy
    n_damf = n_core = 0
    try:
        while True:
            x = src.read(1 << 17)
            y = core.read(len(x)) if len(x) else core.read(1 << 17)
            n_damf += len(x)
            n_core += len(y)
            if len(x) == 0 and len(y) == 0:
                break
            m = min(len(x), len(y))
            if m == 0:
                continue
            x, y = x[:m], y[:m]
            if lfe_i is not None and y.shape[1] >= 4:
                a, b = x[:, lfe_i], y[:, 3]
                acc[:3] += [a @ b, a @ a, b @ b]
            mains = [c for c in range(y.shape[1]) if c != 3]
            a, b = x[:, nb:].sum(axis=1), y[:, mains].sum(axis=1)
            acc[3:] += [a @ b, a @ a, b @ b]
    finally:
        src.close()

    def corr(xy, xx, yy):
        return float(xy / np.sqrt(xx * yy)) if xx > 0 and yy > 0 else None

    def gain_db(xy, xx, yy):
        return float(20 * np.log10(xy / yy)) if xy > 0 and yy > 0 else None

    return {"lfe_corr": corr(*acc[:3]), "lfe_gain_db": gain_db(*acc[:3]),
            "objects_corr": corr(*acc[3:]), "objects_gain_db": gain_db(*acc[3:]),
            "damf_frames": n_damf, "core_frames": n_core}


def derive(src_base: Path | str, dst_base: Path | str) -> None:
    """A second master file set beside `src_base` that shares its .atmos and metadata
    (copied, the .atmos naming its own files); its audio is written by the caller."""
    sa, _, sm = files(src_base)
    da, dau, dm = files(dst_base)
    text = Path(sa).read_text(encoding="utf-8")
    text = text.replace(f"metadata: {Path(sm).name}", f"metadata: {Path(dm).name}")
    text = text.replace(f"audio: {Path(files(src_base)[1]).name}", f"audio: {Path(dau).name}")
    Path(da).write_text(text, encoding="utf-8")
    shutil.copyfile(sm, dm)
