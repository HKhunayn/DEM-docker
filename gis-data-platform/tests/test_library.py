"""Home page, data library (conversion status through the data-platform proxy),
Convert now (only what is missing) and the background tile-count cache."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .conftest import reload_platform, use_role

APP = Path(__file__).resolve().parents[1]
BBOX = [46.70, 24.60, 46.90, 24.80]
OTHER = [46.75, 24.65, 46.85, 24.75]   # inside BBOX
FAR = [6.70, 45.66, 6.90, 45.86]


# ---- fixtures ---------------------------------------------------------------------

@pytest.fixture()
def lib(stub_engine, monkeypatch):
    """Platform app + engine app in one process (same data root); the
    platform's engine client is routed to the engine app (as engine:8787)."""
    scripts = stub_engine / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "build_tiles.py").write_text("ENGINE_VERSION = 4\n", encoding="utf-8")
    (scripts / "publish_tiles.py").write_text("PUBLISH_REVISION = 3\n", encoding="utf-8")
    main = use_role(monkeypatch, "platform")
    platform = TestClient(main.app)
    engine = TestClient(main.create_app("engine"))
    handoff = sys.modules["app.handoff"]

    def via_engine(method, path, body=None, timeout=None):
        r = engine.request(method, path, json=body)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None

    state = {"up": True}

    def request(method, path, body=None, timeout=None):
        if not state["up"]:
            raise handoff.EngineUnavailable("connection refused")
        return via_engine(method, path, body, timeout)

    monkeypatch.setattr(handoff, "request", request)
    conv = sys.modules["app.conversion"]
    conv.reset_measurer()
    return {"platform": platform, "engine": engine, "conv": conv, "state": state,
            "catalog": sys.modules["app.catalog"], "jobs": sys.modules["app.jobs"],
            "tiles": sys.modules["app.config"].TILE_ROOT, "dem": sys.modules["app.config"].DEM_ROOT,
            "stats": sys.modules["app.tilestats"]}


def add_area(lib, name, bbox=BBOX, minzoom=0, maxzoom=6):
    lib["catalog"].upsert_area(name=name, bbox=bbox, provider="copernicus", minzoom=minzoom, maxzoom=maxzoom,
                               dataset={"name": name, "state": "done"})


def write_lerc(lib, name, bbox=BBOX, zooms=range(0, 7), manifest="complete", zooms_done=(), minzoom=0, maxzoom=6,
               engine_version=4, skip=()):
    svc = lib["tiles"] / "arcgis" / "rest" / "services" / name
    n = 0
    for z in zooms:
        x0, y0, x1, y1 = lib["stats"].tile_range(bbox, z)
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                if (z, y, x) in skip:
                    continue
                p = svc / "tile" / str(z) / str(y) / str(x)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"lerc")
                n += 1
    if manifest is None:
        return n
    data = {"engine_version": engine_version, "bbox": bbox, "minzoom": minzoom, "maxzoom": maxzoom,
            "complete": manifest == "complete"}
    if manifest == "force":
        data["force_pending"] = {"bbox": bbox, "minzoom": minzoom, "maxzoom": maxzoom, "zooms_done": list(zooms_done)}
    (svc / "build_manifest.json").write_text(json.dumps(data), encoding="utf-8")
    if manifest == "complete":
        (svc / "ImageServer.json").write_text("{}", encoding="utf-8")
    return n


def publish(lib, bounds, revision=3, counts=None):
    version = "20261029T103817-65e246d5"
    root = lib["tiles"] / "published" / version
    (root / "terrain" / "6" / "41").mkdir(parents=True)
    (root / "terrain" / "6" / "41" / "27.webp").write_bytes(b"w" * 100)
    (root / "flat.webp").write_bytes(b"f" * 10)
    (root / "meta.json").write_text("{}", encoding="utf-8")
    (lib["tiles"] / "published" / "current.json").write_text(json.dumps(
        {"version": version, "bounds": bounds, "revision": revision, "maxzoom": 6,
         "counts": counts or {"linked": 5, "computed": 2, "flat": 1}}), encoding="utf-8")


