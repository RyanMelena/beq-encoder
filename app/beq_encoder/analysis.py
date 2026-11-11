"""1 kHz analysis extraction for the designer, written where the designer can read it.

One ffmpeg pass decodes every channel of the track at 1 kHz in double precision
(`decode_blocks`: `aformat=sample_fmts=dbl,aresample=1000`; the integer path quantises
and fakes a noise floor ~35 dB too high below ~15 Hz). The mono mix is computed
afterwards, as beqforge's tools/extract.py does: mains x 10^(-20.2/20) + LFE x
10^(-10.2/20). The charts and the bass screen measure this same signal, so they read
it through the same `decode_blocks`.

Output, under the shared work root: one IEEE-float64 WAV at 1 kHz whose columns are
the channels in ffmpeg layout order followed by the mono mix, plus meta.json with
the column names and the SHA-256 of each decoded column (designer-interface.md
§7.1, "Arrays by reference"). Cached per source file by size and mtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from . import layouts, pcmio

FS = 1000
MAIN_GAIN = 10.0 ** (-20.2 / 20.0)
LFE_GAIN = 10.0 ** (-10.2 / 20.0)
SCHEMA = 1
CHUNK_FRAMES = 1 << 16


class AnalysisError(Exception):
    pass


def cache_key(path: Path, size: int, mtime: float, track_id: int) -> str:
    h = hashlib.sha256(f"{path}\0{size}\0{mtime!r}\0{track_id}\0{SCHEMA}".encode())
    return h.hexdigest()[:20]


def mono_gains(names: tuple[str, ...]) -> np.ndarray:
    return np.array([LFE_GAIN if n == "LFE" else MAIN_GAIN for n in names], dtype=np.float64)


def decode_blocks(ffmpeg: str, path: str, audio_index: int, channels: int,
                  popen: pcmio.Popen | None = None, start: float = 0.0, length: float = 0.0,
                  what: str = "track", on_error: Callable[[str], None] | None = None,
                  ) -> Iterator[np.ndarray]:
    """Blocks of a track's every channel at `FS` in double precision, from `start` seconds
    for `length` seconds (0: to the end). A failed decode raises RuntimeError at the end."""
    seek = ["-ss", f"{start:.3f}"] if start > 0 else []
    limit = ["-t", f"{length:.3f}"] if length > 0 else []
    cmd = [ffmpeg, "-nostdin", "-v", "error", *seek, *pcmio.DECODE_OPTS, "-i", path,
           "-map", f"0:a:{audio_index}", *limit,
           "-af", f"aformat=sample_fmts=dbl,aresample={FS}", "-f", "f64le", "-c:a", "pcm_f64le",
           "-"]
    with pcmio.PCMStream(cmd, channels, popen=popen, what=what, on_error=on_error) as stream:
        while len(x := stream.read(CHUNK_FRAMES)):
            yield x


def _wav_header(channels: int, frames: int) -> bytes:
    """RIFF/WAVE, format 3 (IEEE float), 64-bit, with the fact chunk non-PCM needs."""
    block = 8 * channels
    data = frames * block
    fmt = struct.pack("<HHIIHH", 3, channels, FS, FS * block, block, 64)
    return (
        b"RIFF" + struct.pack("<I", 4 + (8 + len(fmt)) + 12 + (8 + data))
        + b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"fact" + struct.pack("<II", 4, frames & 0xFFFFFFFF)
        + b"data" + struct.pack("<I", data)
    )


def load_cached(work_root: Path, key: str) -> dict | None:
    meta = work_root / "analysis" / key / "meta.json"
    try:
        m = json.loads(meta.read_text())
    except (OSError, ValueError):
        return None
    wav = work_root / m["wav"]
    if m.get("schema") != SCHEMA or not wav.is_file() or wav.stat().st_size != m["wav_bytes"]:
        return None
    os.utime(meta.parent)  # eviction goes by last use
    return m


_cache_lock = threading.Lock()  # prune against whoever is claiming an analysis
_claimed: Counter[str] = Counter()


@contextmanager
def in_use(key: str) -> Iterator[None]:
    """While the body runs, `prune` leaves the analysis of `key` alone."""
    with _cache_lock:
        _claimed[key] += 1
    try:
        yield
    finally:
        with _cache_lock:
            _claimed[key] -= 1
            if not _claimed[key]:
                del _claimed[key]


def prune(work_root: Path, max_bytes: int) -> None:
    """Drop the least recently used cached extractions beyond max_bytes, except any
    a running job has claimed (`in_use`)."""
    base = work_root / "analysis"
    with _cache_lock:
        if not base.is_dir():
            return
        dirs = []
        for d in base.iterdir():
            if d.is_dir():
                size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
                dirs.append((d.stat().st_mtime, size, d))
        total = sum(s for _, s, _ in dirs)
        for _, size, d in sorted(dirs):
            if total <= max_bytes:
                break
            if d.name not in _claimed:
                shutil.rmtree(d, ignore_errors=True)
                total -= size


def extract(ffmpeg: str, path: Path, track, duration_s: float | None, work_root: Path, key: str,
            popen: pcmio.Popen, progress: Callable[[str, float | None], None],
            log: Callable[[str], None], check: Callable[[], None]) -> dict:
    """Decode `track` of `path` to the analysis WAV; returns its meta dict.

    The decode runs through `popen`, so a job's Cancel reaches it, and `check` stops
    the read when the job is cancelled."""
    ch = int(track.channels or 0)
    names = layouts.names(ch, track.layout)
    if names is None:
        raise AnalysisError(
            f"Unsupported or unknown channel layout {track.layout!r} ({ch} channels): "
            "the designer needs speaker roles, which a channel count alone does not give"
        )
    out_dir = work_root / "analysis" / key
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    wav = out_dir / "analysis.wav"
    cols = ch + 1  # channels, then the mono mix
    gains = mono_gains(names)
    stage = "Analysis: decoding at 1 kHz"
    progress(stage, 0)
    hashers = [hashlib.sha256() for _ in range(cols)]
    frames = 0
    peak = 0.0
    t0 = time.monotonic()
    blocks = decode_blocks(ffmpeg, str(path), track.audio_index, ch, popen=popen,
                           what="track for analysis", on_error=lambda ln: log(f"[ffmpeg] {ln}"))
    try:
        with open(wav, "wb") as fh:
            fh.write(_wav_header(cols, 0))
            for x in blocks:
                block = np.empty((len(x), cols), dtype="<f8")
                block[:, :ch] = x
                block[:, ch] = x @ gains
                for i in range(cols):
                    hashers[i].update(np.ascontiguousarray(block[:, i]).tobytes())
                peak = max(peak, float(np.abs(x).max()))
                fh.write(block.tobytes())
                frames += len(x)
                check()
                if duration_s:
                    progress(stage, min(100.0, 100 * frames / FS / duration_s))
            fh.seek(0)
            fh.write(_wav_header(cols, frames))
    except RuntimeError as exc:
        raise AnalysisError(str(exc)) from None
    if frames < FS:
        raise AnalysisError("Analysis decode produced under a second of audio")
    rel = wav.relative_to(work_root).as_posix()
    meta = {
        "schema": SCHEMA,
        "fs": FS,
        "frames": frames,
        "duration_s": frames / FS,
        "channels": list(names),
        "layout": track.layout,
        "mapping": layouts.MAPPING,
        "coverage": "complete_programme",
        "wav": rel,
        "wav_bytes": wav.stat().st_size,
        "columns": {**{n: i for i, n in enumerate(names)}, "mono_mix": ch},
        "sha256": {**{n: hashers[i].hexdigest() for i, n in enumerate(names)},
                   "mono_mix": hashers[ch].hexdigest()},
        "peak": peak,
        "track_id": track.track_id,
        "source": str(path),
        "seconds": round(time.monotonic() - t0, 1),
        "created_at": time.time(),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=1))
    return meta
