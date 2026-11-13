"""End-to-end JobRunner tests with a fake truehdd and a fake DEE.

ffmpeg can't make a TrueHD *Atmos* stream, so plain TrueHD is treated as
Atmos by patching the MediaInfo commercial name the probe looks for. The fake
truehdd "decodes" a source bitstream to a fixed DAMF (LFE + 11 objects). The fake
DEE writes a real TrueHD stream (the source's audio re-encoded by ffmpeg,
optionally delayed) so the A/V sync check has content to correlate, and next to it
the DAMF a real DEE would produce (objects reordered and re-quantised, padded to
16 elements; or broken on request), which the fake truehdd returns when asked to
decode that output.
"""

import ast
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from beq_encoder import config, dee, media, pcmio, synccheck
from beq_encoder.beqfilter import FilterJob, apply, filter_stream
from beq_encoder.biquad import FilterSpec, design_sos
from beq_encoder.db import DB
from beq_encoder.pipeline import (Cancelled, FileStamp, JobFailed, JobRunner, source_pin, temp_mkv,
                                  tools)
from beq_encoder.pipeline.processes import Processes
from beq_encoder.pipeline.route_pcm import block_writer
from beq_encoder.worker import JobQueue, Scanner

from .helpers import TRUEHDD_EXACT, TRUEHDD_RELEASE, make_signal, read_caf, write_caf

HERE = Path(__file__).parent
pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("mkvmerge")), reason="needs ffmpeg + mkvtoolnix"
)

FAKE_SHIM = r"""#!@PY@
import os, sys
a = sys.argv[1:]


def usage(msg):
    print('dee_shim ERROR: ' + msg)
    sys.exit(2)


if not a or a[0] != 'dthd_encoder':
    usage('unknown tool')
if '--' not in a:
    usage('the tool arguments follow --')
switches, rest = a[1:a.index('--')], a[a.index('--') + 1:]
if len(switches) % 2:
    usage('a switch without its value')
opts = dict(zip(switches[::2], switches[1::2]))
values = {'--binary': None, '--as-is': ('on', 'off'), '--oamd-as-is': ('on', 'off'),
          '--drc': ('none', 'film_standard', 'film_light', 'music_standard', 'music_light',
                    'speech')}
for k, v in opts.items():
    if k not in values or values[k] and v not in values[k]:
        usage(f'{k} {v}')
binary = opts.get('--binary') or os.path.join(os.path.dirname(sys.argv[0]), 'dee_dthd_encoder')
if not os.path.isfile(binary):
    usage(f'optional capability preflight: {binary}: No such file or directory (os error 2)')
if '--help' not in rest and os.environ.get('FAKE_DEE_ARGS'):
    open(os.environ['FAKE_DEE_ARGS'], 'w').write(repr(a))
os.environ['FAKE_SHIM'] = repr(opts)
os.execv(binary, [binary, *rest])
"""

FAKE_ENCODER = r"""#!@PY@
import ast, os, re, subprocess, sys
sys.path.insert(0, @HERE@)
a = sys.argv[1:]
if '--help' in a:
    print('Dolby TrueHD with Dolby Atmos encoder.')
    sys.exit(0)


def fail(msg):
    print('[2026-01-01 00:00:00.000] ERROR: ' + msg)
    print('---- Summary of errors and warnings ----')
    print('ERROR: ' + msg)
    print('----------------------------------------')
    sys.exit(1)


# as DEE: -f and -i belong to the presentation before them; every option takes one value
pres, opts = [], {}
for k, v in zip(a[::2], a[1::2]):
    if k == '--presentation':
        pres.append({'name': v})
    elif k in ('-f', '--input-format', '-i', '--input'):
        if not pres:
            fail(k + ' needs a presentation before it')
        pres[-1]['format' if 'f' in k else 'input'] = v
    elif k in ('-o', '--temp-dir', '--loudness-management', '--progress', '--overwrite'):
        opts[k] = v
    else:
        fail('unknown option ' + k)
fed = [p for p in pres if 'input' in p]
if len(a) % 2 or len(fed) != 1 or '-o' not in opts:
    fail('one presentation takes the input, and there is one output')
src, fmt, kind = fed[0]['input'], fed[0].get('format'), fed[0]['name'].split(':')[0]
out, tmp = opts['-o'], opts.get('--temp-dir', '/')
if not os.path.isdir(tmp):
    fail(f'Directory "{tmp}" does not exist.')
if os.path.exists(out) and opts.get('--overwrite') != '1':
    fail(f'File "{out}" exists.')
m = re.fullmatch(r'skip:dialnorm=(-?\d+)', opts.get('--loudness-management', ''))
if not m or not -31 <= int(m[1]) <= -1:
    fail('the dialogue level must be given, -31 to -1')
shim = ast.literal_eval(os.environ.get('FAKE_SHIM', '{}'))
as_is = shim.get('--as-is') == 'on'
pcm = fmt == 'wav'
if pcm:  # as DEE: a WAV is read by slot in Dolby order (the channel mask is ignored)
    ch = int(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=channels',
                             '-of', 'csv=p=0', src], capture_output=True, text=True).stdout)
    if kind != f'{ch}ch':
        fail(f'presentation {kind} does not take a {ch}-channel WAV')
elif fmt != 'atmos_mezz' or kind != 'atmos' or 'number_of_elements=16' not in fed[0]['name'] \
        or not os.path.isfile(src + '.audio'):
    fail('an Atmos master takes the atmos presentation')
for step, name in ((1, 'preprocessing'), (2, 'Encoding')):
    print(f'[2026-01-01 00:00:00.000] Step {step}/2 : {name}', flush=True)
    sys.stdout.write(''.join(f'\r[{"=" * (p // 10)}>] {p} %' for p in (0, 50, 100)) + '\n')
if os.environ.get('FAKE_DEE_FAIL'):
    fail('license expired')
d = os.environ.get('FAKE_DEE_DELAY_MS', '0')
pan = os.environ.get('FAKE_DEE_PAN')
af = f'adelay={d}:all=1' + (',' + pan if pan else '')
if pcm:
    import numpy as np
    from scipy.io import wavfile
    x = wavfile.read(src)[1].astype(float) / 2**31
    if not as_is and np.abs(x).max() > 0.75:  # DEE changes samples above 0.75 FS
        af += ',volume=0.99'
    if ch == 8:
        af += ',pan=7.1|c0=c0|c1=c1|c2=c2|c3=c3|c4=c6|c5=c7|c6=c4|c7=c5'
codec = ['-c:a', 'truehd', '-strict', '-2', '-f', 'truehd']
if pcm and ch == 8:  # ffmpeg's TrueHD encoder can't make 7.1: lossless FLAC stands in
    codec = ['-c:a', 'flac', '-sample_fmt', 's32', '-bits_per_raw_sample', '24', '-f', 'flac']
# Atmos: re-encode the source film's audio (the DAMF is fake); PCM: encode the real input
inp = src if pcm else os.environ['FAKE_DEE_SOURCE']
subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', inp, '-map', '0:a:0',
                '-af', af, *codec, out], check=True)
for ext in ('.log.html', '.mll'):
    open(out + ext, 'w').write('DEE writes these beside its output')
if not pcm:  # what the fake truehdd will decode this output to
    from helpers import fake_dee_atmos
    fake_dee_atmos(src, out + '.damf', os.environ.get('FAKE_DEE_ATMOS', ''))
"""


def make_dee(folder: Path) -> tuple[Path, Path]:
    """A DEE 6.7 folder holding the fake dee_dthd_encoder, and the fake dee_shim."""
    folder.mkdir(parents=True, exist_ok=True)
    _exe(folder / "dee_dthd_encoder", FAKE_ENCODER.replace("@PY@", sys.executable)
         .replace("@HERE@", repr(str(HERE))))
    return folder, _exe(folder.parent / "dee_shim",
                        FAKE_SHIM.replace("@PY@", sys.executable))


