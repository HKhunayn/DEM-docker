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


def test_same_name_with_other_provider_is_rejected(client):
    first = client.post("/api/areas", json=AREA)
    assert first.status_code == 201
    # same (name, provider) refreshes the record
    again = client.post("/api/areas", json={**AREA, "bbox": [6.0, 45.0, 7.0, 46.0]})
    assert again.status_code == 201 and again.json()["id"] == first.json()["id"]
    # the ArcGIS service dir is keyed by name only: another provider would overwrite it
    clash = client.post("/api/areas", json={**AREA, "provider": "fabdem"})
    assert clash.status_code == 409
    assert "copernicus" in clash.json()["detail"] and "demo-area" in clash.json()["detail"]
    assert client.get("/api/areas").json()["count"] == 1


def test_job_with_clashing_provider_is_rejected(client, monkeypatch):
    monkeypatch.setattr("app.fetch_api.probe_providers",
                        lambda: {"copernicus": (True, ""), "fabdem": (True, "")})
    client.post("/api/areas", json=AREA)
    response = client.post("/api/jobs", json={**AREA, "provider": "fabdem"})
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
    assert client.get("/api/jobs").json()["count"] == 0
    # after deleting the area the name is free for the other provider
    area_id = client.get("/api/areas").json()["areas"][0]["id"]
    client.delete(f"/api/areas/{area_id}")
    assert client.post("/api/areas", json={**AREA, "provider": "fabdem"}).status_code == 201