def library(lib, settle=True):
    """GET /api/library after the background counts finished."""
    body = lib["platform"].get("/api/library").json()
    if settle:
        assert lib["conv"].measurer().wait_idle(20)
        body = lib["platform"].get("/api/library").json()
    return body


def area(body, name):
    return next(a for a in body["areas"] if a["name"] == name)


def expected(lib, bbox=BBOX, minzoom=0, maxzoom=6):
    return sum(lib["stats"].expected_tiles(bbox, minzoom, maxzoom).values())


def wait_job(jobs, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = jobs.current_job(tail=0)
        if cur and cur["state"] not in jobs.ACTIVE_STATES:
            return cur
        time.sleep(0.05)
    raise AssertionError("job did not finish")


# ---- pages ------------------------------------------------------------------------

def test_home_page_has_both_buttons(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    assert re.search(r'<a class="big" id="btn-download" href="/fetch/">', html)
    assert re.search(r'<a class="big alt" id="btn-library" href="/library/">', html)
    assert "Download data" in html and "Data library" in html
    # engine status line + public serving URLs in the footer
    assert 'id="enginestatus"' in html and "/api/engine/status" in html and "/api/library/urls" in html
    # fully local: no CDN / external scripts or styles
    for page in ("/", "/library/", "/fetch/"):
        text = client.get(page).text
        assert not re.search(r'<(script|link)[^>]+(src|href)="https?://', text), page
    # JSON service info moved off "/"
    info = client.get("/api/info").json()
    assert info["role"] == "platform" and info["library"] == "/library/" and info["fetch"] == "/fetch/"


def test_library_page_and_navigation(client):
    r = client.get("/library/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    html = r.text
    for marker in ('href="/"', 'href="/fetch/"', "/api/library", "Convert now", 'id="enginestatus"',
                   'id="banner"', 'id="modal"', "measuring", "fully converted", "Download missing",
                   "re-count tiles", "Imagery sets"):
        assert marker in html, marker
    assert client.get("/library", follow_redirects=False).headers["location"] == "/library/"
    fetch = client.get("/fetch/").text
    assert 'href="/"' in fetch and 'href="/library/"' in fetch
    urls = client.get("/api/library/urls").json()
    assert urls["terrain"].endswith(":5000/tiles/terrain.json")
    assert urls["arcgis"].endswith(":5000/arcgis/rest/services/<area>/ImageServer")


def test_engine_root_lists_public_entry_points_only(engine):
    info = engine.get("/").json()
    assert info["role"] == "engine" and "jobs" not in info and "docs" not in info
    assert info["terrain"] == "/tiles/terrain.json"


# ---- tile math + counting ----------------------------------------------------------

def test_tile_range_matches_the_engine():
    scripts = APP / "engine" / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("bt_parity", scripts / "build_tiles.py")
        bt = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(bt)
        except ImportError as exc:     # engine deps (rasterio, ...) not installed here
            pytest.skip(f"engine dependencies missing: {exc}")
    finally:
        sys.path.remove(str(scripts))
    from app import tilestats
    for bbox in (BBOX, [31.5967, 10.9125, 60.4688, 37.4736], [-180, -85, 180, 85], [6.6, 45.6, 7.2, 46.0],
                 [46.0, 24.0, 47.0, 25.0]):
        for z in range(0, 15):
            assert tilestats.tile_range(bbox, z) == bt.tile_range(bbox, z), (bbox, z)


def test_measurer_never_blocks_and_refreshes_on_signature_change(tmp_path):
    from app.tilestats import Measurer
    calls = []

    def slow(v):
        def fn():
            time.sleep(0.4)
            calls.append(v)
            return {"v": v}
        return fn

    m = Measurer(tmp_path / "s.json")
    t0 = time.monotonic()
    value, info = m.get("k", ["sig1"], slow(1))
    assert time.monotonic() - t0 < 0.2          # never waits for the count
    assert value is None and info["measuring"] and info["measuredAt"] is None
    assert m.wait_idle(10)
    value, info = m.get("k", ["sig1"], slow(99))
    assert value == {"v": 1} and not info["measuring"] and info["upToDate"] and calls == [1]
    # changed on disk -> old value + re-measure in the background
    value, info = m.get("k", ["sig2"], slow(2))
    assert value == {"v": 1} and info["measuring"]
    assert m.wait_idle(10)
    assert m.get("k", ["sig2"], slow(3))[0] == {"v": 2}
    # max_age: re-count while something converts - but at most every 5x the
    # last count's duration (a 0.4 s count is not repeated within 2 s)
    value, info = m.get("k", ["sig2"], slow(4), max_age=0.01)
    assert not info["measuring"] and value == {"v": 2}
    time.sleep(2.1)
    value, info = m.get("k", ["sig2"], slow(4), max_age=0.01)
    assert info["measuring"] and m.wait_idle(10) and m.get("k", ["sig2"], slow(5))[0] == {"v": 4}
    # persisted: a restarted engine does not re-count an unchanged pyramid
    m2 = Measurer(tmp_path / "s.json")
    value, info = m2.get("k", ["sig2"], slow(6))
    assert value == {"v": 4} and not info["measuring"]
    # invalidate (POST refresh) forces a re-count
    m2.invalidate()
    assert m2.get("k", ["sig2"], slow(7))[1]["measuring"]
    assert m2.wait_idle(10) and m2.get("k", ["sig2"], slow(8))[0] == {"v": 7}


def test_job_progress_from_the_build_log():
    from app import conversion, tilestats
    bbox = [40.0, 20.0, 50.0, 30.0]
    per_zoom = tilestats.expected_tiles(bbox, 0, 8)
    total = sum(per_zoom.values())
    assert per_zoom[8] > 20
    log = ["sources: 4 DEM file(s)"]
    for z in range(0, 8):
        n = per_zoom[z]
        log.append(f"z{z}: x=1..2 y=1..2 tiles={n} written=0 skipped={n} empty=0 failed=0 (cumulative written 0)")
    log.append(f"  z8 progress 10/{per_zoom[8]} workers=4, mem 20%")
    job = {"id": "j", "name": "A", "bbox": bbox, "minzoom": 0, "maxzoom": 8, "state": "running", "step": "build",
           "steps": [{"name": "check", "state": "done"}, {"name": "build", "state": "running"}],
           "lercOnly": True, "mode": "lerc", "log": log, "elapsed": 3.0}
    p = conversion.job_progress(job)
    skipped = sum(per_zoom[z] for z in range(8))
    assert p["tilesDone"] == skipped + 10 and p["tilesExpected"] == total
    assert p["buildPercent"] == round((skipped + 10) * 100 / total, 1)
    assert p["skipped"] == skipped and p["written"] == 0
    assert p["zoom"] == 8 and p["mode"] == "lerc"
    log.append(f"  z8 progress 20/{per_zoom[8]} workers=4")
    p2 = conversion.job_progress(job)
    assert p2["tilesDone"] == skipped + 20 and p2["percent"] > p["percent"]     # it moves
    log.append(f"z8: x=1..2 y=1..2 tiles={per_zoom[8]} written={per_zoom[8]} skipped=0 empty=0 failed=0")
    p3 = conversion.job_progress(job)
    assert p3["tilesDone"] == total and p3["buildPercent"] == 100.0 and p3["written"] == per_zoom[8]
    # full job: publish step reported per zoom
    full = dict(job, lercOnly=False, mode="full", step="publish",
                steps=[{"name": n, "state": "done"} for n in ("check", "build", "verify")] + [{"name": "publish", "state": "running"}],
                log=log + ["  publish z3 done: linked=1 computed=2 flat=0 (1s)"])
    pf = conversion.job_progress(full)
    assert pf["mode"] == "full" and pf["publishPercent"] == round(4 * 100 / 9, 1) and 90 < pf["percent"] < 100
    # verify step: per-zoom check lines move the percent too
    ver = dict(full, step="verify",
               steps=[{"name": n, "state": "done"} for n in ("check", "build")]
               + [{"name": "verify", "state": "running"}, {"name": "publish", "state": "pending"}],
               log=log + ["z0: no sample point with source data in 1 tile(s) — skipped",
                          "z3: 1/1 tiles, 1 points, pixel 4443 m, median |tile-source| 0.65 m (PASS)"])
    pv = conversion.job_progress(ver)
    assert pv["verifyPercent"] == round(4 * 100 / 9, 1) and 85 < pv["percent"] < 90


# ---- library API: percent for complete / partial / stale / missing ----------------

def test_library_percent_complete_partial_stale_missing(lib):
    for name in ("Done", "Forced", "Resume", "Stale", "Old", "Missing"):
        add_area(lib, name)
    exp = expected(lib)
    write_lerc(lib, "Done")
    per = lib["stats"].expected_tiles(BBOX, 0, 6)
    write_lerc(lib, "Forced", zooms=range(0, 5), manifest="force", zooms_done=[0, 1, 2])
    n_resume = write_lerc(lib, "Resume", zooms=range(0, 6), manifest="partial")
    write_lerc(lib, "Stale", manifest=None)
    write_lerc(lib, "Old", engine_version=3)
    first = lib["platform"].get("/api/library").json()
    assert first["engine"]["running"] is True
    assert area(first, "Resume")["conversion"]["arcgis"]["measured"]["measuring"] is True   # counting started
    body = library(lib)
    done = area(body, "Done")["conversion"]["arcgis"]
    assert (done["state"], done["percent"], done["tilesDone"], done["expected"]) == ("converted", 100, exp, exp)
    assert done["bytes"] > 0 and done["files"] == exp + 2 and done["measured"]["measuredAt"]
    assert done["url"] == "/arcgis/rest/services/Done/ImageServer"
    forced = area(body, "Forced")["conversion"]["arcgis"]
    trusted = per[0] + per[1] + per[2]
    assert forced["state"] == "partial" and forced["tilesDone"] == trusted     # z3/z4 tiles not trusted
    assert forced["percent"] == trusted * 100 // exp
    resume = area(body, "Resume")["conversion"]["arcgis"]
    assert resume["state"] == "partial" and resume["tilesDone"] == n_resume and 0 < resume["percent"] < 100
    for name in ("Stale", "Old"):
        stale = area(body, name)["conversion"]["arcgis"]
        assert (stale["state"], stale["percent"], stale["tilesDone"]) == ("stale", 0, 0), name
        assert stale["tilesPresent"] == exp
    missing = area(body, "Missing")["conversion"]["arcgis"]
    assert (missing["state"], missing["percent"]) == ("missing", 0)
    # nothing published: the primary needs the full build, the others are "not converted"
    assert body["primary"] is not None
    primary = area(body, body["primary"])["conversion"]
    assert primary["webp"]["state"] == "missing" and primary["convert"]["mode"] == "full"
    # sources: no DEM on disk
    src = area(body, "Done")["sources"]
    assert (src["needed"], src["present"], src["downloadable"]) == (1, 0, 1)
    assert area(body, "Done")["arcgisUrl"].endswith(":5000/arcgis/rest/services/Done/ImageServer")


def test_library_webp_published_covered_and_not_published(lib):
    add_area(lib, "Main")
    add_area(lib, "Inside", bbox=OTHER)
    add_area(lib, "Far", bbox=FAR)
    publish(lib, BBOX)
    for name, bbox in (("Main", BBOX), ("Inside", OTHER), ("Far", FAR)):
        write_lerc(lib, name, bbox=bbox)
    body = library(lib)
    assert body["primary"] == "Main"
    main = area(body, "Main")["conversion"]
    assert main["primary"] is True
    assert (main["webp"]["state"], main["webp"]["percent"], main["webp"]["tilesDone"]) == ("converted", 100, 7)
    assert main["webp"]["bytes"] == 100 + 10 + 2 and main["webp"]["files"] == 3
    assert main["convert"]["mode"] is None and main["convert"]["fullyConverted"] is True
    inside = area(body, "Inside")["conversion"]
    assert inside["webp"]["state"] == "covered" and inside["webp"]["coveredBy"] == "Main"
    far = area(body, "Far")["conversion"]
    assert far["webp"]["state"] == "not-published" and "Main" in far["webp"]["reason"]
    assert far["convert"]["publishAvailable"] is True and "REPLACES the published set of Main" in far["convert"]["publishMessage"]


def test_live_job_progress_shown_while_converting(lib, monkeypatch):
    add_area(lib, "Main")
    publish(lib, BBOX)
    write_lerc(lib, "Main", zooms=range(0, 3), manifest="partial")
    monkeypatch.setenv("STUB_SLEEP", "3")
    r = lib["platform"].post("/api/library/Main/convert", json={})
    assert r.status_code == 202 and r.json()["mode"] == "lerc"
    time.sleep(0.5)
    body = lib["platform"].get("/api/library").json()
    assert body["busy"] is True and body["job"]["name"] == "Main" and body["job"]["mode"] == "lerc"
    main = area(body, "Main")["conversion"]
    assert main["job"]["id"] == body["job"]["id"]
    assert main["arcgis"]["state"] == "converting" and main["arcgis"]["live"] is True
    assert main["webp"]["state"] == "converted"            # lerc-only: WebP untouched
    assert main["convert"]["available"] is False and "converting Main" in main["convert"]["reason"]
    # a second Convert now while busy is refused with the reason
    r = lib["platform"].post("/api/library/Main/convert", json={})
    assert r.status_code == 409 and "converting Main" in r.json()["detail"]
    wait_job(lib["jobs"])


# ---- Convert now ------------------------------------------------------------------

def test_convert_now_picks_lerc_only_when_only_arcgis_is_missing(lib):
    add_area(lib, "Main")
    publish(lib, BBOX)                                  # WebP published for this area
    write_lerc(lib, "Main", manifest="partial", skip={(6, 27, 41)})
    body = library(lib)
    conv = area(body, "Main")["conversion"]["convert"]
    assert conv["available"] and conv["mode"] == "lerc"
    assert "only the missing ArcGIS tiles" in conv["message"] and "already baked are kept" in conv["message"]
    assert "WebP) set is not touched" in conv["message"]
    r = lib["platform"].post("/api/library/Main/convert", json={})
    assert r.status_code == 202, r.text
    assert r.json()["mode"] == "lerc"
    job = wait_job(lib["jobs"])
    assert job["lercOnly"] is True and job["origin"] == "user"
    assert [s["name"] for s in job["steps"]] == ["check", "build"]


def test_convert_now_picks_full_when_the_webp_set_is_missing(lib):
    add_area(lib, "Main")
    write_lerc(lib, "Main")                             # ArcGIS complete, nothing published
    body = library(lib)
    conv = area(body, "Main")["conversion"]["convert"]
    assert conv["mode"] == "full" and "ArcGIS tiles are already complete and are kept" in conv["message"]
    r = lib["platform"].post("/api/library/Main/convert", json={})
    assert r.status_code == 202 and r.json()["mode"] == "full"
    job = wait_job(lib["jobs"])
    assert job["lercOnly"] is False and [s["name"] for s in job["steps"]] == ["check", "build", "verify", "publish"]


def test_convert_now_refuses_when_nothing_is_missing(lib):
    add_area(lib, "Main")
    publish(lib, BBOX)
    write_lerc(lib, "Main")
    body = library(lib)
    conv = area(body, "Main")["conversion"]["convert"]
    assert conv["available"] is False and conv["fullyConverted"] is True and "nothing to do" in conv["reason"]
    r = lib["platform"].post("/api/library/Main/convert", json={})
    assert r.status_code == 409 and "fully converted" in r.json()["detail"]
    assert lib["jobs"].current_job(tail=0) is None       # no job started
    assert lib["platform"].post("/api/library/Nope/convert", json={}).status_code == 404


def test_publish_a_non_primary_area_is_explicit_and_full(lib):
    add_area(lib, "Main")
    add_area(lib, "Far", bbox=FAR)
    publish(lib, BBOX)
    write_lerc(lib, "Main")
    write_lerc(lib, "Far", bbox=FAR)
    library(lib)
    assert lib["platform"].post("/api/library/Far/convert", json={}).status_code == 409
    r = lib["platform"].post("/api/library/Far/convert", json={"publish": True})
    assert r.status_code == 202 and r.json()["mode"] == "full" and r.json()["replaces"] == "Main"
    assert wait_job(lib["jobs"])["name"] == "Far"


def test_engine_down_library_explains_and_convert_is_refused(lib):
    add_area(lib, "Main")
    write_lerc(lib, "Main", manifest="partial")
    library(lib)                                        # remembered as the last known status
    lib["state"]["up"] = False
    body = lib["platform"].get("/api/library").json()
    assert body["engine"]["running"] is False and "engine not running" in body["engine"]["message"]
    assert "last known" in body["engine"]["message"]
    main = area(body, "Main")
    assert main["conversionLive"] is False and main["conversion"]["arcgis"]["state"] == "partial"
    assert main["sources"]["needed"] == 1
    r = lib["platform"].post("/api/library/Main/convert", json={})
    assert r.status_code == 503 and "engine is not running" in r.json()["detail"]


def test_engine_never_started_library_still_lists_areas(client):
    sys.modules["app.catalog"].upsert_area(name="Main", bbox=BBOX, provider="copernicus", minzoom=0, maxzoom=6)
    body = client.get("/api/library").json()        # ENGINE_URL points at a closed port
    assert body["engine"]["running"] is False and "no status known" in body["engine"]["message"]
    assert area(body, "Main")["conversion"] is None
    assert client.post("/api/library/Main/convert", json={}).status_code == 503


def test_lerc_tiles_removed_after_completion_are_detected_and_lerc_only(lib):
    add_area(lib, "Main")
    publish(lib, BBOX)
    write_lerc(lib, "Main")
    assert area(library(lib), "Main")["conversion"]["arcgis"]["percent"] == 100
    svc = lib["tiles"] / "arcgis" / "rest" / "services" / "Main"
    for p in list((svc / "tile" / "6").rglob("*"))[:3]:
        if p.is_file():
            p.unlink()
    m = json.loads((svc / "build_manifest.json").read_text())
    m["complete"] = False
    (svc / "build_manifest.json").write_text(json.dumps(m))
    body = library(lib)
    arc = area(body, "Main")["conversion"]["arcgis"]
    assert arc["state"] == "partial" and arc["percent"] < 100 and arc["tilesDone"] < arc["expected"]
    assert area(body, "Main")["conversion"]["convert"]["mode"] == "lerc"


def test_sources_and_download_missing(lib):
    add_area(lib, "Two", bbox=[46.5, 24.5, 47.5, 24.9])   # cells E046 + E047
    dem = lib["dem"]
    (dem / "Copernicus_DSM_COG_10_N24_00_E046_00_DEM.tif").write_bytes(b"II*\x00" + b"0" * 100)
    src = area(lib["platform"].get("/api/library").json(), "Two")["sources"]
    assert (src["needed"], src["present"], src["downloadable"], src["bytes"]) == (2, 1, 1, 104)
    assert src["missingFiles"] == ["Copernicus_DSM_COG_10_N24_00_E047_00_DEM.tif"]
    r = lib["platform"].post("/api/library/Two/download")
    assert r.status_code == 202 and "1 missing DEM file" in r.json()["message"]
    wait_job(lib["jobs"])
    (dem / "Copernicus_DSM_COG_10_N24_00_E047_00_DEM.tif").write_bytes(b"II*\x00")
    r = lib["platform"].post("/api/library/Two/download")
    assert r.status_code == 409 and "on disk" in r.json()["detail"]


def test_imagery_sets_listed_with_size(lib):
    root = sys.modules["app.config"].IMAGERY_ROOT / "s2"
    (root / "3" / "4").mkdir(parents=True)
    (root / "3" / "4" / "5.jpg").write_bytes(b"j" * 50)
    (root / "3" / "4" / "6.jpg").write_bytes(b"j" * 30)
    (root / "imagery.json").write_text(json.dumps({"name": "s2", "provider": "s2cloudless", "minzoom": 0,
                                                   "maxzoom": 3, "tiles": 2, "bbox": [0, 0, 1, 1]}))
    lib["platform"].get("/api/library")
    sys.modules["app.library_api"]._imagery_measurer().wait_idle(10)
    s = lib["platform"].get("/api/library").json()["imagery"][0]
    assert (s["name"], s["tiles"], s["expectedTiles"]) == ("s2", 2, 2)
    assert s["bytes"] == 80 + len((root / "imagery.json").read_bytes())
    assert s["url"].endswith(":5000/tiles/imagery-sets/s2/{z}/{x}/{y}")


def test_refresh_recounts(lib):
    add_area(lib, "Main")
    write_lerc(lib, "Main", zooms=range(0, 3), manifest="partial")
    first = area(library(lib), "Main")["conversion"]["arcgis"]["tilesDone"]
    # tiles added inside existing row dirs do not change any dir mtime: refresh re-counts
    svc = lib["tiles"] / "arcgis" / "rest" / "services" / "Main" / "tile" / "2"
    extra = next(svc.iterdir()) / "99999"
    assert lib["platform"].post("/api/library/refresh").status_code == 202
    assert area(library(lib), "Main")["conversion"]["arcgis"]["tilesDone"] == first
    extra.write_bytes(b"x")                              # outside the range: not counted as done
    lib["platform"].post("/api/library/refresh")
    after = area(library(lib), "Main")["conversion"]["arcgis"]
    assert after["tilesDone"] == first and after["files"] == first + 2


# ---- engine public port ------------------------------------------------------------

def test_engine_public_nginx_exposes_only_serving():
    conf = "\n".join(line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
                      for line in (APP / "nginx" / "engine.conf").read_text(encoding="utf-8").splitlines())
    # no catch-all proxy any more
    assert not re.search(r"location / \{\s*proxy_pass", conf)
    assert re.search(r"if \(\$request_method !~ \^\(GET\|HEAD\|OPTIONS\)\$\) \{\s*return 403;", conf)
    assert re.search(r"location /api/ \{[^}]*return 403", conf)
    for exact in ("/api/catalog", "/api/areas", "/api/services", "/api/imagery", "/api/tiles/status",
                  "/tiles/terrain.json", "/health"):
        assert f"location = {exact} {{ proxy_pass" in conf, exact
    for internal in ("/api/jobs", "/api/conversion", "/api/build", "/api/tiles/rebuild-missing"):
        assert f"location = {internal}" not in conf and f"location {internal}" not in conf
    # the repo around the package (absent when only gis-data-platform/ is mounted)
    viewer_conf = APP.parent / "gis-viewers" / "nginx.conf"
    if viewer_conf.is_file():
        # directives only (comments may name the internal ports)
        viewer = "\n".join(line.split("#", 1)[0] for line in viewer_conf.read_text(encoding="utf-8").splitlines())
        assert "data-platform:8787" not in viewer and "engine:8787" not in viewer
        assert "limit_except GET HEAD OPTIONS" in viewer
    compose = APP.parent / "docker-compose.yml"
    if compose.is_file():
        assert '"${GIS_DATA_BIND:-127.0.0.1}:${GIS_DATA_PORT:-5100}:8787"' in compose.read_text(encoding="utf-8")