FAKE_TRUEHDD = """#!{py}
import os, shutil, subprocess, sys
sys.path.insert(0, {here!r})
from helpers import TRUEHDD_RELEASE, write_damf, make_signal, move_objects
a = sys.argv[1:]
if a == ['--version']:  # with it, how the fake DEE's output reads (helpers.fake_dee_atmos)
    print(os.environ.get('FAKE_TRUEHDD_VERSION', TRUEHDD_RELEASE))
    sys.exit(0)
if 'info' in a:
    print('Presentation 3')
    print('    Dialogue Level          -27 dBFS')
    sys.exit(0)
out, src = a[a.index('--output-path') + 1], a[-1]
assert os.path.getsize(src) > 1000, 'no TrueHD data'
if os.path.isfile(src + '.damf.atmos'):  # the fake DEE's output
    for ext in ('.atmos', '.atmos.audio', '.atmos.metadata'):
        shutil.copy(src + '.damf' + ext, out + ext)
else:  # a source track: as many samples as the stream holds, less any it is told to lose
    walk = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                           'frame=nb_samples', '-of', 'csv=p=0', src],
                          capture_output=True, text=True, check=True)
    frames = sum(int(n) for n in walk.stdout.replace(',', ' ').split())
    write_damf(out, make_signal(frames=frames - int(os.environ.get('FAKE_TRUEHDD_LOSES', '0')),
                                ch=12))
    move_objects(out, 3 * 1536)  # 1112 samples into a frame, where a disc's updates sit
"""


def _exe(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture(scope="module")
def assets(tmp_path_factory):
    d = tmp_path_factory.mktemp("assets")
    src = d / "Test Movie (2020) {tmdb-333371}.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=20",
         *[a for k in range(6) for a in (
             "-f", "lavfi", "-i", f"anoisesrc=color=pink:duration=20:sample_rate=48000:seed={k + 1}")],
         "-filter_complex",
         "[1:a][2:a][3:a][4:a][5:a][6:a]join=inputs=6:channel_layout=5.1[a];[1:a]anull[s]",
         "-map", "0:v", "-map", "[a]", "-map", "[s]",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a:0", "truehd", "-strict", "-2",
         "-c:a:1", "ac3", "-ac:a:1", "2", "-metadata:s:a:0", "language=eng",
         "-metadata:s:a:0", "title=TrueHD Atmos 7.1", str(src)],
        check=True,
    )
    truehdd = _exe(d / "truehdd", FAKE_TRUEHDD.format(py=sys.executable, here=str(HERE)))
    folder, shim = make_dee(d / "dee")
    return {"src": src, "truehdd": truehdd, "dee": folder, "shim": shim}


