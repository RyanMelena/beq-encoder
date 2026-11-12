"""The web flow through the real routes and job queue: Run -> ready for review ->
Encode with this filter -> done, and Run -> no BEQ. Fake designer and DEE as elsewhere."""

import importlib
import json
import re
import shutil
import threading
import time

import pytest

from .test_tracks import CAT, SOURCES, FakeDesigner, _t, sources  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("mkvmerge")), reason="needs ffmpeg + mkvtoolnix"
)


@pytest.fixture
def app(tmp_path, sources, monkeypatch):  # noqa: F811
    media_dir = tmp_path / "media" / "Test Movie (2020) {tmdb-333371}"
    media_dir.mkdir(parents=True)
    for name in ("lpcm20", "dts51"):
        shutil.copy(sources[name], media_dir / f"Test Movie (2020) {name}.mkv")
    cfg = tmp_path / "config"
    (cfg / "catalogue").mkdir(parents=True)
    (cfg / "catalogue" / "database.json").write_text(json.dumps(CAT))
    fd = FakeDesigner(tmp_path / "work")
    monkeypatch.setenv("ALLOW_TEST_FORMATS", "true")
    monkeypatch.setenv("MEDIA_ROOTS", str(tmp_path / "media"))
    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("DEE_PATH", str(sources["dee"]))
    monkeypatch.setenv("DEE_SHIM", str(sources["shim"]))
    monkeypatch.setenv("DESIGNER_URL", fd.url)
    monkeypatch.setenv("CATALOGUE_URL", "http://127.0.0.1:9/none")
    monkeypatch.setenv("FAKE_DEE_ARGS", str(tmp_path / "dee-args.txt"))
    from fastapi.testclient import TestClient

    from beq_encoder import media, web

    real = media.classify
    monkeypatch.setattr(media, "classify",
                        lambda *a: "dtshd_ma" if real(*a) == "dts" else real(*a))
    web = importlib.reload(web)
    with TestClient(web.app) as c:
        for _ in range(50):
            if web.catalogue.entries:
                break
            time.sleep(0.1)
        yield c, web, media_dir
    fd.close()


def _wait(web, job_id, statuses, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        row = web.db.one("SELECT status, message FROM jobs WHERE id=?", (job_id,))
        if row["status"] in statuses:
            return row
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} stuck at {row['status']}: {row['message']}")


def _media_id(web, path):
    return web.scan_file(web.S, web.db, path)


BROWSER = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def test_browsers_get_an_error_page_in_the_app_shell(app):
    c, _, _ = app
    for url, heading in (("/media/99999", "Film not found"), ("/jobs/99999", "Job not found"),
                         ("/no/such/page", "Page not found"), ("/media/abc", "Page not found")):
        r = c.get(url, headers=BROWSER)
        assert r.status_code >= 400 and r.headers["content-type"].startswith("text/html"), url
        assert f"<h1>{heading}</h1>" in r.text and f"<title>{heading} · BEQ Encoder</title>" in r.text
        assert '<html lang="en">' in r.text and 'class="brand"' in r.text and 'href="/jobs"' in r.text


def test_failed_path_forms_return_to_the_library_at_the_field(app):
    c, _, _ = app
    r = c.post("/media/add", data={"path": "/nope/<b>x</b>.mkv", "show": "stale", "q": "bla"},
               headers=BROWSER)
    assert r.status_code == 400 and "<h1>Library</h1>" in r.text
    assert "That path is not an existing .mkv file." in r.text
    assert 'value="/nope/&lt;b&gt;x&lt;/b&gt;.mkv"' in r.text and "<b>x</b>" not in r.text
    assert '<details class="lib-add" open>' in r.text and 'aria-invalid="true"' in r.text
    assert 'class="n-warn on"' in r.text or 'class=" on"' in r.text
    assert 'value="bla"' in r.text
    r = c.post("/scan", data={"folder": "/etc"}, headers=BROWSER)
    assert r.status_code == 400 and "<h1>Library</h1>" in r.text
    assert "Folder must be an existing folder under the media roots." in r.text
    assert 'id="f-folder" type="text" name="folder" required value="/etc"' in r.text
    r = c.post("/media/add", data={"path": ""}, headers=BROWSER)
    assert r.status_code == 422 and "No path was entered." in r.text and "Enter a path" in r.text


