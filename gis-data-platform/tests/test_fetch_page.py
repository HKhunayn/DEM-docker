"""The fetch page (data-platform) and its local vendor assets."""
import re


def test_fetch_page_serves_html(client):
    response = client.get("/fetch/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    for marker in (
        "Fetch elevation data",
        'id="map"',
        'id="providers"',
        'id="areas"',
        'id="minlon"',
        'id="selection"',
        'id="est"',
        'id="go"',
        'id="state"',
        'id="phase"',
        'id="banner"',
        'id="log"',
        # the engine's side of the pipeline (followed through the platform)
        'id="enginestatus"',
        'id="pipeline"',
        "api/engine/status",
        "api/engine/build/",
        "engine not running - it builds this area when it starts",
        "api/fetch/providers",
        "api/areas",
        "api/jobs",
    ):
        assert marker in html, marker
    # relative urls only: the page also works behind the viewer's /fetch/ proxy
    assert not re.search(r'["`]/(api|vendor|tiles|arcgis)/', html)
    assert client.get("/fetch").status_code == 200          # redirected to /fetch/
    assert client.get("/fetch", follow_redirects=False).headers["location"] == "/fetch/"
    assert client.get("/fetch/index.html").status_code == 200


def test_leaflet_vendor_served_from_engine_node_modules(client):
    for prefix in ("", "/fetch"):
        js = client.get(f"{prefix}/vendor/leaflet/dist/leaflet.js")
        assert js.status_code == 200
        assert "leaflet" in js.text
        css = client.get(f"{prefix}/vendor/leaflet/dist/leaflet.css")
        assert css.status_code == 200
        assert client.get(f"{prefix}/vendor/leaflet/dist/missing.js").status_code == 404
        # traversal outside the engine's leaflet package must not resolve
        assert client.get(f"{prefix}/vendor/leaflet/dist/%2e%2e/%2e%2e/%2e%2e/terrainhub.py").status_code == 404