@pytest.fixture
def env(tmp_path, assets, monkeypatch):
    monkeypatch.setattr(media, "ATMOS_COMMERCIAL", "Dolby TrueHD")
    monkeypatch.setenv("ALLOW_TEST_FORMATS", "true")
    monkeypatch.setenv("MEDIA_ROOTS", str(tmp_path / "media"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("TRUEHDD_PATH", str(assets["truehdd"]))
    monkeypatch.setenv("DEE_PATH", str(assets["dee"]))
    monkeypatch.setenv("DEE_SHIM", str(assets["shim"]))
    monkeypatch.setenv("FAKE_DEE_SOURCE", str(assets["src"]))
    monkeypatch.setenv("FAKE_DEE_ARGS", str(tmp_path / "dee-args.txt"))
    monkeypatch.delenv("FAKE_DEE_DELAY_MS", raising=False)
    monkeypatch.delenv("FAKE_DEE_FAIL", raising=False)
    monkeypatch.delenv("FAKE_DEE_PAN", raising=False)
    monkeypatch.delenv("FAKE_DEE_ATMOS", raising=False)
    monkeypatch.delenv("FAKE_TRUEHDD_LOSES", raising=False)
    monkeypatch.delenv("FAKE_TRUEHDD_VERSION", raising=False)
    s = config.load()
    movie = tmp_path / "media" / "Test Movie (2020) {tmdb-333371}"
    movie.mkdir(parents=True)
    shutil.copy(assets["src"], movie / assets["src"].name)
    return s, movie / assets["src"].name, tmp_path


ENTRY = {
    "digest": "x", "title": "Test Movie", "year": "2020", "tmdb": "333371",
    "audio_types": ["Atmos"], "author": "tester", "label": "tester", "origin": "catalogue",
    "source": "Disc", "edition": "", "mv": "0",
    "url": "", "filters": [
        {"type": "LowShelf", "freq": 22.0, "gain": 5.0, "q": 0.9, "count": 3},
        {"type": "PeakingEQ", "freq": 30.0, "gain": 2.0, "q": 1.5, "count": None},
    ],
}


def _params(pr, **kw):
    return {"phase": "encode", "entry": ENTRY, "track_id": pr.audio[0].track_id,
            "format": "truehd", "target_peak_dbfs": -1.0, **kw}


def _run(s, path, params, tmp):
    stages = []
    r = JobRunner(s, 1, str(path), params, lambda st, p: stages.append((st, p)), tmp / "job.log")
    return r.run(), stages


def _dee_args(tmp):
    return ast.literal_eval((tmp / "dee-args.txt").read_text())


def _pairs(args):
    """Each option with the value after it."""
    return set(zip(args, args[1:]))


@pytest.mark.parametrize("decoder,times", [(TRUEHDD_RELEASE, "frame"),
                                           (TRUEHDD_EXACT, "sample")])
def test_truehd_atmos_end_to_end(env, monkeypatch, decoder, times):
    s, path, tmp = env
    monkeypatch.setenv("FAKE_TRUEHDD_VERSION", decoder)
    before = media.probe(str(path))
    programme = pcmio.find_gaps("ffprobe", str(path), 48000).frames
    assert [a.atmos for a in before.audio] == [True, False]
    result, stages = _run(s, path, _params(before), tmp)

    after = media.probe(str(path))
    assert [t["type"] for t in after.tracks] == ["video", "audio", "audio", "audio"]
    beq = after.audio[1]
    assert beq.name == "BEQ TrueHD Atmos (tester)" and beq.beq and not beq.default
    assert "TrueHD" in beq.codec and beq.language == "en"
    assert result["filter_report"]["gain_db"] < 0
    assert abs(result["sync"]["lag_ms"]) <= 1.0 and result["sync"]["confidence"] > 0.5
    args = _dee_args(tmp)
    shim, dee_args = args[:args.index("--")], args[args.index("--") + 1:]
    # every sample and every object position coded as read, no DRC profile
    assert _pairs(shim) >= {("--as-is", "on"), ("--oamd-as-is", "on"), ("--drc", "none")}
    assert ("--presentation", "atmos:number_of_elements=16:legacy_authoring=0") in _pairs(dee_args)
    # the source's dialogue level (fake truehdd info: -27); the downmix's DRC off by default
    assert ("--loudness-management", "skip:dialnorm=-27") in _pairs(dee_args)
    assert ("--presentation", "2ch:drc_default_on=0") in _pairs(dee_args)
    names = {st for st, _ in stages}
    assert {"Decoding TrueHD Atmos (truehdd)", "BEQ filter: writing",
            "Encoding TrueHD Atmos (DEE step 2/2: encoding)",
            "Checking: decoding the encoded TrueHD Atmos",
            "Checking the TrueHD Atmos encode against the filtered source",
            "Remuxing (mkvmerge)", "Checking A/V sync and channel order", "Done"} <= names
    fid = result["filter_report"]["fidelity"]
    assert fid["ok"] and fid["method"] == "sample-exact"
    assert fid["objects"] == 11 and fid["out_elements"] == 16 and fid["bed"] == "LFE"
    assert fid["frames"] == programme and fid["max_err_lsb"] <= 0.5
    assert fid["limits"]["max_err_lsb"] == 0.5
    assert fid["mapping"] == {str(c): c for c in range(12)}  # in master order
    # every object plays throughout: each one's position checked in every frame, and
    # read to the sample, every move at its own update
    assert fid["object_frames"] == 11 * -(-programme // 1536)
    assert fid["update_times"] == times and fid["max_update_shift"] == 0
    assert f"position update times read to the {times}" in (tmp / "job.log").read_text()
    tags = subprocess.run(["mkvextract", str(path), "tags", "-"], capture_output=True,
                          text=True).stdout
    assert "BEQ_CHECK" in tags and "sample-exact:" in tags
    assert set(result["sync"]["channel_corr"]) >= {"L", "R", "C", "Ls", "Rs"}
    assert min(result["sync"]["channel_corr"].values()) > 0.9
    assert not list(path.parent.glob(".*beq-tmp*")) and not (s.work_dir / "job-1").exists()


def test_sync_error_is_measured_and_corrected(env, monkeypatch):
    s, path, tmp = env
    monkeypatch.setenv("FAKE_DEE_DELAY_MS", "37")
    result, _ = _run(s, path, _params(media.probe(str(path))), tmp)
    assert result["sync"]["applied_ms"] == pytest.approx(-37, abs=1)
    assert abs(result["sync"]["lag_ms"]) <= 1.0
    assert "Correcting BEQ track sync" in (tmp / "job.log").read_text()


def test_swapped_channels_fail_the_job(env, monkeypatch):
    s, path, tmp = env
    monkeypatch.setenv("FAKE_DEE_PAN", "pan=5.1|c0=c1|c1=c0|c2=c2|c3=c3|c4=c5|c5=c4")
    digest = path.read_bytes()[:4096]
    with pytest.raises(JobFailed, match="Channel order differs.*L↔R"):
        _run(s, path, _params(media.probe(str(path))), tmp)
    assert path.read_bytes()[:4096] == digest


@pytest.mark.parametrize("mode,says", [
    ("drop", "not carried by any one element"),
    ("drop-loud", "does not line up with the filtered source"),
    ("sample", "differs from the filtered source by"),
    ("late", "the position metadata does not match"),
    ("wrong", "the position metadata does not match"),
])
def test_an_atmos_encode_that_is_not_the_filtered_source_fails(env, monkeypatch, mode, says):
    s, path, tmp = env
    monkeypatch.setenv("FAKE_DEE_ATMOS", mode)
    digest = path.read_bytes()[:4096]
    with pytest.raises(JobFailed, match=says):
        _run(s, path, _params(media.probe(str(path))), tmp)
    assert path.read_bytes()[:4096] == digest


def test_updates_at_the_frame_start_fail_where_the_decoder_reads_the_sample(env, monkeypatch):
    """DEE's own timing (dee-shim#3): every update moved to the start of its frame."""
    s, path, tmp = env
    monkeypatch.setenv("FAKE_DEE_ATMOS", "frame-start")
    monkeypatch.setenv("FAKE_TRUEHDD_VERSION", TRUEHDD_EXACT)
    with pytest.raises(JobFailed, match="the position update times do not match"):
        _run(s, path, _params(media.probe(str(path))), tmp)


def test_objects_in_another_order_pass(env, monkeypatch):
    s, path, tmp = env
    monkeypatch.setenv("FAKE_DEE_ATMOS", "reorder")
    result, _ = _run(s, path, _params(media.probe(str(path))), tmp)
    fid = result["filter_report"]["fidelity"]
    assert fid["ok"] and fid["mapping"]["1"] == 11 and fid["mapping"]["11"] == 1


def test_keep_work_keeps_the_atmos_masters(env, tmp_path):
    s, path, tmp = env
    s.keep_work = True
    _run(s, path, _params(media.probe(str(path))), tmp)
    work = s.work_dir / "job-1"
    for base in ("source", "atmos", "check"):
        assert (work / f"{base}.atmos.audio").exists(), base


def test_dee_is_given_the_beq_rounded_without_dither(env):
    """DEE 6.7 under --as-is changes samples of a dithered master by 1-4 LSB (measured on
    Edge of Tomorrow and Klaus), and codes the same master rounded bit for bit."""
    from scipy.signal import sosfilt
    s, path, tmp = env
    s.keep_work = True
    result, _ = _run(s, path, _params(media.probe(str(path))), tmp)
    work = s.work_dir / "job-1"
    source = read_caf(work / "source.atmos.audio")
    given = read_caf(work / "atmos.atmos.audio")
    sos = design_sos([FilterSpec.from_catalogue(f) for f in ENTRY["filters"]], 48000.0)
    exact = sosfilt(sos, source, axis=0) * result["filter_report"]["gain"]
    assert np.abs(given - exact).max() * 2**23 <= 0.5 + 1e-6


def test_a_truehd_decode_shorter_than_the_track_fails_the_job(env, monkeypatch):
    s, path, tmp = env
    monkeypatch.setenv("FAKE_TRUEHDD_LOSES", "4800")
    pr = media.probe(str(path))
    before = _snapshot(path)
    with pytest.raises(JobFailed, match=r"truehdd decoded \d+ samples .* which holds \d+"):
        _run(s, path, _params(pr), tmp)
    assert _snapshot(path) == before and not (tmp / "dee-args.txt").exists()


def test_a_timeline_walk_that_fails_refuses_an_atmos_track(env):
    s, path, tmp = env
    real = shutil.which("ffprobe")
    s.ffprobe = str(_exe(tmp / "ffprobe-dies",
                         '#!/bin/sh\ncase "$*" in *nb_samples*) exit 3 ;; esac\n'
                         f'exec "{real}" "$@"\n'))
    pr = media.probe(str(path))
    before = _snapshot(path)
    with pytest.raises(JobFailed, match="Could not map the .* timeline"):
        _run(s, path, _params(pr), tmp)
    assert _snapshot(path) == before and not (tmp / "dee-args.txt").exists()


def test_lossy_outputs_and_a_dee_without_the_truehd_encoder_are_refused(env):
    s, path, tmp = env
    pr = media.probe(str(path))
    for fmt in ("ddp", "ac4"):
        with pytest.raises(JobFailed, match="lossy conversions are currently out of scope"):
            _run(s, path, _params(pr, format=fmt), tmp)
    with pytest.raises(JobFailed, match="keep only its bed"):  # FLAC of an Atmos track
        _run(s, path, _params(pr, format="flac"), tmp)
    (tmp / "dee-without-truehd").mkdir()
    s.dee_path = str(tmp / "dee-without-truehd")
    with pytest.raises(JobFailed, match="No dee_dthd_encoder"):
        _run(s, path, _params(pr), tmp)
    assert not (tmp / "dee-args.txt").exists()


def test_rerun_replaces_the_beq_track_and_its_attachments(env):
    s, path, tmp = env
    _run(s, path, _params(media.probe(str(path))), tmp)
    first = media.probe(str(path))
    assert len(first.audio) == 3 and len(first.beq_attachments) == 3
    _run(s, path, _params(first, replace_existing_beq=True, default_flag=True), tmp)
    after = media.probe(str(path))
    assert len(after.audio) == 3 and len(after.beq_tracks) == 1
    assert after.audio[1].name == "BEQ TrueHD Atmos (tester)" and after.audio[1].default
    assert not after.audio[0].default
    assert len(after.beq_attachments) == 3


def test_dee_summary_error_fails_job(env, monkeypatch):
    s, path, tmp = env
    monkeypatch.setenv("FAKE_DEE_FAIL", "1")
    digest = path.read_bytes()[:4096]
    with pytest.raises(JobFailed, match="license expired"):
        _run(s, path, _params(media.probe(str(path))), tmp)
    assert path.read_bytes()[:4096] == digest


def test_missing_dee_fails_early(env):
    s, path, tmp = env
    s.dee_path = str(tmp / "nope")
    with pytest.raises(JobFailed, match="DEE not found"):
        _run(s, path, _params(media.probe(str(path))), tmp)


def test_lossy_track_gets_no_second_lossy_generation(env):
    s, path, tmp = env
    pr = media.probe(str(path))
    assert pr.audio[1].kind == "ac3"
    with pytest.raises(JobFailed, match="not lossless"):
        _run(s, path, {**_params(pr), "track_id": pr.audio[1].track_id, "format": "ddp",
                       "bitrate": 1024}, tmp)


def test_truehdd_failure_leaves_source_untouched(env, assets):
    s, path, tmp = env
    s.truehdd_path = str(_exe(tmp / "truehdd-fails-decode",
                              f'#!/bin/sh\ncase "$*" in *info*) exec "{assets["truehdd"]}" "$@" ;; '
                              'esac\nexit 1\n'))
    digest = path.read_bytes()[:4096]
    with pytest.raises(JobFailed, match="truehdd decode failed"):
        _run(s, path, _params(media.probe(str(path))), tmp)
    assert path.read_bytes()[:4096] == digest
    assert not any(s.work_dir.iterdir())


def test_a_dolby_track_whose_dialogue_level_cannot_be_read_fails_the_job(env, monkeypatch):
    s, path, tmp = env
    s.truehdd_path = "/bin/false"
    digest = path.read_bytes()[:4096]
    with pytest.raises(JobFailed, match="dialogue level"):
        _run(s, path, _params(media.probe(str(path))), tmp)
    assert path.read_bytes()[:4096] == digest
    assert not any(s.work_dir.iterdir())
    # the AC-3 route through MediaInfo: nothing in the file says what its level is
    monkeypatch.setattr(media, "dialnorm", lambda *a: None)
    pr = media.probe(str(path))
    with pytest.raises(JobFailed, match="dialogue level"):
        _run(s, path, {**_pcm_params(pr), "format": "truehd"}, tmp)
    assert path.read_bytes()[:4096] == digest


INFO_HEAD = 4096
# a truehdd that lists a presentation's level only when shown at least FAKE_INFO_NEEDS bytes
FAKE_INFO = """#!{py}
import os, sys
path = sys.argv[-1]
size = os.path.getsize(path)
with open({log!r}, 'a') as fh:
    fh.write(f'{{os.path.basename(path)}} {{size}}\\n')
if size >= int(os.environ.get('FAKE_INFO_NEEDS', '0')):
    print('  Presentation 0\\n    Dialogue Level          -31 dBFS')
    print('  Presentation 3\\n    Dialogue Level          -27 dBFS')
sys.exit(int(os.environ.get('FAKE_INFO_EXIT', '0')))
"""


@pytest.fixture
def info_job(tmp_path, monkeypatch):
    """(what truehd_dialnorm needs of a job, a TrueHD stream longer than the part truehdd
    is shown, the log of what truehdd was shown)."""
    monkeypatch.setattr(tools, "DIALNORM_HEAD_BYTES", INFO_HEAD)
    log = tmp_path / "shown.log"
    exe = _exe(tmp_path / "truehdd-info", FAKE_INFO.format(py=sys.executable, log=str(log)))
    raw = tmp_path / "source.thd"
    raw.write_bytes(bytes(range(256)) * 40)
    job = SimpleNamespace(s=SimpleNamespace(truehdd_path=str(exe)),
                          procs=Processes(lambda _msg: None))
    return job, raw, lambda: log.read_text().split("\n")[:-1]


def test_the_dialogue_level_is_read_from_the_start_of_the_stream(info_job):
    job, raw, shown = info_job
    whole = raw.read_bytes()
    assert tools.truehd_dialnorm(job, raw) == -27  # the top presentation's
    assert shown() == [f"source-head.thd {INFO_HEAD}"]
    assert raw.read_bytes() == whole and sorted(p.name for p in raw.parent.glob("*.thd")) == [
        "source.thd"]


def test_a_stream_whose_start_gives_no_level_is_read_whole(info_job, monkeypatch):
    job, raw, shown = info_job
    monkeypatch.setenv("FAKE_INFO_NEEDS", str(INFO_HEAD + 1))
    assert tools.truehd_dialnorm(job, raw) == -27
    assert shown() == [f"source-head.thd {INFO_HEAD}", f"source.thd {raw.stat().st_size}"]
    monkeypatch.setenv("FAKE_INFO_NEEDS", str(10**9))  # nor does the whole of it
    with pytest.raises(JobFailed, match="Could not read the dialogue level .*exit 0"):
        tools.truehd_dialnorm(job, raw)
    monkeypatch.setenv("FAKE_INFO_NEEDS", "0")
    monkeypatch.setenv("FAKE_INFO_EXIT", "3")  # a level from a run that failed is not one
    with pytest.raises(JobFailed, match="Could not read the dialogue level .*exit 3"):
        tools.truehd_dialnorm(job, raw)
    assert [p.name for p in raw.parent.glob("*.thd")] == ["source.thd"]


def test_dee_probe_and_licence_redaction(tmp_path, assets):
    d = dee.Dee(assets["dee"], assets["shim"])
    dee._PROBES.clear()
    assert d.probe() is None and d.status()["ok"]
    broken = tmp_path / "broken"
    broken.mkdir()
    _exe(broken / "dee_dthd_encoder", "#!/bin/sh\necho 'ERROR: not this build'\nexit 1\n")
    b = dee.Dee(broken, assets["shim"])
    assert "ERROR: not this build" in (b.probe() or "")
    assert any("did not run" in p for p in b.status()["problems"])
    assert not dee.Dee(assets["dee"], tmp_path / "no-shim").status()["ok"]
    assert dee.redact("Licensed features: AB12CD-99XY") == "[DEE licence details redacted]"
    assert "[redacted]" in dee.redact("[2026-01-01 10:00:00.0] ERROR: license expired X12345A")
    assert dee.redact("[2026-01-01 10:00:00.0] Step 2/2 : Encoding").endswith("Step 2/2 : Encoding")


# -- job and file safety --------------------------------------------------------------
def _pcm_params(pr, **kw):
    return {"phase": "encode", "entry": ENTRY, "track_id": pr.audio[1].track_id,
            "format": "flac", "target_peak_dbfs": -1.0, **kw}


def _snapshot(path):
    st = path.stat()
    return st.st_size, st.st_mtime_ns, path.read_bytes()


def _no_temp_left(path):
    return not list(path.parent.glob(".*beq-tmp*"))


@pytest.mark.parametrize("stage", ["Reading the", "Remuxing (mkvmerge)"])
def test_source_changed_mid_job_is_never_overwritten(env, stage):
    s, path, tmp = env
    pr = media.probe(str(path))
    newer = b"a newer film arrived"
    seen = []

    def progress(st, _pct):
        if st.startswith(stage) and not seen:
            seen.append(st)
            with open(path, "ab") as fh:
                fh.write(newer)

    r = JobRunner(s, 1, str(path), _pcm_params(pr), progress, tmp / "job.log")
    with pytest.raises(JobFailed, match="changed"):
        r.run()
    assert seen and path.read_bytes().endswith(newer)
    assert _no_temp_left(path)


@pytest.mark.parametrize("tamper", [lambda pin: pin.update(size=pin["size"] + 1),
                                    lambda pin: pin["track"].update(kind="dtsx")])
def test_encode_refuses_a_file_other_than_the_one_prepared(env, tamper):
    s, path, tmp = env
    pr = media.probe(str(path))
    pin = source_pin(FileStamp.of(path), pr.audio[1])
    tamper(pin)
    before = _snapshot(path)
    with pytest.raises(JobFailed, match="prepared from"):
        _run(s, path, _pcm_params(pr, source=pin), tmp)
    assert _snapshot(path) == before


def test_the_swap_is_flushed_before_the_rename_and_the_directory_after(env, monkeypatch):
    s, path, tmp = env
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append(("fsync", os.readlink(f"/proc/self/fd/{fd}")))
        return real_fsync(fd)

    def replace(a, b):
        events.append(("replace", str(b)))
        return real_replace(a, b)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    s.charts = False
    _run(s, path, _pcm_params(media.probe(str(path))), tmp)
    temp = temp_mkv(path, 1)
    assert (events.index(("fsync", str(temp))) < events.index(("replace", str(path)))
            < events.index(("fsync", str(path.parent))))


HANGS = [
    ("ffprobe", "stream=codec_name", "pcm"),  # the probe every job starts with
    ("ffprobe", "nb_samples", "pcm"),
    ("ffprobe", "stream=channels,channel_layout", "pcm"),
    ("truehdd", "info", "atmos"),
]


def _hanging(tmp, real, match):
    """A stand-in for `real` that hangs where its arguments contain `match`, leaving a
    marker file so the test knows the job reached it."""
    marker = tmp / f"hanging-{Path(real).name}"
    script = _exe(tmp / f"hang-{Path(real).name}",
                  f'#!/bin/sh\ncase "$*" in *{match}*) : > "{marker}"; exec sleep 300 ;; esac\n'
                  f'exec "{real}" "$@"\n')
    return script, marker


@pytest.mark.parametrize("tool,match,route", HANGS)
def test_cancel_stops_a_running_subprocess_and_leaves_the_source_alone(env, assets, tool, match,
                                                                       route):
    s, path, tmp = env
    real = {"ffprobe": shutil.which("ffprobe"), "truehdd": str(assets["truehdd"])}[tool]
    script, marker = _hanging(tmp, real, match)
    if tool == "ffprobe":
        s.ffprobe = str(script)
    else:
        s.truehdd_path = str(script)
    pr = media.probe(str(path))
    params = _pcm_params(pr) if route == "pcm" else _params(pr)
    before = _snapshot(path)
    r = JobRunner(s, 1, str(path), params, lambda *a: None, tmp / "job.log")
    raised = []

    def go():
        try:
            r.run()
        except BaseException as exc:  # noqa: BLE001
            raised.append(exc)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    deadline = time.monotonic() + 60
    while not marker.exists():
        assert time.monotonic() < deadline, "the job never reached the stalled tool"
        time.sleep(0.05)
    r.cancel()
    t.join(30)
    assert not t.is_alive()
    assert isinstance(raised[0], Cancelled)
    assert _snapshot(path) == before
    assert _no_temp_left(path) and not (s.work_dir / "job-1").exists()


@pytest.mark.parametrize("keep_work", [False, True])
def test_restart_removes_what_interrupted_jobs_left_and_nothing_else(env, keep_work):
    s, path, tmp = env
    s.keep_work = keep_work
    db = DB(s.db_path)
    other = path.with_name("Other Film.mkv")
    interrupted = db.create_job(None, str(path), {"phase": "encode"})
    finished = db.create_job(None, str(other), {"phase": "encode"})
    db.update_job(interrupted, status="running")
    db.update_job(finished, status="done")
    for job in (interrupted, finished):
        (s.work_dir / f"job-{job}").mkdir(parents=True)
        (s.work_dir / f"job-{job}" / "beq.wav").write_bytes(b"pcm")
    leftover = temp_mkv(path, interrupted)
    decoys = [temp_mkv(other, finished), path.with_name(f".{path.stem}.beq-tmp.mkv"),
              path.with_name(f".{path.stem}.keep.mkv")]
    for f in (leftover, *decoys):
        f.write_bytes(b"film-sized")
    before = _snapshot(path)

    queue = JobQueue(s, db)
    queue.start()
    queue.stop()

    row = db.one("SELECT status, message FROM jobs WHERE id=?", (interrupted,))
    assert row["status"] == "failed" and row["message"] == "Interrupted by restart"
    assert db.one("SELECT status FROM jobs WHERE id=?", (finished,))["status"] == "done"
    assert not leftover.exists()
    assert (s.work_dir / f"job-{interrupted}").exists() == keep_work
    assert (s.work_dir / f"job-{finished}" / "beq.wav").exists()
    assert all(f.exists() for f in decoys) and _snapshot(path) == before


def test_a_cancelled_queued_job_is_never_started(env, monkeypatch):
    s, path, tmp = env
    db = DB(s.db_path)
    queue = JobQueue(s, db)
    job = db.create_job(None, str(path), {"phase": "prepare"})
    row = db.one("SELECT * FROM jobs WHERE id=?", (job,))  # what the queue had selected
    assert queue.cancel(job) is True
    assert queue.cancel(job + 99) is False
    started = []
    monkeypatch.setattr(JobRunner, "run", lambda self: started.append(self) or {})
    queue._start(row)
    time.sleep(0.2)
    assert not started and not queue.running
    assert db.one("SELECT status FROM jobs WHERE id=?", (job,))["status"] == "cancelled"


def test_one_active_job_per_file(env):
    s, path, tmp = env
    db = DB(s.db_path)
    first = db.create_job(None, str(path), {"phase": "prepare"})
    assert first is not None
    assert db.create_job(None, str(path), {"phase": "prepare"}) is None
    db.update_job(first, status="done")
    assert db.create_job(None, str(path), {"phase": "prepare"}) is not None


def test_a_scan_keeps_the_rows_of_a_missing_or_empty_root(env):
    s, path, tmp = env
    gone, empty, media_root = tmp / "gone", tmp / "empty", tmp / "media"
    empty.mkdir()
    s.media_roots = [media_root, gone, empty]
    db = DB(s.db_path)
    kept = [gone / "a.mkv", empty / "b.mkv"]
    deleted = media_root / "deleted since the last scan.mkv"
    for p in (*kept, deleted):
        db.upsert_media(path=str(p), title="A film")
    scanner = Scanner(s, db)
    assert scanner.start()
    deadline = time.monotonic() + 90
    while scanner.state["finished_at"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    paths = {row["path"] for row in db.q("SELECT path FROM media")}
    assert {str(p) for p in kept} <= paths and str(path) in paths
    assert str(deleted) not in paths
    assert "empty" in scanner.state["error"]


def test_a_sync_check_that_cannot_run_stops_the_swap(env, monkeypatch):
    s, path, tmp = env
    s.charts = False
    monkeypatch.setattr(synccheck, "compare",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no decode")))
    pr = media.probe(str(path))
    before = _snapshot(path)
    with pytest.raises(JobFailed, match="could not run"):
        _run(s, path, _pcm_params(pr), tmp)
    assert _snapshot(path) == before


def test_an_inconclusive_sync_reading_is_still_a_pass(env, monkeypatch):
    s, path, tmp = env
    s.charts = False
    monkeypatch.setattr(synccheck, "compare",
                        lambda *a, **k: synccheck.CheckResult(0.0, 0.1, 6.0, channels=2))
    result, _ = _run(s, path, _pcm_params(media.probe(str(path))), tmp)
    assert result["sync"]["confidence"] == 0.1
    assert any("inconclusive" in w for w in result["warnings"])


def _with_chapters_and_a_font(path, tmp):
    chapters = tmp / "chapters.txt"
    chapters.write_text("CHAPTER01=00:00:00.000\nCHAPTER01NAME=Opening\n"
                        "CHAPTER02=00:00:05.000\nCHAPTER02NAME=Middle\n")
    font = tmp / "font.ttf"
    font.write_bytes(b"not really a font")
    out = tmp / "with-extras.mkv"
    subprocess.run(["mkvmerge", "-q", "-o", str(out), "--chapters", str(chapters),
                    "--attachment-mime-type", "font/ttf", "--attach-file", str(font), str(path)],
                   check=True)
    shutil.move(str(out), str(path))


@pytest.mark.parametrize("flag,match", [("--no-video", "tracks"),
                                        ("--no-chapters", "chapters"),
                                        ("--no-attachments", "attachments")])
def test_a_remux_that_loses_anything_never_replaces_the_source(env, flag, match):
    s, path, tmp = env
    s.charts = False
    _with_chapters_and_a_font(path, tmp)
    real = shutil.which("mkvmerge")
    s.mkvmerge = str(_exe(tmp / "lossy-mkvmerge",
                          f'#!/bin/sh\ncase " $* " in *" --gui-mode "*) '
                          f'exec "{real}" {flag} "$@" ;; esac\nexec "{real}" "$@"\n'))
    pr = media.probe(str(path))
    before = _snapshot(path)
    with pytest.raises(JobFailed, match=match):
        _run(s, path, _pcm_params(pr), tmp)
    assert _snapshot(path) == before and _no_temp_left(path)


def test_a_remux_that_loses_part_of_a_track_never_replaces_the_source(env):
    s, path, tmp = env
    s.charts = False
    # the same film with the second audio track cut to 5 of its 20 s: the video keeps the
    # container's duration where it was, so only a per-track measure can see this
    cut = tmp / "cut.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-t", "5", "-i", str(path),
                    "-map", "0:v", "-map", "0:a:0", "-map", "1:a:1", "-c", "copy", str(cut)],
                   check=True)
    real = shutil.which("mkvmerge")
    s.mkvmerge = str(_exe(tmp / "lossy-mkvmerge",
                          f"#!{sys.executable}\nimport os, sys\n"
                          f"cut, src = {str(cut)!r}, {str(path)!r}\n"
                          "args = [cut if a == src and '--gui-mode' in sys.argv else a "
                          "for a in sys.argv[1:]]\n"
                          f"os.execv({real!r}, [{real!r}, *args])\n"))
    pr = media.probe(str(path))
    before = _snapshot(path)
    with pytest.raises(JobFailed, match=r"audio track .* has \d+ frames, expected \d+"):
        _run(s, path, _pcm_params(pr), tmp)
    assert _snapshot(path) == before and _no_temp_left(path)


def test_stale_frame_tags_on_the_source_do_not_block_a_whole_remux(env):
    s, path, tmp = env
    s.charts = False
    stale = tmp / "stale-tags.xml"
    stale.write_text('<?xml version="1.0"?><Tags><Tag><Targets/><Simple>'
                     "<Name>NUMBER_OF_FRAMES</Name><String>99999</String></Simple></Tag></Tags>")
    tagged = tmp / "tagged.mkv"
    subprocess.run(["mkvmerge", "-q", "-o", str(tagged), str(path)], check=True)
    subprocess.run(["mkvpropedit", "-q", str(tagged), "--tags", f"track:a2:{stale}"], check=True)
    shutil.move(str(tagged), str(path))
    pr = media.probe(str(path))
    assert _run(s, path, _pcm_params(pr), tmp)[0]["route"] == "pcm"


def _warning_mkvmerge(tmp, real, text):
    """mkvmerge, plus one warning of `text` and its exit code 1."""
    return str(_exe(tmp / "warning-mkvmerge",
                    f'#!/bin/sh\nif [ "$1" = "--gui-mode" ]; then\n  "{real}" "$@"\n'
                    f"  echo '#GUI#warning {text}'\n  exit 1\nfi\n"
                    f'exec "{real}" "$@"\n'))


def test_a_mkvmerge_warning_of_lost_data_never_replaces_the_source(env):
    s, path, tmp = env
    s.charts = False
    s.mkvmerge = _warning_mkvmerge(tmp, shutil.which("mkvmerge"),
                                   "track 1: This audio track contains 3000 bytes of invalid "
                                   "data which were skipped before timestamp 00:00:04.192. "
                                   "The audio/video synchronization may have been lost.")
    pr = media.probe(str(path))
    before = _snapshot(path)
    with pytest.raises(JobFailed, match="lost data"):
        _run(s, path, _pcm_params(pr), tmp)
    assert _snapshot(path) == before and _no_temp_left(path)


def test_a_harmless_mkvmerge_warning_passes_and_is_reported(env):
    s, path, tmp = env
    s.charts = False
    s.mkvmerge = _warning_mkvmerge(tmp, shutil.which("mkvmerge"), "track 1: fine print")
    result, _ = _run(s, path, _pcm_params(media.probe(str(path))), tmp)
    assert "mkvmerge: track 1: fine print" in result["warnings"]


def test_a_failed_embed_remuxes_without_the_charts_and_says_so(env):
    s, path, tmp = env
    s.mkvpropedit = str(_exe(tmp / "broken-mkvpropedit", "#!/bin/sh\nexit 2\n"))
    result, _ = _run(s, path, _pcm_params(media.probe(str(path))), tmp)
    assert result["warnings"] == ["the charts could not be embedded in the file"]
    assert media.probe(str(path)).beq_attachments == []
    tags = subprocess.run(["mkvextract", str(path), "tags", "-"], capture_output=True,
                          text=True).stdout
    assert "BEQ_CHART_SPECTRUM" not in tags and "BEQ_CHECK" in tags


def test_a_temp_mkv_the_embed_left_unreadable_is_rebuilt_without_the_charts(env):
    s, path, tmp = env
    s.mkvpropedit = str(_exe(tmp / "wrecking-mkvpropedit", '#!/bin/sh\n: > "$1"\nexit 0\n'))
    result, _ = _run(s, path, _pcm_params(media.probe(str(path))), tmp)
    assert "the charts could not be embedded in the file" in result["warnings"]
    after = media.probe(str(path))
    assert after.beq_attachments == [] and len(after.beq_tracks) == 1


@pytest.mark.parametrize("tagged", [False, True])
def test_a_copy_that_stops_short_of_the_track_fails_the_job(env, tagged):
    s, path, tmp = env
    s.charts = False
    if tagged:  # mkvmerge's statistics tags say how many frames the source track holds
        remuxed = tmp / "remuxed.mkv"
        subprocess.run(["mkvmerge", "-q", "-o", str(remuxed), str(path)], check=True)
        shutil.move(str(remuxed), str(path))
    real = shutil.which("ffmpeg")
    s.ffmpeg = str(_exe(tmp / "ffmpeg-stops-early",  # the read ends early and ffmpeg still exits 0
                        '#!/bin/sh\ncase "$*" in *-progress*) '
                        f'exec "{real}" -t 5 "$@" ;; esac\nexec "{real}" "$@"\n'))
    pr = media.probe(str(path))
    before = _snapshot(path)
    with pytest.raises(JobFailed, match=r"holds \d+ of its \d+ frames"):
        _run(s, path, _pcm_params(pr), tmp)
    assert _snapshot(path) == before and _no_temp_left(path)
    assert "BEQ filter" not in (tmp / "job.log").read_text()


def test_a_timeline_with_no_frames_refuses_the_track(env):
    s, path, tmp = env
    real = shutil.which("ffprobe")
    s.ffprobe = str(_exe(tmp / "ffprobe-no-frames",
                         '#!/bin/sh\ncase "$*" in *pts_time,nb_samples*) exit 0 ;; esac\n'
                         f'exec "{real}" "$@"\n'))
    pr = media.probe(str(path))
    before = _snapshot(path)
    with pytest.raises(JobFailed, match="Could not map the .* timeline"):
        _run(s, path, _pcm_params(pr), tmp)
    assert _snapshot(path) == before


def test_a_film_that_starts_late_keeps_the_tracks_place_on_its_timeline(env):
    s, path, tmp = env
    s.charts = False
    late = tmp / "late.mkv"
    subprocess.run(["mkvmerge", "-q", "-o", str(late), "--sync", "0:1000", "--sync", "1:1000",
                    "--sync", "2:1000", str(path)], check=True)
    shutil.move(str(late), str(path))
    pts = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:1", "-show_entries",
                          "packet=pts_time", "-read_intervals", "%+#1", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout
    result, _ = _run(s, path, _pcm_params(media.probe(str(path))), tmp)
    assert result["sync"]["applied_ms"] == int(float(pts.strip().strip(",")) * 1000) > 900


def test_a_mux_that_leaves_the_beq_track_short_never_replaces_the_source(env):
    s, path, tmp = env
    s.charts = False
    real = shutil.which("mkvmerge")
    cut = tmp / "cut.flac"
    s.mkvmerge = str(_exe(tmp / "cutting-mkvmerge",
                          f"#!{sys.executable}\nimport os, subprocess, sys\n"
                          f"def beq(a):\n"
                          "    if not a.endswith('beq.flac'): return a\n"
                          "    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', a, '-t', '8',\n"
                          f"                    '-c', 'copy', {str(cut)!r}], check=True)\n"
                          f"    return {str(cut)!r}\n"
                          "args = sys.argv[1:]\n"
                          "if '--gui-mode' in args: args = [beq(a) for a in args]\n"
                          f"os.execv({real!r}, [{real!r}, *args])\n"))
    pr = media.probe(str(path))
    before = _snapshot(path)
    with pytest.raises(JobFailed, match=r"audio track .* has \d+ frames, expected \d+"):
        _run(s, path, _pcm_params(pr), tmp)
    assert _snapshot(path) == before and _no_temp_left(path)


def test_a_refused_chmod_does_not_stop_the_swap(env, monkeypatch):
    s, path, tmp = env
    s.charts = False
    real = os.chmod

    def refuse(target, mode, **kw):
        if ".beq-tmp" in str(target):
            raise PermissionError(1, "Operation not permitted")
        return real(target, mode, **kw)

    monkeypatch.setattr(os, "chmod", refuse)
    _run(s, path, _pcm_params(media.probe(str(path))), tmp)
    assert len(media.probe(str(path)).beq_tracks) == 1
    assert "could not carry the source's mode" in (tmp / "job.log").read_text()


def test_a_report_that_fails_after_the_swap_does_not_fail_the_job(env):
    s, path, tmp = env
    s.charts = False

    def progress(stage, _pct):
        if stage == "Done":
            raise OSError("the database went away")

    r = JobRunner(s, 1, str(path), _pcm_params(media.probe(str(path))), progress, tmp / "job.log")
    assert r.run()["phase"] == "encode"
    assert len(media.probe(str(path)).beq_tracks) == 1


def test_replacing_a_default_beq_track_gives_the_default_back_to_the_original(env):
    s, path, tmp = env
    s.charts = False
    _run(s, path, _pcm_params(media.probe(str(path)), default_flag=True), tmp)
    first = media.probe(str(path))
    assert first.beq_tracks[0].default and not first.audio[1].default
    _run(s, path, _pcm_params(first, default_flag=False), tmp)
    after = media.probe(str(path))
    assert not after.beq_tracks[0].default and after.audio[1].default


def _space_for(monkeypatch, free):
    usage = shutil.disk_usage(".")
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: usage._replace(free=int(free)))


@pytest.mark.parametrize("spare,fits", [(-1, False), (1_000_000, True)])
def test_the_space_check_wants_the_source_plus_the_new_track(env, monkeypatch, spare, fits):
    s, path, tmp = env
    s.charts = False
    pr = media.probe(str(path))
    track_bytes = pr.duration_s * 48000 * 3 * 2  # stereo FLAC of 20 s, at most raw 24-bit PCM
    _space_for(monkeypatch, path.stat().st_size * 1.01 + track_bytes + spare)
    if fits:
        assert _run(s, path, _pcm_params(pr), tmp)[0]["route"] == "pcm"
    else:
        with pytest.raises(JobFailed, match="Media volume needs"):
            _run(s, path, _pcm_params(pr), tmp)


@pytest.mark.parametrize("codec", ["ac3", "eac3"])
def test_a_finished_encode_can_be_retried_after_it_added_a_truehd_track(env, codec):
    s, path, tmp = env
    s.charts = False
    only = tmp / "only.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-map", "0:v",
                    "-map", "0:a:1", "-c:v", "copy", "-c:a", codec, "-b:a", "384k", str(only)],
                   check=True)
    shutil.move(str(only), str(path))
    pr = media.probe(str(path))
    assert not any("truehd" in t["codec"].lower() for t in pr.tracks)
    params = _params(pr, replace_existing_beq=True)
    first, _ = _run(s, path, params, tmp)
    assert any("truehd" in t["codec"].lower() for t in media.probe(str(path)).tracks)
    retried, _ = _run(s, path, {**params, "source": first["source"]}, tmp)
    assert retried["source"]["track"] == first["source"]["track"]