def test_all_files_includes_unreadable_files_and_counts_add_up(app):
    c, web, d = app
    web.scan_file(web.S, web.db, d / "Test Movie (2020) lpcm20.mkv")
    # A real file, so the start-up scan running alongside cannot prune it as a deleted one.
    (d / "broken.mkv").write_bytes(b"not a matroska file")
    web.scan_file(web.S, web.db, d / "broken.mkv")
    page = c.get("/?show=all").text
    assert "broken.mkv" in page and "lpcm20" in page
    count = {key: int(re.search(rf'show={key}&q=" class="[^"]*"[^>]*><b>(\d+)</b>', page)[1])
             for key in web.LIBRARY_VIEWS}
    assert count["errors"] == 1 and count["all"] == count["beq"] + count["todo"] + count["errors"]
    assert '<a class="btn ghost" href="/media/' in page


def test_api_and_json_clients_keep_json_errors(app):
    c, _, _ = app
    for url, headers in (("/media/99999", {}), ("/media/99999", {"accept": "application/json"}),
                         ("/api/jobs/99999", BROWSER)):
        r = c.get(url, headers=headers)
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}, (url, headers)
    r = c.post("/media/add", data={"path": "/nope.mkv"}, headers={"accept": "application/json"})
    assert r.status_code == 400 and r.json() == {"detail": "Not an existing .mkv file"}
    r = c.post("/media/add", data={"path": ""})
    assert r.status_code == 422 and r.json()["detail"][0]["type"] == "missing"


def test_add_file_scans_in_the_background(app):
    c, web, d = app
    path = d / "Test Movie (2020) lpcm20.mkv"
    r = c.post("/media/add", data={"path": str(path)}, follow_redirects=False)
    assert r.status_code == 303 and "show=all" in r.headers["location"]
    for _ in range(100):
        if web.db.one("SELECT id FROM media WHERE path=?", (str(path),)):
            break
        time.sleep(0.1)
    assert web.db.one("SELECT has_audio FROM media WHERE path=?", (str(path),))["has_audio"] == 1
    # every scanned file is assessed; the library shows it
    for _ in range(50):
        row = web.db.one("SELECT assess_json FROM media WHERE path=?", (str(path),))
        if row["assess_json"]:
            break
        time.sleep(0.1)
    a = json.loads(row["assess_json"])
    assert a["best"]["label"] == "LPCM 2.0"
    for show in ("", "candidates", "todo", "stale", "beq", "all", "errors"):
        page = c.get(f"/?show={show}")
        assert page.status_code == 200 and "Stale BEQ" in page.text
    assert "lpcm20" in c.get("/?show=all").text
    # what DEE can make right now, stated on the library page (the fake DEE)
    text = " ".join(re.sub(r"<[^>]+>", "", c.get("/").text).split())
    assert "Output: TrueHD Atmos and TrueHD (DEE)" in text
    lib = c.get("/api/library").json()
    assert {"watch", "audio", "scan", "pending"} <= set(lib)
    mid = web.db.one("SELECT id FROM media WHERE path=?", (str(path),))["id"]
    assert "Bass check" in c.get(f"/media/{mid}").text or "catalogue" in c.get(f"/media/{mid}").text


