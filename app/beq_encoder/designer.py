"""Client for a beqforge designer (designer-interface.md §7.1, HTTP binding).

Requests send the analysis arrays by reference (contract 1.2): each array names a
column of the analysis WAV under the shared work root, with the SHA-256 of its
decoded float64 bytes. The designer checks the path, rate, frame count and digest
before it uses anything, and answers 422 for a reference it cannot honour.

A decline is a result (`kind == "declined"`), not an error. Anything else that
goes wrong (connection, non-2xx, a body that is not a DesignResponse) raises
DesignerError.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

TYPE_MAP = {"low_shelf": "LowShelf", "high_shelf": "HighShelf", "peaking_eq": "PeakingEQ"}
UA = {"User-Agent": "beq-encoder", "Content-Type": "application/json"}


class DesignerError(Exception):
    pass


@dataclass(slots=True)
class BassManagement:
    lpf_fs: float = 80.0
    lpf_position: str = "Before"  # Before | After | Off
    headroom_type: str = "WCS"  # WCS | "<dB>"

    def to_json(self) -> dict:
        return {"lpf_fs": self.lpf_fs, "lpf_position": self.lpf_position,
                "headroom_type": self.headroom_type, "clip_before": False, "clip_after": False}


def _url(base: str, path: str) -> str:
    return base.rstrip("/") + path


def _get(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def health(base_url: str, timeout: float = 5.0) -> dict:
    """GET /health, plus an `ok`/`problems` summary for the UI banner."""
    st: dict = {"url": base_url, "ok": False, "problems": []}
    try:
        h = _get(_url(base_url, "/health"), timeout)
    except Exception as exc:  # noqa: BLE001 - surfaced as text
        st["problems"].append(f"Designer not reachable at {base_url}: {exc}")
        return st
    st.update(h)
    try:
        cv = tuple(int(x) for x in str(h.get("contract_version", "0")).split(".")[:2])
    except ValueError:
        cv = (0, 0)
    if cv < (1, 2):
        st["problems"].append(f"Designer speaks contract {h.get('contract_version')}; 1.2+ needed")
    if h.get("shared_root") is not True:
        st["problems"].append("Designer has no shared root (BEQFORGE_SHARED_ROOT=/work)")
    st["ok"] = not st["problems"]
    return st


def build_request(meta: dict, bm: BassManagement | None) -> dict:
    """The /design body for an analysis (see analysis.extract), arrays by reference."""
    def ref(name: str) -> dict:
        return {"dtype": "float64", "shape": [meta["frames"]],
                "file": {"path": meta["wav"], "channel": meta["columns"][name]},
                "sha256": meta["sha256"][name]}

    return {
        "contract_version": "1.2",
        "fs": meta["fs"],
        "coverage": meta["coverage"],
        "mono_mix": ref("mono_mix"),
        "channels": {n: ref(n) for n in meta["channels"]},
        "bass_management": bm.to_json() if bm else None,
    }


def post_design(base_url: str, body: dict, timeout: float,
                check_cancel: Callable[[], None] | None = None) -> dict:
    """POST /design; polls check_cancel while waiting (a design takes minutes)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(_url(base_url, "/design"), data=data, headers=UA,
                                 method="POST")
    box: dict = {}

    def call() -> None:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                box["body"] = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            box["err"] = DesignerError(f"Designer answered HTTP {exc.code}: {detail}")
        except Exception as exc:  # noqa: BLE001
            box["err"] = DesignerError(f"Designer request failed: {exc}")

    th = threading.Thread(target=call, daemon=True)
    th.start()
    while th.is_alive():
        th.join(1.0)
        if check_cancel:
            check_cancel()
    if "err" in box:
        raise box["err"]
    resp = box["body"]
    validate(resp)
    return resp


def validate(resp: dict) -> None:
    """The parts of §3-§5 this caller relies on."""
    if not isinstance(resp, dict):
        raise DesignerError("Designer response is not an object")
    cands, reason = resp.get("candidates"), resp.get("decline_reason")
    if (cands is None) == (reason is None):
        raise DesignerError("Designer response must hold exactly one of candidates / decline")
    if cands is not None and (not isinstance(cands, list) or not cands):
        raise DesignerError("Designer returned an empty candidate list")
    for c in (cands or []) + (resp.get("rejected") or []):
        fs = c.get("filters")
        if not isinstance(fs, list) or len(fs) > 10:
            raise DesignerError("Designer candidate has no filter list or over 10 sections")
        for f in fs:
            if f.get("type") not in TYPE_MAP:
                raise DesignerError(f"Unsupported designer filter type {f.get('type')!r}")
            vals = (f.get("freq_hz"), f.get("gain_db"), f.get("q"))
            if not all(isinstance(v, int | float) and math.isfinite(v) for v in vals) or \
                    f["freq_hz"] <= 0 or f["q"] <= 0:
                raise DesignerError(f"Invalid designer biquad {f}")


def to_catalogue_filters(filters: list[dict]) -> list[dict]:
    return [{"type": TYPE_MAP[f["type"]], "freq": float(f["freq_hz"]),
             "gain": float(f["gain_db"]), "q": float(f["q"]), "count": 1} for f in filters]


def strategy(c: dict) -> str:
    return ((c.get("commentary") or {}).get("strategy") or c.get("method") or "design").strip()


def candidate_entry(c: dict, resp: dict, release: str, title: str, year: str,
                    rank: int, rejected: bool = False) -> dict:
    """A designer candidate in the shape the pipeline/UI use for catalogue entries."""
    filters = to_catalogue_filters(c["filters"])
    strat = strategy(c)
    label = f"beqforge {release} {strat}".strip()
    digest = hashlib.sha256(json.dumps(
        {"f": filters, "v": resp.get("design_version"), "s": resp.get("design_settings"),
         "strategy": strat, "rejected": rejected},
        sort_keys=True).encode()).hexdigest()[:24]
    return {
        "digest": f"design:{digest}",
        "origin": "designer",
        "label": label,
        "author": f"beqforge {release}".strip(),
        "title": title, "year": year, "edition": "", "source": "designer",
        "audio_types": [], "mv": str(round(float(c.get("mv_adjust_db") or 0), 1)),
        "url": "", "warning": "", "note": "",
        "filters": filters,
        "strategy": strat,
        "rank": rank,
        "rejected": rejected,
        "rejection_reasons": c.get("rejection_reasons") or [],
        "confidence": c.get("confidence"),
        "gain_reduction_db": c.get("gain_reduction_db"),
        "method": c.get("method"),
        "residual_db": c.get("residual_db"),
        "residual_rms_db": c.get("residual_rms_db"),
        "channel_scope": c.get("channel_scope"),
        "commentary": c.get("commentary") or {},
        "designer_release": release,
        "design_version": resp.get("design_version"),
        "design_settings": resp.get("design_settings"),
        "designer_build": resp.get("designer_build"),
    }