class _Array:
    def __init__(self, x):
        self.x, self.ch = x, x.shape[1]

    def read(self, n):
        out, self.x = self.x[:n], self.x[n:]
        return out


@pytest.mark.parametrize("error_lsb,ok", [(1.4, True), (1.6, False)])
def test_verify_allows_only_what_dither_and_rounding_can_do(error_lsb, ok):
    x = np.full((2000, 2), 0.25)
    out = x.copy()
    out[1000, 1] += error_lsb * pcmio.LSB24
    passthrough = np.array([[1.0, 0.0, 0.0, 1.0, 0.0, 0.0]])
    assert pcmio.verify(_Array(x), _Array(out), passthrough, 1.0, 0).ok is ok


def test_verify_never_passes_a_nan_gain():
    x = np.full((2000, 2), 0.25)
    passthrough = np.array([[1.0, 0.0, 0.0, 1.0, 0.0, 0.0]])
    assert pcmio.verify(_Array(x), _Array(x), passthrough, 1.0, 0).ok
    assert not pcmio.verify(_Array(x), _Array(x), passthrough, float("nan"), 0).ok


# -- cancel, a dead encoder, and the children of a job ----------------------------------
def _procs():
    return Processes(lambda _msg: None)


def test_every_child_is_reaped_when_the_job_ends():
    procs = _procs()
    p = procs.popen(["sleep", "300"])
    procs.reap()
    assert p.poll() is not None


