import struct

import numpy as np


def write_caf(path, x: np.ndarray, bits=24, little_endian=False, sr=48000.0, unknown_size=False):
    """x: float array (frames, channels) in [-1, 1)."""
    frames, ch = x.shape
    fs = 2 ** (bits - 1)
    q = np.clip(np.rint(x * fs), -fs, fs - 1).astype(np.int64)
    e = "<" if little_endian else ">"
    if bits == 24:
        w = (q.reshape(-1).astype(np.int32) << 8).astype(f"{e}i4").view(np.uint8).reshape(-1, 4)
        raw = (w[:, 1:] if little_endian else w[:, :3]).tobytes()
    else:
        raw = q.reshape(-1).astype(f"{e}i{bits // 8}").tobytes()
    flags = 2 if little_endian else 0
    bpf = bits // 8 * ch
    with open(path, "wb") as fh:
        fh.write(b"caff" + struct.pack(">HH", 1, 0))
        fh.write(b"desc" + struct.pack(">q", 32))
        fh.write(struct.pack(">d4sIIIII", sr, b"lpcm", flags, bpf, 1, ch, bits))
        # an unrelated chunk the reader must skip
        fh.write(b"chan" + struct.pack(">q", 12) + struct.pack(">III", 0, 0, 0))
        size = -1 if unknown_size else len(raw) + 4
        fh.write(b"data" + struct.pack(">q", size) + struct.pack(">I", 0))
        fh.write(raw)


def make_signal(frames=48000 * 3, ch=16, seed=1):
    rng = np.random.default_rng(seed)
    t = np.arange(frames) / 48000
    x = np.empty((frames, ch))
    for c in range(ch):
        x[:, c] = 0.25 * np.sin(2 * np.pi * (15 + 3 * c) * t) + 0.05 * rng.standard_normal(frames)
    return x


