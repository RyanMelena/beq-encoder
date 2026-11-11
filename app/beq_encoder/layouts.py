"""Channel layouts, by ffmpeg layout name, in ffmpeg's native channel order.

The table is beqforge's (`tools/extract.py`, mapping "ffmpeg_layout_v1"): speaker
roles come from the layout name, never from a channel count alone. The one
difference is mono, which the designer contract (designer-interface.md §2) sends
as "FC" (ffmpeg's label) where extract.py says "M".
"""

from __future__ import annotations

MAPPING = "ffmpeg_layout_v1"

LAYOUTS: dict[str, tuple[str, ...]] = {
    "mono": ("FC",),
    "stereo": ("L", "R"),
    "2.1": ("L", "R", "LFE"),
    "3.0": ("L", "R", "C"),
    "3.0(back)": ("L", "R", "Cb"),
    "4.0": ("L", "R", "C", "Cb"),
    "quad": ("L", "R", "Lb", "Rb"),
    "quad(side)": ("L", "R", "Ls", "Rs"),
    "3.1": ("L", "R", "C", "LFE"),
    "4.1": ("L", "R", "C", "LFE", "Cb"),
    "5.0": ("L", "R", "C", "Lb", "Rb"),
    "5.0(side)": ("L", "R", "C", "Ls", "Rs"),
    "5.1": ("L", "R", "C", "LFE", "Lb", "Rb"),
    "5.1(side)": ("L", "R", "C", "LFE", "Ls", "Rs"),
    "6.0": ("L", "R", "C", "Cb", "Ls", "Rs"),
    "6.0(front)": ("L", "R", "Lc", "Rc", "Ls", "Rs"),
    "hexagonal": ("L", "R", "C", "Lb", "Rb", "Cb"),
    "6.1": ("L", "R", "C", "LFE", "Cb", "Ls", "Rs"),
    "6.1(back)": ("L", "R", "C", "LFE", "Lb", "Rb", "Cb"),
    "6.1(front)": ("L", "R", "LFE", "Lc", "Rc", "Ls", "Rs"),
    "7.0": ("L", "R", "C", "Lb", "Rb", "Ls", "Rs"),
    "7.0(front)": ("L", "R", "C", "Lc", "Rc", "Ls", "Rs"),
    "7.1": ("L", "R", "C", "LFE", "Lb", "Rb", "Ls", "Rs"),
    "7.1(wide)": ("L", "R", "C", "LFE", "Lb", "Rb", "Lc", "Rc"),
    "7.1(wide-side)": ("L", "R", "C", "LFE", "Lc", "Rc", "Ls", "Rs"),
}

# What ffmpeg picks when a stream only states a channel count
DEFAULT_FOR_COUNT = {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 5: "5.0", 6: "5.1",
                     7: "6.1", 8: "7.1"}

# MKV/MediaInfo-style speaker-count label: "7.1", "5.1", "2.0", "1.0"
def label(channels: int, layout: str | None = None) -> str:
    names = LAYOUTS.get(layout or "") or LAYOUTS.get(DEFAULT_FOR_COUNT.get(channels, ""), ())
    if names and len(names) == channels:
        lfe = sum(1 for n in names if n == "LFE")
        return f"{channels - lfe}.{lfe}"
    return f"{channels}ch"


def names(channels: int, layout: str | None) -> tuple[str, ...] | None:
    """Channel names for a stream, or None when the layout is unknown/ambiguous."""
    got = LAYOUTS.get(layout or "")
    if got is not None and len(got) == channels:
        return got
    return None


def names_or_generic(channels: int, layout: str | None) -> tuple[str, ...]:
    """For display and diagnostics only: falls back to c0..cN for unknown layouts."""
    got = names(channels, layout)
    if got:
        return got
    dflt = LAYOUTS.get(DEFAULT_FOR_COUNT.get(channels, ""))
    if dflt and len(dflt) == channels:
        return dflt
    return tuple(f"c{i}" for i in range(channels))


def output_layout(channels: int, layout: str | None) -> str:
    """The -ch_layout to encode PCM with: the source's, or the default for the count."""
    if names(channels, layout):
        return layout  # type: ignore[return-value]
    dflt = DEFAULT_FOR_COUNT.get(channels)
    if dflt is None:
        raise ValueError(f"No channel layout for {channels} channels")
    return dflt
