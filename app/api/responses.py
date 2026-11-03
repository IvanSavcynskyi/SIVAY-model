"""Small public responses; the complete calculation stays in storage."""
from datetime import date
from typing import Literal

from pydantic import Field

from app.domain import Model, RecommendationResult


class CropEconomicsResponse(Model):
    price_uah_t: float | None
    price_date: date | None
    yield_t_field: float | None
    revenue_uah_field: float | None
    costs_uah_field: float | None
    margin_uah_field: float | None


class CropResponse(Model):
    rank: int = Field(ge=1, le=5)
    crop_id: str
    name_uk: str
    score: float = Field(description="Центральна оцінка mid у шкалі 0–1, не ймовірність урожаю.")
    score_explanation: str
    economics: CropEconomicsResponse


class RecommendationResponse(Model):
    recommendation_id: str
    snapshot_id: str
    status: Literal["ok", "limited", "needs_input", "abstain", "error"]
    as_of_date: date
    demo: bool
    results: list[CropResponse] = Field(max_length=5)
    warnings: list[str]


def _economics(value, field_area_ha):
    if value is None:
        return CropEconomicsResponse(price_uah_t=None, price_date=None, yield_t_field=None,
                                     revenue_uah_field=None, costs_uah_field=None, margin_uah_field=None)

    def total(per_ha):
        return per_ha * field_area_ha if per_ha is not None and field_area_ha is not None else None

    return CropEconomicsResponse(
        price_uah_t=value.price.base if value.price else None,
        price_date=value.price.price_date if value.price else None,
        yield_t_field=total(value.yield_t_ha.base if value.yield_t_ha else None),
        revenue_uah_field=total(value.revenue_uah_ha.base if value.revenue_uah_ha else None),
        costs_uah_field=total(value.costs_uah_ha),
        margin_uah_field=total(value.margin_uah_ha.base if value.margin_uah_ha else None),
    )


def _texts(crop, events, input_reasons):
    if crop.personalization:
        reasons = [f"Базова придатність за GAEZ: {crop.gaez.suitability_index.mid:.1f} зі 100."]
        if crop.personalization.weighted:
            weighted = crop.personalization.weighted
            reasons.append(f"Після погодних та індексних поправок: {weighted.final_score.mid*100:.1f} зі 100; множник до базового бала {weighted.multiplier.mid:.3f}.")
        reasons += [item.explanation_uk for item in crop.personalization.assessments
                    if item.outcome == "risk" or (
                        item.outcome == "pass" and item.rule_id.startswith("rotation."))]
    else:
        reasons = [f"Придатність за GAEZ: {crop.gaez.suitability_index.mid:.1f} зі 100."]
        if crop.rotation.rules_fired:
            reasons.append("Враховано сівозміну.")
        if crop.weather.signal not in {"unknown", "disabled", "not_applicable"}:
            reasons.append("Враховано погодні умови посіву.")
    limitations = []
    for item in crop.limitations:
        if item.text in input_reasons:
            continue
        event = events[item.trace_event_id]
        if event.code == "ECONOMICS" and crop.economics and not crop.economics.complete:
            continue
        if event.code == "STABILITY" and crop.stability.label == "unavailable":
            continue
        text = item.text
        if event.code == "MISSING_SOURCES":
            names = {"gaez": "GAEZ", "history": "історія культур", "weather": "погода",
                     "local_weather": "погода", "satellite": "супутникові індекси",
                     "field_indices": "супутникові індекси", "imagery": "знімки",
                     "prices": "ціни", "costs": "витрати"}
            missing = list(dict.fromkeys(names.get(name, name)
                                        for name in event.params.get("sources", [])))
            text = "Відсутні дані: " + ", ".join(missing) + "."
        elif text.startswith("Оцінка GAEZ має крок"):
            text = "Дані GAEZ не описують неоднорідність усередині поля."
        elif text.startswith("GAEZ використовує спільний код"):
            text = "Озимий і ярий варіанти цієї культури в GAEZ окремо не оцінені."
        elif text.startswith("Обмеження персональних даних:"):
            text = "Частина персональних даних непридатна для оцінки."
        limitations.append(text)
    return list(dict.fromkeys(reasons)), list(dict.fromkeys(limitations))


def recommendation_response(result: RecommendationResult,
                            field_area_ha: float | None = None) -> RecommendationResponse:
    """Project POST, GET and replay identically without changing the stored result."""
    events = {event.id: event for event in result.trace}
    input_reasons = {item.why for item in result.requests_for_input}
    texts = [_texts(crop, events, input_reasons) for crop in result.results]
    limitations = [items[1] for items in texts]
    warnings = [text for items in limitations for text in items]
    warnings += [item.why for item in result.requests_for_input]
    if any(crop.status == "conditional" for crop in result.results):
        warnings.append("Рекомендації містять умовні кандидати; врахуйте обмеження перед посівом.")
    if result.mode == "LIMITED":
        warnings.insert(0, "Результат потребує агрономічної перевірки.")
    if not result.results and result.status != "error":
        warnings.append("Недостатньо даних або немає придатних культур для заданих умов.")
        causes = {
            "TARGET_SOWING_YEAR_PASSED": "Рік посіву для заданого сезону вже минув; уточніть сезон.",
            "GAEZ_NO_COVERAGE": "Немає доступних даних GAEZ для оцінки культур у заданих умовах.",
            "NOT_IN_GAEZ_CATALOG": "Для частини культур немає відповідних даних у каталозі GAEZ.",
            "GAEZ_ZERO_SUITABILITY": "Оцінені культури мають нульову придатність за GAEZ.",
        }
        warnings += [causes[item.reason] for item in [*result.not_evaluated, *result.below_threshold]
                     if item.reason in causes]
        warnings += [item.text for item in result.excluded]
        if any(event.code == "REGION_NOT_SUPPORTED" for event in result.trace):
            warnings.append("Розташування поля не підтверджено в підтримуваному регіоні.")
    if result.provenance.source_errors:
        warnings.append("Частина джерел даних недоступна; результат враховує наявні дані.")
    if result.provenance.demo:
        warnings.insert(0, "Демонстраційний результат на синтетичних даних.")
    if any(crop.economics and not crop.economics.complete and (
            crop.economics.price or crop.economics.yield_t_ha or crop.economics.costs_uah_ha is not None)
           for crop in result.results):
        warnings.append("Економічні дані неповні; суми враховують лише надані статті витрат.")

    status = result.status
    if status == "ok":
        status = {"LIMITED": "limited", "NEEDS_INPUT": "needs_input",
                  "ABSTAIN": "abstain"}.get(result.mode, "ok")
    return RecommendationResponse(
        recommendation_id=result.recommendation_id, snapshot_id=result.provenance.snapshot_id,
        status=status, as_of_date=result.as_of_date, demo=result.provenance.demo,
        results=[CropResponse(
            rank=crop.rank, crop_id=crop.crop_variant, name_uk=crop.name_uk,
            score=(crop.ranking_score or crop.agro_score).mid,
            score_explanation=" ".join([
                "Центральне значення оцінки у шкалі 0–1; не ймовірність урожаю.", *reasons]),
            economics=_economics(crop.economics, field_area_ha),
        ) for crop, (reasons, _) in zip(result.results, texts)],
        warnings=list(dict.fromkeys(warnings)),
    )
