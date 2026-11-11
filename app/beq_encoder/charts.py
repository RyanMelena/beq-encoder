"""Before/after charts for every BEQ track: a peak/average spectrum and a heat map.

Both are measured, never modelled: "before" is the source track, "after" is the
encoded BEQ track decoded back, so a chart shows what the file now holds. Each
track is reduced to one signal, the mono mix the designer works on (every main
channel × 10^(-20.2/20) + LFE × 10^(-10.2/20): the LFE 10 dB hotter, the sum
normalised so it cannot exceed full scale), decoded at 1 kHz in double precision.

Spectrum: Hann windows of 4096 samples (4.1 s, 0.24 Hz resolution, 2x zero-padded),
50 % overlap, over the whole programme. *Peak* is the loudest window in each bin; *average* is the
power mean. Levels are dBFS for a sine (a full-scale tone reads 0 dBFS).

Heat map: the same mix in 2048-sample windows (2 s, 0.49 Hz) every 0.5 s, each
column of the image the loudest of the windows it covers.

The BEQ track's uniform level gain (attenuation applied so the filtered peak fits
the target) is taken back out of the "after" curves before plotting, so above the
filters' range the two lines meet and what differs is the BEQ alone. The gain is
printed on both charts.
"""

from __future__ import annotations

import io
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import analysis

FS = analysis.FS
SPEC_NPERSEG = 4096
HEAT_NPERSEG = 2048
HEAT_HOP = 500
HEAT_COLUMNS = 1400
SPEC_RANGE_HZ = (1.0, 160.0)
HEAT_RANGE_HZ = (1.0, 60.0)
SMOOTH_OCT = 1 / 24
HEAT_DB = (-95.0, -25.0)

MONO_NOTE = ("mono mix of every channel: mains −20.2 dB, LFE −10.2 dB "
             "(LFE +10 dB relative, normalised to full scale)")


# ----------------------------------------------------------------------- measuring

@dataclass(slots=True)
class Measurement:
    freqs: np.ndarray            # spectrum bins (Hz)
    peak_db: np.ndarray          # loudest window per bin
    avg_db: np.ndarray           # power mean per bin
    heat_freqs: np.ndarray       # heat-map rows (Hz)
    heat_times: np.ndarray       # heat-map column centres (s)
    heat_db: np.ndarray          # (rows, columns)
    duration_s: float
    frames: int

    def shifted(self, db: float) -> Measurement:
        return Measurement(self.freqs, self.peak_db + db, self.avg_db + db, self.heat_freqs,
                           self.heat_times, self.heat_db + db, self.duration_s, self.frames)

    def to_json(self) -> dict:
        lo, hi = SPEC_RANGE_HZ
        sel = (self.freqs >= lo) & (self.freqs <= hi)
        return {"freqs_hz": np.round(self.freqs[sel], 4).tolist(),
                "peak_dbfs": np.round(self.peak_db[sel], 2).tolist(),
                "average_dbfs": np.round(self.avg_db[sel], 2).tolist()}


class _Frames:
    """Accumulates Hann-window spectra of one stream, chunk by chunk."""

    def __init__(self, nperseg: int, hop: int, fmax: float):
        self.n, self.hop = nperseg, hop
        self.win = np.hanning(nperseg + 2)[1:-1]  # symmetric-free, no zero end points
        self.scale = 2.0 / self.win.sum()          # sine amplitude
        self.nfft = 2 * nperseg                    # 2x zero padding: <0.5 dB scalloping
        k = int(math.floor(fmax * self.nfft / FS)) + 1
        self.k = k
        self.buf = np.zeros(0)
        self.rows: list[np.ndarray] = []

    def end_excerpt(self) -> None:
        """What comes next is unrelated audio: no window may span it and this."""
        self.buf = np.zeros(0)

    def feed(self, x: np.ndarray) -> None:
        self.buf = np.concatenate([self.buf, x])
        n, hop = self.n, self.hop
        count = (len(self.buf) - n) // hop + 1 if len(self.buf) >= n else 0
        if count <= 0:
            return
        idx = np.arange(n)[None, :] + hop * np.arange(count)[:, None]
        seg = self.buf[idx] * self.win
        amp = np.abs(np.fft.rfft(seg, n=self.nfft, axis=1)[:, : self.k]) * self.scale
        self.rows.append(amp.astype(np.float32))
        self.buf = self.buf[count * hop:]

    def matrix(self) -> np.ndarray:
        if not self.rows:
            return np.zeros((0, self.k), dtype=np.float32)
        return np.concatenate(self.rows)


