"""The Dolby Encoding Engine 6.7 TrueHD encoder, run through dee_shim.

`dee_dthd_encoder` is DEE 6.7's TrueHD (and TrueHD Atmos) encoder as built for Linux by
dthd-mac2linux (DEE ships it for macOS only). dee_shim runs it with switches its own
command line lacks, and those are what make the encode sample-exact:

  --as-is on       every sample coded as read (without it DEE changes samples above
                   0.75 FS by 1 LSB, and re-clusters Atmos objects)
  --oamd-as-is on  Atmos object metadata coded as authored (Atmos input only)
  --drc none       no dynamic range compression profile on any presentation

Only lossless TrueHD is made: DEE's DD+ / AC-4 encoders are lossy, and lossy
conversions are out of scope.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ENCODER = "dee_dthd_encoder"
SHIM_DEFAULT = "/usr/local/bin/dee_shim"
SHIM_SWITCHES = ("--as-is", "on", "--drc", "none")
DEFAULT_DIALNORM = -31  # no dialogue normalisation (DTS, PCM and FLAC sources carry none)
# The 2-channel downmix presentation every TrueHD stream has, with DRC off by default
DRC_OFF_2CH = "2ch:drc_default_on=0"


@dataclass(frozen=True, slots=True)
class Input:
    """What DEE is given and the presentation that carries it as it is."""
    format: str                  # DEE's --input-format
    presentation: str
    shim: tuple[str, ...] = ()   # dee_shim switches this input needs beyond SHIM_SWITCHES


ATMOS_INPUT = Input("atmos_mezz", "atmos:number_of_elements=16:legacy_authoring=0",
                    ("--oamd-as-is", "on"))
PCM_INPUTS = {2: Input("wav", DRC_OFF_2CH), 6: Input("wav", "6ch"), 8: Input("wav", "8ch")}
PCM_TRUEHD_CHANNELS = tuple(PCM_INPUTS)
# layouts DEE's PCM presentations take as they are, by channel count
PCM_TRUEHD_LAYOUTS = {2: ("stereo",), 6: ("5.1", "5.1(side)"), 8: ("7.1",)}
# DEE reads a WAV's channels by position in Dolby order (L R C LFE Ls Rs Lrs Rrs for 7.1)
# and ignores the channel mask, whose 7.1 order (ffmpeg's too) puts the back pair before
# the side pair: without this the side and rear surrounds of every 7.1 track swap.
# DeeZy remaps the same way (pan=7.1|...|c4=c6|c5=c7|c6=c4|c7=c5).
WAV_CHANNEL_ORDER = {8: [0, 1, 2, 3, 6, 7, 4, 5]}

# DEE can print the licence's feature list; never show it in logs or the UI
_LICENCE_RE = re.compile(r"licen[cs]e|feature|serial|activation|node.?lock|hardware.?id", re.I)
_TOKEN_RE = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{6,}\b")
_ERROR_RE = re.compile(r"^\s*(?:\[[^]]*\]\s*)?(?:dee_shim )?ERROR")

# The result of actually running the encoder once ({key: (when, problem or None)})
_PROBES: dict[str, tuple[float, str | None]] = {}
_PROBE_LOCK = threading.Lock()
PROBE_TTL = 3600.0

STEP_RE = re.compile(r"\bStep (\d+)/(\d+) : (.+?)\s*$")
PERCENT_RE = re.compile(r"^\[[=> ]*\]\s*(\d+(?:\.\d+)?)\s*%")
SUMMARY_START = "---- Summary of errors and warnings ----"
SHIM_ERROR = "dee_shim ERROR:"


@dataclass(slots=True)
class Dee:
    folder: Path  # the DEE 6.7 tools, dee_dthd_encoder among them
    shim: Path = Path(SHIM_DEFAULT)

    @property
    def encoder(self) -> Path:
        return self.folder / ENCODER

    def command(self, inp: Input, source: Path, output: Path, temp: Path,
                dialnorm: int = DEFAULT_DIALNORM) -> list[str]:
        """`dialnorm`: the source's dialogue level (-31..-1 dBFS), written as is instead
        of DEE measuring its own (which would change the playback level on a decoder
        that applies dialnorm). -31 means no attenuation."""
        dn = max(-31, min(-1, int(dialnorm)))
        downmix = [] if inp.presentation.startswith("2ch") else ["--presentation", DRC_OFF_2CH]
        return [str(self.shim), "dthd_encoder", "--binary", str(self.encoder),
                *SHIM_SWITCHES, *inp.shim, "--",
                *downmix, "--presentation", inp.presentation,
                "--input-format", inp.format, "-i", str(source),
                "--loudness-management", f"skip:dialnorm={dn}",
                "--progress", "1", "--overwrite", "1",
                "-o", str(output), "--temp-dir", str(temp)]

    def status(self) -> dict:
        st: dict = {"path": str(self.folder), "shim": str(self.shim), "ok": False,
                    "problems": []}
        if not self.folder.is_dir():
            st["problems"].append(f"DEE not found at {self.folder} (set DEE_PATH in .env to "
                                  f"the DEE 6.7 folder holding {ENCODER})")
        elif not self.encoder.is_file():
            st["problems"].append(f"No {ENCODER} in {self.folder}: the TrueHD encoder is the "
                                  "dthd-mac2linux build of DEE 6.7's")
        if not self.shim.is_file():
            st["problems"].append(f"dee_shim not found at {self.shim} (DEE_SHIM)")
        if not st["problems"]:
            ran = self.probe(run=False)
            if ran:
                st["problems"].append(ran)
        st["ok"] = not st["problems"]
        return st

    def probe(self, run: bool = True, timeout: float = 120.0) -> str | None:
        """Run the encoder's help through dee_shim with every switch the encodes use, once,
        and report a problem if that pair does not run (cached). dee_shim refuses a DEE
        build it cannot patch before the encoder starts.

        With run=False the last result is returned whatever its age (cheap, for page
        loads), so a failure stays visible until a later probe succeeds."""
        key = f"{self.folder}|{self.shim}"
        with _PROBE_LOCK:
            hit = _PROBES.get(key)
        if hit and (not run or time.time() - hit[0] < PROBE_TTL):
            return hit[1]
        if not run:
            return None
        cmd = [str(self.shim), "dthd_encoder", "--binary", str(self.encoder), *SHIM_SWITCHES,
               *ATMOS_INPUT.shim, "--", "--help"]
        problem: str | None = None
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                                 timeout=timeout, stdin=subprocess.DEVNULL)
            text = (out.stdout or "") + (out.stderr or "")
            if out.returncode != 0 or "Dolby TrueHD" not in text:
                said = [ln.strip() for ln in text.splitlines() if _ERROR_RE.match(ln)][:1]
                problem = (f"The TrueHD encoder did not run under dee_shim (exit "
                           f"{out.returncode}{': ' + redact(said[0]) if said else ''}): "
                           "check DEE_PATH points at DEE 6.7 with the dthd-mac2linux "
                           f"{ENCODER}")
        except Exception as exc:  # a probe that cannot finish is a failed probe, retried later
            problem = f"The TrueHD encoder did not run: {exc}"
        with _PROBE_LOCK:
            _PROBES[key] = (time.time(), problem)
        return problem

    def reprobe_while_failing(self) -> None:
        """For a background thread: keep trying a DEE that failed its probe, so one that is
        fixed in its mounted folder needs no restart to be used."""
        while self.probe(run=False):
            time.sleep(PROBE_TTL)
            self.probe()


def redact(line: str) -> str:
    """Mask licence details in a line of DEE output (keeps ERROR text readable)."""
    if _LICENCE_RE.search(line):
        return _TOKEN_RE.sub("[redacted]", line) if _ERROR_RE.match(line) \
            else "[DEE licence details redacted]"
    return line


def summary_errors(lines: list[str]) -> list[str]:
    """ERROR lines from DEE's end-of-job summary and dee_shim's own (DEE can exit 0 on
    some failures)."""
    out, inside = [], False
    for ln in lines:
        s = ln.strip()
        if SUMMARY_START in s:
            inside = True
        elif s.startswith(SHIM_ERROR) or inside and s.startswith("ERROR"):
            out.append(s)
    return out or [ln.strip() for ln in lines if _ERROR_RE.match(ln)][-5:]
