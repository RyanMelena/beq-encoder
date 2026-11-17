"""Which source tracks get a BEQ track, and in what format.

The rules:

* No audio data may be lost: the BEQ track carries everything the source track
  decodes to, changed only by the BEQ filter and its uniform gain, and every path
  proves that sample by sample or fails.
* Lossy conversions are out of scope (for now): no lossy re-encode, and no output
  that drops what the source carries, such as rendering objects down to a bed.
  Decoding a lossy core (DD+, AC-3, DTS) and encoding it losslessly is not a lossy
  conversion: every decoded sample is kept and checked.
* The BEQ track is a Dolby bitstream the processor decodes and upmixes, never
  FLAC/PCM: lossless TrueHD, natively where possible.

What that gives today:

  TrueHD Atmos             truehdd -> DAMF -> BEQ -> DEE TrueHD Atmos     (native)
  TrueHD, DTS-HD MA, LPCM, ffmpeg PCM -> BEQ -> DEE TrueHD               (lossless; native
  FLAC, AC-3, DD+, DTS ...  2.0 / 5.1 / 7.1 at 48 kHz                     for TrueHD)
  DD+ Atmos (JOC)          Cavern -> DAMF -> BEQ -> DEE TrueHD Atmos      (lossless from
                           Cavern's decode; refused without cavern-dump)
  DTS:X                    refused: no available decoder gives its objects, and DTS:X
                           output is lossy
  other layouts / rates    refused: DEE's PCM presentations are 2.0/5.1/7.1;
                           other sample rates are not verified with DEE yet

FLAC remains a test-only output behind ALLOW_TEST_FORMATS, for development without
DEE, and only where the TrueHD plan itself is possible (never a bed of an object
track).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import layouts
from .dee import PCM_TRUEHD_CHANNELS, PCM_TRUEHD_LAYOUTS
from .media import AudioTrack

PCM_TRUEHD_RATES = (48000,)
LOSSY_OUT_OF_SCOPE = "lossy conversions are currently out of scope"
CAVERN_DUMP_DEFAULT = "/opt/cavern-dump/cavern-dump"


def cavern_available() -> bool:
    """DD+ Atmos objects can be decoded (cavern-dump is in the image)."""
    p = os.environ.get("CAVERN_DUMP", CAVERN_DUMP_DEFAULT)
    return bool(p) and os.path.isfile(p) and os.access(p, os.X_OK)


@dataclass(frozen=True, slots=True)
class Plan:
    ok: bool
    fmt: str = ""  # truehd (Atmos or channel-based, by source)
    route: str = ""  # damf | joc | pcm
    label: str = ""  # what the BEQ track will be, e.g. "TrueHD Atmos", "TrueHD 5.1"
    native: bool = False
    reason: str = ""  # why not, when not ok, in the pipeline's words: for logs and messages
    said: str = ""  # why not, as one plain sentence: what the screens show


def plan(track: AudioTrack) -> Plan:
    rate = int(track.sample_rate or 48000)
    if rate not in PCM_TRUEHD_RATES:
        return Plan(False, reason=(
            f"{track.label} at {rate / 1000:g} kHz: only 48 kHz is verified with DEE's "
            "TrueHD encoder; resampling would lose data"), said=(
            f"Its sample rate is {rate / 1000:g} kHz, and the app makes a BEQ track only "
            "from 48 kHz audio, because changing the rate would lose audio."))
    if track.atmos:
        return Plan(True, "truehd", "damf", "TrueHD Atmos", native=True)
    if track.kind == "eac3_joc" and cavern_available():
        return Plan(True, "truehd", "joc", "TrueHD Atmos", native=False)
    if track.objects:
        return Plan(False, reason=(
            f"{track.kind_name}: its objects can't be carried into lossless TrueHD Atmos "
            f"without a lossy step, re-encoding to {track.kind_name} would itself be lossy, "
            f"and the {track.layout_label} bed alone would drop the objects; "
            f"{LOSSY_OUT_OF_SCOPE}"), said=(
            f"The app can't make a BEQ track from {track.kind_name} without losing some of "
            "its audio, and it makes only lossless BEQ tracks."))
    ch = int(track.channels or 0)
    if ch not in PCM_TRUEHD_CHANNELS:
        return Plan(False, reason=(
            f"{track.label}: TrueHD from PCM is made as 2.0, 5.1 or 7.1 only; another "
            "layout would change the channels"), said=(
            f"It has {ch} channels, and the app makes a BEQ track only from 2.0, 5.1 or "
            "7.1 audio."))
    if layouts.names(ch, track.layout) is None:
        return Plan(False, reason=(
            f"{track.label}: its channel layout is unknown; encoding it by position "
            "would relabel its speakers"), said=(
            "The file does not say which speaker each of its channels is for, so a BEQ "
            "track could put them on the wrong speakers."))
    if track.layout not in PCM_TRUEHD_LAYOUTS[ch]:
        return Plan(False, reason=(
            f"{track.label} ({track.layout}): DEE's TrueHD encoder takes "
            f"{' or '.join(PCM_TRUEHD_LAYOUTS[ch])} for {ch} channels; this layout's "
            "speakers would be relabelled"), said=(
            f"Its speaker layout is {track.layout}, which a BEQ track with {ch} channels "
            "can't keep."))
    return Plan(True, "truehd", "pcm", f"TrueHD {track.layout_label}",
                native=track.kind == "truehd")


def formats(track: AudioTrack, allow_test: bool) -> list[str]:
    """Output formats offered for a track: the plan's, plus test-only FLAC (only where
    the plan itself is possible, so FLAC never stands in for a refused track)."""
    p = plan(track)
    if not p.ok:
        return []
    out = [p.fmt]
    if allow_test and p.route == "pcm":
        out.append("flac")
    return out
