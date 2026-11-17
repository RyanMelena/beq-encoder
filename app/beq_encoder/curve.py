"""A filter's curve as inline SVG."""

from __future__ import annotations

import math

import numpy as np

from .biquad import FilterSpec, response_db


def span_db(curves: list[tuple[list[dict], float]], floor_db: float = -math.inf
            ) -> tuple[float, float]:
    """The gain axis that holds every (filters, low-end cutoff) in `curves`, so that charts a
    user compares share one scale."""
    f = np.geomspace(5, 250, 240)
    lo, hi = -3.0, 3.0
    for filters, hz in curves:
        specs = [FilterSpec.from_catalogue(x) for x in filters]
        if specs or hz:
            r = response_db(specs, f, highpass_hz=hz)
            lo, hi = min(lo, float(r.min())), max(hi, float(r.max()))
    return max(floor_db, math.floor(lo / 5) * 5), math.ceil(hi / 5) * 5


def response_svg(curves: list[tuple], w: int = 640, h: int = 240,
                 legend: bool | list[tuple[str, int]] = True, points: int = 240,
                 floor_db: float = -math.inf,
                 span: tuple[float, float] | None = None,
                 cutoff_marks: list[tuple[float, dict]] = ()) -> str:
    """One or more filter cascades' magnitude response, 5-250 Hz, as inline SVG. Each curve
    is (label, filters), (label, filters, low-end cutoff in Hz), or that and a dict of
    attributes for its line, where "colour" picks the line's colour. `span` sets the gain
    axis, for charts that share one. `legend` may be the entries to show: (label, colour).
    Each line and legend entry carries data-n, its colour plus one: the filter's number in
    the list beside the chart. `cutoff_marks`: (Hz, attributes) for each low-end cutoff to
    mark with an upright line and its name."""
    f = np.geomspace(5, 250, points)
    data, attrs = [], []
    for label, filters, *more in curves:
        specs = [FilterSpec.from_catalogue(x) for x in filters]
        hz = more[0] if more else 0.0
        attrs.append(more[1] if len(more) > 1 else {})
        data.append((label, response_db(specs, f, highpass_hz=hz) if specs or hz
                     else np.zeros_like(f)))
    if not data:
        return ""
    allv = np.concatenate([d for _, d in data])
    lo, hi = span or (max(floor_db, math.floor(min(-3.0, float(allv.min())) / 5) * 5),
                      math.ceil(max(3.0, float(allv.max())) / 5) * 5)
    data = [(label, np.clip(d, lo, hi)) for label, d in data]
    pl, pr, pt, pb = 96, 10, 10, 48

    def x(v: float) -> float:
        return pl + (math.log10(v) - math.log10(5)) / (math.log10(250) - math.log10(5)) * (
            w - pl - pr
        )

    def y(v: float) -> float:
        return pt + (hi - v) / (hi - lo) * (h - pt - pb)

    def text(lx: float, ly: float, anchor: str, s: object, dy: int = 0, cls: str = "ax",
             extra: str = "", dx: int = 0) -> str:
        return (f'<text class="{cls}" x="{lx / w:.2%}" y="{ly / h:.2%}" dx="{dx}" dy="{dy}" '
                f'text-anchor="{anchor}"{extra}>{s}</text>')

    def attrs_of(d: dict) -> str:
        return "".join(f' {k}="{v}"' for k, v in d.items())

    # Marks stretch with the chart; labels sit outside the viewBox so they keep their CSS size.
    marks: list[str] = []
    labels: list[str] = []
    for fr in (10, 20, 50, 100, 200):
        marks.append(f'<line class="grid" x1="{x(fr):.1f}" x2="{x(fr):.1f}" y1="{pt}" y2="{h - pb}"/>')
        labels.append(text(x(fr), h - pb, "middle", fr, dy=16))
    step = 5 if hi - lo <= 30 else 10
    for v in range(int(lo), int(hi) + 1, step):
        cls = "zero" if v == 0 else "grid"
        marks.append(f'<line class="{cls}" x1="{pl}" x2="{w - pr}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        labels.append(text(pl - 6, y(v), "end", f"{v:+d}" if v else "0", dy=4))
    for hz, extra in cutoff_marks:
        if 5 <= hz <= 250:
            marks.append(f'<line class="cutoff-mark" x1="{x(hz):.1f}" x2="{x(hz):.1f}" '
                         f'y1="{pt}" y2="{h - pb}"{attrs_of(extra)}/>')
            # Named at its foot, clear of the legend at the top right: a filter's lines are
            # high at the cutoff and low only well below it.
            labels.append(text(x(hz), h - pb, "start", f"Cutoff {hz:g} Hz", dy=-6, dx=5,
                               cls="ax cutoff-label", extra=attrs_of(extra)))
    for i, (_label, d) in enumerate(data):
        pts = " ".join(f"{x(a):.1f},{y(b):.1f}" for a, b in zip(f, d, strict=True))
        colour = attrs[i].get("colour", i)
        extra = attrs_of({"data-n": colour + 1,
                          **{k: v for k, v in attrs[i].items() if k != "colour"}})
        marks.append(f'<polyline class="curve c{colour % 8}" points="{pts}"{extra}/>')
    entries = legend if isinstance(legend, list) else \
        [(label, i) for i, (label, _) in enumerate(data)] if legend and len(data) > 1 else []
    for row, (label, colour) in enumerate(entries):
        ly = pt + 14 + 16 * row
        marks.append(f'<line class="curve key c{colour % 8}" data-n="{colour + 1}" '
                     f'x1="{w - 40}" x2="{w - 10}" y1="{ly - 4}" y2="{ly - 4}"/>')
        labels.append(text(w - 46, ly, "end", _esc(label), cls="ax key",
                           extra=f' data-n="{colour + 1}"'))
    labels.append(text((pl + w - pr) / 2, h, "middle", "Frequency (Hz)", dy=-4, cls="ax ax-title"))
    labels.append(text(0, (pt + h - pb) / 2, "middle", "Gain (dB)", dy=4, cls="ax ax-title ax-y"))
    return (f'<svg class="resp" role="img" aria-label="Filter response: gain in dB against frequency in Hz">'
            f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none">{"".join(marks)}</svg>'
            f'{"".join(labels)}</svg>')


CUTOFF_FLOOR_DB = -20.0  # how far down a chart follows a low-end cutoff's slope


def compare_svg(filters: list[tuple[str, list[dict]]], cutoffs: list[float], active: float,
                span: tuple[float, float], first: int = 0) -> str:
    """Several filters on one set of axes, so that the user compares them: each (label, bands)
    in its own colour and line pattern, counted from `first`, with a legend. It holds each
    filter's curve at every cutoff in `cutoffs` and shows the one at `active`, so the wizard
    swaps the curves shown when the cutoff changes, without asking the server. Each cutoff
    but Off has its mark, shown with its curves."""
    def shown(hz: float) -> dict:
        return {"data-cutoff": f"{hz:g}", **({} if hz == active else {"hidden": "hidden"})}

    return response_svg(
        [(label, bands, hz, {"colour": first + i, **shown(hz)})
         for i, (label, bands) in enumerate(filters) for hz in cutoffs],
        legend=[(label, first + i) for i, (label, _) in enumerate(filters)], points=120,
        floor_db=CUTOFF_FLOOR_DB, span=span,
        cutoff_marks=[(hz, shown(hz)) for hz in cutoffs if hz])


def swatch(colour: int) -> str:
    """A filter's line as its curve draws it, to sit beside its name."""
    return (f'<svg class="resp swatch" viewBox="0 0 30 8" aria-hidden="true">'
            f'<line class="curve c{colour % 8}" x1="1" x2="29" y1="4" y2="4"/></svg>')


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
