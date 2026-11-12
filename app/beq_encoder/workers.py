"""The threads the per-sample work of a block is spread over.

numpy and scipy release the GIL inside their loops, and everything done here is
independent per sample or per channel, so the result is identical, only faster.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

import numpy as np

T = TypeVar("T")

WORKERS = max(1, min(16, os.cpu_count() or 4))
# threads start on first use, so building it here costs nothing at import
POOL = ThreadPoolExecutor(WORKERS, thread_name_prefix="block")
# Below this many rows a part costs more to hand over than to do; at it, a part of a
# 16-channel block still fits a core's cache
MIN_ROWS = 1 << 13


def over_rows(fn: Callable[[int, int], T], rows: int) -> list[T]:
    """`fn(first, stop)` over `rows` split into contiguous parts, one a thread; the
    results in row order. `fn` must not itself wait on the pool."""
    parts = min(WORKERS, rows // MIN_ROWS)
    if parts < 2:
        return [fn(0, rows)]
    edges = [rows * i // parts for i in range(parts + 1)]
    return list(POOL.map(fn, edges[:-1], edges[1:]))


def abs_peak(x: np.ndarray) -> float:
    """The largest magnitude in `x` (0.0 when empty); NaN if it holds one, as np.max."""
    if x.size == 0:
        return 0.0
    return float(np.max(over_rows(lambda a, b: float(np.max(np.abs(x[a:b]))), len(x))))