def test_a_cancel_that_races_a_start_stops_that_process(monkeypatch):
    procs = _procs()
    real, started = subprocess.Popen, []

    def popen(*a, **k):
        p = real(*a, **k)
        started.append(p)
        procs.cancel()  # arrives after the process exists, before the job tracks it
        return p

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(Cancelled):
        procs.popen(["sleep", "300"])
    monkeypatch.undo()
    assert procs.live == started
    procs.reap()
    assert started[0].poll() is not None


def test_a_dead_encoder_fails_with_its_own_stderr():
    procs = _procs()
    with pytest.raises(JobFailed, match="WAV encode failed: No space left"):
        with procs.feed(["sh", "-c", "echo 'No space left on device' >&2; exit 1"],
                        "WAV encode") as emit:
            for _ in range(200):
                emit(b"\0" * (1 << 16))
    assert procs.live[0].poll() is not None


def test_a_feed_survives_a_tool_that_fills_its_stderr():
    procs = _procs()
    noisy = "head -c 300000 /dev/zero | tr '\\0' x >&2; exec cat > /dev/null"
    finished = threading.Event()

    def run():
        with procs.feed(["sh", "-c", noisy], "WAV encode") as emit:
            for _ in range(200):
                emit(b"\0" * (1 << 16))
        finished.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(60)
    assert finished.is_set(), "the feed stalled against the tool's full stderr pipe"


