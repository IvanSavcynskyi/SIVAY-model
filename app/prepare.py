"""I/O boundary: validate, collect as-of features, then seal an immutable snapshot."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from shapely.geometry import shape
from shapely import transform

from app.adapters.gaez_store import GaezStore
from app.canonical import digest
from app.config import Settings
from app.domain import (
    CellValue, FieldFeatureSnapshot, GaezFieldValue, RecommendationRequest, RuleSet,
    CropCost, MarketPrice, SatelliteFeatures, SourceError, WeatherFeatures, as_of_cutoff,
)
from app.features.history import normalize_history
from app.geo import GeometryError, ValidatedGeometry, validate_geometry


def validate_field(request: RecommendationRequest) -> ValidatedGeometry:
    geom = validate_geometry(request.field.geometry, request.field.crs)
    if not .1 <= geom.area_ha <= 20000:
        raise GeometryError("INVALID_FIELD_AREA: supported area is 0.1–20000 ha")
    return geom


def region_for_geometry(geom: ValidatedGeometry, layer_path: Path | None) -> str | None:
    """Require an actual configured boundary layer, never infer oblast from a bbox."""
    if layer_path is None:
        return None
    payload = json.loads(layer_path.read_text(encoding="utf-8"))
    if payload.get("type") != "FeatureCollection":
        raise ValueError("Region layer must be a WGS84 GeoJSON FeatureCollection")
    matches = []
    swapped = transform(geom.geometry, lambda xy: xy[:, ::-1])
    suspected_swap = False
    for feature in payload["features"]:
        boundary = shape(feature["geometry"])
        if not boundary.is_valid:
            raise ValueError("Invalid administrative boundary")
        if boundary.covers(geom.geometry):
            matches.append(feature["properties"]["region"])
        elif boundary.covers(swapped):
            suspected_swap = True
    if not matches and suspected_swap:
        raise GeometryError("SUSPECTED_AXIS_SWAP: expected longitude,latitude in EPSG:4326")
    return matches[0] if len(matches) == 1 else None


class Preparer:
    def __init__(self, settings: Settings, rules: RuleSet, storage=None):
        self.settings, self.rules = settings, rules
        self.storage = storage
        self.gaez = GaezStore(settings.gaez_root, settings.gaez_manifest)

    def request_hash(self, request: RecommendationRequest, imported=None) -> str:
        # Every local source version and input explicitness participates in invalidation.
        paths = [self.settings.regions_path, self.settings.prices_path,
                 self.settings.exchange_rates_path, self.settings.weather_path,
                 self.settings.satellite_path, self.gaez.manifest_path,
                 self.settings.personalization_policy_path]
        sources = {str(p): digest(p.read_text()) if p.exists() else None for p in paths if p}
        if imported is None:
            imported = self._sources(request)
        return digest({"request": request.model_dump(mode="json", by_alias=True, exclude_unset=True),
                       "rules": self.rules.version, "sources": sources, "imported": imported,
                       "demo": self.settings.demo, "prepare_version": "field-signals-1.0.0"})

    def _sources(self, request: RecommendationRequest):
        if self.storage is None:
            return {}
        cutoff = as_of_cutoff(request.as_of_date)
        sources = {kind: self.storage.get_source(kind, request.field.field_id, cutoff)
                   for kind in ("history", "costs", "satellite:NDVI", "satellite:NDMI",
                                "weather", "personalization")}
        sources["images"] = self.storage.get_sources("image:", request.field.field_id, cutoff)
        sources["prices"] = self.storage.get_source("prices", "*", cutoff)
        return sources

    def prepare(self, request: RecommendationRequest) -> FieldFeatureSnapshot:
        self.gaez = GaezStore(self.settings.gaez_root, self.settings.gaez_manifest)
        geom = validate_field(request)
        imported = self._sources(request)
        errors: list[SourceError] = []
        assumptions = []
        if "management" not in request.model_fields_set:
            assumptions += ["input_level=high (assumed)", "irrigation=none (assumed)"]
        else:
            if "input_level" not in request.management.model_fields_set:
                assumptions.append("input_level=high (assumed)")
            if "irrigation" not in request.management.model_fields_set:
                assumptions.append("irrigation=none (assumed)")
        if geom.repaired:
            assumptions.append("geometry_repaired")
        if self.settings.demo:
            region = "UA-53"
            assumptions.append("DEMO: region UA-53 and GAEZ values are synthetic test fixtures")
            gaez = self._demo_gaez(request, geom)
        else:
            try:
                region = region_for_geometry(geom, self.settings.regions_path)
            except GeometryError:
                raise
            except (OSError, ValueError, KeyError) as exc:
                region = None
                errors.append(SourceError(source="region", code="BOUNDARY_LAYER_ERROR", message=str(exc)))
            if region is None:
                errors.append(SourceError(source="region", code="REGION_UNRESOLVED",
                    message="Configure a local administrative boundary layer for the supported region"))
            wsim = {("high", "none"): "HRLM", ("low", "none"): "LRLM",
                    ("high", "irrigated"): "HILM", ("low", "irrigated"): "LILM"}[
                        request.management.input_level, request.management.irrigation]
            crop_codes = sorted({crop.gaez_code for crop in self.rules.catalog if crop.gaez_code})
            gaez = {}
            for code in crop_codes:
                try:
                    gaez.update(self.gaez.values_for_field(geom, "EPSG:4326", request.as_of_date, [code], wsim))
                    if "management" not in request.model_fields_set or "input_level" not in request.management.model_fields_set:
                        alternate = "LRLM" if wsim == "HRLM" else "LILM" if wsim == "HILM" else "HRLM"
                        gaez[code] += self.gaez.values_for_field(geom, "EPSG:4326", request.as_of_date, [code], alternate)[code]
                except (OSError, ValueError) as exc:
                    errors.append(SourceError(source="gaez", code="LAYER_READ_ERROR", message=f"{code}: {exc}"))
                    gaez[code] = []
            if not any(gaez.values()):
                errors.append(SourceError(source="gaez", code="GAEZ_NO_COVERAGE",
                    message="No verified local layers available at as_of_date"))
        history_rows = request.crop_history
        if not history_rows and imported.get("history"):
            history_rows = imported["history"]["data"]
        history = normalize_history(history_rows, request.history_gaps, request.as_of_date)
        prices = [MarketPrice.model_validate(row) for row in imported["prices"]["data"]] if imported.get("prices") else []
        costs = {crop: CropCost.model_validate(cost) for crop, cost in imported["costs"]["data"].items()} if imported.get("costs") else {}
        rates = []
        if self.settings.prices_path or self.settings.exchange_rates_path:
            from app.adapters.prices import load_exchange_rates_csv, load_prices_csv
            for source, filename, loader, dest in [
                ("prices", self.settings.prices_path, load_prices_csv, prices),
                ("exchange_rates", self.settings.exchange_rates_path, load_exchange_rates_csv, rates),
            ]:
                if filename:
                    try:
                        dest.extend(loader(filename, request.as_of_date))
                    except (OSError, ValueError) as exc:
                        errors.append(SourceError(source=source, code="IMPORT_ERROR", message=str(exc)))
        weather = WeatherFeatures(issues=["WEATHER_NOT_CONFIGURED", "UNAPPROVED_WEATHER_MULTIPLIERS_DISABLED"])
        satellite = SatelliteFeatures(issues=["SATELLITE_NOT_CONFIGURED"])
        if imported.get("satellite:NDVI") and not self.settings.satellite_path:
            from app.features.satellite import build_satellite_features
            from app.domain import SatelliteSeries
            satellite = build_satellite_features(
                SatelliteSeries.model_validate(imported["satellite:NDVI"]["data"]), request.as_of_date)
        if self.settings.weather_path:
            from app.adapters.weather import FileWeatherProvider
            from app.features.weather import build_weather_features
            try:
                centroid = geom.geometry.centroid
                records = FileWeatherProvider(self.settings.weather_path).records_for(
                    centroid.y, centroid.x, request.as_of_date)
                weather = build_weather_features(records, request.as_of_date, self.rules.parameters)
            except (OSError, ValueError, KeyError) as exc:
                errors.append(SourceError(source="weather", code="IMPORT_ERROR", message=str(exc)))
        elif self.settings.weather_enabled:
            errors.append(SourceError(source="weather", code="WEATHER_SOURCE_REQUIRED",
                message="Configure SIVAY_WEATHER_PATH with timestamped provider records"))
        if self.settings.satellite_path:
            from app.adapters.satellite import parse_satellite_series
            from app.features.satellite import build_satellite_features
            try:
                payload = json.loads(self.settings.satellite_path.read_text(encoding="utf-8"))
                if payload.get("field_id") != request.field.field_id:
                    raise ValueError("SATELLITE_FIELD_MISMATCH")
                series = parse_satellite_series(payload["series"], request.as_of_date)
                satellite = build_satellite_features(series, request.as_of_date)
            except (OSError, ValueError, KeyError) as exc:
                errors.append(SourceError(source="satellite", code="IMPORT_ERROR", message=str(exc)))
        now = datetime.now(timezone.utc)
        from app.personalize import prepare_signals
        signals = prepare_signals(request, geom, imported, self.settings, errors)
        snap = FieldFeatureSnapshot(snapshot_id="pending", field_id=request.field.field_id,
            geom_hash=geom.geom_hash, as_of_date=request.as_of_date, prepared_at=now, region=region,
            gaez=gaez, history=history, weather=weather, satellite=satellite,
            field_signals=signals, prices=prices,
            exchange_rates=rates, costs_per_ha=costs, source_errors=errors, assumptions=assumptions,
            request_hash=self.request_hash(request, imported),
            input_hash=digest(request.model_dump(mode="json", by_alias=True, exclude_unset=True)),
            demo=self.settings.demo)
        snap.snapshot_id = "snap_" + digest(snap.model_dump(mode="json", exclude={"snapshot_id"}))[:32]
        return snap

    def _demo_gaez(self, request: RecommendationRequest, geom: ValidatedGeometry):
        """Synthetic data is only reachable through explicitly enabled demo mode."""
        codes = sorted({crop.gaez_code for crop in self.rules.catalog if crop.gaez_code})
        result = {}
        cutoff = as_of_cutoff(request.as_of_date)
        # Fixture scores exercise ordering; they make no claim about any real crop or field.
        for index, code in enumerate(codes):
            value = 8200 - index * 350
            for_wsim = []
            for wsim in ("HRLM", "LRLM", "HILM", "LILM"):
                layer = f"DEMO.RES05-SUX.HP0120.AGERA5.HIST.{code}.{wsim}"
                score = value if wsim[0] == "H" else value * .8
                for_wsim.append(GaezFieldValue(layer_code=layer, variable="RES05-SUX",
                    unit="index_0_10000", crop_code=code, wsim=wsim, native_res_deg=1/12,
                    field_area_ha=geom.area_ha, cell_area_ha_mean=6000,
                    field_to_cell_ratio=geom.area_ha / 6000, coverage_valid=1, n_cells=1,
                    mean_w=score, min=score, max=score,
                    per_cell=[CellValue(value=score, area_fraction=1)],
                    source_sha256=digest({"fixture": layer, "value": score}),
                    retrieved_at=cutoff, available_at=cutoff))
            result[code] = for_wsim
        return result
