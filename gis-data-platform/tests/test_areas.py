"""Saved-area records: persisted in the platform catalog, exposed via /api/areas."""


AREA = {
    "name": "demo-area",
    "bbox": [6.6, 45.6, 7.2, 46.0],
    "provider": "copernicus",
    "minzoom": 0,
    "maxzoom": 13,
}


def test_create_area_persists_in_catalog(client, tmp_path):
    response = client.post("/api/areas", json=AREA)
    assert response.status_code == 201
    area = response.json()
    assert area["name"] == "demo-area"
    assert area["bbox"] == [6.6, 45.6, 7.2, 46.0]
    assert area["provider"] == "copernicus"
    assert area["minzoom"] == 0
    assert area["maxzoom"] == 13
    assert area["crs"] == "EPSG:4326"
    assert area["dataset"] is None
    assert area["id"] and area["created"] and area["updated"]

    listed = client.get("/api/areas").json()
    assert listed["count"] == 1
    assert listed["areas"][0]["id"] == area["id"]

    fetched = client.get(f"/api/areas/{area['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == area["id"]

    catalog_file = tmp_path / "data" / "catalog" / "catalog.json"
    assert catalog_file.exists()
    assert "demo-area" in catalog_file.read_text(encoding="utf-8")


def test_delete_area_removes_record(client):
    area = client.post("/api/areas", json=AREA).json()
    deleted = client.delete(f"/api/areas/{area['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    assert client.get(f"/api/areas/{area['id']}").status_code == 404
    assert client.get("/api/areas").json()["count"] == 0
    assert client.delete(f"/api/areas/{area['id']}").status_code == 404


def test_area_validation_rejects_bad_input(client):
    assert client.post("/api/areas", json={**AREA, "bbox": [7.2, 45.6, 6.6, 46.0]}).status_code == 422
    assert client.post("/api/areas", json={**AREA, "bbox": [6.6, 45.6, 7.2]}).status_code == 422
    assert client.post("/api/areas", json={**AREA, "bbox": [6.6, 45.6, 200.0, 46.0]}).status_code == 422
    assert client.post("/api/areas", json={**AREA, "provider": "srtm"}).status_code == 422
    assert client.post("/api/areas", json={**AREA, "name": "bad name!"}).status_code == 422
    assert client.post("/api/areas", json={**AREA, "minzoom": 5, "maxzoom": 2}).status_code == 422


def test_concurrent_catalog_writes_are_serialized(client):
    """Job threads and API requests both write the catalog; writes must not race."""
    import sys
    import threading

    catalog = sys.modules["app.catalog"]
    area = catalog.create_area("target", [6.6, 45.6, 7.2, 46.0], "copernicus", 0, 13)
    errors: list[Exception] = []

    def updater() -> None:
        try:
            for i in range(10):
                catalog.update_area(area["id"], dataset={"name": "target", "state": "done", "i": i})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def creator() -> None:
        try:
            for i in range(10):
                catalog.create_area(f"w{i}", [6.6, 45.6, 7.2, 46.0], "copernicus", 0, 13)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=t) for t in (updater, creator, updater, creator)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    listed = client.get("/api/areas").json()
    assert listed["count"] == 21
    final = catalog.find_area(area["id"])
    assert final["dataset"]["state"] == "done"
