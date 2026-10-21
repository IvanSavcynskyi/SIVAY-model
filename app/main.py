"""ASGI entrypoint: uvicorn app.main:app."""
from contextlib import asynccontextmanager

from datetime import date

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import read_indices, router
from app.config import Settings
from app.service import RecommendationService


def create_app(settings: Settings | None = None,
               service: RecommendationService | None = None) -> FastAPI:
    configured = settings or (service.settings if service else Settings())
    @asynccontextmanager
    async def lifespan(application):
        application.state.service = service or RecommendationService(configured)
        try:
            yield
        finally:
            application.state.service.storage.close()

    application = FastAPI(title="SIVAY", version="0.1.0", lifespan=lifespan,
                          description="Відтворювані рекомендації культур з журналом розрахунку.")
    application.include_router(router, prefix="/v1")
    if configured.cors_origins:
        application.add_middleware(CORSMiddleware, allow_origins=configured.cors_origins,
                                  allow_methods=["GET", "POST", "PUT", "OPTIONS"],
                                  allow_headers=["Content-Type"])

    @application.get("/api/fields/{field_id}/indices", tags=["platform"])
    def platform_indices(field_id: str, request: Request,
                         index: str = Query(default="NDVI", pattern="^(NDVI|NDMI)$"),
                         from_date: date | None = Query(default=None, alias="from"),
                         to: date | None = None):
        payload = read_indices(field_id, request, index, from_date, to)
        return {"field_id": field_id, "available_at": payload["available_at"],
                **payload["data"]}

    return application


app = create_app()
