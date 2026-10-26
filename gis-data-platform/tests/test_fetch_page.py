"""The fetch page and its local vendor assets."""


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
        "/api/fetch/providers",
        "/api/areas",
        "/api/jobs",
    ):
        assert marker in html, marker
    assert client.get("/fetch").status_code == 200
    assert client.get("/fetch/index.html").status_code == 200


def test_leaflet_vendor_served_from_engine_node_modules(client):
    js = client.get("/vendor/leaflet/dist/leaflet.js")
    assert js.status_code == 200
    assert "leaflet" in js.text
    css = client.get("/vendor/leaflet/dist/leaflet.css")
    assert css.status_code == 200
    assert client.get("/vendor/leaflet/dist/missing.js").status_code == 404
    # traversal outside the engine's leaflet package must not resolve
    assert client.get("/vendor/leaflet/dist/%2e%2e/%2e%2e/%2e%2e/terrainhub.py").status_code == 404
