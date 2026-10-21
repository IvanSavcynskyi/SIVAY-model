"""Verified local GAEZ rasters. PREPARE reads files; RANK never calls this adapter.

No URL is guessed and no dataset is downloaded by this module. A manifest records
the source URL, verified availability, local checksum, and native raster metadata.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import numpy as np
import rasterio
from pydantic import AwareDatetime, Field, model_validator

from app.domain.models import CellValue, GaezFieldValue, Model, as_of_cutoff
from app.geo import ValidatedGeometry, extract_raster, validate_geometry


class LayerNotAvailable(LookupError):
    pass


class ManifestLayer(Model):
    layer_code: str = Field(min_length=1)
    variable: str
    unit: str = Field(min_length=1)
    crop_code: str
    wsim: str = "HRLM"
    period: str = "HP0120"
    clim: str = "AGERA5"
    ssp: str = "HIST"
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dtype: str
    nodata: float
    excluded_values: list[float] = Field(default_factory=list)
    native_res_deg: float = Field(gt=0)
    source_url: str = Field(pattern=r"^https://")
    available_at: AwareDatetime
    retrieved_at: AwareDatetime

    @model_validator(mode="after")
    def verified(self):
        # Prevent confusing an observation period with its publication time.
        if self.available_at > self.retrieved_at:
            raise ValueError("available_at cannot be later than retrieved_at")
        return self


def safe_local_path(root: Path, name: str) -> Path:
    candidate = Path(name)
    if candidate.is_absolute() or ".." in candidate.parts or ":" in name:
        raise ValueError("INVALID_LOCAL_PATH: expected a relative path within the GAEZ root")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("INVALID_LOCAL_PATH: symlink escapes the GAEZ root")
    return resolved


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _base_variable(variable: str) -> str:
    return variable.removeprefix("RES05-").removesuffix("30AS")


def inspect_layer(path: Path, layer: ManifestLayer, *, verify_values: bool = True) -> None:
    if file_sha256(path) != layer.sha256:
        raise ValueError(f"CHECKSUM_MISMATCH: {layer.layer_code}")
    base = _base_variable(layer.variable)
    categorical = base in {"SCX", "SIX"}
    known = {"SUX": ("int16", -9), "SXX": ("int16", -9), "SCX": ("uint8", 0), "SIX": ("uint8", 0)}
    if base in known and (layer.dtype, layer.nodata) != known[base]:
        raise ValueError(f"INVALID_GAEZ_METADATA: {base} expects {known[base]}")
    with rasterio.open(path) as src:
        if src.crs is None or src.crs.to_epsg() != 4326 or src.count != 1:
            raise ValueError("INVALID_RASTER: expected one band in EPSG:4326")
        if src.dtypes[0] != layer.dtype or src.nodata != layer.nodata:
            raise ValueError("INVALID_RASTER_METADATA: dtype or NoData differs from manifest")
        if src.transform.b or src.transform.d or src.transform.a <= 0 or src.transform.e >= 0:
            raise ValueError("INVALID_RASTER: expected a north-up grid")
        if not np.allclose(src.res, [layer.native_res_deg] * 2, rtol=0, atol=1e-9):
            raise ValueError("INVALID_RASTER_METADATA: resolution differs from manifest")
        if src.scales != (1.0,) or src.offsets != (0.0,):
            raise ValueError("INVALID_RASTER_METADATA: unexpected scale or offset")
        if verify_values:
            for _, window in src.block_windows(1):
                data = src.read(1, window=window, masked=True).compressed()
                if layer.excluded_values:
                    data = data[~np.isin(data, layer.excluded_values)]
                if not np.isfinite(data).all():
                    raise ValueError("INVALID_RASTER_VALUE: nonfinite unmasked value")
                if categorical and ((data < 1).any() or (data > 9).any()):
                    raise ValueError("INVALID_RASTER_VALUE: suitability classes must be 1..9")
                if base in {"SUX", "SXX", "SX1", "SX2", "SX3"} and ((data < 0).any() or (data > 10000).any()):
                    raise ValueError("INVALID_RASTER_VALUE: index must be 0..10000")


class GaezStore:
    def __init__(self, root: str | Path, manifest_path: str | Path | None = None):
        self.root = Path(root).resolve()
        self.manifest_path = Path(manifest_path) if manifest_path else self.root / "manifest.json"
        self.layers: dict[str, ManifestLayer] = {}
        self._cache: dict[tuple, object] = {}
        self._verified: dict[str, tuple] = {}
        if self.manifest_path.exists():
            payload = json.loads(self.manifest_path.read_text())
            records = payload["layers"] if isinstance(payload, dict) else payload
            for record in records:
                layer = ManifestLayer.model_validate(record)
                if layer.layer_code in self.layers:
                    raise ValueError(f"DUPLICATE_LAYER: {layer.layer_code}")
                safe_local_path(self.root, layer.path)
                self.layers[layer.layer_code] = layer

    def available_layers(self, as_of: date | datetime) -> list[ManifestLayer]:
        cutoff = as_of_cutoff(as_of) if not isinstance(as_of, datetime) else as_of
        if cutoff.tzinfo is None:
            raise ValueError("as_of datetime requires a timezone")
        return [layer for _, layer in sorted(self.layers.items()) if layer.available_at <= cutoff]

    def extract(self, layer_code: str, geometry, crs: str, as_of: date | datetime) -> GaezFieldValue:
        layer = next((v for v in self.available_layers(as_of) if v.layer_code == layer_code), None)
        if layer is None:
            raise LayerNotAvailable(f"LAYER_NOT_AVAILABLE: {layer_code} at requested as_of")
        geom = geometry if isinstance(geometry, ValidatedGeometry) else validate_geometry(geometry, crs)
        path = safe_local_path(self.root, layer.path)
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if self._verified.get(layer_code) != signature:
            inspect_layer(path, layer)
            self._verified[layer_code] = signature
        key = (layer.sha256, geom.geom_hash, signature, tuple(layer.excluded_values), _base_variable(layer.variable) in {"SCX", "SIX"})
        if key not in self._cache:
            self._cache[key] = extract_raster(path, geom, categorical=_base_variable(layer.variable) in {"SCX", "SIX"}, excluded_values=layer.excluded_values)
        result = self._cache[key]
        values = asdict(result)
        cells = [CellValue(value=c.value, area_fraction=c.area_fraction, row=c.row, col=c.col) for c in result.per_cell]
        values.pop("per_cell")
        # Domain additions remain optional; extraction retains all class statistics.
        values = {k: v for k, v in values.items() if k in GaezFieldValue.model_fields}
        return GaezFieldValue(
            **values, per_cell=cells,
            layer_code=layer.layer_code, variable=layer.variable, unit=layer.unit,
            crop_code=layer.crop_code, wsim=layer.wsim, period=layer.period, clim=layer.clim,
            ssp=layer.ssp, source_sha256=layer.sha256, source_url=layer.source_url,
            available_at=layer.available_at, retrieved_at=layer.retrieved_at,
        )

    def values_for_field(
        self, geometry, crs: str, as_of: date | datetime, crop_codes: list[str], wsim: str = "HRLM"
    ) -> dict[str, list[GaezFieldValue]]:
        geom = geometry if isinstance(geometry, ValidatedGeometry) else validate_geometry(geometry, crs)
        result = {crop: [] for crop in sorted(set(crop_codes))}
        for layer in self.available_layers(as_of):
            if layer.crop_code in result and layer.wsim == wsim:
                result[layer.crop_code].append(self.extract(layer.layer_code, geom, crs, as_of))
        return result

    def verify(self) -> int:
        for layer in self.layers.values():
            inspect_layer(safe_local_path(self.root, layer.path), layer)
        return len(self.layers)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify", "register"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--record", help="Operator-verified JSON metadata; checksum/dtype/NoData/resolution are read from disk")
    args = parser.parse_args()
    store = GaezStore(args.root, args.manifest)
    if args.command == "register":
        if not args.record:
            parser.error("register requires --record")
        record = json.loads(Path(args.record).read_text())
        path = safe_local_path(store.root, record["path"])
        with rasterio.open(path) as src:
            record.update(sha256=file_sha256(path), dtype=src.dtypes[0], nodata=src.nodata, native_res_deg=abs(src.res[0]))
        layer = ManifestLayer.model_validate(record)
        inspect_layer(path, layer)
        if layer.layer_code in store.layers:
            raise ValueError("Layer already registered; update manifest explicitly")
        store.layers[layer.layer_code] = layer
        store.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        store.manifest_path.write_text(json.dumps({"layers": [v.model_dump(mode="json") for _, v in sorted(store.layers.items())]}, indent=2) + "\n")
    print(json.dumps({"verified_layers": store.verify()}))


if __name__ == "__main__":
    main()
