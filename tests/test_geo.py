import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import MultiPolygon, Polygon, box, mapping
from shapely.ops import transform

from app.geo import GeometryError, extract_raster, validate_geometry


def raster(tmp_path, data, *, nodata=-9, north=2):
    path = tmp_path / "synthetic.tif"
    array = np.asarray(data)
    with rasterio.open(path, "w", driver="GTiff", width=array.shape[1], height=array.shape[0], count=1,
                       dtype=array.dtype, crs="EPSG:4326", transform=from_origin(0, north, 1, 1), nodata=nodata) as dst:
        dst.write(array, 1)
    return path


def test_native_area_weighting_and_significant_extrema(tmp_path):
    path = raster(tmp_path, np.array([[10, 20], [30, 40]], dtype="int16"))
    result = extract_raster(path, validate_geometry(box(0, 0, 2, 2), "EPSG:4326"))
    assert result.mean_w == pytest.approx(25, abs=0.003)
    assert result.coverage_valid == pytest.approx(1, abs=1e-7)
    assert result.min == 10 and result.max == 40
    assert result.n_cells == 4
    assert sum(c.area_fraction for c in result.per_cell) == pytest.approx(1, abs=1e-7)


def test_small_intersection_does_not_set_extrema(tmp_path):
    path = raster(tmp_path, np.array([[10, 1000]], dtype="int16"), north=1)
    result = extract_raster(path, validate_geometry(box(0, 0, 1.05, 1), "EPSG:4326"))
    assert result.mean_w == pytest.approx((10 + 50) / 1.05, abs=0.001)
    assert result.min == result.max == 10


def test_nodata_is_missing_and_outside_extent_reduces_coverage(tmp_path):
    path = raster(tmp_path, np.array([[10, -9]], dtype="int16"), north=1)
    result = extract_raster(path, validate_geometry(box(0, 0, 4, 1), "EPSG:4326"))
    assert result.mean_w == 10
    assert result.coverage_valid == pytest.approx(0.25, abs=1e-6)
    assert result.per_cell[1].value is None


def test_classes_are_shares_and_water_is_separate(tmp_path):
    path = raster(tmp_path, np.array([[2, 8], [9, 0]], dtype="uint8"), nodata=0)
    result = extract_raster(path, validate_geometry(box(0, 0, 2, 2), "EPSG:4326"), categorical=True)
    assert result.mean_w is None and result.min is None and result.max is None
    assert result.class_shares == {2: pytest.approx(0.5), 8: pytest.approx(0.5)}
    assert result.coverage_valid == pytest.approx(0.5, abs=0.001)
    assert result.water_share == pytest.approx(0.25, abs=0.001)
    assert result.worst_class == 8


def test_holes_and_multipolygons_preserve_area(tmp_path):
    path = raster(tmp_path, np.array([[10, 20], [30, 40]], dtype="int16"))
    hole = Polygon(box(0, 0, 2, 2).exterior.coords, [box(0.2, 0.2, 0.8, 0.8).exterior.coords])
    result = extract_raster(path, validate_geometry(hole, "EPSG:4326"))
    expected = (10 + 20 + 30 * 0.64 + 40) / 3.64
    assert result.mean_w == pytest.approx(expected, abs=0.005)
    multi = MultiPolygon([box(0.1, 1.1, 0.9, 1.9), box(1.1, 0.1, 1.9, 0.9)])
    result = extract_raster(path, validate_geometry(multi, "EPSG:4326"))
    assert result.mean_w == pytest.approx(25, abs=0.004)
    assert result.n_cells == 2


def test_crs_roundtrip_and_hash_are_canonical():
    field = box(34.501, 49.598, 34.523, 49.612)
    a = validate_geometry(field, "EPSG:4326")
    projected = transform(Transformer.from_crs(4326, 32636, always_xy=True).transform, field)
    b = validate_geometry(mapping(projected), "EPSG:32636")
    reverse = Polygon(list(field.exterior.coords)[::-1])
    assert a.geom_hash == b.geom_hash == validate_geometry(reverse, "EPSG:4326").geom_hash
    assert a.area_ha == b.area_ha


@pytest.mark.parametrize("geometry,crs", [
    (box(0, 0, 1, 1), ""),
    (box(0, 100, 1, 101), "EPSG:4326"),
    ({"type": "Point", "coordinates": [34, 49]}, "EPSG:4326"),
    (Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)]), "EPSG:4326"),
])
def test_bad_geometry_is_rejected(geometry, crs):
    with pytest.raises(GeometryError):
        validate_geometry(geometry, crs)


def test_no_intersection_returns_unknown_not_zero(tmp_path):
    path = raster(tmp_path, np.array([[10]], dtype="int16"), north=1)
    result = extract_raster(path, validate_geometry(box(5, 5, 6, 6), "EPSG:4326"))
    assert result.coverage_valid == 0
    assert result.mean_w is None
    assert result.n_cells == 0


def test_high_latitude_cells_have_lower_area_weight(tmp_path):
    path = raster(tmp_path, np.array([[100], [0]], dtype="int16"), north=80)
    result = extract_raster(path, validate_geometry(box(0, 78, 1, 80), "EPSG:4326"))
    assert 47 < result.mean_w < 49  # Northern cell has less area than southern cell.


def test_single_cell_is_constant(tmp_path):
    path = raster(tmp_path, np.array([[7300]], dtype="int16"), north=1)
    result = extract_raster(path, validate_geometry(box(0.1, 0.1, 0.2, 0.2), "EPSG:4326"))
    assert result.mean_w == result.min == result.max == 7300
    assert result.n_cells == 1
    assert result.field_to_cell_ratio == pytest.approx(0.01, abs=1e-5)
