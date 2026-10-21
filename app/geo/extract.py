"""Exact polygon-cell intersections, without raster resampling or class averaging.

Shapely intersections supply the same native-grid clipping operation required of
exactextract. This implementation is intentionally bounded to field-size windows.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window, from_bounds
from shapely.geometry import box

from .geometry import ValidatedGeometry, area_m2


@dataclass(frozen=True)
class ExtractedCell:
    row: int
    col: int
    value: float | None
    area_fraction: float  # Share of the entire field, not of the raster cell.
    pixel_fraction: float


@dataclass(frozen=True)
class RasterExtraction:
    native_res_deg: float
    field_area_ha: float
    cell_area_ha_mean: float
    field_to_cell_ratio: float
    coverage_valid: float
    n_cells: int
    mean_w: float | None = None
    min: float | None = None
    max: float | None = None
    class_shares: dict[int, float] | None = None
    dominant_class: int | None = None
    worst_class: int | None = None
    water_share: float | None = None
    per_cell: list[ExtractedCell] = field(default_factory=list)


def extract_raster(
    path: str | Path,
    field_geometry: ValidatedGeometry,
    *,
    categorical: bool = False,
    excluded_values: list[float] | None = None,
    max_window_cells: int = 1_000_000,
) -> RasterExtraction:
    geom = field_geometry.geometry
    field_area = field_geometry.area_ha * 10000
    with rasterio.open(path) as src:
        if src.crs is None or src.crs.to_epsg() != 4326:
            raise ValueError("GAEZ raster must use EPSG:4326; resampling is prohibited")
        if src.count != 1 or src.transform.b or src.transform.d:
            raise ValueError("Expected a single-band north-up raster")
        if src.transform.a <= 0 or src.transform.e >= 0:
            raise ValueError("Expected an eastward, north-up raster grid")
        resolution = abs(src.transform.a)
        reference_pixel = box(*rasterio.transform.array_bounds(1, 1, src.transform))
        reference_area = area_m2(reference_pixel)
        covered_geometry = geom.intersection(box(*src.bounds))
        cells: list[ExtractedCell] = []
        areas: list[float] = []
        weighted: list[tuple[float, float]] = []
        significant: list[float] = []
        class_areas: dict[int, float] = {}
        water_area = 0.0
        valid_area = 0.0
        if not covered_geometry.is_empty and covered_geometry.area > 0:
            win = from_bounds(*covered_geometry.bounds, transform=src.transform)
            row0 = max(0, math.floor(win.row_off))
            col0 = max(0, math.floor(win.col_off))
            row1 = min(src.height, math.ceil(win.row_off + win.height))
            col1 = min(src.width, math.ceil(win.col_off + win.width))
            if (row1 - row0) * (col1 - col0) > max_window_cells:
                raise ValueError("FIELD_TOO_LARGE: raster extraction window exceeds configured limit")
            window = Window(col0, row0, col1 - col0, row1 - row0)
            values = src.read(1, window=window, masked=True)
            for r, c in np.ndindex(values.shape):
                row, col = row0 + r, col0 + c
                x0, y0 = src.transform.c + col * src.transform.a, src.transform.f + row * src.transform.e
                x1, y1 = x0 + src.transform.a, y0 + src.transform.e
                pixel = box(x0, y1, x1, y0)
                overlap = geom.intersection(pixel)
                if overlap.is_empty or overlap.area <= 0:
                    continue
                overlap_area = area_m2(overlap)
                pixel_area = area_m2(pixel)
                areas.append(pixel_area)
                value = values[r, c]
                missing = bool(np.ma.is_masked(value))
                number = None if missing else float(value)
                if number is not None and not math.isfinite(number):
                    number = None
                if number in (excluded_values or []):
                    number = None
                fraction = min(1.0, overlap_area / field_area)
                cells.append(ExtractedCell(row, col, number, fraction, overlap.area / pixel.area))
                if number is None:
                    continue
                if categorical:
                    if not number.is_integer() or not 0 <= number <= 9:
                        raise ValueError("INVALID_CLASS: expected integral suitability class 0..9")
                    if number == 9:
                        water_area += overlap_area
                        continue
                    if number == 0:
                        continue
                    key = int(number)
                    class_areas[key] = class_areas.get(key, 0) + overlap_area
                else:
                    weighted.append((number, overlap_area))
                    if fraction >= 0.1 - 1e-8:
                        significant.append(number)
                valid_area += overlap_area
        cell_area = float(np.mean(areas)) if areas else reference_area
        mean = sum(v * a for v, a in weighted) / valid_area if weighted else None
        shares = {k: v / valid_area for k, v in sorted(class_areas.items())} if valid_area and categorical else ({} if categorical else None)
        dominant = max(shares, key=lambda k: (shares[k], -k)) if shares else None
        worst = max((k for k, v in (shares or {}).items() if v >= 0.1 - 1e-8), default=None)
        return RasterExtraction(
            native_res_deg=resolution,
            field_area_ha=field_geometry.area_ha,
            cell_area_ha_mean=cell_area / 10000,
            field_to_cell_ratio=field_area / cell_area,
            coverage_valid=min(1, max(0, valid_area / field_area)),
            n_cells=len(cells),
            mean_w=mean,
            min=min(significant) if significant else None,
            max=max(significant) if significant else None,
            class_shares=shares,
            dominant_class=dominant,
            worst_class=worst,
            water_share=min(1, water_area / field_area) if categorical else None,
            per_cell=cells,
        )
