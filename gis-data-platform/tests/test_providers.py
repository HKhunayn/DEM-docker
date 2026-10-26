"""Provider availability for the fetch page (Copernicus GLO-30, FABDEM)."""


def test_provider_list_reports_availability(client, monkeypatch):
    monkeypatch.setattr(
        "app.fetch_api.probe_providers",
        lambda: {
            "copernicus": (True, ""),
            "fabdem": (False, "remotezip/requests not installed in engine python"),
        },
    )
    providers = client.get("/api/fetch/providers").json()["providers"]
    assert [p["id"] for p in providers] == ["copernicus", "fabdem"]
    by_id = {p["id"]: p for p in providers}
    assert by_id["copernicus"]["available"] is True
    assert by_id["fabdem"]["available"] is False
    assert "remotezip" in by_id["fabdem"]["reason"]
    assert "GLO-30" in by_id["copernicus"]["label"]
    assert "FABDEM" in by_id["fabdem"]["label"]
    assert "NON-COMMERCIAL" in by_id["fabdem"]["license"]


def test_provider_probe_reflects_engine_environment(client):
    # stub engine ships terrainhub.py, so Copernicus must work; FABDEM depends
    # on remotezip/requests being importable by the engine python.
    providers = {p["id"]: p for p in client.get("/api/fetch/providers").json()["providers"]}
    assert providers["copernicus"]["available"] is True
    assert isinstance(providers["fabdem"]["available"], bool)