def test_a_cancel_during_a_feed_is_cancelled_and_reaps_the_encoder():
    procs = _procs()
    with pytest.raises(Cancelled):
        with procs.feed(["sh", "-c", "exec cat > /dev/null"], "WAV encode") as emit:
            procs.cancel()
            for _ in range(200):
                emit(b"\0" * (1 << 16))
    assert procs.live[0].poll() is not None


@pytest.mark.parametrize("stage", ["BEQ filter: writing FLAC", "Verifying the FLAC track"])
def test_cancelling_a_pcm_stream_is_recorded_as_cancelled_and_reaps(env, stage):
    s, path, tmp = env
    s.charts = False
    pr = media.probe(str(path))
    before = _snapshot(path)
    runner = []

    def progress(st, _pct):
        if st.startswith(stage) and runner:
            runner.pop().cancel()

    r = JobRunner(s, 1, str(path), _pcm_params(pr), progress, tmp / "job.log")
    runner.append(r)
    with pytest.raises(Cancelled):
        r.run()
    assert not runner, "the job never reached the stage"
    assert _snapshot(path) == before and _no_temp_left(path)
    assert not (s.work_dir / "job-1").exists()
    assert r.job.procs.live and all(p.poll() is not None for p in r.job.procs.live)


def test_an_encoder_that_dies_on_the_way_fails_the_job_with_its_error(env):
    s, path, tmp = env
    real = shutil.which("ffmpeg")
    s.ffmpeg = str(_exe(tmp / "ffmpeg-no-space",
                        '#!/bin/sh\ncase "$*" in *"-f s32le"*) echo "No space left on device" '
                        f'>&2; exit 1 ;; esac\nexec "{real}" "$@"\n'))
    with pytest.raises(JobFailed, match="FLAC encode failed: No space left"):
        _run(s, path, _pcm_params(media.probe(str(path))), tmp)


