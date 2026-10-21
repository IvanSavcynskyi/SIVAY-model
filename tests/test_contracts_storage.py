from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.domain import FieldFeatureSnapshot, Interval, RecommendationRequest
from app.storage.database import Storage


def request():
    return RecommendationRequest.model_validate({
        "field": {"field_id": "f1", "geometry": {"type": "Polygon", "coordinates": [
            [[34.5, 49.6], [34.51, 49.6], [34.51, 49.61], [34.5, 49.61], [34.5, 49.6]]]},
            "crs": "EPSG:4326"},
        "as_of_date": "2026-09-01", "target_season": {"harvest_year": 2027, "include": ["winter", "spring"]},
    })


@pytest.mark.parametrize("field", ["as_of_date", "target_season"])
def test_required_request_fields(field):
    data = request().model_dump()
    del data[field]
    with pytest.raises(ValidationError):
        RecommendationRequest.model_validate(data)


def test_interval_order_and_missing_yield_zero_rejected():
    with pytest.raises(ValidationError):
        Interval(lo=.8, mid=.7, hi=.9)
    data = request().model_dump()
    data["crop_history"] = [{"season_year": 2025, "crop": "wheat", "yield_t_ha": 0}]
    with pytest.raises(ValidationError):
        RecommendationRequest.model_validate(data)


def test_persistent_immutable_snapshot(tmp_path):
    url = f"sqlite:///{tmp_path / 'state.db'}"
    storage = Storage(url)
    storage.initialize()
    snap = FieldFeatureSnapshot(snapshot_id="snap_1", field_id="f1", geom_hash="hash",
        as_of_date=date(2026, 9, 1), prepared_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        request_hash="request_hash")
    storage.put_snapshot(snap)
    storage.put_snapshot(snap)
    with pytest.raises(ValueError, match="immutable"):
        storage.put_snapshot(snap.model_copy(update={"region": "UA-53"}))
    storage.close()
    reopened = Storage(url)
    assert reopened.get_snapshot("snap_1") == snap
    reopened.close()


def test_jobs_lease_and_compare_and_swap(tmp_path):
    storage = Storage(f"sqlite:///{tmp_path / 'jobs.db'}")
    storage.initialize()
    job = storage.create_job(request())
    claimed = storage.claim_job()
    assert claimed and claimed[0] == job.job_id
    assert storage.claim_job() is None
    assert not storage.finish_job(job.job_id, "wrong-token", snapshot_id="bad")
    assert storage.finish_job(job.job_id, claimed[1], snapshot_id="snap_1", partial=True)
    assert storage.get_job(job.job_id).status == "partial"
    storage.close()


def test_worker_crash_recovers_expired_lease(tmp_path):
    from sqlalchemy import update
    from app.storage.database import JobRow
    storage = Storage(f"sqlite:///{tmp_path / 'jobs.db'}")
    storage.initialize()
    job = storage.create_job(request())
    first = storage.claim_job()
    with storage.engine.begin() as conn:
        conn.execute(update(JobRow).where(JobRow.id == job.job_id).values(
            lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
    second = storage.claim_job()
    assert second and second[1] != first[1]
    assert not storage.finish_job(job.job_id, first[1], snapshot_id="stale")
    assert storage.finish_job(job.job_id, second[1], snapshot_id="fresh")
    storage.close()


def test_axis_swap_is_detected_against_configured_country(tmp_path):
    import json
    from app.geo import GeometryError, validate_geometry
    from app.prepare import region_for_geometry

    region_file = tmp_path / "regions.geojson"
    region_file.write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"region": "UA"}, "geometry": {
            "type": "Polygon", "coordinates": [[[34, 49], [35, 49], [35, 50], [34, 50], [34, 49]]]}}]}))
    req = request()
    original = req.field.geometry.model_dump()
    assert region_for_geometry(validate_geometry(original, "EPSG:4326"), region_file) == "UA"
    swapped = {"type": "Polygon", "coordinates": [
        [[lat, lon] for lon, lat in original["coordinates"][0]]]}
    with pytest.raises(GeometryError, match="SUSPECTED_AXIS_SWAP"):
        region_for_geometry(validate_geometry(swapped, "EPSG:4326"), region_file)
