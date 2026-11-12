"""Before an encode: the filtered peak of each filter, estimated from the 1 kHz analysis.

The BEQ filter pass lowers every channel by one gain so that the filtered peak fits the
target peak (beqfilter.filter_stream). BEQ filters act on the bass only, so the same
filters designed at the analysis rate, run over the analysis' channels, give about the
peak that pass will measure. Content above FS/2 is not in the analysis, and for Atmos the
analysis is the channel presentation while the encode filters the objects.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

import numpy as np

from . import analysis
from .beqfilter import dbfs
from .biquad import FilterSpec, design_sos, sosfilt_channels

MAX_BAND_HZ = 0.4 * analysis.FS  # a band this high is beyond what a design at FS can represent


def predict(blocks: Iterable[np.ndarray], channels: Sequence[str],
            filters: Mapping[str, list[dict]]) -> dict[str, dict]:
    """{digest: {"peak_dbfs", "channel"}}: the highest filtered channel peak of each filter
    over `blocks` (frames, channels) at analysis.FS, in one pass. A filter with no bands,
    a band FilterSpec rejects or one at MAX_BAND_HZ or above gets no entry, nor does silence."""
    sos = {}
    for digest, bands in filters.items():
        try:
            specs = [FilterSpec.from_catalogue(f) for f in bands]
        except (KeyError, ValueError):
            continue
        if specs and all(s.freq < MAX_BAND_HZ for s in specs):
            sos[digest] = design_sos(specs, analysis.FS)
    state = {d: np.zeros((len(s), 2, len(channels))) for d, s in sos.items()}
    peaks = {d: np.zeros(len(channels)) for d in sos}
    for x in blocks:
        for d, s in sos.items():
            y, state[d] = sosfilt_channels(s, x, state[d])
            np.maximum(peaks[d], np.abs(y).max(axis=0), out=peaks[d])
    out = {}
    for d, p in peaks.items():
        i = int(p.argmax())
        if p[i] > 0:
            out[d] = {"peak_dbfs": round(dbfs(float(p[i])), 2), "channel": channels[i]}
    return out
