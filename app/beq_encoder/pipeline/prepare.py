"""Phase 1 of a job: find the track, then a catalogue match or a design for it."""

from __future__ import annotations

import time
from dataclasses import asdict

from .. import analysis, designer, media, support
from .job import Job, source_pin
from .processes import JobFailed


def run_prepare(job: Job) -> dict:
    p = job.params
    stamp, probe = job.read_source()
    if p.get("track_id") is not None:
        track = probe.track(int(p["track_id"]))
        if track is None:
            raise JobFailed(f"Track {p['track_id']} is not in the file any more; rescan it")
    else:
        track = probe.best
        if track is None:
            raise JobFailed("No usable audio track (all commentary, descriptive, forced or BEQ)")
    names = media.parse_name(job.src)
    film = {"tmdb": names.tmdb, "title": names.title or job.src.stem,
            "year": names.year or "", "edition": names.edition or ""}
    job.log(f"Film: {film}; track #{track.track_id} {track.label} {track.codec} "
            f"lang={track.language} layout={track.layout} objects={track.objects}")
    plan = support.plan(track)
    result: dict = {"phase": "prepare", "film": film, "track": _track_dict(track),
                    "source": source_pin(stamp, track), "catalogue": [], "design": None,
                    "plan": {"ok": plan.ok, "label": plan.label, "native": plan.native,
                             "reason": plan.reason}}
    if not plan.ok:
        job.log(f"Not supported: {plan.reason}")
        result.update(outcome="unsupported", message=f"Not supported: {plan.reason}")
        return result

    job.progress("Matching the BEQ catalogue", None)
    if job.catalogue is not None and not job.catalogue.loaded_at and not p.get("force_designer"):
        raise JobFailed("The BEQ catalogue is not loaded"
                        + (f" ({job.catalogue.error})" if job.catalogue.error else " yet")
                        + ", so this film's entries could not be checked: run it again once "
                          "it has loaded, or ask the designer without it")
    matches = []
    if job.catalogue is not None:
        matches = job.catalogue.for_track(film["tmdb"], film["title"], film["year"],
                                          film["edition"], track.kind, track.layout_label)
    result["catalogue"] = [{"match": m, "entry": e.to_dict()} for m, e in matches]
    exact = [e for m, e in matches if m == "exact"]
    job.log(f"Catalogue: {len(matches)} entries for the film, {len(exact)} for this track "
            f"({track.label}): " + "; ".join(f"{m} {e.label}" for m, e in matches))

    if exact and not p.get("force_designer"):
        if not any(e.filters for e in exact):
            who = ", ".join(sorted({e.author for e in exact}))
            result.update(outcome="no_beq",
                          message=f"Catalogue: this {track.label} track does not need BEQ "
                                  f"({who})")
            return result
        result.update(outcome="awaiting", message=f"{len(exact)} catalogue entr"
                      f"{'y' if len(exact) == 1 else 'ies'} for this track; pick one")
        return result

    result["design"] = d = _design(job, track, probe, film)
    if d["declined"]:
        result.update(outcome="no_beq",
                      message=f"Designer declined: {d['decline_reason']}"
                              + (f" ({d['decline_message']})" if d["decline_message"] else ""))
    else:
        result.update(outcome="awaiting",
                      message=f"{len(d['candidates'])} designer candidate(s); pick one")
    return result


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
    st = job.src.stat()
    key = analysis.cache_key(job.src, st.st_size, st.st_mtime, track.track_id)
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
