"""Application orchestration. The ranker receives sealed features only."""
from app.config import Settings
from app.canonical import digest
from app.domain import RecommendationRequest, RecommendationResult
from app.prepare import Preparer
from app.rules import load_rules
from app.storage.database import Storage


class RecommendationService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.profile = "validated" if settings.demo else settings.ranking_profile
        rule_path = "rules/demo" if settings.demo and str(settings.rules_path) == "rules" else settings.rules_path
        self.rules = load_rules(rule_path, allow_demo=settings.demo)
        if self.rules.demo and not settings.demo:
            raise ValueError("Demo rules require SIVAY_DEMO=true")
        self.storage = Storage(settings.database_url)
        self.storage.initialize()
        self.preparer = Preparer(settings, self.rules, self.storage)

    def recommend(self, request: RecommendationRequest) -> RecommendationResult:
        from app.explain import enrich_result
        snapshot = self.storage.find_snapshot(self.preparer.request_hash(request))
        if snapshot is None:
            snapshot = self.preparer.prepare(request)
            self.storage.put_snapshot(snapshot)
        result = enrich_result(self._rank(request, snapshot))
        self.storage.put_recommendation(request, result)
        return result

    def replay(self, request: RecommendationRequest, snapshot_id: str) -> RecommendationResult:
        from app.explain import enrich_result
        snapshot = self.storage.get_snapshot(snapshot_id)
        if snapshot is None:
            raise LookupError("SNAPSHOT_NOT_FOUND")
        if snapshot.input_hash != digest(request.model_dump(mode="json", by_alias=True, exclude_unset=True)):
            raise ValueError("SNAPSHOT_REQUEST_MISMATCH")
        return enrich_result(self._rank(request, snapshot))

    def _rank(self, request, snapshot):
        if self.profile == "gaez_baseline":
            from app.ranking.baseline import recommend_baseline
            return recommend_baseline(request, snapshot, self.rules)
        from app.ranking import recommend
        return recommend(request, snapshot, self.rules)

    def process_one_job(self) -> bool:
        claimed = self.storage.claim_job(self.settings.job_lease_seconds, self.settings.job_max_attempts)
        if not claimed:
            return False
        job_id, token, request = claimed
        try:
            snapshot = self.storage.find_snapshot(self.preparer.request_hash(request))
            if snapshot is None:
                snapshot = self.preparer.prepare(request)
                self.storage.put_snapshot(snapshot)
            self.storage.finish_job(job_id, token, snapshot_id=snapshot.snapshot_id,
                                    partial=bool(snapshot.source_errors))
        except Exception as exc:
            self.storage.finish_job(job_id, token, error=f"{type(exc).__name__}: {exc}")
        return True
