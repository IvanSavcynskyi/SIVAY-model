import json
from datetime import date

import numpy as np
import pytest
import rasterio
from pydantic import ValidationError
from rasterio.transform import from_origin
from shapely.geometry import box

from app.adapters.gaez_store import GaezStore, LayerNotAvailable, file_sha256, inspect_layer, ManifestLayer


def make_store(tmp_path, **overrides):
    path = tmp_path / "wheat.tif"
    with rasterio.open(path, "w", driver="GTiff", width=2, height=1, count=1,
                       dtype="int16", crs="EPSG:4326", transform=from_origin(34, 50, 1, 1), nodata=-9) as dst:
        dst.write(np.array([[7000, 9000]], dtype="int16"), 1)
    layer = dict(layer_code="GAEZ-V5.RES05-SUX.HP0120.AGERA5.HIST.WHE.HRLM", variable="RES05-SUX",
                 unit="SI*100", crop_code="WHE", path="wheat.tif", sha256=file_sha256(path), dtype="int16",
                 nodata=-9, native_res_deg=1, source_url="https://storage.googleapis.com/fao-gismgr-gaez-v5-data/test",
                 available_at="2026-09-01T12:00:00Z", retrieved_at="2026-10-21T00:00:00Z")
    layer.update(overrides)
    (tmp_path / "manifest.json").write_text(json.dumps({"layers": [layer]}))
    return GaezStore(tmp_path), layer


def test_real_local_raster_and_availability_cutoff(tmp_path):
    store, layer = make_store(tmp_path)
    with pytest.raises(LayerNotAvailable):
        store.extract(layer["layer_code"], box(34, 49, 36, 50), "EPSG:4326", date(2026, 8, 31))
    value = store.extract(layer["layer_code"], box(34, 49, 36, 50), "EPSG:4326", date(2026, 9, 1))
    assert value.mean_w == pytest.approx(8000)
    assert value.coverage_valid == pytest.approx(1, abs=1e-7)
    assert value.source_sha256 == layer["sha256"]
    assert store.values_for_field(box(34, 49, 35, 50), "EPSG:4326", date(2026, 10, 1), ["WHE", "SOY"])["SOY"] == []


@pytest.mark.parametrize("path", ["../outside.tif", "/tmp/outside.tif", "https://example.com/a.tif", "/vsicurl/evil"])
def test_unsafe_paths_rejected(tmp_path, path):
    with pytest.raises(ValueError, match="INVALID_LOCAL_PATH"):
        make_store(tmp_path, path=path)


def test_symlink_escape_rejected(tmp_path):
    (tmp_path / "escape.tif").symlink_to(tmp_path.parent / "outside.tif")
    with pytest.raises(ValueError, match="INVALID_LOCAL_PATH"):
        make_store(tmp_path, path="escape.tif")


def test_checksum_and_metadata_validation(tmp_path):
    store, layer = make_store(tmp_path, sha256="0" * 64)
    with pytest.raises(ValueError, match="CHECKSUM_MISMATCH"):
        store.verify()
    store, layer = make_store(tmp_path, dtype="float32")
    with pytest.raises(ValueError, match="INVALID_GAEZ_METADATA"):
        store.verify()


def test_unknown_publication_timestamp_rejected(tmp_path):
    with pytest.raises(ValidationError):
        make_store(tmp_path, available_at=None)
    with pytest.raises(ValidationError):
        make_store(tmp_path, available_at="2026-09-01T00:00:00")


def test_out_of_range_is_rejected_before_extraction(tmp_path):
    _, data = make_store(tmp_path)
    path = tmp_path / "wheat.tif"
    with rasterio.open(path, "r+") as dst:
        dst.write(np.array([[7000, 10001]], dtype="int16"), 1)
    data["sha256"] = file_sha256(path)
    with pytest.raises(ValueError, match="INVALID_RASTER_VALUE"):
        inspect_layer(path, ManifestLayer.model_validate(data))


def test_changed_file_invalidates_cached_extraction(tmp_path):
    store, layer = make_store(tmp_path)
    args = layer["layer_code"], box(34, 49, 36, 50), "EPSG:4326", date(2026, 10, 1)
    store.extract(*args)
    with rasterio.open(tmp_path / "wheat.tif", "r+") as dst:
        dst.write(np.array([[100, 200]], dtype="int16"), 1)
    with pytest.raises(ValueError, match="CHECKSUM_MISMATCH"):
        store.extract(*args)


def test_explicit_uninterpreted_negative_source_flag_is_missing(tmp_path):
    _, data = make_store(tmp_path, excluded_values=[-1])
    path = tmp_path / "wheat.tif"
    with rasterio.open(path, "r+") as dst:
        dst.write(np.array([[7000, -1]], dtype="int16"), 1)
    data["sha256"] = file_sha256(path)
    (tmp_path / "manifest.json").write_text(json.dumps({"layers": [data]}))
    store = GaezStore(tmp_path)
    value = store.extract(data["layer_code"], box(34, 49, 36, 50), "EPSG:4326", date(2026, 10, 1))
    assert value.mean_w == 7000
    assert value.coverage_valid == pytest.approx(0.5, abs=1e-6)
    assert value.per_cell[1].value is None
