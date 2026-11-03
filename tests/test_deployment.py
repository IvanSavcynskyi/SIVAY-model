"""Deployment access control and immutable source evidence."""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from test_api import request_payload


@pytest.fixture
def protected_api(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'secured.db'}", demo=True,
                        api_key="test-deployment-credential-32-characters")
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.mark.parametrize("path", ["/v1/health", "/v1/recommendations/missing/analysis",
                                   "/api/fields/f1/indices"])
def test_data_routes_require_bearer_key(protected_api, path):
    assert protected_api.get(path).status_code == 401
    assert protected_api.get(path, headers={"Authorization": "Bearer incorrect"}).status_code == 401


def test_authenticated_verdict_and_evidence_share_snapshot(protected_api):
    client = protected_api
    client.headers["Authorization"] = "Bearer test-deployment-credential-32-characters"
    assert client.get("/v1/health").status_code == 200
    response = client.post("/v1/recommendations", json=request_payload())
    assert response.status_code == 200, response.text
    result = response.json()
    analysis = client.get(f"/v1/recommendations/{result['recommendation_id']}/analysis")
    assert analysis.status_code == 200
    evidence = analysis.json()
    assert evidence["snapshot_id"] == result["snapshot_id"]
    assert evidence["field"] == request_payload()["field"]
    assert evidence["area_ha"] > 0
    assert [crop["crop_id"] for crop in evidence["crops"]] == [crop["crop_id"] for crop in result["results"]]
    assert client.get("/v1/recommendations/missing/analysis").status_code == 404
    assert "test-deployment-credential" not in client.get("/openapi.json").text
