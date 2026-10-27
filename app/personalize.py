"""Collect field-specific sources at PREPARE, before sealing the snapshot."""
import json
from datetime import datetime

from app.canonical import digest
from app.domain import (FieldImageInput, FieldPersonalizationInput, FieldSignals,
                        FieldWeatherInput, PersonalizationPolicy, SatelliteSeries,
                        SourceError, as_of_cutoff)
from app.features.imagery import image_indices
from app.features.personalization import build_index_signals, build_local_weather


def validate_personalization(value, rules):
    from app.ranking.personalized import parent
    identifiers = {c.id for c in rules.catalog} | {parent(c.id) for c in rules.catalog}
    selectors = set(value.sowing_dates)
    if value.policy:
        selectors.update(value.policy.min_return_years)
        selectors.update(value.policy.avoid_predecessors)
        selectors.update(crop for crops in value.policy.avoid_predecessors.values() for crop in crops)
        selectors.update(crop for rule in value.policy.rules for crop in rule.crops if crop != "*")
    unknown = selectors - identifiers
    if unknown:
        raise ValueError("UNKNOWN_POLICY_CROP: " + ", ".join(sorted(unknown)))


def prepare_signals(request, geometry, imported, settings, errors):
    saved = (FieldPersonalizationInput.model_validate(imported["personalization"]["data"])
             if imported.get("personalization") else None)
    direct = request.personalization

    def choose(name):
        for value in (direct, saved):
            if value is not None and name in value.model_fields_set:
                return getattr(value, name)
        return None

    policy = choose("policy") or PersonalizationPolicy.model_validate_json(
        settings.personalization_policy_path.read_text())
    signals = FieldSignals(policy=policy)
    signals_context = choose("sowing_dates") or {}
    signals.weather["sowing_dates"] = {k: v.isoformat() for k, v in signals_context.items()}
    entries = {}
    for index in ("NDVI", "NDMI"):
        doc = imported.get(f"satellite:{index}")
        if doc:
            entries[index] = (SatelliteSeries.model_validate(doc["data"]), "indices:" + digest(doc))
    if settings.satellite_path:
        try:
            doc = json.loads(settings.satellite_path.read_text())
            if doc.get("field_id") == request.field.field_id:
                series = SatelliteSeries.model_validate(doc["series"])
                entries[series.index] = (series, "indices-file:" + digest(doc))
        except (OSError, ValueError, KeyError) as exc:
            errors.append(SourceError(source="satellite", code="IMPORT_ERROR", message=str(exc)))
    for value in (saved, direct):
        if value:
            for series in value.indices:
                entries[series.index] = (series, "indices-request:" + digest(series.model_dump(mode="json")))
    all_entries = list(entries.values())
    cutoff = as_of_cutoff(request.as_of_date)
    for doc in imported.get("images", []):
        image = FieldImageInput.model_validate(doc["data"])
        if image.available_at > cutoff or image.acquired_at > cutoff:
            signals.issues.append("IMAGE_TEMPORAL_REJECTED")
            continue
        try:
            series, metadata = image_indices(image, geometry)
            signals.images.append(metadata)
            all_entries.extend((item, "image:" + metadata["sha256"]) for item in series)
        except (OSError, ValueError) as exc:
            signals.issues.append("IMAGE_PROCESSING_FAILED")
            errors.append(SourceError(source="imagery", code="IMAGE_PROCESSING_FAILED", message=str(exc)))
    # Explicit platform indices win over image-derived statistics on the same day.
    all_entries.sort(key=lambda entry: not entry[1].startswith("image:"))
    build_index_signals(all_entries, request.as_of_date, policy, signals)
    data, source = choose("weather"), None
    if data is not None:
        source = "weather-request:" + digest(data.model_dump(mode="json"))
    elif imported.get("weather"):
        doc = imported["weather"]
        data, source = FieldWeatherInput.model_validate(doc["data"]), "weather:" + digest(doc)
    elif settings.weather_path:
        try:
            doc = json.loads(settings.weather_path.read_text())
            data, source = FieldWeatherInput.model_validate(doc), "weather-file:" + digest(doc)
        except (OSError, ValueError, KeyError) as exc:
            errors.append(SourceError(source="weather", code="IMPORT_ERROR", message=str(exc)))
    if data is not None:
        try:
            build_local_weather(data, geometry, request.as_of_date, policy, signals, source)
        except ValueError as exc:
            signals.issues.append(str(exc))
            errors.append(SourceError(source="weather", code="LOCAL_WEATHER_INVALID", message=str(exc)))
    for kind in ("personalization", "weather", "satellite:NDVI", "satellite:NDMI"):
        if imported.get(kind):
            doc = imported[kind]
            signals.source_availability["ingestion:" + digest(doc)] = datetime.fromisoformat(
                doc["available_at"].replace("Z", "+00:00"))
    for doc in imported.get("images", []):
        signals.source_availability["ingestion:" + digest(doc)] = datetime.fromisoformat(
            doc["available_at"].replace("Z", "+00:00"))
    # Preserve planting plans alongside the immutable evidence, independent of source.
    signals.weather["sowing_dates"] = {k: v.isoformat() for k, v in signals_context.items()}
    signals.issues = sorted(set(signals.issues))
    return signals
