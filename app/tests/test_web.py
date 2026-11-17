"""The web flow through the real routes and job queue: Library -> film detail page -> wizard
-> a BEQ track or a BEQ clip, with BEQ Forge and the measurements in the background. Fake
designer and DEE as elsewhere (docs/ux-build-spec.md has the checks these cover)."""

import importlib
import json
import os
import re
import shutil
import time
from html import unescape

import pytest

from .test_tracks import CAT, FakeDesigner, sources  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("mkvmerge")), reason="needs ffmpeg + mkvmerge"
)

# A catalogue filter made for the LPCM 2.0 track of the test film, so that it has a default.
PCM = {"title": "Test Movie", "year": 2020, "theMovieDB": "333371", "audioTypes": ["LPCM 2.0"],
       "author": "p1", "source": "Disc", "digest": "pcm20", "content_type": "film",
       "catalogue_url": "https://beqcatalogue.example/test-movie",
       "filters": [{"type": "LowShelf", "freq": 22, "gain": 4, "q": 0.8, "count": 1}]}
BROWSER = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
# A primary button's text: a button that is neither a ghost button nor a tooltip's.
PRIMARY = re.compile(r'<button(?![^>]*(?:ghost|class="tip"))[^>]*>(.*?)</button>', re.S)


@pytest.fixture
def app(tmp_path, sources, monkeypatch):  # noqa: F811
    media_dir = tmp_path / "media" / "Test Movie (2020) {tmdb-333371}"
    media_dir.mkdir(parents=True)
    for name in ("lpcm20", "dts51"):
        shutil.copy(sources[name], media_dir / f"Test Movie (2020) {name}.mkv")
    cfg = tmp_path / "config"
    (cfg / "catalogue").mkdir(parents=True)
    (cfg / "catalogue" / "database.json").write_text(json.dumps([*CAT, PCM]))
    fd = FakeDesigner(tmp_path / "work")
    env = {"ALLOW_TEST_FORMATS": "true", "MEDIA_ROOTS": str(tmp_path / "media"),
           "CONFIG_DIR": str(cfg), "WORK_DIR": str(tmp_path / "work"),
           "CLIP_DIR": str(tmp_path / "clips"), "DEE_PATH": str(sources["dee"]),
           "DEE_SHIM": str(sources["shim"]), "DESIGNER_URL": fd.url,
           "CATALOGUE_URL": "http://127.0.0.1:9/none", "ASSESS_AUDIO": "false",
           "FAKE_DEE_ARGS": str(tmp_path / "dee-args.txt")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
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


def _film(web, d, name="lpcm20"):
    mid = web.scan_file(web.S, web.db, d / f"Test Movie (2020) {name}.mkv")
    web.library_watch.reassess([mid])
    return mid


def _wait(web, job_id, statuses=("done", "failed", "cancelled"), timeout=120, table="jobs"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        row = web.db.one(f"SELECT * FROM {table} WHERE id=?", (job_id,))
        if row["status"] in statuses:
            return row
        time.sleep(0.2)
    raise AssertionError(f"{table} {job_id} stuck at {row['status']}: {row['message']}")


def _settle(web, mid):
    """Let the work the pages started end, so the next step reads a quiet film."""
    for r in web.db.q("SELECT id FROM jobs WHERE media_id=? AND status IN ('queued', 'running')",
                      (mid,)):
        _wait(web, r["id"])


def _text(html: str) -> str:
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _links(html: str) -> dict[str, str]:
    """Each link's words to its address."""
    return {_text(m[2]): unescape(m[1]) for m in re.finditer(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                                                            html, re.S)}


class Browser:
    """Follows the page's own links and forms the way a person clicks them, and counts the
    clicks."""

    def __init__(self, c):
        self.c, self.clicks, self.page, self.url = c, 0, "", ""

    def open(self, url):
        r = self.c.get(url, headers=BROWSER)
        assert r.status_code == 200, (url, r.status_code)
        self.page, self.url = r.text, str(r.url)
        return self

    def link(self, words, href=None):
        """Follow the link with these words, or the one with these words to `href`."""
        self.clicks += 1
        if href is not None:
            assert re.search(rf'<a [^>]*href="{re.escape(href)}"[^>]*>\s*{re.escape(words)}',
                             self.page), (words, href)
            return self.open(href)
        return self.open(_links(self.page)[words])

    def button(self, words):
        """Submit the form that holds the button with these words, as the button would."""
        self.clicks += 1
        for form in re.finditer(r"<form([^>]*)>(.*?)</form>", self.page, re.S):
            m = re.search(rf'<button([^>]*)>\s*{re.escape(words)}\s*</button>', form[2])
            if not m:
                continue
            attrs = dict(re.findall(r'(\w+)="([^"]*)"', form[1]))
            fields = []
            # A select's options belong to that select alone, wherever it sits in the form.
            for tag in re.finditer(r"<(?:(select)|input)([^>]*)>(?(1)(.*?</select>))", form[2],
                                   re.S):
                a = dict(re.findall(r'([\w-]+)="([^"]*)"', tag[2]))
                checked = re.search(r"\bchecked\b", tag[2])
                if "name" not in a or "disabled" in tag[2].split(">")[0].split():
                    continue
                if tag[1] == "select":
                    chosen = re.search(r'<option value="([^"]*)"[^>]*\bselected', tag[3] or "") or \
                        re.search(r'<option value="([^"]*)"', tag[3] or "")
                    fields.append((a["name"], unescape(chosen[1]) if chosen else ""))
                elif a.get("type") in ("radio", "checkbox"):
                    if checked:
                        fields.append((a["name"], unescape(a.get("value", "on"))))
                else:
                    fields.append((a["name"], unescape(a.get("value", ""))))
            b = dict(re.findall(r'(\w+)="([^"]*)"', m[1]))
            if "name" in b:
                fields.append((b["name"], b.get("value", "")))
            action = unescape(attrs.get("action", self.url)).split("#")[0]
            if attrs.get("method", "get").lower() == "post":
                form_data: dict[str, list[str]] = {}
                for k, v in fields:
                    form_data.setdefault(k, []).append(v)
                r = self.c.post(action, data=form_data, headers=BROWSER)
            else:
                r = self.c.get(action, params=fields, headers=BROWSER)
            assert r.status_code == 200, (words, r.status_code, r.text[:300])
            self.page, self.url = r.text, str(r.url)
            return self
        raise AssertionError(f"no button {words!r} on {self.url}")


def test_one_click_from_the_library_shows_a_films_filters_with_no_job(app):
    c, web, d = app
    mid = _film(web, d)
    b = Browser(c).open("/")
    b.link("Test Movie (2020)", f"/films/{mid}")
    assert b.clicks == 1 and b.url.endswith(f"/films/{mid}")
    assert "Filters (" in b.page and "BEQ Catalogue · p1" in b.page
    assert "BEQ Catalogue page" in b.page and "Made for Track" in _text(b.page)
    assert web.db.one("SELECT COUNT(*) n FROM jobs")["n"] == 0, "seeing filters starts no job"


def test_three_clicks_from_the_film_add_a_beq_track_with_the_defaults(app):
    c, web, d = app
    mid = _film(web, d)
    b = Browser(c).open(f"/films/{mid}")
    head = b.page.split('class="film-head"')[1].split("</header>")[0]
    assert len(re.findall(r'class="btn"', head)) == 1, "one primary button"
    b.link("Add BEQ track")
    assert "Add a BEQ track to Test Movie (2020)" in b.page
    assert re.search(r'name="filter"[^>]*value="pcm20" checked', b.page), "the default filter"
    b.button("Continue to options")
    assert "changes the film file" in b.page
    b.button("Add BEQ track")
    assert b.clicks == 3 and f"/films/{mid}" in b.url and "Adding a BEQ track" in b.page
    job = web.db.one("SELECT * FROM jobs WHERE media_id=? AND phase='encode'", (mid,))
    params = json.loads(job["params_json"])
    assert params["entry"]["digest"] == "pcm20" and params["highpass_hz"] == web.S.highpass_hz
    row = _wait(web, job["id"])
    assert row["status"] == "done", row["message"]
    _settle(web, mid)
    page = c.get(f"/films/{mid}").text
    assert "BEQ track added" in page and "Back to the Library" in page
    assert re.search(r"quieter than the original track|same level|louder", page)
    assert "In use" in page and 'id="track-2" class="is-beq"' in page
    head = _text(page.split('class="film-head"')[1].split("</header>")[0])
    assert "Has a BEQ track Track 2 , made with BEQ Catalogue · p1 (LPCM 2.0)" in head
    assert re.search(r"quieter than the original track|same level|louder", head), \
        "the heading says what to do with the volume, whatever the age of the result"
    assert ">Add BEQ track</a>" in page, "a BEQ track does not finish a film"
    assert c.get(f"/jobs/{row['id']}/charts/spectrum").content[:4] == b"\x89PNG"
    assert re.search(r"Adding a BEQ track Test Movie \(2020\) BEQ track added Level change \S+ dB",
                     _text(c.get("/activity").text)), \
        "a finished row says what, the film, how it ended and its result, in that order"


def test_a_file_the_app_cannot_read_says_so_once_beside_rescan_file(app):
    c, web, d = app
    mid = web.db.upsert_media(path=str(d / "Broken (2020).mkv"), size=1, mtime=1.0,
                              title="Broken", year="2020", error="mkvmerge -J failed")
    page = c.get(f"/films/{mid}").text
    assert _text(page.split("<main>")[1]).count("could not read the file") == 1
    head = page.split('class="film-head"')[1].split("</header>")[0]
    assert PRIMARY.findall(head) == ["Rescan file"]
    assert page.count(">Rescan file<") == 1 and ">Add BEQ track</a>" not in page
    for part in ("filters", "tracks", "rating"):
        assert f'id="{part}"' not in page, "a part with nothing to show is not a heading"


def test_beq_forge_designs_in_the_background_and_its_filter_joins_the_list(app):
    c, web, d = app
    mid = _film(web, d)
    b = Browser(c).open(f"/films/{mid}/add?step=filter")
    assert "which takes" in b.page and "Design a filter with BEQ Forge" in b.page
    b.button("Design a filter")
    assert "BEQ Forge is designing a filter for this track" in b.page
    assert "Continue to options" in b.page, "the user can pick another filter meanwhile"
    job = web.db.one("SELECT id FROM jobs WHERE media_id=? AND phase='design'", (mid,))
    assert _wait(web, job["id"])["status"] == "done"
    _settle(web, mid)
    page = c.get(f"/films/{mid}/add?step=filter").text
    assert "<b>BEQ Forge" in page and page.count('name="filter"') >= 2
    assert "awaiting" not in {r["status"] for r in web.db.q("SELECT status FROM jobs")}
    forge = next(f for f in web.films.load(web.db, web.catalogue, mid).filters if f.forge)
    assert forge.levels, "a design is measured while its audio is at hand"
    assert "Designing a filter" in c.get("/activity").text


def test_opening_the_wizard_measures_each_filter_once_at_every_cutoff(app):
    c, web, d = app
    mid = _film(web, d)
    page = c.get(f"/films/{mid}/add").text
    assert "Measuring" in page and "Continue to options" in page, "choosing never waits"
    c.get(f"/films/{mid}/add?cutoff=15")
    measuring = web.db.q("SELECT id FROM jobs WHERE media_id=? AND phase='measure'", (mid,))
    assert len(measuring) == 1, "a change of cutoff starts no new work"
    assert _wait(web, measuring[0]["id"])["status"] == "done"
    page = c.get(f"/films/{mid}/add?cutoff=0").text
    levels = json.loads(re.search(r"let levels = (\{.*?\});", page)[1])
    assert set(levels["pcm20"]) == {f"{hz:g}" for hz in web._ladder()}
    assert all(re.fullmatch(r"[-+]?\d+\.\d dB", v) for v in levels["pcm20"].values())
    bass = json.loads(re.search(r"const bass = (\{.*?\});", page)[1])["pcm20"]
    assert bass["0"] != bass["25"], "the cutoff changes the bass restored"
    assert page.count("data-cutoff=") >= len(web._ladder()), "a curve for every cutoff"
    c.get(f"/films/{mid}/add")
    assert web.db.one("SELECT COUNT(*) n FROM jobs WHERE phase='measure'")["n"] == 1


def test_the_wizard_keeps_its_choices_across_steps_outputs_and_visits(app):
    c, web, d = app
    mid = _film(web, d)
    c.get(f"/films/{mid}/add?step=options&filter=ddpa&cutoff=20")
    page = c.get(f"/films/{mid}/add").text
    assert 'class="step open" id="options"' in page, "reopens the step the user left"
    assert "BEQ Catalogue · a3 (DD+ Atmos) · Low-end cutoff: 20 Hz" in _text(page)
    page = c.get(f"/films/{mid}/clip").text
    assert "Make a BEQ clip of Test Movie (2020)" in page and "Add a BEQ track instead" in page
    page = c.get(f"/films/{mid}/clip?step=filter").text
    assert re.search(r'name="filters"[^>]*value="ddpa" checked', page)
    assert re.search(r'<option value="20" selected', page)
    page = c.get(f"/films/{mid}/add?step=filter").text
    assert re.search(r'name="filter"[^>]*value="ddpa" checked', page), "a filter for another mix"
    assert '<details class="more" open>' in page


def test_a_filter_on_the_film_page_opens_the_wizard_with_it_selected(app):
    c, web, d = app
    mid = _film(web, d)
    page = c.get(f"/films/{mid}").text
    href = next(unescape(h) for h in re.findall(r'href="([^"]+filter=ddpa[^"]*)"', page))
    page = c.get(href).text
    assert 'class="step open" id="options"' in page
    assert "BEQ Catalogue · a3" in _text(page.split('id="options"')[0])


def test_a_clip_compares_filters_at_the_full_tracks_level_then_offers_each(app):
    c, web, d = app
    mid = _film(web, d)
    page = c.get(f"/films/{mid}/clip").text
    assert "Make a BEQ clip of" in page and 'class="step open" id="filter"' in page
    measure = web.db.one("SELECT id FROM jobs WHERE phase='measure'")["id"]
    _wait(web, measure)
    b = Browser(c).open(f"/films/{mid}/clip?step=filter&filters=pcm20&filters=ddpa")
    b.button("Continue to options")
    assert "does not change the film file" in b.page and 'name="path"' not in b.page
    assert f"Clip folder: {web.S.clip_dir}" in _text(b.page)
    b.button("Make BEQ clip")
    clip = web.db.one("SELECT * FROM clips WHERE media_id=?", (mid,))
    items = json.loads(clip["params_json"])["filters"]
    assert [i["entry"]["digest"] for i in items] == ["pcm20", "ddpa"]
    assert all(i["peak_dbfs"] is not None for i in items), "the full track's level"
    row = _wait(web, clip["id"], table="clips")
    assert row["status"] == "done", row["message"]
    page = c.get(f"/films/{mid}").text
    assert "BEQ clip made" in page and str(web.S.clip_dir) in page
    result = _text(page.split('id="result"')[1].split("</section>")[0])
    assert re.search(r"1st the original track 2nd BEQ Catalogue · p1 \(LPCM 2\.0\) "
                     r"\( filter \d+ under Filters \) 3rd BEQ Catalogue · a3 "
                     r"\(DD\+ Atmos\) \( filter \d+ under Filters \)", result), \
        "which track of the clip holds which filter"
    assert "Add BEQ track" not in result, "one add action per filter, in its row"
    adds = re.findall(r'class="clip-adds"><a [^>]*href="([^"]+)">Add BEQ track with this filter<',
                      page)
    assert len(adds) == 2 and "filter=pcm20" in unescape(adds[0])
    assert page.count(">Add BEQ track with this filter<") == len(
        re.findall(r'<tbody class="filter"', page)), "and every other filter has its own"
    page = c.get(unescape(adds[1])).text
    assert 'class="step open" id="options"' in page and "BEQ Catalogue · a3" in _text(page)


def test_a_failed_encode_says_what_happened_and_offers_one_fix(app):
    c, web, d = app
    mid = _film(web, d)
    path = str(d / "Test Movie (2020) lpcm20.mkv")
    job = web.db.create_job(mid, path, {"phase": "encode", "track_id": 1, "format": "flac",
                                        "entry": web.catalogue.get("pcm20").to_dict()})
    web.db.update_job(job, status="failed", finished_at=time.time(),
                      message="BEQ track still +41.20 ms out of sync")
    page = c.get(f"/films/{mid}").text
    result = _text(page.split('id="result"')[1].split("</section>")[0])
    assert "Adding a BEQ track failed" in result and "did not stay in time" in result
    assert "The film file was not changed." in result
    assert result.count("Try again") == 1 and page.count(">Try again<") == 1
    assert "Filter BEQ Catalogue · p1 (LPCM 2.0) Low-end cutoff Off" in result, \
        "Try again sits under what it reuses"
    assert "with the same track, filter and low-end cutoff" in result, "and says what it reuses"
    change = re.search(r'<a href="([^"]+)">change the filter or the low-end cutoff</a>', page)
    assert 'class="step open" id="filter"' in c.get(unescape(change[1])).text, \
        "advice to change a choice links to the step that changes it"
    primary = PRIMARY.findall(page)
    assert primary == ["Try again"] and 'class="btn"' not in page, \
        "the fix is the one primary button while the failure leads the page"
    assert ">Add BEQ track</a>" in page and "Starts adding a BEQ track again" in result
    assert "1 failed" in _text(page.split("<nav>")[1].split("</nav>")[0])
    assert "did not stay in time" in c.get("/activity").text, "the log says the same sentence"
    row = _text(c.get("/").text.split(f'href="/films/{mid}#result"')[1].split("</td>")[0])
    assert row.split(">", 1)[1].split() == "Adding a BEQ track Failed The new track went out " \
        "of time".split(), "the Library row has a Failed badge and the cause in a few words"
    assert re.search(rf'<button[^>]*formaction="/work/job-{job}/retry"[^>]*>Try again</button>',
                     c.get("/").text), "and its Next step is the fix, by name"
    assert f'href="/films/{mid}"' in c.get("/?activity=failed").text, \
        "the Failed filter lists the film the header counts"
    r = c.post(f"/work/job-{job}/retry", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/films/{mid}#job-")
    new = int(r.headers["location"].rsplit("-", 1)[1])
    assert json.loads(web.db.one("SELECT params_json FROM jobs WHERE id=?", (new,))[
        "params_json"])["source"]["size"] == os.path.getsize(path), "bound to the file as it is"


def test_one_beq_track_at_a_time_and_only_for_the_file_the_page_showed(app):
    c, web, d = app
    mid = _film(web, d)
    c.get(f"/films/{mid}/add")
    os.utime(d / "Test Movie (2020) lpcm20.mkv", (1, 1))
    r = c.post(f"/films/{mid}/add", data={"options": "1"})
    assert r.status_code == 409 and "rescan" in r.text
    _film(web, d)
    _settle(web, mid)
    assert c.post(f"/films/{mid}/add", data={"options": "1"}, follow_redirects=False
                  ).status_code == 303
    assert c.post(f"/films/{mid}/add", data={"options": "1"}).status_code == 409


def test_the_library_sorts_filters_and_remembers_both(app):
    c, web, d = app
    pcm, dts = _film(web, d), _film(web, d, "dts51")
    page = c.get("/?filters=some").text
    assert f'href="/films/{pcm}"' in page and f'href="/films/{dts}"' in page
    page = c.get("/?filters=some&beq=none&sort=filters&dir=desc").text
    assert "BEQ filters: Has filters · BEQ tracks: No BEQ tracks" in _text(page)
    assert "Sorted by BEQ filters, most first" in _text(page) and ">Clear<" in page
    page = c.get("/").text
    assert "Sorted by BEQ filters, most first" in _text(page), "kept after opening a film"
    page = c.get("/?clear=1").text
    assert "Sorted by title, a to z" in _text(page) and ">Clear<" not in page
    assert c.get("/?activity=failed").text.count('href="/films/') == 0
    for key in ("Title", "Candidate rating", "BEQ filters", "BEQ tracks", "Activity",
                "Next step"):
        assert f">{key}" in page
    assert f'href="/films/{pcm}/add">Add BEQ track<' in page, "a row's next step is a link"
    r = c.get("/?show=candidates", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/?rating=strong&rating=possible&beq=none"


def test_a_library_rows_next_step_is_the_one_step_its_film_needs(app):
    _, web, _ = app
    row = {"id": 7, "error": None, "probe_json": "{}", "rating_key": "strong",
           "filter_count": 0, "beq_count": 0, "assess_json": None}

    def step(declined=(), live=(), **more):
        found = web._row_next({**row, **more}, list(live), None, set(declined))
        return found and tuple(found)

    assert step() == ("Design a filter with BEQ Forge", "/films/7/add?step=filter", False), \
        "a film with no filter is offered the step where BEQ Forge designs one"
    unlikely = (web.NO_NEED, "", False)
    assert step(declined=[7]) == unlikely, \
        "not where BEQ Forge already found nothing to restore: the row says why instead"
    assert step(rating_key="unlikely") == unlikely
    assert step(rating_key="unknown", rating_label="Unknown: too little bass") == unlikely
    assert step(rating_key="unknown", rating_label="Unknown: bass check failed") == (
        web.CHECK_AGAIN, "", False), \
        "nor where the bass check could not say the film is a candidate"
    assert step(filter_count=1) == ("Add BEQ track", "/films/7/add", False)
    assert step(filter_count=2, beq_count=1) == (
        "Add another BEQ track", "/films/7/add", False)
    assert step(filter_count=1, beq_count=1) == (web.ALL_USED, "", False), \
        "every filter in use: nothing to do, and the row says so"
    clip = web.activity.Work(1, "clip", "running", 7, "Film", "/x.mkv")
    assert step(filter_count=1, live=[clip]) == ("After the BEQ clip", "", False), \
        "a film waits for its BEQ clip"
    assert not web._can_add({**row, "filter_count": 1}, [clip]), "and has no tick box meanwhile"
    assert step(error="x", probe_json=None) == ("Rescan file", "/films/7/rescan", True), \
        "a file the app can't read is offered its fix, by name"


def test_bulk_add_uses_the_defaults_and_names_the_films_it_skips(app):
    c, web, d = app
    pcm, dts = _film(web, d), _film(web, d, "dts51")
    plan = {p["id"]: p for p in c.get(f"/api/library/plan?film={pcm}&film={dts}").json()["films"]}
    assert plan[pcm]["filter"] == "BEQ Catalogue · p1 (LPCM 2.0)" and not plan[pcm]["why"]
    assert "no filter was made" in plan[dts]["why"] and not plan[dts]["filter"]
    page = c.post("/library/add", data={"film": [str(pcm), str(dts)]}, headers=BROWSER).text
    assert "Adding a BEQ track to 1 film" in page and "Skipped Test Movie (2020)" in page
    job = web.db.one("SELECT params_json FROM jobs WHERE media_id=? AND phase='encode'", (pcm,))
    assert json.loads(job["params_json"])["entry"]["digest"] == "pcm20"
    assert web.db.one("SELECT COUNT(*) n FROM jobs WHERE media_id=?", (dts,))["n"] == 0


def test_settings_holds_the_default_cutoff_maintenance_and_the_clip_folder(app):
    c, web, d = app
    page = c.get("/settings").text
    assert str(web.S.clip_dir) in page and 'name="clip' not in page
    for words in ("Scan now", "Rescan every file", "Refresh", "BEQ Forge", "Logs"):
        assert words in page
    c.post("/settings/cutoff", data={"cutoff_hz": "15"})
    mid = _film(web, d)
    assert re.search(r'<option value="15" selected>15 Hz \(your default\)',
                     c.get(f"/films/{mid}/add?step=filter").text)
    assert c.post("/settings/cutoff", data={"cutoff_hz": "500"}).status_code == 400
    r = c.post("/media/add", data={"path": "/etc/x.mkv"}, headers=BROWSER)
    assert str(r.url.path) == "/settings" and "That path is not inside a library folder." in r.text
    assert 'aria-invalid="true"' in r.text


def test_old_addresses_lead_to_the_new_pages(app):
    c, web, d = app
    mid = _film(web, d)
    job = web.db.create_job(mid, str(d / "Test Movie (2020) lpcm20.mkv"), {"phase": "encode"})
    for old, new in ((f"/media/{mid}", f"/films/{mid}"), ("/jobs", "/activity"),
                     (f"/jobs/{job}", f"/films/{mid}#job-{job}"), ("/system", "/settings")):
        r = c.get(old, follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == new, old


def test_a_job_left_waiting_for_a_choice_is_closed_and_keeps_its_filters(app):
    c, web, d = app
    mid = _film(web, d)
    design = {"candidates": [{"digest": "design:old", "origin": "designer", "label": "beqforge 1",
                              "filters": PCM["filters"], "author": "beqforge 1"}]}
    path = d / "Test Movie (2020) lpcm20.mkv"
    st = path.stat()
    job = web.db.create_job(mid, str(path), {"phase": "prepare"}, {
        "track": {"track_id": 1}, "design": design,
        "source": {"size": st.st_size, "mtime_ns": st.st_mtime_ns}}, status="awaiting")
    web.retire_waiting()
    assert web.db.one("SELECT status FROM jobs WHERE id=?", (job,))["status"] == "done"
    assert web.films.load(web.db, web.catalogue, mid).filter("design:old").forge


def test_no_screen_shows_a_status_that_waits_for_the_user_or_the_word_job(app):
    c, web, d = app
    mid = _film(web, d)
    c.get(f"/films/{mid}/add")
    for url in ("/", f"/films/{mid}", f"/films/{mid}/add", "/activity"):
        text = _text(c.get(url).text.split("<main>")[1])
        assert "Waiting for you" not in text, url
        if url != "/activity":
            assert not re.search(r"\b[Jj]obs?\b", text.replace("Technical details", "")), url


def test_errors_keep_the_app_shell_for_browsers_and_json_for_clients(app):
    c, _, _ = app
    r = c.get("/films/99999", headers=BROWSER)
    assert r.status_code == 404 and "<h1>Film not found</h1>" in r.text and 'href="/activity"' in r.text
    r = c.get("/films/99999")
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    r = c.post("/media/add", data={"path": "/nope.mkv"}, headers={"accept": "application/json"})
    assert r.status_code == 400 and r.json() == {"detail": "Not an existing .mkv file"}


def test_every_wizard_step_has_one_primary_button_named_for_what_it_does(app):
    c, web, d = app
    mid = _film(web, d)
    named = {"track": "Continue to filter", "filter": "Continue to options"}
    for output, last in (("add", "Add BEQ track"), ("clip", "Make BEQ clip")):
        for step in ("track", "filter", "options"):
            page = c.get(f"/films/{mid}/{output}?step={step}").text
            open_step = page.split('class="step open"')[1].split('<li class="step')[0]
            primary = [_text(b) for b in PRIMARY.findall(open_step)]
            assert primary == [named.get(step, last)], (output, step, primary)


def test_a_custom_cutoff_is_measured_when_it_is_set(app):
    c, web, d = app
    mid = _film(web, d)
    page = c.get(f"/films/{mid}/add?step=filter").text
    assert re.search(r'<select name="cutoff" id="cutoff">', page) and \
        '<option value="custom">' in page, "one cutoff menu, whose last entry is Custom"
    first = web.db.one("SELECT id FROM jobs WHERE phase='measure'")["id"]
    assert _wait(web, first)["status"] == "done"
    assert sorted(json.loads(web.db.one("SELECT params_json FROM jobs WHERE id=?", (first,))[
        "params_json"])["cutoffs"]) == web.store.cutoff_ladder(web.cutoff_default())
    page = c.get(f"/films/{mid}/add?step=filter&cutoff=12.5").text
    assert '<option value="custom" selected>' in page
    assert re.search(r'id="cutoff-custom"[^>]*value="12.5"', page)
    assert 'data-cutoff="12.5"' in page and "Measuring" in page
    page = c.get(f"/films/{mid}/add?step=options&cutoff=custom&custom_hz=7.5").text
    assert "Low-end cutoff: 7.5 Hz" in _text(page), "the Custom entry sends its own field"
    custom = web.db.one("SELECT id, params_json FROM jobs WHERE phase='measure' AND id>?",
                        (first,))
    assert json.loads(custom["params_json"])["cutoffs"] == [12.5], "only what is not known"
    assert _wait(web, custom["id"])["status"] == "done"
    levels = c.get(f"/api/films/{mid}/wizard?track=1&cutoff=12.5").json()["levels"]
    assert re.fullmatch(r"[-+]?\d+\.\d dB", levels["pcm20"]["12.5"])


def test_the_library_search_matches_every_word_and_updates_in_place(app):
    c, web, d = app
    pcm, dts = _film(web, d), _film(web, d, "dts51")
    page = c.get("/?q=test+2020+lpcm20").text
    assert f'href="/films/{pcm}"' in page and f'href="/films/{dts}"' not in page
    assert '<label for="q" class="small muted">Search</label>' in page
    r = c.get("/?q=dts51&partial=1").json()
    assert f'href="/films/{dts}"' in r["results"] and f'href="/films/{pcm}"' not in r["results"]
    assert re.search(r'All <span class="chip-n">1</span>', r["chips"]), "counts follow the search"
    assert r["query"] == "q=dts51"
    page = c.get("/?q=no-such-film").text
    assert "No film matches" in page and ">Clear</a> to see every film" in page


def test_each_filter_says_when_it_last_changed(app):
    c, web, d = app
    mid = _film(web, d)
    page = c.get(f"/films/{mid}").text
    from beq_encoder.words import fmt_day
    assert f"Last updated {fmt_day(1611984156)}" in page, "under the filter's name"  # atmos1


def test_the_clip_window_recommends_a_passage_and_offers_it_again_after_a_change(app):
    c, web, d = app
    mid = _film(web, d)
    m = web.db.one("SELECT * FROM media WHERE id=?", (mid,))
    key = web.films.load(web.db, web.catalogue, mid).default_track.key
    st = (d / "Test Movie (2020) lpcm20.mkv").stat()
    db_levels = [-40.0] * 30
    db_levels[12:15] = [-10.0, -9.0, -10.0]  # the heaviest bass, 120 to 150 s
    web.store.save_profile(web.db, mid, key, {"size": st.st_size, "mtime_ns": st.st_mtime_ns},
                           {"bucket_s": 10.0, "db": db_levels})
    assert m is not None
    page = c.get(f"/films/{mid}/clip?step=options&seconds=30").text
    assert re.search(r'id="start"[^>]*value="0:02:00"', page), "the start begins at the recommendation"
    assert 'id="rec-start">0:02:00<' in page and re.search(r'id="use-rec" hidden', page)
    page = c.get(f"/films/{mid}/clip?step=options&start=0:00:40").text
    assert re.search(r'id="start"[^>]*value="0:00:40"', page)
    assert re.search(r'id="use-rec">', page), "offered again once the user moved away from it"
    assert 'id="rec-mark"' in page