@pytest.mark.parametrize("route", ["damf", "joc"])
def test_an_atmos_track_with_a_timeline_gap_is_refused(env, monkeypatch, route):
    s, path, tmp = env
    split = path.with_name("head.mkv"), path.with_name("tail.mkv")
    gapped = path.with_name("Test Movie (2020) gap.mkv")
    for part, span in zip(split, ("00:00:00-00:00:06", "00:00:06-00:00:20")):
        subprocess.run(["mkvmerge", "-q", "-o", str(part), "--split", f"parts:{span}",
                        str(path)], check=True)
    subprocess.run(["mkvmerge", "-q", "-o", str(gapped), str(split[0]), "+", "--sync", "1:80",
                    str(split[1])], check=True)
    path = gapped
    if route == "joc":
        real = media.classify
        monkeypatch.setattr(media, "classify",
                            lambda *a: "eac3_joc" if a[1] == "truehd" else real(*a))
        exe = _exe(tmp / "cavern-dump", "#!/bin/sh\nexit 0\n")
        monkeypatch.setenv("CAVERN_DUMP", str(exe))
        s.cavern_dump = str(exe)
    pr = media.probe(str(path))
    before = _snapshot(path)
    with pytest.raises(JobFailed, match=r"gap\(s\) in its timeline"):
        _run(s, path, _params(pr), tmp)
    assert _snapshot(path) == before


