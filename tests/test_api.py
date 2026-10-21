"""HTTP contract and persisted end-to-end workflow checks."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import RecommendationRequest
from app.main import create_app
from app.service import RecommendationService


def request_payload():
    return {
        "field": {"field_id": "f1", "geometry": {"type": "Polygon", "coordinates": [
            [[34.5, 49.6], [34.51, 49.6], [34.51, 49.61], [34.5, 49.61], [34.5, 49.6]]]},
            "crs": "EPSG:4326"},
        "as_of_date": "2026-09-01",
        "target_season": {"harvest_year": 2027, "include": ["winter", "spring"]},
    }


@pytest.fixture
def api(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'api.db'}", demo=True,
                        ranking_profile="validated")
    service = RecommendationService(settings)
    with TestClient(create_app(service=service)) as client:
        yield client, service


def test_recommendation_persisted_reused_and_replayed(api):
    client, service = api
    payload = request_payload()
    response = client.post("/v1/recommendations", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["results"] and len(result["results"]) <= 5
    assert result["provenance"]["demo"]
    assert client.get(f"/v1/recommendations/{result['recommendation_id']}").json() == result
    assert client.post("/v1/recommendations", json=payload).json() == result
    replay = client.post("/v1/recommendations", params={"snapshot_id": result["provenance"]["snapshot_id"]}, json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json() == result
    assert service.storage.get_snapshot(result["provenance"]["snapshot_id"])


def test_prepare_durable_worker_then_recommend(api):
    client, service = api
    response = client.post("/v1/fields/f1/prepare", json=request_payload())
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    assert response.headers["location"] == f"/v1/jobs/{job['job_id']}"
    assert service.process_one_job()
    job = client.get(response.headers["location"]).json()
    assert job["status"] in {"completed", "partial"}
    result = client.post("/v1/recommendations", json=request_payload()).json()
    assert result["provenance"]["snapshot_id"] == job["snapshot_id"]


def test_invalid_input_and_unknown_resources(api):
    client, _ = api
    payload = request_payload()
    del payload["as_of_date"]
    assert client.post("/v1/recommendations", json=payload).status_code == 422
    payload = request_payload()
    del payload["field"]["crs"]
    assert client.post("/v1/recommendations", json=payload).status_code == 422
    payload = request_payload()
    payload["field"]["geometry"]["coordinates"] = []
    result = client.post("/v1/recommendations", json=payload)
    assert result.status_code == 422
    assert result.json()["detail"]["code"] == "INVALID_FIELD"
    assert client.post("/v1/fields/other/prepare", json=request_payload()).status_code == 422
    for path in ["/v1/jobs/missing", "/v1/recommendations/missing"]:
        assert client.get(path).status_code == 404
    assert client.post("/v1/recommendations?snapshot_id=missing", json=request_payload()).status_code == 404


def test_catalog_rules_health_and_openapi(api):
    client, _ = api
    catalog = client.get("/v1/catalog/crops?region=UA-53").json()
    assert catalog["crops"] and catalog["demo"]
    assert client.get("/v1/catalog/crops?region=UNKNOWN").json()["crops"] == []
    assert client.get("/v1/catalog/crops").status_code == 422
    assert client.get("/v1/rules/versions").json()["active_version"] == catalog["rules_version"]
    health = client.get("/v1/health")
    assert health.status_code == 200
    assert health.json()["database"] == "ok"
    assert "DEMO_MODE" in health.json()["readiness_issues"]
    assert not health.json()["recommendation_ready"]
    assert client.get("/docs").status_code == 200
    assert "/v1/recommendations" in client.get("/openapi.json").json()["paths"]


def test_replay_rejects_changed_input(api):
    client, _ = api
    result = client.post("/v1/recommendations", json=request_payload()).json()
    payload = request_payload()
    payload["constraints"] = {"excluded_crops": ["wheat"]}
    changed = client.post("/v1/recommendations", json=payload,
                          params={"snapshot_id": result["provenance"]["snapshot_id"]})
    assert changed.status_code == 422


def test_internal_error_not_disclosed(api, monkeypatch):
    client, service = api
    def broken(_):
        raise OSError("secret /private/source.csv credentials")
    monkeypatch.setattr(service, "recommend", broken)
    result = client.post("/v1/recommendations", json=request_payload())
    assert result.status_code == 503
    assert "secret" not in result.text and "/private/" not in result.text


def test_source_documents_persist_costs_history_indices(api):
    client, _ = api
    assert client.get("/v1/fields/f1/costs").status_code == 404
    costs = {"wheat_winter": {"value": 30000, "drying_per_t": 100}}
    stored = client.put("/v1/fields/f1/costs", json=costs)
    assert stored.status_code == 200, stored.text
    assert stored.json() == client.get("/v1/fields/f1/costs").json()
    assert datetime.fromisoformat(stored.json()["available_at"]).tzinfo
    assert client.put("/v1/fields/f1/costs", json={"wheat_winter": {"value": -1}}).status_code == 422
    history = [{"season_year": 2025, "crop": "soybean", "source": "farm_records", "reliability": "high"}]
    assert client.put("/v1/fields/f1/history", json=history).status_code == 200
    assert client.get("/v1/fields/f1/history").json()["data"][0]["crop"] == "soybean"
    series = {"index": "NDVI", "sensor": "Sentinel-2", "native_resolution_m": 10,
              "processing_version": "fixture-v1", "observations": [
                  {"date": "2026-08-01", "acquired_at": "2026-08-01T10:00:00Z",
                   "available_at": "2026-08-02T10:00:00Z", "valid_pixel_fraction": .9, "mean": .6},
                  {"date": "2026-09-01", "acquired_at": "2026-09-01T10:00:00Z",
                   "available_at": "2026-09-02T10:00:00Z", "valid_pixel_fraction": .8, "mean": .4}]}
    assert client.post("/v1/fields/f1/indices", json=series).status_code == 201
    found = client.get("/api/fields/f1/indices?index=NDVI&from=2026-08-01&to=2026-08-31")
    assert found.status_code == 200
    assert len(found.json()["observations"]) == 1
    assert found.json()["processing_version"] == "fixture-v1"
    assert client.get("/api/fields/f1/indices?index=FAKE").status_code == 422
    assert client.get("/api/fields/f1/indices?from=2026-10-01&to=2026-08-01").status_code == 422
    assert client.get("/v1/fields/f1/indices?index=NDMI").status_code == 404


def test_job_failure_details_are_redacted(api):
    client, service = api
    job = service.storage.create_job(RecommendationRequest.model_validate(request_payload()))
    claimed = service.storage.claim_job()
    service.storage.finish_job(job.job_id, claimed[1], error="private server details")
    response = client.get(f"/v1/jobs/{job.job_id}")
    assert response.json()["error"] == "PREPARE_FAILED"


def test_simple_price_import_metadata_and_validation(api):
    client, _ = api
    assert client.get("/v1/prices").status_code == 404
    payload = {"csv": "crop,price\nwheat_winter,8500\n", "currency": "UAH", "unit": "t",
               "price_date": "2026-08-28", "published_at": "2026-08-29T06:00:00Z",
               "incoterm": "EXW", "vat_included": False, "source": "test-table", "region": "UA"}
    response = client.post("/v1/prices/import", json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["rows_imported"] == 1
    stored = client.get("/v1/prices").json()
    assert stored["data"][0]["price_value"] == 8500
    assert stored["data"][0]["published_at"].startswith("2026-08-29")
    assert stored["data"][0]["available_at"].startswith("2026-08-29")
    assert stored["available_at"] != stored["data"][0]["available_at"]
    payload["csv"] = "crop,price\nwheat_winter,0\n"
    assert client.post("/v1/prices/import", json=payload).status_code == 422
    payload["csv"] = "crop,price\nunknown_crop,100\n"
    assert client.post("/v1/prices/import", json=payload).status_code == 422
    payload["csv"] = "crop,price\nwheat_winter,8500\n"
    payload["published_at"] = "2199-01-01T00:00:00Z"
    assert client.post("/v1/prices/import", json=payload).status_code == 422


def test_health_database_failure_does_not_disclose_internal_details(api, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError
    client, service = api
    def fail_connection():
        raise SQLAlchemyError("private database details")
    monkeypatch.setattr(service.storage.engine, "connect", fail_connection)
    result = client.get("/v1/health")
    assert result.status_code == 503
    assert result.json()["database"] == "unavailable"
    assert not result.json()["recommendation_ready"]
    assert "private" not in result.text
