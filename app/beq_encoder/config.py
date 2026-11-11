"""Runtime settings, all from environment variables."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from pathlib import Path

from .dee import SHIM_DEFAULT, Dee
from .support import CAVERN_DUMP_DEFAULT


def _flag(name: str, default: bool) -> bool:
    """A yes/no setting; an empty or unrecognised value leaves the default."""
    v = (os.environ.get(name) or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    return False if v in ("0", "false", "no", "off") else default


@dataclass(slots=True)
class Settings:
    media_roots: list[Path] = field(default_factory=list)
    config_dir: Path = Path("/config")
    work_dir: Path = Path("/work")
    dee_path: str = "/opt/dee"
    dee_shim: str = SHIM_DEFAULT
    truehdd_path: str = "/usr/local/bin/truehdd"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    mkvmerge: str = "mkvmerge"
    mkvpropedit: str = "mkvpropedit"
    mkvextract: str = "mkvextract"
    cavern_dump: str = CAVERN_DUMP_DEFAULT  # DD+ Atmos objects (Dockerfile)
    charts: bool = True  # before/after charts + report for every BEQ track
    scan_on_start: bool = True
    scan_workers: int = 4               # files probed at once during a scan
    scan_interval_hours: float = 24.0   # 0 = no scheduled scans
    watch: str = "inotify"              # inotify | poll | off
    watch_poll_seconds: int = 60
    watch_settle_seconds: int = 60      # a new file's size must hold this long
    assess_audio: bool = True           # background roll-off screen of each film
    catalogue_url: str = (
        "https://raw.githubusercontent.com/3ll3d00d/beqcatalogue/master/docs/database.json"
    )
    catalogue_max_age_days: float = 1.0  # re-download older than this (stale-BEQ checks)
    max_concurrent_jobs: int = 1
    default_format: str = "truehd"  # the only production output (Atmos or channel-based)
    allow_test_formats: bool = False  # offer FLAC (not a bitstream) for PCM routes: tests only
    default_target_peak_dbfs: float = -1.0
    keep_work: bool = False
    beq_default_flag: bool = False
    track_name_template: str = "BEQ {codec} ({source})"
    designer_url: str = "http://beqforge-designer:8420"
    designer_timeout: float = 1800.0
    analysis_cache_gb: float = 20.0
    bm_crossover_hz: float = 80.0  # 0 = send no bass_management (no gain_reduction_db back)
    bm_lpf_position: str = "Before"
    bm_headroom: str = "WCS"

    @property
    def db_path(self) -> Path:
        return self.config_dir / "beq-encoder.sqlite3"

    @property
    def catalogue_path(self) -> Path:
        return self.config_dir / "catalogue" / "database.json"

    def dee(self) -> Dee:
        return Dee(Path(self.dee_path), Path(self.dee_shim))

    @property
    def log_dir(self) -> Path:
        return self.config_dir / "logs"

    @property
    def charts_dir(self) -> Path:
        return self.config_dir / "charts"


def _check_track_name_template(template: str) -> None:
    """A bad placeholder should stop the service here, not a job hours in."""
    try:
        template.format(orig="", codec="", bitrate="", author="", source="")
    except (IndexError, KeyError, ValueError) as exc:
        raise ValueError(f"TRACK_NAME_TEMPLATE {template!r} is not valid: {exc!r}") from exc


def load() -> Settings:
    s = Settings()
    roots = os.environ.get("MEDIA_ROOTS", "/media")
    s.media_roots = [Path(p) for p in roots.split(":") if p.strip()]
    s.config_dir = Path(os.environ.get("CONFIG_DIR", "/config"))
    s.work_dir = Path(os.environ.get("WORK_DIR", "/work"))
    s.dee_path = os.environ.get("DEE_PATH", "/opt/dee")
    s.dee_shim = os.environ.get("DEE_SHIM", SHIM_DEFAULT)
    s.truehdd_path = os.environ.get(
        "TRUEHDD_PATH", shutil.which("truehdd") or "/usr/local/bin/truehdd"
    )
    s.ffmpeg = os.environ.get("FFMPEG_PATH", "ffmpeg")
    s.ffprobe = os.environ.get("FFPROBE_PATH", "ffprobe")
    s.mkvmerge = os.environ.get("MKVMERGE_PATH", "mkvmerge")
    s.mkvpropedit = os.environ.get("MKVPROPEDIT_PATH", "mkvpropedit")
    s.mkvextract = os.environ.get("MKVEXTRACT_PATH", "mkvextract")
    s.cavern_dump = os.environ.get("CAVERN_DUMP", s.cavern_dump)
    s.charts = _flag("BEQ_CHARTS", True)
    s.scan_on_start = _flag("SCAN_ON_START", True)
    s.scan_workers = max(1, int(os.environ.get("SCAN_WORKERS", "4")))
    s.scan_interval_hours = float(os.environ.get("SCAN_INTERVAL_HOURS", "24"))
    s.watch = os.environ.get("WATCH", "inotify").strip().lower()
    if s.watch not in ("inotify", "poll", "off"):
        s.watch = "inotify"
    s.watch_poll_seconds = max(5, int(os.environ.get("WATCH_POLL_SECONDS", "60")))
    s.watch_settle_seconds = max(0, int(os.environ.get("WATCH_SETTLE_SECONDS", "60")))
    s.assess_audio = _flag("ASSESS_AUDIO", True)
    s.catalogue_url = os.environ.get("CATALOGUE_URL", s.catalogue_url)
    s.catalogue_max_age_days = float(os.environ.get("CATALOGUE_MAX_AGE_DAYS", "1"))
    s.max_concurrent_jobs = max(1, int(os.environ.get("MAX_CONCURRENT_JOBS", "1")))
    s.default_format = os.environ.get("DEFAULT_FORMAT", "truehd")
    s.allow_test_formats = _flag("ALLOW_TEST_FORMATS", False)
    s.default_target_peak_dbfs = float(os.environ.get("TARGET_PEAK_DBFS", "-1.0"))
    s.keep_work = _flag("KEEP_WORK", False)
    s.beq_default_flag = _flag("BEQ_TRACK_DEFAULT", False)
    s.track_name_template = os.environ.get("TRACK_NAME_TEMPLATE", s.track_name_template)
    _check_track_name_template(s.track_name_template)
    s.designer_url = os.environ.get("DESIGNER_URL", s.designer_url).rstrip("/")
    s.designer_timeout = float(os.environ.get("DESIGNER_TIMEOUT", "1800"))
    s.analysis_cache_gb = float(os.environ.get("ANALYSIS_CACHE_GB", "20"))
    s.bm_crossover_hz = float(os.environ.get("BM_CROSSOVER_HZ", "80"))
    s.bm_lpf_position = os.environ.get("BM_LPF_POSITION", "Before")
    s.bm_headroom = os.environ.get("BM_HEADROOM", "WCS")
    for d in (s.config_dir, s.work_dir, s.log_dir, s.catalogue_path.parent, s.charts_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s

