"""Field-clipped spectral statistics from uploaded, quality-masked GeoTIFFs."""
from __future__ import annotations

import base64
import binascii
import hashlib
import math
from datetime import timezone

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.io import MemoryFile
from rasterio.windows import Window, from_bounds
from shapely import area, box, intersection, transform

from app.domain import FieldImageInput, SatelliteObservation, SatelliteSeries

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_WINDOW_PIXELS = 250_000


def decode_image(image: FieldImageInput) -> bytes:
    try:
        data = base64.b64decode(image.data_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("INVALID_IMAGE_BASE64") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("IMAGE_TOO_LARGE: maximum 10 MiB")
    # Only a self-contained TIFF is allowed, never VRT or a remote GDAL source.
    if data[:4] not in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        raise ValueError("EXPECTED_GEOTIFF")
    return data


def validate_image(image: FieldImageInput) -> str:
    data = decode_image(image)
    try:
        with MemoryFile(data) as memory, memory.open() as src:
            if src.driver != "GTiff" or src.crs is None:
                raise ValueError("IMAGE_CRS_REQUIRED")
            if src.transform.b or src.transform.d or src.transform.a <= 0 or src.transform.e >= 0:
                raise ValueError("EXPECTED_NORTH_UP_IMAGE")
            if max(image.bands.values()) > src.count or src.count > 16:
                raise ValueError("INVALID_IMAGE_BANDS")
            if src.width * src.height > 50_000_000:
                raise ValueError("IMAGE_DIMENSIONS_TOO_LARGE")
    except rasterio.errors.RasterioError as exc:
        raise ValueError("INVALID_GEOTIFF") from exc
    return hashlib.sha256(data).hexdigest()


def _project(geometry, source, destination):
    projector = Transformer.from_crs(source, destination, always_xy=True)
    return transform(geometry, lambda xy: np.column_stack(projector.transform(xy[:, 0], xy[:, 1])))


def _quantile(values, weights, fractions):
    order = np.argsort(values)
    cumulative = np.cumsum(weights[order]) / weights.sum()
    return np.interp(fractions, cumulative, values[order]).tolist()


def image_indices(image: FieldImageInput, field_geometry):
    """Intersect native pixels with the field; weight by clipped equal-area area.

    Raster bands are never resampled. Clear pixels, radiometry, NoData and zero
    denominators are checked before either index is computed.
    """
    checksum = validate_image(image)
    series = []
    metadata = {"image_id": image.image_id, "sha256": checksum,
                "sensor": image.sensor, "processing_version": image.processing_version,
                "acquired_at": image.acquired_at.isoformat(),
                "available_at": image.available_at.isoformat()}
    with MemoryFile(decode_image(image)) as memory, memory.open() as src:
        geom = _project(field_geometry.geometry, 4326, src.crs)
        covered = geom.intersection(box(*src.bounds))
        if covered.is_empty or covered.area <= 0:
            raise ValueError("IMAGE_DOES_NOT_COVER_FIELD")
        win = from_bounds(*covered.bounds, transform=src.transform)
        col0, row0 = max(0, math.floor(win.col_off)), max(0, math.floor(win.row_off))
        col1 = min(src.width, math.ceil(win.col_off + win.width))
        row1 = min(src.height, math.ceil(win.row_off + win.height))
        if (row1 - row0) * (col1 - col0) > MAX_WINDOW_PIXELS:
            raise ValueError("IMAGE_WINDOW_TOO_LARGE: use a coarser or field-clipped raster")
        window = Window(col0, row0, col1 - col0, row1 - row0)
        rows, cols = np.indices((row1 - row0, col1 - col0))
        x = src.transform.c + (cols.ravel() + col0) * src.transform.a
        y = src.transform.f + (rows.ravel() + row0) * src.transform.e
        pixels = box(x, y + src.transform.e, x + src.transform.a, y)
        clipped = intersection(pixels, geom)
        weights = area(_project(clipped, src.crs, 6933))
        total = float(area(_project(geom, src.crs, 6933)))
        resolution = math.sqrt(float(np.median(area(_project(pixels, src.crs, 6933)))))
        metadata.update(native_resolution_m=resolution, crs=str(src.crs))
        bands = {name: src.read(number, window=window, masked=True).astype(float).ravel()
                 for name, number in image.bands.items()}
        quality = bands.pop("quality")
        clear = (~np.ma.getmaskarray(quality) & np.isin(quality.data, image.clear_values) & (weights > 0))
        scaled = {name: values * image.scale + image.offset for name, values in bands.items()}
        for index, second in (("NDVI", "red"), ("NDMI", "swir")):
            if index in scaled:
                values = scaled[index]
            elif "nir" in scaled and second in scaled:
                nir, other = scaled["nir"], scaled[second]
                denominator = nir + other
                with np.errstate(divide="ignore", invalid="ignore"):
                    values = np.ma.masked_where(
                        (denominator <= 0) | (nir < 0) | (other < 0) | (nir > 1) | (other > 1),
                        (nir - other) / denominator)
            else:
                continue
            valid = clear & ~np.ma.getmaskarray(values) & np.isfinite(values.data)
            valid &= (values.data >= -1) & (values.data <= 1)
            if not valid.any():
                continue
            numbers, areas = values.data[valid], weights[valid]
            mean = float(np.average(numbers, weights=areas))
            p10, median, p90 = _quantile(numbers, areas, [.1, .5, .9])
            observation = SatelliteObservation(date=image.acquired_at.astimezone(timezone.utc).date(), acquired_at=image.acquired_at,
                available_at=image.available_at, valid_pixel_fraction=min(1, float(areas.sum() / total)),
                mean=mean, median=median, p10=p10, p90=p90,
                std=float(np.sqrt(np.average((numbers - mean) ** 2, weights=areas))))
            series.append(SatelliteSeries(index=index, sensor=image.sensor,
                native_resolution_m=resolution, processing_version=image.processing_version,
                observations=[observation]))
        metadata["indices"] = [s.index for s in series]
        if not series:
            raise ValueError("IMAGE_HAS_NO_VALID_INDEX_PIXELS")
    return series, metadata
