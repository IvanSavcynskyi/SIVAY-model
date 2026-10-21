from app.config import Settings
from app.domain import EconomicsResult, RecommendationRequest, ScenarioValues
from app.explain import enrich_result
from app.ranking import recommend
from app.service import RecommendationService


def test_explanations_are_deterministic_and_reference_computed_facts(tmp_path):
    service = RecommendationService(Settings(database_url=f"sqlite:///{tmp_path / 'explain.db'}", demo=True,
                                            ranking_profile="validated"))
    try:
        request = RecommendationRequest.model_validate({
            "field": {"field_id": "f1", "geometry": {"type": "Polygon", "coordinates": [
                [[34.5, 49.6], [34.51, 49.6], [34.51, 49.61], [34.5, 49.61], [34.5, 49.6]]]},
                "crs": "EPSG:4326"}, "as_of_date": "2026-09-01",
            "target_season": {"harvest_year": 2027, "include": ["winter", "spring"]},
        })
        snapshot = service.preparer.prepare(request)
        raw = recommend(request, snapshot, service.rules)
        before = raw.model_dump_json()
        rendered = enrich_result(raw)
        assert rendered.results
        assert raw.model_dump_json() == before
        assert enrich_result(rendered).model_dump_json() == rendered.model_dump_json()
        ids = {event.id for event in rendered.trace}
        for crop in rendered.results:
            for explanation in [*crop.reasons, *crop.limitations]:
                assert explanation.trace_event_id in ids
                assert explanation.text
            assert any("синтетичні" in item.text for item in crop.limitations)
            assert any("комірки" in item.text for item in crop.limitations)
            assert any("не ймовірністю" in item.text or "не є ймовірністю" in item.text for item in crop.limitations)
            assert any(str(crop.stability.scenarios_evaluated) in item.text for item in crop.reasons)
        # Rendering a baseline must not claim that rotation or robustness ran.
        baseline = raw.model_copy(deep=True)
        for event in baseline.trace:
            if event.code == "AGRO_SCORE":
                event.code = "BASELINE_GAEZ"
        for crop in baseline.results:
            crop.stability.label = "unavailable"
        rendered_baseline = enrich_result(baseline)
        for crop in rendered_baseline.results:
            assert any("Базова придатність" in item.text for item in crop.reasons)
            assert not any("після врахування сівозміни" in item.text for item in crop.reasons)
            assert not any("сценаріях перевірки" in item.text for item in crop.reasons)
            assert any("не оцінена" in item.text for item in crop.limitations)

        economic = raw.model_copy(deep=True)
        economic.results[0].economics = EconomicsResult(
            complete=True, applied_to_rank=False,
            margin_uah_ha=ScenarioValues(low=-500, base=1000, high=2000))
        explained = enrich_result(economic).results[0]
        assert any("1,000" in item.text for item in explained.reasons)
        assert not any("враховано для впорядкування" in item.text for item in explained.reasons)
        assert any("лише надані статті витрат" in item.text for item in explained.limitations)
    finally:
        service.storage.close()
