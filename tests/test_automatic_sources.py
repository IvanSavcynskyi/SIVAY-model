import json
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.adapters.public_weather import PublicWeather
from app.adapters.public_indices import PublicIndices
from app.automatic import refresh_automatic
from app.config import Settings
from app.domain import RecommendationRequest
from app.storage.database import Storage
from test_personalization import DAY, field_geometry, weather_input
from test_ranking import request

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def test_public_weather_uses_pinned_run_and_retrieval_availability():
    calls = []
    def respond(req):
        calls.append(req)
        if "archive" in req.url.host:
            assert req.url.params["models"] == "ecmwf_ifs"
            times = [(DAY-timedelta(days=i)).isoformat() for i in range(35, 1, -1)]
        else:
            assert req.url.params["run"] == "2026-09-01T00:00"
            assert "start_date" not in req.url.params and "end_date" not in req.url.params
            times = [(DAY+timedelta(days=i)).isoformat() for i in range(1, 8)]
        return httpx.Response(200, json={"latitude": 49.6, "longitude": 34.5, "daily": {
            "time": times, "temperature_2m_min": [5]*len(times), "temperature_2m_max": [20]*len(times),
            "precipitation_sum": [1]*len(times), "et0_fao_evapotranspiration": [2]*len(times)}})
    data = PublicWeather(httpx.MockTransport(respond), clock=lambda: NOW).collect(field_geometry())
    assert len(calls) == 2
    rows = data["weather"]["records"]
    assert {row["kind"] for row in rows} == {"reanalysis", "forecast"}
    assert all(datetime.fromisoformat(row["available_at"].replace("Z", "+00:00")) == NOW for row in rows)
    forecasts = [r for r in rows if r["kind"] == "forecast"]
    assert len(forecasts) == 7 and all(r["issued_at"].startswith("2026-09-01T00:00") for r in forecasts)
    assert data["weather"]["resolution_m"] == 9000


def test_failed_forecast_preserves_available_history():
    def respond(req):
        if "single-runs" in req.url.host:
            return httpx.Response(404)
        return httpx.Response(200, json={"daily": {"time": ["2026-08-30"], "precipitation_sum": [1]}})
    data = PublicWeather(httpx.MockTransport(respond), clock=lambda: NOW).collect(field_geometry())
    assert len(data["weather"]["records"]) == 1
    assert data["forecast_error"] == "PUBLIC_FORECAST_UNAVAILABLE"


def test_sentinel_cog_native_swir_grid_radiometric_offsets_and_quality(tmp_path):
    files = {}
    for band, number, scale in (("red", 2000, .0025), ("nir", 4000, .0025),
                                ("swir16", 3000, .005), ("scl", 4, .005)):
        size = 4 if scale == .0025 else 2
        path = tmp_path / (band+".tif")
        with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=1,
            dtype="uint16", crs="EPSG:4326", nodata=0, transform=from_origin(34.5, 49.61, scale, scale)) as dst:
            dst.write(np.full((size, size), number, dtype="uint16"), 1)
        files[band] = path
    item = {"id": "synthetic-scene", "geometry": field_geometry().geojson,
            "properties": {"datetime": "2026-08-31T10:00:00Z"}, "assets": {
                band: {"href": f"https://sentinel-cogs.s3.us-west-2.amazonaws.com/{band}.tif",
                       "raster:bands": [{"scale": .0001, "offset": -.1}]} for band in files}}
    previous = json.loads(json.dumps(item))
    previous["id"] = "previous-scene"
    previous["properties"]["datetime"] = "2026-08-25T10:00:00Z"
    features = [item, item, item, previous]
    def respond(req):
        payload = json.loads(req.content)
        assert payload["collections"] == ["sentinel-2-c1-l2a"]
        assert payload["intersects"] == json.loads(json.dumps(field_geometry().geojson))
        return httpx.Response(200, json={"features": features})
    def opener(url):
        return rasterio.open(files[url.rsplit("/", 1)[-1].removesuffix(".tif")])
    data = PublicIndices(httpx.MockTransport(respond), opener=opener, clock=lambda: NOW).collect(field_geometry())
    entries = {s["index"]: s["observations"][0] for s in data["indices"]}
    assert entries["NDVI"]["mean"] == pytest.approx(.5, abs=1e-6)
    assert entries["NDMI"]["mean"] == pytest.approx(.2, abs=1e-6)
    assert entries["NDMI"]["valid_pixel_fraction"] == pytest.approx(1)
    assert data["images"][0]["stac_item_id"] == "synthetic-scene"
    assert len(data["images"]) == 2
    for feature in features:
        feature["assets"]["red"]["href"] = "https://example.com/untrusted.tif"
    with pytest.raises(ValueError, match="NO_USABLE"):
        PublicIndices(httpx.MockTransport(respond), opener=opener, clock=lambda: NOW).collect(field_geometry())


