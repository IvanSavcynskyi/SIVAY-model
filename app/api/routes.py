"""Thin routes; validation and I/O remain at the preparation boundary."""
import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.adapters.simple_prices import SimplePriceImport, parse_simple_prices
from app.domain import (CropCost, CropHistoryRecord, JobResult, RecommendationRequest,
                        RecommendationResult, SatelliteSeries)
from app.geo import GeometryError
from app.prepare import validate_field

router = APIRouter()
logger = logging.getLogger(__name__)


def _service(request: Request):
    return request.app.state.service


def _validate(payload):
    try:
        validate_field(payload)
    except GeometryError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_FIELD", "message": str(exc)}) from exc


def _unavailable(exc):
    logger.error("Service operation failed", exc_info=exc)
    return HTTPException(status_code=503, detail={"code": "SERVICE_UNAVAILABLE",
                        "message": "Сервіс тимчасово недоступний; повторіть запит пізніше."})


@router.post("/recommendations", response_model=RecommendationResult)
def recommend(payload: RecommendationRequest, request: Request,
              snapshot_id: str | None = Query(default=None, min_length=1)):
    """Use prepared features when available; otherwise prepare synchronously.

    Pass snapshot_id to replay an immutable snapshot without collecting sources.
    """
    _validate(payload)
    service = _service(request)
    try:
        return service.replay(payload, snapshot_id) if snapshot_id else service.recommend(payload)
    except GeometryError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_FIELD", "message": str(exc)}) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "SNAPSHOT_NOT_FOUND"}) from exc
    except ValueError as exc:
        if str(exc) == "SNAPSHOT_REQUEST_MISMATCH":
            raise HTTPException(status_code=422, detail={"code": "SNAPSHOT_REQUEST_MISMATCH"}) from exc
        raise _unavailable(exc) from exc
    except (SQLAlchemyError, OSError, RuntimeError) as exc:
        raise _unavailable(exc) from exc


@router.get("/recommendations/{recommendation_id}", response_model=RecommendationResult)
def saved_recommendation(recommendation_id: str, request: Request):
    try:
        result = _service(request).storage.get_recommendation(recommendation_id)
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from exc
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "RECOMMENDATION_NOT_FOUND"})
    return result


@router.post("/fields/{field_id}/prepare", status_code=202, response_model=JobResult)
def prepare(field_id: str, payload: RecommendationRequest, request: Request, response: Response):
    if field_id != payload.field.field_id:
        raise HTTPException(status_code=422, detail={"code": "FIELD_ID_MISMATCH",
                            "message": "Ідентифікатор поля в URL і тілі запиту має збігатися."})
    _validate(payload)
    try:
        job = _service(request).storage.create_job(payload)
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from exc
    response.headers["Location"] = f"/v1/jobs/{job.job_id}"
    return job


@router.get("/jobs/{job_id}", response_model=JobResult)
def job_status(job_id: str, request: Request):
    try:
        job = _service(request).storage.get_job(job_id)
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from exc
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND"})
    if job.error:
        job = job.model_copy(update={"error": "PREPARE_FAILED"})
    return job


@router.get("/catalog/crops")
def catalog(request: Request, region: str = Query(min_length=1)):
    rules = _service(request).rules
    return {"region": region, "rules_version": rules.version, "demo": rules.demo,
            "crops": [crop.model_dump(mode="json") for crop in rules.catalog if region in crop.regions]}


@router.get("/rules/versions")
def versions(request: Request):
    rules = _service(request).rules
    return {"active_version": rules.version, "versions": [{"version": rules.version,
            "parameters_status": rules.parameters.status, "demo": rules.demo}]}