def test_a_bad_track_name_template_fails_before_the_expensive_stages(env):
    s, path, tmp = env
    s.track_name_template = "BEQ {nope}"
    with pytest.raises(JobFailed, match="TRACK_NAME_TEMPLATE"):
        _run(s, path, _params(media.probe(str(path))), tmp)
    assert not (s.work_dir / "job-1").exists()


def test_a_beq_track_is_refused_as_a_source(env):
    s, path, tmp = env
    _run(s, path, _params(media.probe(str(path))), tmp)
    pr = media.probe(str(path))
    with pytest.raises(JobFailed, match="is itself a BEQ track"):
        _run(s, path, {**_params(pr), "track_id": pr.beq_tracks[0].track_id}, tmp)


# -- the BEQ sequence, byte for byte: seeded dither, pinned output ---------------------------
FILTERS = [FilterSpec("LowShelf", 40.0, -4.2, 1.0), FilterSpec("LowShelf", 22.0, 5.0, 0.9, count=3)]

# output of the two-pass 24-bit sequence over make_signal(frames=96000), recorded from the
# implementation these replace
GOLDEN_PCM = {
    (None, 6): ("c6ed96ac6c67c69b1750e3bfe0f084bfd52cbde007fb6125cbd592cf6075476c",
                "0x1.e0e8ecc088bc7p-1", 0),
    (None, 8): ("38f2ba710545776612fb0d8f457dc436007b72c6da906bbaa697987918aa1644",
                "0x1.e0e8ecc088bc7p-1", 0),
    (6.0, 6): ("f3a9701ae6f09d4760af37f00451d8c7a9279276cbf005a173d1b81b4d72c2e9",
               "0x1.fec982d5bb8afp+0", 82798),
    (6.0, 8): ("a37fa7b98c80fc8913987515ff49504da8693701717e93b7c87781030819bf47",
               "0x1.fec982d5bb8afp+0", 82801),
}
GOLDEN_CAF = {None: "2fc6ad35dd6b94c3ee0b513f7b287b8e1a2288065f151a45a3a7c92d68c4c584",
              6.0: "b18fbeed05d7cbeb862359077b413c59010f958b2aa436593b7091ad85b1c875"}


def _seed(monkeypatch):
    monkeypatch.setattr(np.random, "default_rng",
                        lambda *_a: np.random.Generator(np.random.PCG64(1)))


@pytest.mark.parametrize("fixed", [None, 6.0])
@pytest.mark.parametrize("ch,order", [(6, None), (8, [0, 1, 2, 3, 6, 7, 4, 5])])
def test_the_pcm_route_writes_its_golden_bytes(fixed, ch, order):
    x = make_signal(frames=48000 * 2, ch=ch)
    sos = design_sos(FILTERS, 48000.0)
    written = bytearray()

    def blocks(_stage):
        return (x[i:i + 4096] for i in range(0, len(x), 4096))

    res = filter_stream(blocks, block_writer(written.extend, order), sos, ch, -1.0, fixed,
                        pcmio.LSB24, rng=np.random.default_rng(1))
    digest, gain, clipped = GOLDEN_PCM[fixed, ch]
    assert hashlib.sha256(bytes(written)).hexdigest() == digest
    assert res.gain == float.fromhex(gain) and res.clipped == clipped


@pytest.mark.parametrize("fixed", [None, 6.0])
def test_the_caf_route_writes_its_golden_bytes_in_a_new_file_and_in_place(tmp_path, monkeypatch,
                                                                         fixed):
    x = make_signal(frames=48000 * 2, ch=12)
    src = tmp_path / "s.caf"
    write_caf(src, x)
    job = FilterJob(FILTERS, -1.0, fixed)
    _seed(monkeypatch)
    apply(str(src), job, out_path=str(tmp_path / "new.caf"))
    assert hashlib.sha256((tmp_path / "new.caf").read_bytes()).hexdigest() == GOLDEN_CAF[fixed]
    _seed(monkeypatch)
    apply(str(src), job)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == GOLDEN_CAF[fixed]
