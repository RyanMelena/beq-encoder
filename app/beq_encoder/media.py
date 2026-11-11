"""Probe MKV files: tracks (mkvmerge -J), layouts (ffprobe), object audio (MediaInfo), naming.

Every audio track gets a `kind` (truehd_atmos, dtshd_ma, eac3_joc, ac3, ...) and a
tier; `pick_best` chooses the track a BEQ is made from:

  0  lossless with objects   TrueHD Atmos, DTS:X
  1  lossless channel-based  TrueHD, DTS-HD MA, LPCM, FLAC, ALAC
  2  lossy with objects      DD+ Atmos (E-AC-3 JOC)
  3  lossy                   DD+, DTS core / HRA / ES / Express, AC-3, AAC, Opus, ...

within a tier: more channels, then the original-language flag, then the default
flag, then file order. Commentary, descriptive-audio, forced and BEQ tracks are
never picked automatically.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from pymediainfo import MediaInfo

from . import layouts

TOOL_TIMEOUT_S = 300

ATMOS_COMMERCIAL = "Dolby TrueHD with Dolby Atmos"
BEQ_NAME_RE = re.compile(r"\bBEQ\b", re.I)
EXTRA_NAME_RE = re.compile(
    r"commentary|descriptive|audio description|described video|\bAD\b|narrat", re.I
)
TMDB_RES = [
    re.compile(r"\{tmdb-(\d+)\}", re.I),
    re.compile(r"\[tmdbid[-=](\d+)\]", re.I),
    re.compile(r"\btmdbid[-=](\d+)", re.I),
    re.compile(r"\btmdb[-=](\d+)", re.I),
]

# kind -> (tier, lossless, objects, display name)
KINDS: dict[str, tuple[int, bool, bool, str]] = {
    "truehd_atmos": (0, True, True, "TrueHD Atmos"),
    "dtsx": (0, True, True, "DTS:X"),
    "truehd": (1, True, False, "TrueHD"),
    "dtshd_ma": (1, True, False, "DTS-HD MA"),
    "pcm": (1, True, False, "LPCM"),
    "flac": (1, True, False, "FLAC"),
    "alac": (1, True, False, "ALAC"),
    "eac3_joc": (2, False, True, "DD+ Atmos"),
    "eac3": (3, False, False, "DD+"),
    "dtshd_hra": (3, False, False, "DTS-HD HRA"),
    "dts": (3, False, False, "DTS"),
    "ac3": (3, False, False, "AC-3"),
    "aac": (3, False, False, "AAC"),
    "opus": (3, False, False, "Opus"),
    "vorbis": (3, False, False, "Vorbis"),
    "mp3": (3, False, False, "MP3"),
    "other": (4, False, False, "audio"),
}


@dataclass(slots=True)
class AudioTrack:
    track_id: int  # mkvmerge track id (0-based, all tracks)
    audio_index: int  # index among audio tracks (ffmpeg 0:a:N)
    codec: str
    language: str
    name: str
    default: bool
    channels: int | None
    atmos: bool  # TrueHD Atmos: decodable to DAMF by truehdd
    beq: bool
    delay_ms: int = 0
    kind: str = "other"
    layout: str | None = None  # ffmpeg channel layout name, e.g. "7.1", "5.1(side)"
    profile: str = ""
    original: bool = False
    commentary: bool = False
    descriptive: bool = False
    forced: bool = False
    layout_assumed: bool = False  # no layout in the stream: the default for its count
    sample_rate: int | None = None
    beq_tags: dict = field(default_factory=dict)  # BEQ_* track tags, lower-case keys

    @property
    def tier(self) -> int:
        return KINDS.get(self.kind, KINDS["other"])[0]

    @property
    def lossless(self) -> bool:
        return KINDS.get(self.kind, KINDS["other"])[1]

    @property
    def objects(self) -> bool:
        return KINDS.get(self.kind, KINDS["other"])[2]

    @property
    def kind_name(self) -> str:
        return KINDS.get(self.kind, KINDS["other"])[3]

    @property
    def layout_label(self) -> str:
        """"7.1", "5.1", "2.0" ..."""
        return layouts.label(self.channels or 0, self.layout)

    @property
    def label(self) -> str:
        return f"{self.kind_name} {self.layout_label}"

    @property
    def extra(self) -> bool:
        """Commentary / descriptive / forced: never a BEQ source by default."""
        return self.commentary or self.descriptive or self.forced

    @property
    def pickable(self) -> bool:
        return not self.beq and not self.extra and bool(self.channels)


@dataclass(slots=True)
class ProbeResult:
    path: str
    duration_s: float | None
    tracks: list[dict]  # all tracks, compact form, in file order
    audio: list[AudioTrack] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)  # id, file_name, content_type, ...

    @property
    def beq_attachments(self) -> list[dict]:
        """Attachments this encoder wrote (charts, report): replaced with the BEQ track."""
        return [a for a in self.attachments if str(a.get("file_name", "")).startswith("beq-")]

    @property
    def atmos_tracks(self) -> list[AudioTrack]:
        return [a for a in self.audio if a.atmos]

    @property
    def beq_tracks(self) -> list[AudioTrack]:
        return [a for a in self.audio if a.beq]

    @property
    def best(self) -> AudioTrack | None:
        return pick_best(self.audio)

    def track(self, track_id: int) -> AudioTrack | None:
        return next((a for a in self.audio if a.track_id == track_id), None)

    def to_json(self) -> str:
        return json.dumps(
            {
                "path": self.path,
                "duration_s": self.duration_s,
                "tracks": self.tracks,
                "audio": [asdict(a) for a in self.audio],
                "attachments": self.attachments,
            }
        )

    @classmethod
    def from_json(cls, s: str) -> ProbeResult:
        d = json.loads(s)
        known = {f.name for f in fields(AudioTrack)}
        return cls(
            path=d["path"],
            duration_s=d["duration_s"],
            tracks=d["tracks"],
            audio=[AudioTrack(**{k: v for k, v in a.items() if k in known}) for a in d["audio"]],
            attachments=d.get("attachments") or [],
        )


def rank_key(a: AudioTrack) -> tuple:
    return (a.tier, -(a.channels or 0), not a.original, not a.default, a.audio_index)


def pick_best(audio: list[AudioTrack]) -> AudioTrack | None:
    """The track to BEQ: best tier, then channels, original language, default flag."""
    cands = [a for a in audio if a.pickable]
    return min(cands, key=rank_key) if cands else None


def classify(mkv_codec: str, ff_codec: str, ff_profile: str, commercial: str) -> str:
    c, f, p, m = mkv_codec.lower(), ff_codec.lower(), ff_profile.lower(), commercial.lower()
    if commercial == ATMOS_COMMERCIAL or ("truehd" in c and "atmos" in c) or (
        f == "truehd" and "atmos" in p
    ):
        return "truehd_atmos"
    if "truehd" in c or f in ("truehd", "mlp"):
        return "truehd"
    if f == "dts" or c.startswith("dts"):
        if "dts:x" in p or "dts:x" in m or "dts-x" in m or "xll x" in m:
            return "dtsx"
        if "ma" in p.split() or "master audio" in c or "master audio" in m or p == "dts-hd ma":
            return "dtshd_ma"
        if "hra" in p or "high resolution" in c or "high resolution" in m:
            return "dtshd_hra"
        return "dts"
    if f == "eac3" or "e-ac-3" in c or "e-ac3" in c:
        if "atmos" in p or "atmos" in m or "joc" in p or "joc" in m:
            return "eac3_joc"
        return "eac3"
    if f == "ac3" or c in ("ac-3", "ac3"):
        return "ac3"
    if f.startswith("pcm") or c.startswith("pcm"):
        return "pcm"
    for k in ("flac", "alac", "aac", "opus", "vorbis", "mp3"):
        if f == k or c.startswith(k):
            return k
    return "other"


Popen = Callable[..., subprocess.Popen]


def _is_truehd(mkv_codec: str | None) -> bool:
    return "truehd" in (mkv_codec or "").lower()


def _tool_output(cmd: list[str], popen: Popen) -> subprocess.CompletedProcess:
    proc = popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True)
    try:
        out, err = proc.communicate(timeout=TOOL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def mkvmerge_identify(mkvmerge: str, path: str, popen: Popen = subprocess.Popen) -> dict:
    out = _tool_output([mkvmerge, "-J", path], popen)
    if out.returncode not in (0, 1):  # 1 = warnings
        raise RuntimeError(f"mkvmerge -J failed: {out.stderr or out.stdout}")
    return json.loads(out.stdout)


def ffprobe_audio(ffprobe: str, path: str, popen: Popen = subprocess.Popen) -> list[dict]:
    """codec_name / profile / channels / channel_layout per audio stream, in order."""
    out = _tool_output(
        [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_name,profile,channels,channel_layout,sample_rate", "-of", "json", path],
        popen,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr or out.stdout}")
    return json.loads(out.stdout).get("streams", [])


def probe(path: str, mkvmerge: str = "mkvmerge", ffprobe: str = "ffprobe",
          popen: Popen = subprocess.Popen) -> ProbeResult:
    """`popen` starts the tools; a job passes its own, so its Cancel stops them."""
    ident = mkvmerge_identify(mkvmerge, path, popen)
    if not ident.get("container", {}).get("recognized"):
        raise RuntimeError("mkvmerge did not recognise the container")
    dur_ns = ident.get("container", {}).get("properties", {}).get("duration")
    mkv_audio = [t for t in ident.get("tracks", []) if t.get("type") == "audio"]
    ff = ffprobe_audio(ffprobe, path, popen)
    if len(ff) != len(mkv_audio):  # the streams are paired with mkvmerge's tracks by position
        raise RuntimeError(f"ffprobe lists {len(ff)} audio streams, mkvmerge {len(mkv_audio)}")

    # MediaInfo is far slower than mkvmerge -J (it reads much more of the file, which
    # hurts over SMB). ffprobe's profile already names DTS:X ("DTS-HD MA + DTS:X") and
    # DD+ JOC ("Dolby Digital Plus + Dolby Atmos"); MediaInfo is still asked about
    # TrueHD, for Atmos and the track delay.
    need_mi = any(_is_truehd(t.get("codec")) for t in mkv_audio)
    mi_audio = {}
    for t in (MediaInfo.parse(path).audio_tracks if need_mi else []):
        try:
            mi_audio[int(t.track_id)] = t
        except (TypeError, ValueError):
            continue

    tracks: list[dict] = []
    audio: list[AudioTrack] = []
    for t in ident.get("tracks", []):
        p = t.get("properties", {})
        lang = p.get("language_ietf") or p.get("language") or "und"
        name = p.get("track_name") or ""
        tracks.append(
            {
                "id": t["id"],
                "type": t["type"],
                "codec": t.get("codec", ""),
                "language": lang,
                "name": name,
                "default": bool(p.get("default_track")),
                "channels": p.get("audio_channels"),
            }
        )
        if t["type"] != "audio":
            continue
        fs = ff[len(audio)]
        codec = t.get("codec", "")
        # only for a TrueHD track, so what a track reports never depends on the file's others
        mit = mi_audio.get(int(p.get("number", -1))) if _is_truehd(codec) else None
        commercial = getattr(mit, "commercial_name", "") or ""
        mi_text = " ".join(
            str(getattr(mit, k, "") or "") for k in ("commercial_name", "format", "format_additionalfeatures")
        ) if mit is not None else ""
        kind = classify(codec, fs.get("codec_name") or "", fs.get("profile") or "", commercial)
        if kind != "truehd_atmos":
            kind = classify(codec, fs.get("codec_name") or "", fs.get("profile") or "", mi_text) \
                if mi_text.strip() else kind
        delay = 0
        if mit is not None:
            for fld in ("delay_relative_to_video", "source_delay"):
                v = getattr(mit, fld, None)
                if v not in (None, ""):
                    try:
                        delay = int(float(v))
                        break
                    except (TypeError, ValueError):
                        pass
        channels = _mi_channels(mit) or fs.get("channels") or p.get("audio_channels")
        ff_ch = int(fs.get("channels") or 0)
        layout = fs.get("channel_layout") or None
        if layout and layouts.names(ff_ch, layout) is None:
            layout = None
        assumed = False
        if layout is None and ff_ch and (ff_ch <= 2 or kind in ("pcm", "flac", "alac")):
            # PCM in Matroska states only a count; its order is the WAV default for it
            layout, assumed = layouts.DEFAULT_FOR_COUNT.get(ff_ch), True
        audio.append(
            AudioTrack(
                track_id=t["id"],
                audio_index=len(audio),
                codec=commercial or codec,
                language=lang,
                name=name,
                default=bool(p.get("default_track")),
                channels=channels,
                atmos=kind == "truehd_atmos",
                beq=bool(BEQ_NAME_RE.search(name)),
                beq_tags={k[8:]: v for k, v in p.items() if k.startswith("tag_beq_")},
                delay_ms=delay,
                kind=kind,
                layout=layout,
                profile=fs.get("profile") or "",
                original=bool(p.get("flag_original")),
                commentary=bool(p.get("flag_commentary"))
                or bool(re.search(r"commentary", name, re.I)),
                descriptive=bool(p.get("flag_visual_impaired"))
                or bool(EXTRA_NAME_RE.search(name) and not re.search(r"commentary", name, re.I)),
                forced=bool(p.get("forced_track")),
                layout_assumed=assumed,
                sample_rate=int(fs.get("sample_rate") or p.get("audio_sampling_frequency") or 0)
                or None,
            )
        )
    return ProbeResult(
        path=path,
        duration_s=(dur_ns / 1e9) if dur_ns else None,
        tracks=tracks,
        audio=audio,
        attachments=[{"id": a.get("id"), "file_name": a.get("file_name", ""),
                      "content_type": a.get("content_type", ""),
                      "description": a.get("description", ""), "size": a.get("size")}
                     for a in ident.get("attachments", [])],
    )


def _mi_channels(mit) -> int | None:
    """MediaInfo reports the top presentation (mkvmerge may report a TrueHD downmix)."""
    v = getattr(mit, "channel_s", None) if mit is not None else None
    try:
        return int(str(v).split("/")[0].strip()) if v else None
    except ValueError:
        return None


@dataclass(slots=True)
class NameInfo:
    tmdb: str | None
    title: str | None
    year: str | None
    edition: str | None


def parse_name(path: Path) -> NameInfo:
    tmdb = None
    for rx in TMDB_RES:
        m = rx.search(str(path))
        if m:
            tmdb = m.group(1)
            break
    title = year = edition = None
    # Radarr/Jellyfin style folder: "Title (Year) {tmdb-123}" — exact, so try it first
    m = re.match(r"^(.+?)\s*\(((?:19|20)\d\d)\)", path.parent.name)
    if m:
        title, year = m.group(1).strip(), m.group(2)
    try:
        from guessit import guessit

        gf = guessit(path.name, {"type": "movie"})
        ed = gf.get("edition")
        if ed:
            edition = " ".join(ed) if isinstance(ed, list) else str(ed)
        if not title:
            gp = guessit(path.parent.name, {"type": "movie"})
            g = gp if gp.get("title") and gp.get("year") else gf
            title = str(g["title"]) if g.get("title") else None
            year = str(g["year"]) if g.get("year") else None
    except Exception:
        pass
    return NameInfo(tmdb=tmdb, title=title, year=year, edition=edition)


def dialnorm(path: str, audio_index: int = 0) -> int | None:
    """The dialogue level (dialnorm, dBFS) of an AC-3 / E-AC-3 track, from MediaInfo."""
    tracks = MediaInfo.parse(path).audio_tracks
    if audio_index >= len(tracks):
        return None
    t = tracks[audio_index]
    for key in ("dialnorm", "dialnorm_average"):
        v = getattr(t, key, None)
        if v not in (None, ""):
            try:
                return int(round(float(str(v).split("/")[0].split()[0])))
            except ValueError:
                continue
    return None