@router.get("/health")
def health(request: Request, response: Response):
    service = _service(request)
    database = "ok"
    try:
        with service.storage.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        database = "unavailable"
        response.status_code = 503
    rules = service.rules
    validated_issues = []
    if not any(crop.status == "approved" for crop in rules.catalog):
        validated_issues.append("NO_APPROVED_CATALOG")
    if not any(window.status == "approved" for window in rules.sowing_windows):
        validated_issues.append("NO_APPROVED_SOWING_WINDOWS")
    if rules.parameters.status != "approved":
        validated_issues.append("UNAPPROVED_PARAMETERS")
    configured = service.settings
    issues = ["DEMO_MODE"] if rules.demo else []
    if configured.regions_path is None:
        issues.append("REGION_LAYER_NOT_CONFIGURED")
    elif not configured.regions_path.is_file():
        issues.append("REGION_LAYER_UNAVAILABLE")
    if not service.preparer.gaez.layers:
        issues.append("NO_LOCAL_GAEZ_LAYERS")
    elif not any(layer.available_at <= datetime.now(timezone.utc) and
                 (service.preparer.gaez.root / layer.path).is_file()
                 for layer in service.preparer.gaez.layers.values()):
        issues.append("NO_AVAILABLE_LOCAL_GAEZ_LAYERS")
    validated_ready = database == "ok" and not issues and not validated_issues
    if service.profile == "validated":
        issues += validated_issues
    return {"status": "unavailable" if database != "ok" else "degraded" if issues else "ok",
            "database": database, "demo": rules.demo, "rules_version": rules.version,
            "ranking_profile": service.profile,
            "recommendation_ready": database == "ok" and not issues, "readiness_issues": issues,
            "validated_rules_ready": validated_ready, "validated_rules_issues": validated_issues}


def _get_source(request, kind, field_id):
    try:
        source = _service(request).storage.get_source(kind, field_id)
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from exc
    if source is None:
        raise HTTPException(status_code=404, detail={"code": "FIELD_SOURCE_NOT_FOUND"})
    return source


def _put_source(request, kind, field_id, data):
    payload = {"field_id": field_id, "available_at": datetime.now(timezone.utc).isoformat(),
               "data": data}
    try:
        _service(request).storage.put_source(kind, field_id, payload)
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from exc
    return payload


@router.put("/fields/{field_id}/costs")
def put_costs(field_id: str, payload: dict[str, CropCost], request: Request):
    """Store user-provided UAH costs per hectare; no default crop costs are invented."""
    return _put_source(request, "costs", field_id,
                       {crop: cost.model_dump(mode="json") for crop, cost in payload.items()})


@router.get("/fields/{field_id}/costs")
def get_costs(field_id: str, request: Request):
    return _get_source(request, "costs", field_id)


@router.put("/fields/{field_id}/history")
def put_history(field_id: str, payload: list[CropHistoryRecord], request: Request):
    """Store declared crop history for later recommendation requests."""
    return _put_source(request, "history", field_id,
                       [record.model_dump(mode="json", by_alias=True) for record in payload])


@router.get("/fields/{field_id}/history")
def get_history(field_id: str, request: Request):
    return _get_source(request, "history", field_id)


@router.post("/fields/{field_id}/indices", status_code=201)
def put_indices(field_id: str, payload: SatelliteSeries, request: Request):
    """Ingest computed index statistics; this route does not process satellite pixels."""
    return _put_source(request, f"satellite:{payload.index}", field_id,
                       payload.model_dump(mode="json"))


def read_indices(field_id: str, request: Request, index: str,
                 from_date: date | None, to: date | None):
    if from_date and to and from_date > to:
        raise HTTPException(status_code=422, detail={"code": "INVALID_DATE_RANGE"})
    payload = _get_source(request, f"satellite:{index}", field_id)
    data = dict(payload["data"])
    data["observations"] = [observation for observation in data["observations"]
                            if (not from_date or date.fromisoformat(observation["date"]) >= from_date)
                            and (not to or date.fromisoformat(observation["date"]) <= to)]
    return {**payload, "data": data}


@router.get("/fields/{field_id}/indices")
def get_indices(field_id: str, request: Request,
                index: str = Query(default="NDVI", pattern="^(NDVI|NDMI)$"),
                from_date: date | None = Query(default=None, alias="from"),
                to: date | None = None):
    return read_indices(field_id, request, index, from_date, to)


@router.post("/prices/import", status_code=201)
def import_prices(payload: SimplePriceImport, request: Request):
    """Import crop/price CSV with explicit date, unit, currency and quote basis."""
    if payload.published_at > datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail={"code": "FUTURE_PRICE_PUBLICATION",
                            "message": "Час публікації ціни не може бути в майбутньому."})
    try:
        prices = parse_simple_prices(payload, _service(request).rules)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_PRICE_IMPORT",
                            "message": str(exc)}) from exc
    result = _put_source(request, "prices", "*", [price.model_dump(mode="json") for price in prices])
    return {**result, "rows_imported": len(prices)}


@router.get("/prices")
def get_prices(request: Request):
    return _get_source(request, "prices", "*")
