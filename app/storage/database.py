"""Portable local storage; PostgreSQL uses JSONB and row-level SKIP LOCKED."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Integer, String, create_engine, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.domain import FieldFeatureSnapshot, JobResult, RecommendationRequest, RecommendationResult
from app.canonical import digest

Payload = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    pass


class SnapshotRow(Base):
    __tablename__ = "field_feature_snapshot"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    field_id: Mapped[str] = mapped_column(String, index=True)
    request_hash: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(Payload)


class RecommendationRow(Base):
    __tablename__ = "recommendation"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    request: Mapped[dict] = mapped_column(Payload)
    result: Mapped[dict] = mapped_column(Payload)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, index=True)
    request: Mapped[dict] = mapped_column(Payload)
    snapshot_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceDocumentRow(Base):
    __tablename__ = "source_document"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    field_id: Mapped[str] = mapped_column(String, index=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict] = mapped_column(Payload)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Storage:
    def __init__(self, database_url: str):
        kwargs: dict[str, Any] = {}
        if database_url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if database_url in ("sqlite://", "sqlite:///:memory:"):
                from sqlalchemy.pool import StaticPool
                kwargs["poolclass"] = StaticPool
        self.engine = create_engine(database_url, **kwargs)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    def put_snapshot(self, snap: FieldFeatureSnapshot) -> None:
        payload = snap.model_dump(mode="json")
        with Session(self.engine) as session, session.begin():
            existing = session.get(SnapshotRow, snap.snapshot_id)
            if existing:
                if existing.payload != payload:
                    raise ValueError("Snapshot is immutable")
                return
            session.add(SnapshotRow(id=snap.snapshot_id, field_id=snap.field_id,
                                    request_hash=snap.request_hash, payload=payload))

    def get_snapshot(self, snapshot_id: str) -> FieldFeatureSnapshot | None:
        with Session(self.engine) as session:
            row = session.get(SnapshotRow, snapshot_id)
            return FieldFeatureSnapshot.model_validate(row.payload) if row else None

    def find_snapshot(self, request_hash: str) -> FieldFeatureSnapshot | None:
        with Session(self.engine) as session:
            row = session.scalars(select(SnapshotRow).where(
                SnapshotRow.request_hash == request_hash).order_by(SnapshotRow.id).limit(1)).first()
            return FieldFeatureSnapshot.model_validate(row.payload) if row else None

    def put_recommendation(self, request: RecommendationRequest, result: RecommendationResult) -> None:
        payload = result.model_dump(mode="json")
        with Session(self.engine) as session, session.begin():
            existing = session.get(RecommendationRow, result.recommendation_id)
            if existing:
                if existing.result != payload:
                    raise ValueError("Recommendation is immutable")
                return
            session.add(RecommendationRow(id=result.recommendation_id,
                request=request.model_dump(mode="json", by_alias=True, exclude_unset=True), result=payload, created_at=result.computed_at))

    def get_recommendation(self, recommendation_id: str) -> RecommendationResult | None:
        with Session(self.engine) as session:
            row = session.get(RecommendationRow, recommendation_id)
            return RecommendationResult.model_validate(row.result) if row else None

    def put_source(self, kind: str, field_id: str, payload: dict) -> None:
        timestamp = payload.get("available_at")
        available_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if isinstance(timestamp, str) else timestamp
        if not isinstance(available_at, datetime) or available_at.tzinfo is None:
            raise ValueError("Source ingestion requires a timezone-aware available_at")
        identifier = digest({"kind": kind, "field_id": field_id, "payload": payload})
        with Session(self.engine) as session, session.begin():
            if session.get(SourceDocumentRow, identifier) is None:
                session.add(SourceDocumentRow(id=identifier, kind=kind, field_id=field_id,
                    available_at=available_at, payload=payload))

    def get_source(self, kind: str, field_id: str, as_of: datetime | None = None) -> dict | None:
        with Session(self.engine) as session:
            query = select(SourceDocumentRow).where(SourceDocumentRow.kind == kind,
                                                    SourceDocumentRow.field_id == field_id)
            if as_of is not None:
                query = query.where(SourceDocumentRow.available_at <= as_of)
            row = session.scalars(query.order_by(SourceDocumentRow.available_at.desc(),
                                               SourceDocumentRow.id.desc()).limit(1)).first()
            return row.payload if row else None

    def get_sources(self, prefix: str, field_id: str, as_of: datetime) -> list[dict]:
        """Latest visible version of each named source (e.g. one per image id)."""
        with Session(self.engine) as session:
            query = select(SourceDocumentRow).where(
                SourceDocumentRow.kind.startswith(prefix, autoescape=True),
                SourceDocumentRow.field_id == field_id,
                SourceDocumentRow.available_at <= as_of).order_by(
                    SourceDocumentRow.available_at.desc(), SourceDocumentRow.id.desc())
            latest = {}
            for row in session.scalars(query):
                latest.setdefault(row.kind, row.payload)
            return [latest[kind] for kind in sorted(latest)]

    def create_job(self, request: RecommendationRequest) -> JobResult:
        now = utcnow()
        row = JobRow(id="job_" + uuid4().hex, status="queued", request=request.model_dump(mode="json", by_alias=True, exclude_unset=True),
                     created_at=now, updated_at=now, attempts=0)
        with Session(self.engine) as session, session.begin():
            session.add(row)
            session.flush()
            return self._job(row)

    @staticmethod
    def _job(row: JobRow) -> JobResult:
        return JobResult(job_id=row.id, status=row.status, snapshot_id=row.snapshot_id,
                         error=row.error, created_at=aware(row.created_at), updated_at=aware(row.updated_at))

    def get_job(self, job_id: str) -> JobResult | None:
        with Session(self.engine) as session:
            row = session.get(JobRow, job_id)
            return self._job(row) if row else None

    def claim_job(self, lease_seconds: int = 300, max_attempts: int = 3) -> tuple[str, str, RecommendationRequest] | None:
        now = utcnow()
        with Session(self.engine) as session, session.begin():
            expired = (JobRow.status == "running") & (JobRow.lease_until < now)
            session.execute(update(JobRow).where(expired, JobRow.attempts >= max_attempts).values(
                status="failed", error="WORKER_LEASE_EXHAUSTED", updated_at=now,
                lease_token=None, lease_until=None))
            eligible = (JobRow.status == "queued") | expired
            query = select(JobRow).where(eligible, JobRow.attempts < max_attempts).order_by(JobRow.created_at).limit(1)
            if self.engine.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            row = session.scalars(query).first()
            if row is None:
                return None
            token = uuid4().hex
            # Compare-and-swap also prevents double claim by local SQLite workers.
            changed = session.execute(update(JobRow).execution_options(synchronize_session=False).where(JobRow.id == row.id, eligible,
                JobRow.attempts == row.attempts).values(status="running", lease_token=token,
                lease_until=now + timedelta(seconds=lease_seconds), attempts=row.attempts + 1,
                updated_at=now))
            if changed.rowcount != 1:
                return None
            return row.id, token, RecommendationRequest.model_validate(row.request)

    def finish_job(self, job_id: str, token: str, *, snapshot_id: str | None = None,
                   partial: bool = False, error: str | None = None) -> bool:
        with Session(self.engine) as session, session.begin():
            changed = session.execute(update(JobRow).where(JobRow.id == job_id,
                JobRow.status == "running", JobRow.lease_token == token).values(
                    status="failed" if error else ("partial" if partial else "completed"),
                    snapshot_id=snapshot_id, error=error, lease_token=None, lease_until=None,
                    updated_at=utcnow()))
            return changed.rowcount == 1
