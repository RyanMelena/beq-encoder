"""The slow work before a BEQ track: BEQ Forge designs a filter for a track, and each
filter's level change is measured over the whole track at each low-end cutoff."""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict

import numpy as np

from .. import analysis, designer, media, preflight, support
from .job import Job, source_pin
from .processes import JobFailed


PROFILE_BUCKET_S = 10.0


def bass_profile(blocks: Iterable[np.ndarray], channels: list[str],
                 bucket_s: float = PROFILE_BUCKET_S) -> dict:
    """The level of the 1 kHz analysis' mono mix in each `bucket_s` of the running time, in
    dBFS: where the film's bass is heaviest."""
    gains = analysis.mono_gains(tuple(channels))
    size = int(bucket_s * analysis.FS)
    sums, counts = np.zeros(0), np.zeros(0)
    done = 0
    for x in blocks:
        if not len(x):
            continue
        at = (done + np.arange(len(x))) // size
        n = int(at[-1]) + 1
        if n > len(sums):
            sums, counts = np.pad(sums, (0, n - len(sums))), np.pad(counts, (0, n - len(counts)))
        np.add.at(sums, at, (x @ gains) ** 2)
        np.add.at(counts, at, 1)
        done += len(x)
    db = [round(float(10 * np.log10(max(s / c, 1e-12))), 1) for s, c in zip(sums, counts)]
    return {"bucket_s": bucket_s, "db": db}


def _source(job: Job) -> tuple:
    """The file as it stands and the track a job is for: (stamp, probe, track, film)."""
    stamp, probe = job.read_source()
    track = probe.track(int(job.params["track_id"]))
    if track is None or track.beq:
        raise JobFailed(f"Track {job.params['track_id']} is not in the file any more; rescan it")
    names = media.parse_name(job.src)
    film = {"tmdb": names.tmdb, "title": names.title or job.src.stem,
            "year": names.year or "", "edition": names.edition or ""}
    plan = support.plan(track)
    if not plan.ok:
        raise JobFailed(f"Not supported: {plan.reason}")
    return stamp, probe, track, film


def run_design(job: Job) -> dict:
    """BEQ Forge designs filters for one track. The result holds them, each measured at
    every cutoff asked for while the decoded audio is at hand."""
    stamp, probe, track, film = _source(job)
    job.log(f"Film: {film}; track #{track.track_id} {track.label} {track.codec} "
            f"lang={track.language} layout={track.layout} objects={track.objects}")
    d = _design(job, track, probe, film)
    result = {"phase": "design", "film": film, "track": _track_dict(track),
              "track_key": probe.track_key(track), "source": source_pin(stamp, track),
              "design": d, "levels": {}}
    if not d["declined"]:
        result["levels"] = _predict(job, track, probe, d["candidates"],
                                    job.params.get("cutoffs") or [0.0])
    return result


def run_measure(job: Job) -> dict:
    """Measure the level each of `params["filters"]` ends at, at each of `params["cutoffs"]`,
    and the bass level over the running time."""
    stamp, probe, track, _film = _source(job)
    levels = _predict(job, track, probe, job.params.get("filters") or [],
                      job.params.get("cutoffs") or [0.0], must=True)
    key = _analysis_key(job, track)
    with analysis.in_use(key):
        meta = _analyse(job, track, probe, key)
        job.progress("Finding the heaviest bass", None)
        profile = bass_profile(analysis.read_channels(job.s.work_dir, meta), meta["channels"])
    return {"phase": "measure", "track": _track_dict(track),
            "track_key": probe.track_key(track), "source": source_pin(stamp, track),
            "levels": levels, "profile": profile}


def _analysis_key(job: Job, track: media.AudioTrack) -> str:
    st = job.src.stat()
    return analysis.cache_key(job.src, st.st_size, st.st_mtime, track.track_id)


def _predict(job: Job, track: media.AudioTrack, probe: media.ProbeResult,
             entries: list[dict], cutoffs: list[float], must: bool = False) -> dict:
    """{cutoff: {digest: predicted filtered peak}} for each entry with filters, at each
    low-end cutoff (preflight.predict). An estimate never stops a design: when it cannot be
    made, there is none. `must`: the estimate is the job, so its failure is the job's."""
    filters = {e["digest"]: e["filters"] for e in entries if e.get("filters")}
    if not filters:
        return {}
    key = _analysis_key(job, track)
    out: dict = {}
    try:
        with analysis.in_use(key):
            meta = _analyse(job, track, probe, key)
            t0 = time.monotonic()
            for i, hz in enumerate(cutoffs):
                out[f"{hz:g}"] = preflight.predict(
                    _estimate_blocks(job, meta, i, len(cutoffs)), meta["channels"], filters, hz)
    except Exception as exc:
        job.procs.check()  # a cancel kills the decode, which then fails: that is the cancel
        if must:
            raise JobFailed(f"The level changes could not be measured: {exc}") from None
        job.log(f"Gain estimate: not made ({exc})")
        return {}
    labels = {e["digest"]: e.get("label") or e["digest"] for e in entries}
    for hz, predicted in out.items():
        for digest, p in predicted.items():
            job.log(f"Gain estimate {labels[digest]} at cutoff {hz} Hz: filtered peak "
                    f"{p['peak_dbfs']:+.2f} dBFS on {p['channel']}")
    job.log(f"Gain estimate: {len(filters)} filters at {len(cutoffs)} cutoffs in "
            f"{time.monotonic() - t0:.1f} s")
    return out


