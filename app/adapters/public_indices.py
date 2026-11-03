"""Public Sentinel-2 STAC/COG field windows; never download an entire tile."""
import base64
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import math
import time
from urllib.parse import urlsplit

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window, from_bounds, transform as window_transform
from pyproj import Transformer
from shapely import transform
from shapely.geometry import shape

from app.adapters.public_weather import fetch_json
from app.domain import FieldImageInput
from app.features.imagery import image_indices, MAX_WINDOW_PIXELS

ASSET_HOSTS = {"sentinel-cogs.s3.us-west-2.amazonaws.com",
               "e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com"}


class PublicIndices:
    def __init__(self, transport=None, opener=rasterio.open, clock=lambda: datetime.now(timezone.utc)):
        self.transport, self.opener, self.clock = transport, opener, clock

    def collect(self, geometry):
        now = self.clock().astimezone(timezone.utc)
        document = fetch_json("POST", "https://earth-search.aws.element84.com/v1/search", transport=self.transport,
            payload={"collections": ["sentinel-2-c1-l2a"], "intersects": geometry.geojson,
                "datetime": f"{(now-timedelta(days=30)).isoformat()}/{now.isoformat()}",
                "limit": 30, "sortby": [{"field": "properties.datetime", "direction": "desc"}]})
        by_day, rejected, last_error = {}, [], None
        features = document.get("features")
        if not isinstance(features, list):
            raise ValueError("INVALID_STAC_RESPONSE")
        dates = {}
        for item in features[:30]:
            dates.setdefault(str(item.get("properties", {}).get("datetime", ""))[:10], []).append(item)
        # Try one scene from each date before alternate overlapping tiles. A
        # cloudy newest pass must not exhaust the window budget before history.
        features = [items[position] for position in range(max((len(v) for v in dates.values()), default=0))
                    for items in dates.values() if position < len(items)]
        started, processed = time.monotonic(), 0
        for item in features[:30]:
            if sum(value[0] >= .8 for value in by_day.values()) >= 3 or processed >= 8 or time.monotonic()-started > 60:
                break
            try:
                acquired = datetime.fromisoformat(item["properties"]["datetime"].replace("Z", "+00:00"))
                if not now-timedelta(days=30) <= acquired <= now:
                    continue
                day = acquired.date()
                if day in by_day and by_day[day][0] >= .8:
                    continue
                if not shape(item["geometry"]).intersects(geometry.geometry):
                    continue
                processed += 1
                image = self._window_image(item, geometry, acquired)
                entries, metadata = image_indices(image, geometry)
                metadata.update(stac_item_id=item["id"], collection="sentinel-2-c1-l2a",
                    resampling="average_reflectance_nearest_quality_on_native_SWIR_grid",
                    source="Earth Search / Sentinel-2 L2A")
                coverage = min(s.observations[0].valid_pixel_fraction for s in entries)
                if day not in by_day or coverage > by_day[day][0]:
                    by_day[day] = (coverage, metadata, entries)
            except (KeyError, ValueError, OSError, rasterio.errors.RasterioError) as exc:
                rejected.append(item.get("id", "unknown"))
                last_error = str(exc)
        if not by_day:
            raise ValueError("NO_USABLE_SENTINEL_SCENES" + (": " + last_error if last_error else ""))
        # Distinct acquisition dates are needed for a trend. Overlapping tiles
        # from one pass never consume all slots or masquerade as vegetation change.
        selected = sorted(by_day.items(), key=lambda entry: (entry[1][0] >= .8, entry[0]), reverse=True)[:3]
        scenes = [value[1] for _, value in selected]
        series = [s.model_dump(mode="json") for _, value in selected for s in value[2]]
        return {"indices": series, "images": scenes, "rejected_scenes": rejected,
                "retrieved_at": self.clock().astimezone(timezone.utc).isoformat()}

    def _window_image(self, item, geometry, acquired):
        assets = item["assets"]
        names = ("red", "nir", "swir16", "scl")
        for name in names:
            parts = urlsplit(assets[name]["href"])
            if parts.scheme != "https" or parts.hostname not in ASSET_HOSTS or parts.username or parts.password or parts.query:
                raise ValueError("UNTRUSTED_SENTINEL_ASSET")
        # Disable directory listings and retries to bound remote access to COG
        # windows. QA uses nearest-neighbor; reflectance is averaged onto SWIR's
        # native 20m grid, preserving NDMI's actual spatial resolution.
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_TIMEOUT="15",
                          GDAL_HTTP_CONNECTTIMEOUT="10", GDAL_HTTP_MAX_RETRY="1",
                          CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif"), ExitStack() as stack:
            reference = stack.enter_context(self.opener(assets["swir16"]["href"]))
            if reference.crs is None or reference.transform.b or reference.transform.d:
                raise ValueError("INVALID_SENTINEL_GRID")
            projector = Transformer.from_crs(4326, reference.crs, always_xy=True)
            geom = transform(geometry.geometry, lambda xy: np.column_stack(projector.transform(xy[:, 0], xy[:, 1])))
            from shapely.geometry import box
            intersection = geom.intersection(box(*reference.bounds))
            if intersection.is_empty or intersection.area <= 0:
                raise ValueError("SENTINEL_WINDOW_OUTSIDE_TILE")
            win = from_bounds(*intersection.bounds, transform=reference.transform)
            col0, row0 = max(0, math.floor(win.col_off)), max(0, math.floor(win.row_off))
            col1, row1 = min(reference.width, math.ceil(win.col_off+win.width)), min(reference.height, math.ceil(win.row_off+win.height))
            width, height = col1-col0, row1-row0
            if width*height > MAX_WINDOW_PIXELS or width < 1 or height < 1:
                raise ValueError("SENTINEL_FIELD_WINDOW_TOO_LARGE")
            target = window_transform(Window(col0, row0, width, height), reference.transform)
            bands = []
            for name in names:
                src = reference if name == "swir16" else stack.enter_context(self.opener(assets[name]["href"]))
                with WarpedVRT(src, crs=reference.crs, transform=target, width=width, height=height,
                    src_nodata=src.nodata if src.nodata is not None else 0, nodata=-9999,
                    dtype="float32", resampling=Resampling.nearest if name == "scl" else Resampling.average) as vrt:
                    values = vrt.read(1, masked=True).astype("float32")
                if name != "scl":
                    radiometry = assets[name].get("raster:bands", [{}])[0]
                    if "scale" not in radiometry:
                        raise ValueError("SENTINEL_RADIOMETRY_UNAVAILABLE")
                    values = values*float(radiometry["scale"])+float(radiometry.get("offset", 0))
                bands.append(values.filled(-9999))
            with MemoryFile() as memory:
                with memory.open(driver="GTiff", width=width, height=height, count=4, dtype="float32",
                    crs=reference.crs, transform=target, nodata=-9999, compress="deflate") as dst:
                    dst.write(np.stack(bands))
                data = base64.b64encode(memory.read()).decode()
        return FieldImageInput(image_id=item["id"], data_base64=data, acquired_at=acquired,
            available_at=self.clock().astimezone(timezone.utc), sensor="Sentinel-2 L2A C1",
            processing_version="public-s2-common-swir-grid-v1", bands={"red": 1, "nir": 2, "swir": 3, "quality": 4},
            scale=1, clear_values=[4, 5])