def measure_stream(chunks, fmax: float = SPEC_RANGE_HZ[1] + 5,
                   excerpts: bool = False) -> Measurement:
    """Measure a mono 1 kHz signal arriving in chunks (any sizes). With `excerpts`,
    each chunk is a passage on its own and no window spans two of them."""
    spec = _Frames(SPEC_NPERSEG, SPEC_NPERSEG // 2, fmax)
    heat = _Frames(HEAT_NPERSEG, HEAT_HOP, HEAT_RANGE_HZ[1] + 1)
    frames = 0
    for x in chunks:
        x = np.asarray(x, dtype=np.float64)
        frames += len(x)
        spec.feed(x)
        heat.feed(x)
        if excerpts:
            spec.end_excerpt()
            heat.end_excerpt()
    s = spec.matrix().astype(np.float64)
    if len(s) == 0:
        raise ValueError("too little audio to measure (under 4 s)")
    freqs = np.arange(spec.k) * FS / spec.nfft
    peak = 20 * np.log10(np.maximum(s.max(axis=0), 1e-12))
    avg = 10 * np.log10(np.maximum((s * s).mean(axis=0), 1e-24))

    h = heat.matrix()
    hf = np.arange(heat.k) * FS / heat.nfft
    cols = max(1, min(HEAT_COLUMNS, len(h)))
    edges = np.linspace(0, len(h), cols + 1).astype(int)
    pooled = np.stack([h[a:max(b, a + 1)].max(axis=0) for a, b in zip(edges[:-1], edges[1:])]) \
        if len(h) else np.zeros((1, heat.k))
    centre = (edges[:-1] + edges[1:]) / 2 * HEAT_HOP / FS + HEAT_NPERSEG / FS / 2
    hdb = 20 * np.log10(np.maximum(pooled.T.astype(np.float64), 1e-12))
    return Measurement(freqs, peak, avg, hf, centre, hdb, frames / FS, frames)


def decode_mono(ffmpeg: str, path: str, audio_index: int, channels: int,
                names: tuple[str, ...], popen: Callable | None = None,
                on_frames: Callable[[int], None] | None = None):
    """Yield the 1 kHz mono mix of one audio stream, as analysis.py builds it."""
    gains = analysis.mono_gains(names)
    done = 0
    for x in analysis.decode_blocks(ffmpeg, path, audio_index, channels, popen=popen,
                                    what="track for the charts"):
        done += len(x)
        if on_frames:
            on_frames(done)
        yield x @ gains


# ----------------------------------------------------------------------- rendering

@dataclass(slots=True)
class ChartInfo:
    title: str                        # "Edge of Tomorrow (2014)"
    source_track: str                 # "TrueHD Atmos 7.1 · English · track 1"
    beq_track: str                    # "BEQ TrueHD Atmos (aron7awol)"
    beq_source: str                   # "BEQ Catalogue · aron7awol" / "beqforge 0.3.3 · flatten"
    filters: list[dict]               # catalogue-style filter dicts
    gain_db: float = 0.0              # uniform level gain baked into the BEQ track
    peak_in_dbfs: float | None = None
    peak_out_dbfs: float | None = None
    mv_db: float | None = None        # catalogue's master-volume advice, if any
    encoder: str = ""                 # "beq-encoder 0.1.0"
    date: str = ""
    facts: list[tuple[str, str]] = field(default_factory=list)  # extra side-panel facts


BG = "#0d1016"
PANEL = "#121620"
GRID = "#262c38"
AXIS = "#6b7384"
TEXT = "#e8ebf1"
MUTED = "#98a0b0"
FAINT = "#5d6474"
PEAK = "#ffb347"      # amber
AVG = "#4fd1c5"       # teal
FONT = ["Inter", "DejaVu Sans"]


def _figure():
    """A figure of its own, outside pyplot's registry, so jobs can render at the same
    time (MAX_CONCURRENT_JOBS). The rcParams they share only ever get these values."""
    import matplotlib
    from matplotlib import font_manager
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    have = {f.name for f in font_manager.fontManager.ttflist}
    family = [f for f in FONT if f in have] or ["DejaVu Sans"]
    matplotlib.rcParams.update({
        "font.family": family, "font.size": 10, "text.color": TEXT,
        "axes.facecolor": PANEL, "axes.edgecolor": GRID, "axes.labelcolor": MUTED,
        "axes.titlecolor": TEXT, "xtick.color": AXIS, "ytick.color": AXIS,
        "figure.facecolor": BG, "savefig.facecolor": BG, "axes.linewidth": 0.8,
        "xtick.major.size": 0, "ytick.major.size": 0, "xtick.major.pad": 6,
        "ytick.major.pad": 6, "legend.frameon": False,
    })
    fig = Figure(figsize=(12, 7.2))
    FigureCanvasAgg(fig)
    return fig


def _fmt_db(v: float | None, plus: bool = True) -> str:
    if v is None:
        return "—"
    v = 0.0 if abs(v) < 0.05 else v  # no "−0.0"
    t = f"{v:+.1f} dB" if plus else f"{v:.1f} dB"
    return t.replace("-", "\u2212")


def smooth(freqs: np.ndarray, db: np.ndarray, frac: float = SMOOTH_OCT,
           power: bool = True) -> np.ndarray:
    """Fractional-octave smoothing: a Hann window in log frequency, centred on each bin
    and `frac` octaves wide, averaged in power."""
    p = 10 ** (db / 10) if power else db
    out = np.empty_like(p)
    half = frac / 2
    for i, f in enumerate(freqs):
        if f <= 0:
            out[i] = p[i]
            continue
        lo, hi = np.searchsorted(freqs, [f * 2 ** -half, f * 2 ** half])
        w = np.cos(np.pi / 2 * np.log2(freqs[lo:hi] / f) / half) ** 2
        out[i] = (p[lo:hi] * w).sum() / w.sum()
    return 10 * np.log10(np.maximum(out, 1e-24)) if power else out


def _filter_rows(filters: list[dict]) -> list[tuple[str, str, str, str, str]]:
    """Group identical consecutive filters (catalogue lists stacked shelves one by one)."""
    names = {"LowShelf": "Low shelf", "HighShelf": "High shelf", "PeakingEQ": "Peaking"}
    groups: list[list] = []
    for f in filters:
        key = (f["type"], round(float(f["freq"]), 3), round(float(f["gain"]), 3),
               round(float(f["q"]), 4))
        n = int(f.get("count") or 1)
        if groups and groups[-1][0] == key:
            groups[-1][1] += n
        else:
            groups.append([key, n])
    rows = []
    for (t, fr, g, q), n in groups:
        rows.append((names.get(t, t), f"{fr:g} Hz", _fmt_db(g), f"{q:g}",
                     f"×{n}" if n > 1 else ""))
    return rows


def _header(fig, info: ChartInfo, kind: str) -> None:
    fig.text(0.045, 0.962, info.title, fontsize=21, fontweight="bold", color=TEXT,
             va="top")
    fig.text(0.045, 0.918, f"{kind}  ·  {info.beq_track}", fontsize=11.5, color=MUTED,
             va="top")
    fig.text(0.955, 0.962, "BEQ", fontsize=21, fontweight="bold", color=PEAK, va="top",
             ha="right")
    fig.text(0.955, 0.918, info.beq_source, fontsize=10.5, color=MUTED, va="top",
             ha="right")


def _footer(fig, info: ChartInfo, lines: list[str]) -> None:
    parts = [part for ln in lines for part in _wrap(ln, 150)]
    top = y = max(0.064, 0.006 + 0.018 * len(parts))  # the last line must clear the image's edge
    for part in parts:
        fig.text(0.045, y, part, fontsize=8, color=FAINT, va="top")
        y -= 0.018
    right = "  ·  ".join(x for x in (info.encoder, info.date) if x)
    fig.text(0.955, top, right, fontsize=8, color=FAINT, va="top", ha="right")


def _boost(filters: list[dict]) -> tuple[float, float] | None:
    from .biquad import FilterSpec, response_db

    try:
        specs = [FilterSpec.from_catalogue(f) for f in filters]
    except (KeyError, ValueError):
        return None
    if not specs:
        return None
    f = np.geomspace(1.0, 160.0, 600)
    r = response_db(specs, f, 48000.0)
    i = int(np.argmax(np.abs(r)))
    return float(r[i]), float(f[i])


def _facts(info: ChartInfo) -> list[tuple[str, str]]:
    facts = []
    b = _boost(info.filters)
    if b is not None:
        facts.append(("Largest BEQ change", f"{_fmt_db(b[0])} at {b[1]:.0f} Hz"))
    facts += [("Source track", info.source_track),
              ("Level gain", (_fmt_db(info.gain_db) + "  (whole track, all channels)")
               if abs(info.gain_db) >= 0.005 else "none needed")]
    if info.peak_in_dbfs is not None or info.peak_out_dbfs is not None:
        def fs(v):
            return "—" if v is None else _fmt_db(v, False)[:-3]

        facts.append(("Sample peak", f"{fs(info.peak_in_dbfs)} → {fs(info.peak_out_dbfs)} dBFS"))
    if info.mv_db:
        facts.append(("Catalogue MV", _fmt_db(info.mv_db) + "  (not applied)"))
    return facts + list(info.facts)


def _side_panel(fig, info: ChartInfo, x0: float, y0: float, w: float) -> None:
    """Filter table and facts, drawn as text in figure coordinates."""
    y = y0
    fig.text(x0, y, "FILTERS", fontsize=8.5, color=MUTED, fontweight="bold", va="top")
    y -= 0.032
    rows = _filter_rows(info.filters) or [("none", "", "", "", "")]
    cols = [x0, x0 + w * 0.52, x0 + w * 0.76, x0 + w * 0.90, x0 + w]
    heads = ("Type", "Freq", "Gain", "Q", "")
    for c, hd in zip(cols, heads):
        fig.text(c, y, hd, fontsize=8, color=FAINT, va="top",
                 ha="left" if c == x0 else "right")
    y -= 0.026
    for r in rows:
        for i, (c, v) in enumerate(zip(cols, r)):
            fig.text(c, y, v, fontsize=9.5, color=TEXT if i else MUTED, va="top",
                     ha="left" if i == 0 else "right",
                     fontweight="semibold" if i == 2 else "normal")
        y -= 0.026
    y -= 0.02
    for k, v in _facts(info):
        fig.text(x0, y, k.upper(), fontsize=7.6, color=FAINT, va="top", fontweight="bold")
        y -= 0.021
        for part in _wrap(v, 38):
            fig.text(x0, y, part, fontsize=9.2, color=TEXT, va="top")
            y -= 0.021
        y -= 0.012


def _wrap(s: str, n: int) -> list[str]:
    out, cur = [], ""
    for word in s.split(" "):
        if cur and len(cur) + 1 + len(word) > n:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return out + ([cur] if cur else [])


def _db_range(*curves: np.ndarray, span: float = 70.0) -> tuple[float, float]:
    top = max(float(np.nanmax(c)) for c in curves)
    hi = math.ceil((top + 3) / 10) * 10
    return hi - span, hi


def render_spectrum(before: Measurement, after: Measurement, info: ChartInfo,
                    out: Path | io.BytesIO, dpi: int = 160) -> None:
    """Peak and average spectra, before (dashed) and after (solid), 1–160 Hz."""
    from matplotlib.lines import Line2D

    fig = _figure()
    ax = fig.add_axes((0.065, 0.185, 0.645, 0.67))
    lo, hi = SPEC_RANGE_HZ
    sel = (before.freqs >= lo) & (before.freqs <= hi)
    f = before.freqs[sel]
    a = after.shifted(-info.gain_db)
    b_pk, b_av = smooth(f, before.peak_db[sel]), smooth(f, before.avg_db[sel])
    a_pk, a_av = smooth(f, a.peak_db[sel]), smooth(f, a.avg_db[sel])
    ymin, ymax = _db_range(b_pk, a_pk)

    ax.fill_between(f, b_pk, a_pk, where=a_pk > b_pk, color=PEAK, alpha=0.10, lw=0,
                    interpolate=True)
    ax.fill_between(f, b_av, a_av, where=a_av > b_av, color=AVG, alpha=0.12, lw=0,
                    interpolate=True)
    ax.plot(f, b_pk, color=PEAK, lw=1.05, ls=(0, (3.2, 2.2)), alpha=0.75)
    ax.plot(f, b_av, color=AVG, lw=1.05, ls=(0, (3.2, 2.2)), alpha=0.75)
    ax.plot(f, a_pk, color=PEAK, lw=1.7, solid_capstyle="round")
    ax.plot(f, a_av, color=AVG, lw=1.7, solid_capstyle="round")

    ax.set_xlim(lo, hi)
    ax.set_ylim(ymin, ymax)
    ax.set_xticks([1, 10, 20, 30, 40, 50, 60, 80, 100, 120, 140, 160])
    ax.set_yticks(np.arange(ymin, ymax + 1, 10))
    ax.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xlabel("Frequency (Hz)", labelpad=8)
    ax.set_ylabel("Level (dBFS)", labelpad=8)

    handles = [
        Line2D([], [], color=PEAK, lw=1.7, label="Peak — BEQ"),
        Line2D([], [], color=PEAK, lw=1.05, ls=(0, (3.2, 2.2)), alpha=0.8,
               label="Peak — original"),
        Line2D([], [], color=AVG, lw=1.7, label="Average — BEQ"),
        Line2D([], [], color=AVG, lw=1.05, ls=(0, (3.2, 2.2)), alpha=0.8,
               label="Average — original"),
    ]
    leg = ax.legend(handles=handles, loc="upper right", ncols=2, fontsize=9,
                    handlelength=2.6, columnspacing=1.6, borderaxespad=0.9)
    for t in leg.get_texts():
        t.set_color(MUTED)

    _header(fig, info, "Peak & average spectrum")
    _side_panel(fig, info, 0.755, 0.855, 0.20)
    _footer(fig, info, [
        f"Signal: {MONO_NOTE}. Peak = loudest 4.1 s window (0.24 Hz resolution) at each "
        f"frequency over the whole film ({_hms(before.duration_s)}); average = power mean; both shown "
        f"1/24-octave smoothed.",
        "Original measured from the source track, BEQ from the encoded BEQ track"
        + (f" with its {_fmt_db(info.gain_db)} level gain taken back out"
           if abs(info.gain_db) >= 0.005 else "") + ". dBFS for a sine.",
    ])
    fig.savefig(out, dpi=dpi, format="png")


def render_heatmap(before: Measurement, after: Measurement, info: ChartInfo,
                   out: Path | io.BytesIO, dpi: int = 160) -> None:
    """Bass level over the running time, original above and BEQ below."""
    from matplotlib.colors import LinearSegmentedColormap

    cmap = LinearSegmentedColormap.from_list("beq", [
        (0.00, "#0d1016"), (0.18, "#1b1640"), (0.36, "#4b1a6b"), (0.52, "#8f2463"),
        (0.66, "#d13f4a"), (0.80, "#f47b2a"), (0.92, "#fbc04a"), (1.00, "#fff3b0")])
    a = after.shifted(-info.gain_db)
    lo, hi = HEAT_RANGE_HZ
    vmin, vmax = HEAT_DB
    dur = max(before.duration_s, a.duration_s)

    fig = _figure()
    axes = [fig.add_axes((0.065, 0.53, 0.80, 0.325)),
            fig.add_axes((0.065, 0.185, 0.80, 0.325))]
    im = None
    for ax, m, label in zip(axes, (before, a), ("ORIGINAL", "BEQ")):
        rows = (m.heat_freqs >= lo) & (m.heat_freqs <= hi)
        im = ax.imshow(m.heat_db[rows], origin="lower", aspect="auto", cmap=cmap,
                       vmin=vmin, vmax=vmax, interpolation="bilinear",
                       extent=(0, m.duration_s, m.heat_freqs[rows][0],
                               m.heat_freqs[rows][-1]))
        ax.set_xlim(0, dur)
        ax.set_ylim(lo, hi)
        ax.set_yticks([10, 20, 30, 40, 50, 60])
        for y in (20, 40):
            ax.axhline(y, color="#ffffff", alpha=0.07, lw=0.6)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.set_ylabel("Hz", labelpad=6)
        ax.text(0.008, 0.94, label, transform=ax.transAxes, fontsize=9, fontweight="bold",
                color=TEXT, va="top",
                bbox=dict(boxstyle="round,pad=0.35", fc=BG, ec="none", alpha=0.7))
        ticks = _time_ticks(dur)
        ax.set_xticks(ticks)
        ax.set_xticklabels([_hms(t, short=dur >= 3600) for t in ticks] if ax is axes[1] else [])
    axes[1].set_xlabel("Running time (h:mm)" if dur >= 3600 else "Running time (m:ss)",
                       labelpad=8)

    cax = fig.add_axes((0.885, 0.185, 0.013, 0.67))
    cb = fig.colorbar(im, cax=cax)
    cb.outline.set_visible(False)
    cb.ax.tick_params(colors=AXIS, labelsize=8.5)
    cb.set_label("Level (dBFS)", color=MUTED, labelpad=8)

    _header(fig, info, "Bass over the running time")
    _footer(fig, info, [
        f"Signal: {MONO_NOTE}. Each column is the loudest 2 s window (0.49 Hz resolution) in "
        f"{dur / max(1, before.heat_db.shape[1]):.1f} s of the film; colours fixed at "
        f"{vmin:.0f} to {vmax:.0f} dBFS for every film.",
        f"BEQ: {_filter_summary(info.filters)}"
        + (f"; its {_fmt_db(info.gain_db)} level gain taken back out"
           if abs(info.gain_db) >= 0.005 else "") + f". Source: {info.source_track}.",
    ])
    fig.savefig(out, dpi=dpi, format="png")


def _filter_summary(filters: list[dict]) -> str:
    rows = _filter_rows(filters)
    if not rows:
        return "no filters"
    return ", ".join(f"{t.lower()} {fr} {g}{' Q' + q if q else ''}{' ' + n if n else ''}"
                     for t, fr, g, q, n in rows)


def _time_ticks(dur: float) -> list[float]:
    for step in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1200, 1800, 3600):
        if dur / step <= 10:
            break
    return [float(t) for t in np.arange(0, dur + 1e-6, step)]


def _hms(s: float, short: bool = False) -> str:
    s = int(round(s))
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    if short:
        return f"{h}:{m:02d}" if h else f"0:{m:02d}"
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def summary(before: Measurement, after: Measurement, info: ChartInfo) -> dict:
    """Machine-readable companion to the charts (beq.json in the MKV)."""
    a = after.shifted(-info.gain_db)
    return {"schema": 1, "signal": MONO_NOTE, "fs_hz": FS,
            "spectrum": {"window": "hann", "nperseg": SPEC_NPERSEG,
                         "overlap": 0.5,
                         "original": before.to_json(), "beq_level_matched": a.to_json()},
            "gain_db": info.gain_db}


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))