def _estimate_blocks(job: Job, meta: dict, part: int = 0, parts: int = 1
                     ) -> Iterator[np.ndarray]:
    stage = "Estimating the gain of each filter"
    job.progress(stage, 100 * part / parts)
    done = 0
    for x in analysis.read_channels(job.s.work_dir, meta):
        yield x
        done += len(x)
        job.checked_progress(stage, 100 * (part + done / meta["frames"]) / parts)


def _analyse(job: Job, track: media.AudioTrack, probe: media.ProbeResult, key: str) -> dict:
    """The cached 1 kHz analysis of the track, or a fresh one (which may evict others).
    The caller holds `analysis.in_use(key)`."""
    meta = analysis.load_cached(job.s.work_dir, key)
    if meta:
        job.log(f"Analysis: cached {meta['wav']} ({meta['duration_s'] / 60:.1f} min)")
        return meta
    try:
        meta = analysis.extract(job.s.ffmpeg, job.src, track, probe.duration_s, job.s.work_dir,
                                key, job.procs.popen, job.progress, job.log, job.procs.check)
    except analysis.AnalysisError as exc:
        raise JobFailed(str(exc)) from None
    job.log(f"Analysis: {meta['channels']} at {meta['fs']} Hz, "
            f"{meta['duration_s'] / 60:.1f} min in {meta['seconds']} s")
    analysis.prune(job.s.work_dir, int(job.s.analysis_cache_gb * 1e9))
    return meta


def _design(job: Job, track: media.AudioTrack, probe: media.ProbeResult, film: dict) -> dict:
    job.progress("Checking the designer", None)
    h = designer.health(job.s.designer_url)
    for problem in h["problems"]:
        job.log(f"Designer: {problem}")
    if not h["ok"]:
        raise JobFailed(h["problems"][0])
    release = str(h.get("designer_release") or "")
    key = _analysis_key(job, track)
    with analysis.in_use(key):  # the designer reads the WAV by reference until it answers
        meta = _analyse(job, track, probe, key)
        bm = None
        if job.s.bm_crossover_hz > 0:
            bm = designer.BassManagement(job.s.bm_crossover_hz, job.s.bm_lpf_position,
                                         job.s.bm_headroom)
        body = designer.build_request(meta, bm)
        job.progress(f"Designing (beqforge {release})", None)
        job.log(f"Designer: POST {job.s.designer_url}/design (contract "
                f"{body['contract_version']}, {len(meta['channels'])} channels by reference)")
        t0 = time.monotonic()
        try:
            resp = designer.post_design(job.s.designer_url, body, job.s.designer_timeout,
                                        job.procs.check)
        except designer.DesignerError as exc:
            raise JobFailed(str(exc)) from None
    secs = round(time.monotonic() - t0, 1)
    cands = [designer.candidate_entry(c, resp, release, film["title"], film["year"], i)
             for i, c in enumerate(resp.get("candidates") or [])]
    rejected = [designer.candidate_entry(c, resp, release, film["title"], film["year"], i,
                                         rejected=True)
                for i, c in enumerate(resp.get("rejected") or [])]
    for c in cands:
        job.log(f"Designer candidate {c['label']}: confidence {c['confidence']}, gain "
                f"reduction {c['gain_reduction_db']} dB, " + ", ".join(
                    f"{f['type']} {f['freq']:.2f} Hz {f['gain']:+.2f} dB Q {f['q']:.3f}"
                    for f in c["filters"]))
    if resp.get("decline_reason"):
        job.log(f"Designer declined: {resp['decline_reason']}: "
                f"{resp.get('decline_message') or ''}")
    return {
        "declined": bool(resp.get("decline_reason")),
        "decline_reason": resp.get("decline_reason"),
        "decline_message": resp.get("decline_message"),
        "candidates": cands,
        "rejected": rejected,
        "release": release,
        "design_version": resp.get("design_version"),
        "design_settings": resp.get("design_settings"),
        "designer_build": resp.get("designer_build"),
        "run_record": resp.get("run_record"),
        "adjustment_outcomes": resp.get("adjustment_outcomes"),
        "bass_management": body.get("bass_management"),
        "seconds": secs,
        "analysis": {k: meta[k] for k in ("wav", "fs", "frames", "channels", "layout",
                                          "mapping", "coverage")},
    }


def _track_dict(t: media.AudioTrack) -> dict:
    return {**asdict(t), "label": t.label, "kind_name": t.kind_name, "tier": t.tier,
            "lossless": t.lossless, "objects": t.objects, "layout_label": t.layout_label}
