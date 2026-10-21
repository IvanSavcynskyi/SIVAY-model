"""Explicit CRS handling; never guess or silently exchange coordinate axes."""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from pyproj import CRS, Geod, Transformer
from shapely import get_coordinates, make_valid, normalize, segmentize, transform as coordinate_transform
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.geometry.polygon import orient
from shapely.validation import explain_validity

GEOD = Geod(ellps="WGS84")


class GeometryError(ValueError):
    """A field cannot safely be interpreted as a valid WGS84 polygon."""


@dataclass(frozen=True)
class ValidatedGeometry:
    geometry: Polygon | MultiPolygon
    geom_hash: str
    area_ha: float
    repaired: bool = False

    @property
    def geojson(self) -> dict:
        return mapping(self.geometry)


def area_m2(geometry) -> float:
    """Geodesic WGS84 area; densification preserves native lon/lat edges.

    Raster edges are straight in longitude/latitude rather than great-circle arcs.
    Short segments avoid area differences when a polygon is split into cells.
    """
    if geometry.is_empty:
        return 0.0
    if geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
        return sum(area_m2(g) for g in geometry.geoms)
    if geometry.geom_type != "Polygon":
        return 0.0
    dense = segmentize(orient(geometry, sign=1), max_segment_length=0.01)
    return abs(GEOD.geometry_area_perimeter(dense)[0])


def validate_geometry(geometry: Any, crs: str) -> ValidatedGeometry:
    if not crs:
        raise GeometryError("CRS_REQUIRED: provide an explicit EPSG CRS")
    try:
        source = CRS.from_user_input(crs)
        if hasattr(geometry, "model_dump"):
            geometry = geometry.model_dump()
        geom = shape(geometry) if isinstance(geometry, dict) else geometry
        if not isinstance(geom, (Polygon, MultiPolygon)):
            raise GeometryError("INVALID_GEOMETRY: expected Polygon or MultiPolygon")
        if geom.is_empty or geom.has_z:
            raise GeometryError("INVALID_GEOMETRY: expected nonempty two-dimensional geometry")
        if not np.isfinite(get_coordinates(geom)).all():
            raise GeometryError("INVALID_GEOMETRY: coordinates must be finite")
        if source != CRS.from_epsg(4326):
            projector = Transformer.from_crs(source, 4326, always_xy=True)
            geom = coordinate_transform(geom, lambda xy: np.column_stack(projector.transform(xy[:, 0], xy[:, 1])))
    except GeometryError:
        raise
    except Exception as exc:
        raise GeometryError(f"INVALID_GEOMETRY_OR_CRS: {exc}") from exc
    coordinates = get_coordinates(geom)
    if not np.isfinite(coordinates).all():
        raise GeometryError("INVALID_GEOMETRY: CRS transformation produced nonfinite coordinates")
    xmin, ymin, xmax, ymax = geom.bounds
    if xmin < -180 or xmax > 180 or ymin < -90 or ymax > 90:
        raise GeometryError("INVALID_COORDINATES: expected longitude, latitude; check CRS and axis order")
    if xmax - xmin > 180:
        raise GeometryError("ANTIMERIDIAN_UNSUPPORTED: split the field before submission")
    repaired = False
    if not geom.is_valid:
        original_area = area_m2(geom)
        fixed = make_valid(geom)
        if not isinstance(fixed, (Polygon, MultiPolygon)) or original_area <= 0:
            raise GeometryError(f"INVALID_GEOMETRY: {explain_validity(geom)}")
        if abs(area_m2(fixed) - original_area) / original_area >= 0.01:
            raise GeometryError(f"INVALID_GEOMETRY: repair changes area by >=1%; {explain_validity(geom)}")
        geom, repaired = fixed, True
    # Hash precision is also extraction precision: identical hashes must mean identical fields.
    geom = normalize(coordinate_transform(geom, lambda xy: np.round(xy, 7)))
    if not geom.is_valid or geom.is_empty or geom.area == 0:
        raise GeometryError("INVALID_GEOMETRY: geometry collapses at 1e-7 degree precision")
    area = area_m2(geom)
    if not math.isfinite(area) or area <= 0:
        raise GeometryError("INVALID_GEOMETRY: expected positive area")
    return ValidatedGeometry(geom, hashlib.sha256(geom.wkb).hexdigest(), area / 10000, repaired)