def test_automatic_refresh_persists_caches_and_never_fetches_historical_requests(tmp_path, monkeypatch):
    import app.automatic as automatic
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(automatic, "datetime", FrozenClock)
    calls = []
    class Weather:
        def collect(self, geometry):
            calls.append("weather")
            return {"weather": weather_input().model_dump(mode="json")}
    class Indices:
        def collect(self, geometry):
            calls.append("indices")
            raise ValueError("unavailable")
    monkeypatch.setattr(automatic, "PublicWeather", Weather)
    monkeypatch.setattr(automatic, "PublicIndices", Indices)
    storage = Storage(f"sqlite:///{tmp_path / 'auto.db'}")
    storage.initialize()
    req = request()
    imported, errors = {}, []
    settings = Settings(database_url="sqlite://")
    refresh_automatic(req, field_geometry(), imported, settings, storage, errors)
    assert sorted(calls) == ["indices", "weather"]
    assert imported["auto:indices"]["data"]["error"] == "AUTOMATIC_INDICES_UNAVAILABLE"
    assert storage.get_source("auto:weather", req.field.field_id) is not None
    refresh_automatic(req, field_geometry(), imported, settings, storage, errors)
    assert len(calls) == 2
    historical = RecommendationRequest.model_validate(req.model_dump() | {"as_of_date": "2026-08-01"})
    refresh_automatic(historical, field_geometry(), {}, settings, storage, errors)
    assert len(calls) == 2
    assert storage.get_source("auto:weather", req.field.field_id, datetime(2026, 8, 31, tzinfo=timezone.utc)) is None
    storage.close()


def test_http_coordinate_request_fetches_weights_caches_and_replays(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.service import RecommendationService
    import app.automatic as automatic
    import app.prepare as preparation
    from test_gaez import make_store
    from test_personalization import policy, series

    class FrozenClock(datetime):
        current = NOW
        @classmethod
        def now(cls, tz=None):
            return cls.current
    monkeypatch.setattr(automatic, "datetime", FrozenClock)
    monkeypatch.setattr(preparation, "datetime", FrozenClock)
    calls = []
    class Weather:
        def collect(self, geometry):
            calls.append("weather")
            return {"weather": weather_input().model_dump(mode="json")}
    class Indices:
        def collect(self, geometry):
            calls.append("indices")
            return {"indices": [series("NDVI", .6).model_dump(mode="json"),
                series("NDMI", -.2 if FrozenClock.current == NOW else .2).model_dump(mode="json")], "images": []}
    monkeypatch.setattr(automatic, "PublicWeather", Weather)
    monkeypatch.setattr(automatic, "PublicIndices", Indices)
    make_store(tmp_path, available_at="2026-08-01T00:00:00Z")
    boundary = tmp_path / "regions.geojson"
    boundary.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature",
        "properties": {"region": "UA"}, "geometry": {"type": "Polygon", "coordinates": [
            [[34, 49], [36, 49], [36, 50], [34, 50], [34, 49]]]}}]}))
    service = RecommendationService(Settings(database_url=f"sqlite:///{tmp_path / 'http.db'}",
        gaez_root=tmp_path, regions_path=boundary))
    payload = request().model_dump(mode="json", by_alias=True)
    payload["field"]["geometry"] = field_geometry().geojson
    payload["target_season"]["include"] = ["winter"]
    payload["personalization"] = {"sowing_dates": {"wheat_winter": "2026-09-05"}, "policy": policy(rules=[
        {"id": "water", "crops": ["wheat_winter"], "metric": "weather.balance_30d", "operator": "lt",
         "threshold": 0, "full_risk_at": -60, "weight": .2, "source": "synthetic", "explanation_uk": "Вода."},
        {"id": "ndmi", "crops": ["wheat_winter"], "metric": "NDMI.mean", "operator": "lt",
         "threshold": 0, "full_risk_at": -.4, "weight": .3, "source": "synthetic", "explanation_uk": "NDMI."}
    ]).model_dump(mode="json")}
    with TestClient(create_app(service=service)) as client:
        response = client.post("/v1/recommendations", json=payload)
        assert response.status_code == 200, response.text
        first = response.json()
        stored = service.storage.get_recommendation(first["recommendation_id"])
        assert stored.provenance.source_errors == []
        row = stored.results[0].model_dump(mode="json")
        assert first["results"][0]["score"] == row["ranking_score"]["mid"]
        assert row["ranking_score"]["mid"] == pytest.approx(row["agro_score"]["mid"]*.85)
        assert row["personalization"]["metrics"]["NDVI.mean"] == .6
        assert len(row["personalization"]["weighted"]["groups"]) == 1
        assert sorted(calls) == ["indices", "weather"]
        assert client.post("/v1/recommendations", json=payload).json() == first
        assert len(calls) == 2
        snapshot_id = first["snapshot_id"]
        FrozenClock.current = NOW + timedelta(hours=7)
        updated = client.post("/v1/recommendations", json=payload).json()
        assert len(calls) == 4
        assert updated["snapshot_id"] != snapshot_id
        assert updated["results"][0]["score"] == pytest.approx(row["agro_score"]["mid"]*.9)
        assert client.post("/v1/recommendations", json=payload, params={"snapshot_id": snapshot_id}).json() == first
        assert len(calls) == 4