def test_run_review_choose_encode(app):
    c, web, d = app
    path = d / "Test Movie (2020) lpcm20.mkv"
    mid = _media_id(web, path)
    page = c.get(f"/media/{mid}").text
    assert "LPCM" in page and ">Best track<" in page and "Director" in page
    r = c.post(f"/media/{mid}/run", data={}, follow_redirects=False)
    job_id = int(r.headers["location"].rsplit("/", 1)[1])
    row = _wait(web, job_id, {"awaiting", "failed", "no_beq"})
    assert row["status"] == "awaiting", row["message"]
    page = c.get(f"/jobs/{job_id}").text
    assert "beqforge 9.9.9 flatten" in page and "Encode with this filter" in page
    assert "Ready for review" in page and "Don&#39;t add BEQ" in page and "Cancel job" not in page
    assert "introduces a cliff" in page  # the rejected design, review only
    added = time.strftime("%Y-%m-%d", time.localtime(1543898641))
    updated = time.strftime("%Y-%m-%d", time.localtime(1611984156))
    assert f"<dt>Added</dt><dd>{added}</dd>" in page
    assert f"<dt>Updated</dt><dd>{updated}</dd>" in page
    assert 'value="flac"' in page and 'value="ddp"' not in page
    # a second Run is refused while one is ready for review
    assert c.post(f"/media/{mid}/run", data={}).status_code == 409
    j = web._job_dict(web.db.one("SELECT * FROM jobs WHERE id=?", (job_id,)))
    rejected = j["prepare"]["design"]["rejected"][0]["digest"]
    assert c.post(f"/jobs/{job_id}/encode", data={"digest": rejected, "format": "flac"}
                  ).status_code == 400
    digest = j["prepare"]["design"]["candidates"][0]["digest"]
    assert digest in j["prepare"]["predicted"] and rejected not in j["prepare"]["predicted"]
    assert re.search(r"<dt>Estimated gain</dt><dd>about -\d+ dB · set by [LR]</dd>", page)
    r = c.post(f"/jobs/{job_id}/encode", data={"digest": digest, "format": "flac",
                                                "replace_existing_beq": "on"},
               follow_redirects=False)
    assert r.status_code == 303
    row = _wait(web, job_id, {"done", "failed"})
    assert row["status"] == "done", row["message"]
    assert "BEQ FLAC 2.0 (beqforge 9.9.9 flatten)" in row["message"]
    assert "BEQ FLAC 2.0 (beqforge 9.9.9 flatten)" in c.get(f"/media/{mid}").text
    page = c.get(f"/jobs/{job_id}").text
    assert "every sample verified" in page
    assert f'src="/jobs/{job_id}/charts/spectrum"' in page and "charts/heatmap" in page
    for kind in ("spectrum", "heatmap"):
        r = c.get(f"/jobs/{job_id}/charts/{kind}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert r.content[:4] == b"\x89PNG"
    assert c.get(f"/jobs/{job_id}/charts/report").json()["fidelity"]["ok"]
    assert c.get(f"/jobs/{job_id}/charts/nope").status_code == 404
    assert f"/jobs/{job_id}/charts/spectrum" in c.get(f"/media/{mid}").text


def test_run_ends_no_beq_for_catalogue_no_filter_entry(app):
    c, web, d = app
    mid = _media_id(web, d / "Test Movie (2020) dts51.mkv")
    r = c.post(f"/media/{mid}/run", data={}, follow_redirects=False)
    job_id = int(r.headers["location"].rsplit("/", 1)[1])
    row = _wait(web, job_id, {"no_beq", "failed", "awaiting"})
    assert row["status"] == "no_beq" and "does not need BEQ" in row["message"]
    page = c.get(f"/jobs/{job_id}").text
    assert "Design a filter" in page and "Choose a filter anyway" in page
    r = c.post(f"/jobs/{job_id}/review", follow_redirects=False)
    assert r.status_code == 303
    row = web.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert row["status"] == "awaiting"
    page = c.get(f"/jobs/{job_id}?q=Test+Movie").text
    assert "Encode with this filter" in page and "<b>Test Movie (2020)</b>" in page
    assert not re.search(r'name="digest" value="[^"]+" checked', page)  # no silent default
    assert c.post(f"/jobs/{job_id}/review").status_code == 409
    r = c.post(f"/jobs/{job_id}/design", follow_redirects=False)
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    row = _wait(web, new_id, {"awaiting", "failed", "no_beq"})
    assert row["status"] == "awaiting", row["message"]
    assert 'value="truehd"' in c.get(f"/jobs/{new_id}").text  # TrueHD from PCM offered


def test_a_film_row_gets_one_verdict(app):
    _, web, _ = app
    best = {"label": "DTS-HD MA 5.1", "supported": True}
    a = {"best": best, "catalogue": "other_track", "candidate": "maybe"}
    none = {"verdict": "none", "depth15_db": 3.0}
    wedding_ringer = web.film_verdict(a, none)
    assert wedding_ringer["state"] == "maybe" and "another track" in wedding_ringer["note"]
    dune = web.film_verdict({"best": best, "catalogue": "none", "candidate": None}, none, "no_beq")
    assert dune["state"] == "no_need"
    declined = web.film_verdict({"best": best, "catalogue": "none", "candidate": "rolloff"},
                                {"verdict": "clear", "corner_hz": 27.0, "depth15_db": 21.0,
                                 "slope_db_oct": 24, "slope_at_hz": 22, "plateau_dbfs": -31,
                                 "seconds": 300}, "no_beq")
    assert declined["state"] == "maybe"
    clear = {"verdict": "clear", "corner_hz": 27.0, "depth15_db": 21.0, "slope_db_oct": 24,
             "slope_at_hz": 22, "plateau_dbfs": -31, "seconds": 300}
    rolled = web.film_verdict({"best": best, "catalogue": "none", "candidate": "rolloff"}, clear)
    assert web.FILM_STATE[rolled["state"]][0] == "Bass roll-off"
    listed = web.film_verdict({"best": best, "catalogue": "exact", "candidate": "catalogue",
                               "authors": ["a1"], "catalogue_count": 1}, none)
    assert web.FILM_STATE[listed["state"]][0] == "Catalogue filter"
    sicario = web.film_verdict({"best": best, "catalogue": "none", "candidate": None},
                               {"verdict": "error", "error": "ffmpeg exited 1"})
    assert sicario["state"] == "check_failed" and web.FILM_STATE["check_failed"][2] != "bad"
    assert web.file_note("/m/Dune - Part Two (2024) {edition-Cut} [Remux].mkv", "Dune: Part Two",
                         "2024") == "Cut · Remux"
    assert web.file_note("/m/other.mkv", "Dune", "2024") == "other.mkv"


def test_failed_job_headline_is_plain_and_keeps_the_measurements(app):
    c, web, d = app
    mid = _media_id(web, d / "Test Movie (2020) lpcm20.mkv")
    raw = "Lossless check failed: channel 4 differs by 3.2 LSB at frame 8214403"
    job_id = web.db.create_job(mid, str(d / "x.mkv"), {"phase": "prepare"})
    web.db.update_job(job_id, status="failed", stage="Failed", message=raw)
    api = c.get(f"/api/jobs/{job_id}").json()
    assert "LSB" not in api["detail"] and api["raw_detail"] == raw and api["message"] == raw
    page = c.get(f"/jobs/{job_id}").text
    assert raw in page and "LSB" not in page.split('id="msg"')[1].split("</p>")[0]


def test_each_job_status_has_one_label_everywhere(app):
    c, web, d = app
    mid = _media_id(web, d / "Test Movie (2020) lpcm20.mkv")
    for status, (label, symbol) in web.JOB_STATUS.items():
        job_id = web.db.create_job(mid, str(d / f"{status}.mkv"), {"phase": "prepare"})
        stage = "Encoding with DEE" if status == "running" else label
        web.db.update_job(job_id, status=status, stage=stage, message=f"{label}: the reason")
        api = c.get(f"/api/jobs/{job_id}").json()
        assert api["status"] == status and api["message"] == f"{label}: the reason"
        assert (api["status_label"], api["status_symbol"]) == (label, symbol)
        badge = f'class="badge s-{status}" data-sym="{symbol}"'
        for url in (f"/jobs/{job_id}", "/jobs", f"/media/{mid}"):
            assert re.search(badge + r'[^>]*>' + re.escape(label) + "</span>", c.get(url).text), url
        top = re.sub(r"<[^>]+>", " ", c.get(f"/jobs/{job_id}").text.split("<h1", 1)[1].split("<h3", 1)[0])
        assert top.count(label) == 1 and "the reason" in top, (status, top)
    assert ">no_beq<" not in c.get("/jobs").text


def test_scan_one_folder(app):
    c, web, d = app
    assert c.post("/scan", data={"folder": "/etc"}).status_code == 400
    r = c.post("/scan", data={"folder": str(d)}, follow_redirects=False)
    assert r.status_code == 303
    for _ in range(100):
        if not web.scanner.state["running"] and web.scanner.state["done"]:
            break
        time.sleep(0.1)
    paths = {row["path"] for row in web.db.q("SELECT path FROM media")}
    assert {str(p) for p in d.glob("*.mkv")} <= paths


def _awaiting(c, web, path):
    """Run the prepare phase on `path`; (job id, digest of the designer's candidate)."""
    mid = _media_id(web, path)
    r = c.post(f"/media/{mid}/run", data={}, follow_redirects=False)
    job_id = int(r.headers["location"].rsplit("/", 1)[1])
    row = _wait(web, job_id, {"awaiting", "failed", "no_beq"})
    assert row["status"] == "awaiting", row["message"]
    j = web._job_dict(web.db.one("SELECT * FROM jobs WHERE id=?", (job_id,)))
    return job_id, j["prepare"]["design"]["candidates"][0]["digest"]


def test_a_job_prepared_before_gain_estimates_still_shows_its_choices(app):
    c, web, d = app
    job_id, _ = _awaiting(c, web, d / "Test Movie (2020) lpcm20.mkv")
    prep = json.loads(web.db.one("SELECT prepare_json FROM jobs WHERE id=?", (job_id,))["prepare_json"])
    del prep["predicted"]
    web.db.update_job(job_id, prepare_json=json.dumps(prep))
    r = c.get(f"/jobs/{job_id}")
    assert r.status_code == 200 and "Encode with this filter" in r.text
    assert "Estimated gain" not in r.text


def test_two_choices_of_one_job_queue_it_once(app, monkeypatch):
    from fastapi.testclient import TestClient

    c, web, d = app
    job_id, digest = _awaiting(c, web, d / "Test Movie (2020) lpcm20.mkv")
    real_choices = web._choices
    both_read_the_status = threading.Barrier(2, timeout=30)

    def choices(job, q=""):
        both_read_the_status.wait()
        return real_choices(job, q)

    monkeypatch.setattr(web, "_choices", choices)
    codes = []

    def choose():
        r = TestClient(web.app).post(f"/jobs/{job_id}/encode",
                                     data={"digest": digest, "format": "flac"},
                                     follow_redirects=False)
        codes.append(r.status_code)

    threads = [threading.Thread(target=choose) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(codes) == [303, 409]
    assert _wait(web, job_id, {"done", "failed"})["status"] == "done"


@pytest.mark.parametrize("form", [
    {"gain_mode": "fixed", "fixed_gain_db": "nan"},
    {"gain_mode": "fixed", "fixed_gain_db": "inf"},
    {"gain_mode": "fixed", "fixed_gain_db": "1000"},
    {"target_peak_dbfs": "nan"},
    {"target_peak_dbfs": "-inf"},
    {"target_peak_dbfs": "3"},
    {"target_peak_dbfs": "-600"},
])
def test_an_invalid_gain_is_rejected_and_the_job_keeps_waiting(app, form):
    c, web, d = app
    job_id, digest = _awaiting(c, web, d / "Test Movie (2020) lpcm20.mkv")
    r = c.post(f"/jobs/{job_id}/encode", data={"digest": digest, "format": "flac", **form},
               follow_redirects=False)
    assert r.status_code == 400
    assert web.db.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "awaiting"


def test_only_one_active_job_per_file_through_every_route(app):
    c, web, d = app
    path = d / "Test Movie (2020) dts51.mkv"
    mid = _media_id(web, path)
    first = int(c.post(f"/media/{mid}/run", data={}, follow_redirects=False)
                .headers["location"].rsplit("/", 1)[1])
    assert _wait(web, first, {"no_beq", "failed"})["status"] == "no_beq"
    # a design job now holds the file; the finished job's own actions must not add another
    second = int(c.post(f"/media/{mid}/run", data={"force_designer": "on"}, follow_redirects=False)
                 .headers["location"].rsplit("/", 1)[1])
    assert _wait(web, second, {"awaiting", "failed"})["status"] == "awaiting"
    assert c.post(f"/jobs/{first}/design").status_code == 409
    assert c.post(f"/jobs/{first}/review").status_code == 409
    assert c.post(f"/jobs/{first}/retry").status_code == 409
    assert c.post(f"/media/{mid}/run", data={}).status_code == 409
    n = web.db.one("SELECT COUNT(*) n FROM jobs WHERE path=?", (str(path),))["n"]
    assert n == 2


def test_not_adding_beq_leaves_the_film_as_a_decision(app):
    c, web, d = app
    job_id, _ = _awaiting(c, web, d / "Test Movie (2020) lpcm20.mkv")
    assert c.post(f"/jobs/{job_id}/cancel", follow_redirects=False).status_code == 303
    row = web.db.one("SELECT status, stage FROM jobs WHERE id=?", (job_id,))
    assert (row["status"], row["stage"]) == ("cancelled", "Discarded")
    text = " ".join(re.sub(r"<[^>]+>", "", c.get(f"/jobs/{job_id}").text).split())
    assert "Left without BEQ" in text and "You chose not to add BEQ" in text
    assert "Stopped before it finished" not in text


def test_encode_and_retry_are_bound_to_the_file_the_job_read(app):
    c, web, d = app
    path = d / "Test Movie (2020) lpcm20.mkv"
    job_id, digest = _awaiting(c, web, path)
    original = path.read_bytes()
    path.write_bytes(original + b"replaced while waiting for review")
    assert c.post(f"/jobs/{job_id}/encode", data={"digest": digest, "format": "flac"},
                  follow_redirects=False).status_code == 409
    assert web.db.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "awaiting"

    path.write_bytes(original)
    c.post(f"/jobs/{job_id}/cancel", follow_redirects=False)
    job_id, digest = _awaiting(c, web, path)
    assert c.post(f"/jobs/{job_id}/encode", data={"digest": digest, "format": "flac"},
                  follow_redirects=False).status_code == 303
    assert _wait(web, job_id, {"done", "failed"})["status"] == "done"

    # a finished job may run again on the file it left, not on anything else
    r = c.post(f"/jobs/{job_id}/retry", follow_redirects=False)
    assert r.status_code == 303
    retried = int(r.headers["location"].rsplit("/", 1)[1])
    assert _wait(web, retried, {"done", "failed"})["status"] == "done"
    with open(path, "ab") as fh:
        fh.write(b"the file changed afterwards")
    assert c.post(f"/jobs/{job_id}/retry", follow_redirects=False).status_code == 409


def test_a_beq_track_cannot_be_the_source_of_a_run(app):
    from beq_encoder import media as media_mod

    c, web, d = app
    beq = _t(1, "truehd", 2, name="BEQ FLAC 2.0 (a1)", beq=True, layout="stereo",
             language="eng")
    probe = media_mod.ProbeResult(path=str(d / "x.mkv"), duration_s=1.0, tracks=[], audio=[beq])
    mid = web.db.upsert_media(path=str(d / "x.mkv"), size=1, mtime=1.0, has_audio=1, has_beq=1,
                              probe_json=probe.to_json())
    r = c.post(f"/media/{mid}/run", data={"track_id": 1})
    assert r.status_code == 400 and "BEQ track" in r.text
    assert web.db.one("SELECT COUNT(*) n FROM jobs WHERE path=?", (str(d / "x.mkv"),))["n"] == 0


def test_submitted_paths_are_normalised(app, monkeypatch):
    c, web, d = app
    started: list[dict] = []
    monkeypatch.setattr(web.scanner, "start", lambda **kw: started.append(kw) or True)
    path = d / "Test Movie (2020) lpcm20.mkv"
    # The start-up scan can also call start() from its own thread, so look for the
    # request's call among the recorded ones instead of taking the last one.
    r = c.post("/media/add", data={"path": str(d / "sub" / ".." / path.name)},
               follow_redirects=False)
    assert r.status_code == 303 and {"force": True, "files": [path]} in started
    r = c.post("/scan", data={"folder": f"{d}/./sub/.."}, follow_redirects=False)
    assert r.status_code == 303 and any(kw.get("root") == d for kw in started)


def test_a_designed_track_is_read_from_its_tags_whatever_its_name(app, monkeypatch):
    from beq_encoder import media as media_mod

    c, web, d = app
    designer = {"designer_release": "9.9.9", "design_version": 8}
    monkeypatch.setattr(web, "designer_status", lambda max_age=30.0: designer)
    source = _t(0, "lpcm", 2, layout="stereo")
    designed = _t(1, "truehd", 2, name="Custom Name", beq=True, layout="stereo",
                  beq_tags={"source": "designer", "design_version": "8"})
    catalogued = _t(2, "truehd", 2, name="BEQ other", beq=True, layout="stereo",
                    beq_tags={"source": "catalogue"})
    probe = media_mod.ProbeResult(path="/x.mkv", duration_s=1.0, tracks=[],
                                  audio=[source, designed, catalogued])
    row = {"path": "/x.mkv", "tmdb": None, "title": None, "year": None, "edition": None}
    assert web._existing_beq_notes(row, probe) == []  # a new release alone changes no answer
    designer["design_version"] = 9
    notes = web._existing_beq_notes(row, probe)
    assert len(notes) == 1 and "Custom Name" in notes[0] and "design version 8 → 9" in notes[0]
