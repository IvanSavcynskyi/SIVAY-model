"""Bounded public-source refresh before PREPARE; historical replay stays offline."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging

from app.adapters.public_indices import PublicIndices
from app.adapters.public_weather import PublicWeather
from app.domain import SourceError, as_of_cutoff

COLLECTION_VERSION = "public-field-data-1.0.0"
logger = logging.getLogger(__name__)


def refresh_automatic(request, geometry, imported, settings, storage, errors):
    if not settings.automatic_sources_enabled or settings.demo or settings.ranking_profile != "personalized":
        return
    now = datetime.now(timezone.utc)
    # A newly retrieved source cannot establish knowledge on a past date.
    if request.as_of_date != now.date():
        return
    saved = imported.get("personalization", {}).get("data", {}) if imported.get("personalization") else {}
    direct = request.personalization
    own_weather = bool((direct and direct.weather) or saved.get("weather") or imported.get("weather") or settings.weather_path)
    indices = set()
    for name in ("NDVI", "NDMI"):
        if imported.get("satellite:"+name):
            indices.add(name)
    indices.update(row["index"] for row in saved.get("indices", []))
    if direct:
        indices.update(s.index for s in direct.indices)
    for doc in imported.get("images", []):
        bands = doc["data"]["bands"]
        if "NDVI" in bands or {"nir", "red"} <= bands.keys():
            indices.add("NDVI")
        if "NDMI" in bands or {"nir", "swir"} <= bands.keys():
            indices.add("NDMI")
    own_indices = indices == {"NDVI", "NDMI"} or bool(settings.satellite_path)
    work = {}
    for kind, own, ttl, provider in (
        ("weather", own_weather, settings.automatic_weather_ttl_seconds, PublicWeather),
        ("indices", own_indices, settings.automatic_indices_ttl_seconds, PublicIndices),
    ):
        if own:
            continue
        key = "auto:"+kind
        cached = imported.get(key)
        matches = bool(cached and cached["data"].get("geom_hash") == geometry.geom_hash
                       and cached["data"].get("collection_version") == COLLECTION_VERSION)
        fresh = matches and (now-datetime.fromisoformat(cached["available_at"].replace("Z", "+00:00"))).total_seconds() < ttl
        if fresh:
            continue
        work[kind] = (provider, cached if matches else None)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = {kind: executor.submit(provider().collect, geometry) for kind, (provider, _) in work.items()}
        for kind, future in pending.items():
            try:
                data = future.result()
            except Exception:
                logger.warning("Automatic %s source unavailable", kind, exc_info=True)
                # Optional network sources never remove usable GAEZ or crop
                # history; failures are cached briefly and surfaced explicitly.
                previous = work[kind][1]
                data = dict(previous["data"]) if previous else {}
                data["error"] = "AUTOMATIC_"+kind.upper()+"_UNAVAILABLE"
            data.update(geom_hash=geometry.geom_hash, collection_version=COLLECTION_VERSION)
            timestamp = datetime.now(timezone.utc)
            doc = {"field_id": request.field.field_id, "available_at": timestamp.isoformat(), "data": data}
            if storage:
                storage.put_source("auto:"+kind, request.field.field_id, doc)
            if timestamp <= as_of_cutoff(request.as_of_date):
                imported["auto:"+kind] = doc
            else:
                errors.append(SourceError(source=kind, code="SOURCE_RETRIEVED_AFTER_CUTOFF",
                                          message="Public source was retrieved after the requested day."))