def write_damf(base, x: np.ndarray, bed=("LFE",)):
    """A DAMF as truehdd writes one: `<base>.atmos` (bed channels, then objects),
    `.atmos.audio` (24-bit big-endian CAF) and `.atmos.metadata` (one event each)."""
    import yaml
    frames, ch = x.shape
    write_caf(f"{base}.atmos.audio", x)
    beds = [{"channel": n, "ID": i} for i, n in enumerate(bed, start=3)]
    objs = [{"ID": 10 + k} for k in range(ch - len(bed))]
    name = str(base).rsplit("/", 1)[-1]
    doc = {"version": "0.5.1", "presentations": [{
        "type": "home", "simplified": False, "metadata": f"{name}.atmos.metadata",
        "audio": f"{name}.atmos.audio", "offset": 0.0, "fps": 24, "scBedConfiguration": [3],
        "creationTool": "truehdd", "bedInstances": [{"channels": beds}], "objects": objs}]}
    with open(f"{base}.atmos", "w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)
    events = [{"ID": b["ID"], "samplePos": 0, "active": True, "gain": 0} for b in beds]
    for k, o in enumerate(objs):
        pos = [round(-1 + 2 * (k % 4) / 3, 4), round(1 - 2 * (k // 4) / 3, 4), 0]
        events.append({"ID": o["ID"], "samplePos": 0, "active": True, "pos": pos,
                       "size": 0.0, "gain": 0})
    with open(f"{base}.atmos.metadata", "w") as fh:
        yaml.safe_dump({"sampleRate": 48000, "events": events}, fh, sort_keys=False)


def read_caf(path) -> np.ndarray:
    from beq_encoder import caf
    info = caf.read_info(str(path))
    with open(path, "rb") as fh:
        fh.seek(info.data_offset)
        return caf.decode(info, fh.read(info.data_bytes))


FRAME = 1536
DEFAULT_POS = [-1.0, 1.0, 0.0]


def move_objects(base, every: int, into_frame: int = 1112) -> None:
    """Give each object of a DAMF a new position every `every` samples, `into_frame`
    samples into a DEE frame (where a disc's updates sit)."""
    import yaml
    meta = f"{base}.atmos.metadata"
    doc = yaml.safe_load(open(meta))
    frames = len(read_caf(f"{base}.atmos.audio"))
    objs = [e["ID"] for e in doc["events"] if "pos" in e]
    for j, t in enumerate(range(every + into_frame, frames, every), start=1):
        for o in objs:
            doc["events"].append({"ID": o, "samplePos": t, "pos": [
                round((o * 7 + j * 3) % 21 / 10 - 1, 4), round((o * 5 + j * 11) % 21 / 10 - 1, 4),
                0.5 * (j % 2)]})
    with open(meta, "w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)


def object_events(meta_path) -> dict[int, list[tuple[int, list]]]:
    """Each element's (samplePos, pos) events in time order (file order at a tie); an
    event without samplePos is at the previous event's time."""
    import yaml
    out: dict[int, list] = {}
    t = 0
    for ev in yaml.safe_load(open(meta_path))["events"]:
        t = int(ev.get("samplePos", t))
        if "pos" in ev:
            out.setdefault(ev["ID"], []).append((t, ev["pos"]))
    for evs in out.values():
        evs.sort(key=lambda e: e[0])
    return out


# The version the fake truehdd prints, and with it how it reads update times (FAKE_TRUEHDD_VERSION)
TRUEHDD_RELEASE = "truehdd 0.6.2 (truehd library 0.7.2)"   # leaves out the block offset
TRUEHDD_EXACT = "truehdd 0.6.3 (truehd library 0.7.3)"     # reads it


def dee_positions(events: list[tuple[int, list]], audio: np.ndarray,
                  at_sample: bool = True) -> list[tuple[int, list]]:
    """The position events DEE 6.7 codes for one object under dee_shim 0.2 --oamd-as-is
    (measured), as truehdd decodes them: one update per 1536-sample frame holding the
    state after the frame's last update, at that update's sample (`at_sample`: a truehdd
    that reads the block offset) or at the start of the frame (truehdd 0.6.2, and DEE's
    own timing before dee_shim 0.2); none while the object is digitally silent, which
    starts it at the default position and sends its current one in the frame before its
    audio resumes."""
    from bisect import bisect_right
    if not events:
        return []
    times = [t for t, _ in events]
    nf = -(-len(audio) // FRAME)
    loud = [bool(np.any(audio[f * FRAME:(f + 1) * FRAME])) for f in range(nf)]
    out: list[tuple[int, list]] = []
    for f in range(nf):
        soon = loud[f] or f + 1 < nf and loud[f + 1]
        if f == 0 and not soon:
            out.append((0, DEFAULT_POS))
            continue
        k = max(0, bisect_right(times, f * FRAME + FRAME - 1) - 1)
        now = events[k][1]
        if soon and (not out or out[-1][1] != now):
            t = times[k] if at_sample and times[k] >= f * FRAME else f * FRAME
            out.append((t, now))
    return out


def fake_dee_atmos(src_atmos: str, out_base: str, mode: str = "") -> None:
    """What DEE 6.7 under dee_shim 0.2 (--as-is --oamd-as-is) encodes from a DAMF, as the
    fake truehdd decodes it back: every element as it is, in master order, padded to 16
    elements with silence, each object's positions as DEE codes them (dee_positions, read
    as the fake truehdd's FAKE_TRUEHDD_VERSION reads them). Broken on request: `drop`
    leaves out the last object and `drop-loud` the first (each has audio), `sample` puts one sample 4 LSB off, `late` codes one
    position update a frame late, `wrong` codes one position wrong, `frame-start` codes
    every update at the start of its frame (DEE's own timing), `reorder` codes the
    objects in reverse order."""
    import os

    import yaml

    from beq_encoder import atmos
    base = src_atmos[: -len(".atmos")]
    x = read_caf(f"{base}.atmos.audio")
    pres = yaml.safe_load(open(f"{base}.atmos"))["presentations"][0]
    nb = sum(len(b["channels"]) for b in pres["bedInstances"])
    ids = [o["ID"] for o in pres["objects"]]
    order = list(range(len(ids)))
    if mode == "reorder":
        order = order[::-1]
    if mode == "drop":  # the quietest after the BEQ, so the programme still lines up
        order = order[:-1]
    if mode == "drop-loud":  # the loudest: the sum of all elements no longer lines up
        order = order[1:]
    at_sample = mode != "frame-start" and atmos.decoder_update_times(
        os.environ.get("FAKE_TRUEHDD_VERSION", TRUEHDD_RELEASE)) == atmos.SAMPLE
    objs = x[:, nb:]
    y = np.concatenate([x[:, :nb], objs[:, order],
                        np.zeros((len(x), 16 - nb - len(order)))], axis=1)
    if mode == "sample":
        y[len(y) // 2, nb] += 4 / 2**23
    write_damf(out_base, y)
    meta = f"{out_base}.atmos.metadata"
    doc = yaml.safe_load(open(meta))
    doc["events"] = [e for e in doc["events"] if "pos" not in e]
    src = object_events(f"{base}.atmos.metadata")
    for k, j in enumerate(order):
        evs = dee_positions(src.get(ids[j], []), objs[:, j], at_sample)
        if k == 0 and mode in ("late", "wrong"):
            i = next(i for i, (t, _) in enumerate(evs) if i and np.any(objs[t:t + FRAME, j]))
            t, p = evs[i]
            moved = [p[0] + (0.1 if p[0] < 0.9 else -0.1), p[1], p[2]]
            evs[i] = (t + FRAME, p) if mode == "late" else (t, moved)
        doc["events"] += [{"ID": 10 + k, "samplePos": t, "pos": p} for t, p in evs]
    with open(meta, "w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False)


def fake_cavern_dump(argv: list[str]) -> int:
    """Stands in for cavern-dump: E-AC-3 -> DAMF as Cavern writes it. The bed (LFE) is
    on the plain decode's timeline, the objects (the five mains, then silent ones, 15 in
    all) FAKE_CAVERN_LAG samples later (Cavern: 577), rendered `extra` samples past the
    end (FAKE_CAVERN_EXTRA overrides it)."""
    import os
    import subprocess
    mode, src = argv[0], argv[1]
    r = subprocess.run(["ffmpeg", "-v", "error", "-drc_scale", "0", "-i", src, "-f", "f64le",
                        "-"], capture_output=True, check=True)
    x = np.frombuffer(r.stdout, "<f8").reshape(-1, 6)  # L R C LFE Ls Rs
    n = len(x)
    print(f"reader=EnhancedAC3Reader channels=6 rate=48000 length={n}", flush=True)
    if mode == "info":
        return 0
    out = argv[2]
    extra = int(os.environ.get("FAKE_CAVERN_EXTRA", argv[4] if len(argv) > 4 else "0"))
    lag = int(os.environ.get("FAKE_CAVERN_LAG", "577"))
    total = n + extra
    y = np.zeros((total, 16))
    y[:n, 0] = x[:, 3]
    objs = x[:, [0, 1, 2, 4, 5]]
    m = min(n, total - lag)
    y[lag:lag + m, 1:6] = objs[:m]
    print(f"progress {total} {total}", flush=True)
    write_damf(out[: -len(".atmos")], y)
    print(f"written {total} samples", flush=True)
    return 0
