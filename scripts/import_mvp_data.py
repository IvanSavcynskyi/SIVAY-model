#!/usr/bin/env python3
"""Download a small, verified GAEZ baseline and Ukraine ADM0 boundary.

Run from the repository root: .venv/bin/python scripts/import_mvp_data.py
Only official bucket-listed GAEZ objects are selected. Default cap: 200 MiB;
an explicit larger cap is accepted up to the authorized 10 GiB ceiling.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import rasterio
import yaml
from shapely.geometry import Point, shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.adapters.gaez_store import ManifestLayer, file_sha256, inspect_layer  # noqa: E402

BUCKET = "fao-gismgr-gaez-v5-data"
LIST_URL = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o"
BOUNDARY_API = "https://www.geoboundaries.org/api/current/gbOpen/UKR/ADM0/"
DIMENSION_ROOT = f"https://storage.googleapis.com/{BUCKET}/DATA/GAEZ-V5/DIMENSION/"
MAX_CAP = 10 * 1024**3


class Importer:
    def __init__(self, root: Path, budget: int):
        self.root = root
        self.budget = budget
        self.downloaded = 0
        self.client = httpx.Client(timeout=httpx.Timeout(120, connect=30), follow_redirects=True)

    def get(self, url: str, *, params=None) -> httpx.Response:
        for attempt in range(4):
            try:
                result = self.client.get(url, params=params)
                result.raise_for_status()
                self.downloaded += len(result.content)
                if self.downloaded > self.budget:
                    raise ValueError("Download budget exceeded")
                return result
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 3:
                    raise
                time.sleep(min(2**attempt, 8))
        raise RuntimeError("Unreachable retry state")

    def listing(self, variable: str) -> list[dict]:
        prefix = f"DATA/GAEZ-V5/MAPSET/{variable}/GAEZ-V5.{variable}.HP0120.AGERA5.HIST."
        records = []
        params = {"prefix": prefix, "maxResults": 1000}
        for _ in range(100):
            payload = self.get(LIST_URL, params=params).json()
            records.extend(payload.get("items", []))
            if not payload.get("nextPageToken"):
                return records
            params["pageToken"] = payload["nextPageToken"]
        raise ValueError("Unexpectedly large bucket listing")

    def download(self, item: dict, target: Path) -> None:
        expected = int(item["size"])
        expected_md5 = item.get("md5Hash")
        if target.exists() and target.stat().st_size == expected:
            if expected_md5 and md5_b64(target) == expected_md5:
                return
        if self.downloaded + expected > self.budget:
            raise ValueError("Planned downloads exceed configured budget")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + ".partial")
        for attempt in range(4):
            try:
                received = 0
                with self.client.stream("GET", item["mediaLink"]) as response:
                    response.raise_for_status()
                    with partial.open("wb") as output:
                        for chunk in response.iter_bytes():
                            received += len(chunk)
                            self.downloaded += len(chunk)
                            if received > expected or self.downloaded > self.budget:
                                raise ValueError("Response exceeds expected size or download budget")
                            output.write(chunk)
                if received != expected or (expected_md5 and md5_b64(partial) != expected_md5):
                    raise ValueError("Downloaded object failed size/MD5 verification")
                partial.replace(target)
                print(f"downloaded {target.name}: {received:,} bytes", flush=True)
                return
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 3:
                    raise
                time.sleep(min(2**attempt, 8))


def md5_b64(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def import_gaez(importer: Importer, crops: set[str]) -> dict:
    root = importer.root
    old = {}
    if (root / "manifest.json").exists():
        old = {r["layer_code"]: r for r in json.loads((root / "manifest.json").read_text())["layers"]}
    selected = []
    # SCX contains an undocumented class 10 in the real source; omit categorical
    # layers until the complete legend is independently established.
    for variable in ("RES05-SUX",):
        objects = importer.listing(variable)
        for item in objects:
            name = Path(item["name"]).name
            components = name.split(".")
            if len(components) == 8 and components[-1] == "tif" and components[-2] == "HRLM" and components[-3] in crops:
                selected.append((variable, item))
    found = {(var, Path(item["name"]).name.split(".")[-3]) for var, item in selected}
    required = {("RES05-SUX", crop) for crop in crops}
    if found != required:
        raise ValueError(f"Official bucket is missing required layers: {sorted(required - found)}")
    planned = sum(int(item["size"]) for _, item in selected)
    existing = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.exists() else 0
    if planned > importer.budget or existing + planned > MAX_CAP:
        raise ValueError("GAEZ selection exceeds download/storage cap")
    print(f"Selected {len(selected)} official rasters, {planned:,} bytes", flush=True)
    root.mkdir(parents=True, exist_ok=True)
    records = []
    evidence = []
    for variable, item in sorted(selected, key=lambda v: v[1]["name"]):
        filename = Path(item["name"]).name
        target = root / filename
        importer.download(item, target)
        now = datetime.now(timezone.utc).isoformat()
        crop = filename.split(".")[-3]
        code = filename.removesuffix(".tif")
        digest = file_sha256(target)
        previous = old.get(code)
        # GCS creation timestamps do not prove historical public access. Keep a
        # conservative observed-public timestamp unless independently established.
        available = previous["available_at"] if previous and previous["sha256"] == digest else now
        retrieved = previous["retrieved_at"] if previous and previous["sha256"] == digest else now
        with rasterio.open(target) as src:
            layer = ManifestLayer(
                layer_code=code, variable=variable, unit="index_0_10000",
                crop_code=crop, path=filename, sha256=digest, dtype=src.dtypes[0], nodata=src.nodata,
                excluded_values=[-1],
                native_res_deg=abs(src.res[0]), source_url=item["mediaLink"], available_at=available, retrieved_at=retrieved,
            )
        inspect_layer(target, layer)
        records.append(layer.model_dump(mode="json"))
        evidence.append({**item, "local_sha256": digest, "observed_public_at": available})
    for dimension in ("CROP-RES05", "CROP-RES02", "PERIOD", "CLIM", "SSP", "WSIM-RES05"):
        name = f"GAEZ-V5.{dimension}.csv"
        (root / name).write_bytes(importer.get(DIMENSION_ROOT + name).content)
    (root / "gcs-object-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    manifest = {"layers": records}
    temporary = root / "manifest.json.partial"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(root / "manifest.json")
    return {"gaez_layers": len(records), "gaez_raster_bytes": planned}


def import_boundary(importer: Importer, destination: Path) -> dict:
    metadata = importer.get(BOUNDARY_API).json()
    if metadata["boundaryISO"] != "UKR" or metadata["boundaryType"] != "ADM0":
        raise ValueError("Unexpected administrative boundary source")
    url = metadata["gjDownloadURL"]
    response = importer.get(url)
    original = response.json()
    if original["type"] != "FeatureCollection" or len(original["features"]) != 1:
        raise ValueError("Expected exactly one Ukraine national boundary")
    boundary = shape(original["features"][0]["geometry"])
    if not boundary.is_valid:
        raise ValueError("Provider boundary is invalid; do not silently repair national borders")
    # Include the internationally recognized territory, irrespective of control.
    for label, lon, lat in [("Kyiv", 30.5234, 50.4501), ("Simferopol", 34.1024, 44.9521),
                            ("Donetsk", 37.8029, 48.0159), ("Luhansk", 39.3078, 48.5740)]:
        if not boundary.covers(Point(lon, lat)):
            raise ValueError(f"Ukraine boundary unexpectedly excludes {label}")
    payload = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": original["features"][0]["geometry"],
        "properties": {"region": "UA", "name": "Ukraine", "source": "geoBoundaries gbOpen",
                       "boundary_id": metadata["boundaryID"], "license": metadata["boundaryLicense"],
                       "source_url": url},
    }]}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False) + "\n")
    provenance = {"api_url": BOUNDARY_API, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                  "metadata": metadata, "source_sha256": hashlib.sha256(response.content).hexdigest(),
                  "local_sha256": file_sha256(destination), "control_points_checked": ["Kyiv", "Simferopol", "Donetsk", "Luhansk"]}
    destination.with_suffix(".source.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return {"boundary_file": str(destination), "boundary_license": metadata["boundaryLicense"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/gaez"))
    parser.add_argument("--catalog", type=Path, default=Path("rules/catalog.yaml"))
    parser.add_argument("--boundary", type=Path, default=Path("data/regions.geojson"))
    parser.add_argument("--budget-mb", type=int, default=200)
    args = parser.parse_args()
    budget = args.budget_mb * 1024**2
    if not 0 < budget <= MAX_CAP:
        parser.error("--budget-mb must be within the authorized 1..10240 MiB range")
    catalog = yaml.safe_load(args.catalog.read_text())
    if isinstance(catalog, dict):
        catalog = catalog.get("crops", catalog.get("catalog", []))
    crops = {entry["gaez_code"] for entry in catalog if entry.get("gaez_code")}
    if not crops:
        raise ValueError("Catalog contains no GAEZ crop codes")
    importer = Importer(args.root, budget)
    try:
        report = {**import_gaez(importer, crops), **import_boundary(importer, args.boundary)}
        report["downloaded_bytes"] = importer.downloaded
        print(json.dumps(report, indent=2))
    finally:
        importer.client.close()


if __name__ == "__main__":
    main()
