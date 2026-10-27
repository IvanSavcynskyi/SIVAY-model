"""Render facts already computed by RANK; never infer agronomic advice."""
from app.domain import Explanation, RecommendationResult, TraceEvent


ISSUES_UK = {
    "history_incomplete": "Історія культур неповна; інтервал оцінки сівозміни розширено.",
    "weather_missing": "Даних про умови посіву недостатньо; погодний множник має ширший інтервал.",
    "gaez_partial_coverage_no_buffer": "Растр покриває поле частково; нижня межа оцінки враховує невідому частину.",
    "coarse_gaez_conflict": "Шари GAEZ містять суперечливі оцінки непридатності; автоматичне виключення не підтверджено.",
    "gaez_fallback_all_land": "Основний шар SUX відсутній; використано SXX для всієї площі з додатковою невизначеністю.",
}
EFFECTS_UK = {
    "favorable": "сприятливий", "neutral": "нейтральний",
    "discouraged_mild": "помірно небажаний", "discouraged_strong": "суттєво небажаний",
    "incompatible": "несумісний",
}
SOURCE_UK = {"weather": "погода", "history": "історія культур", "satellite": "супутникові індекси",
             "gaez": "GAEZ", "prices": "ціни", "costs": "витрати"}


def _interval(value):
    return f"{value['lo']:.3f}–{value['hi']:.3f} (центральна {value['mid']:.3f})"


def enrich_result(result: RecommendationResult) -> RecommendationResult:
    """Return an independent, idempotently enriched copy, preserving all source refs.

    Score templates read AGRO_SCORE events. Additional events record output facts
    such as resolution and robustness which older rankers did not log separately.
    """
    enriched = result.model_copy(deep=True)

    def event(code, crop, params, source_refs=None):
        existing = next((item for item in enriched.trace if item.code == code and item.crop == crop), None)
        if existing:
            return existing.id
        identifier = f"explain:{code}:{crop}"
        enriched.trace.append(TraceEvent(id=identifier, code=code, crop=crop,
                                        params=params, source_refs=source_refs or []))
        return identifier

    for crop in enriched.results:
        score = next((item for item in enriched.trace if item.code == "PERSONALIZATION" and
                      item.crop == crop.crop_variant), None) or next((item for item in enriched.trace
                      if item.code in {"AGRO_SCORE", "BASELINE_GAEZ"} and item.crop == crop.crop_variant), None)
        # A trace without the expected facts cannot justify a new score claim.
        if score is None:
            continue
        reasons, limitations = [], []

        def reason(text, identifier=score.id):
            reasons.append(Explanation(text=text, trace_event_id=identifier))

        def limitation(text, identifier=score.id):
            limitations.append(Explanation(text=text, trace_event_id=identifier))

        facts = score.params
        if score.code == "PERSONALIZATION":
            reason(f"Персоналізований порядок за ризиками поля; пріоритет ризику {facts['risk_priority']} (0 — нижчий, 3 — вищий).")
            for assessment in facts["assessments"]:
                text = assessment["explanation_uk"]
                if assessment["outcome"] in {"unknown", "not_applicable"}:
                    limitation(text)
                else:
                    reason(text)
            limitation("Правила персоналізації не пройшли польового калібрування; пріоритет ризику не є прогнозом втрат урожаю.")
            if facts["policy_status"] == "experimental":
                limitation("Використано експериментальну політику; регіональні правила господарства потребують уточнення.")
            for issue in facts.get("evidence", {}).get("issues", []):
                limitation(f"Обмеження персональних даних: {issue}.")
        if score.code == "AGRO_SCORE" and all(key in facts for key in ("a", "rotation", "weather", "agro")):
            reason(f"Придатність GAEZ: {_interval(facts['a'])}; після врахування сівозміни та умов посіву агрооцінка: {_interval(facts['agro'])}.")
            rules = facts["rotation"].get("rules_fired", [])
            if rules:
                details = "; ".join(f"{r['id']} v{r['version']}: {EFFECTS_UK.get(r['effect'], r['effect'])}" for r in rules)
                reason(f"Сівозміна: {details}; множник {_interval(facts['rotation']['multiplier'])}.")
            if facts["weather"].get("signal") not in {"unknown", "not_applicable", None}:
                signals = {"dry": "сухі умови", "normal": "звичайні умови", "wet_ok": "достатня вологість"}
                signal = facts["weather"]["signal"]
                reason(f"Умови посіву: {signals.get(signal, signal)}; погодний множник {_interval(facts['weather']['multiplier'])}.")
        for issue in facts.get("issues", []):
            limitation(ISSUES_UK.get(issue, f"Обмеження даних: {issue}."))

        gaez = crop.gaez
        grid_event = event("GAEZ_RESOLUTION", crop.crop_variant, gaez.model_dump(), [gaez.layer])
        if score.code == "PERSONALIZATION":
            reason(f"Базова придатність GAEZ: {_interval(gaez.suitability_index.model_dump())}; персональні ризики змінюють порядок, а не цей індекс.", grid_event)
        if score.code == "BASELINE_GAEZ":
            reason(f"Базова придатність за GAEZ: {_interval(gaez.suitability_index.model_dump())}.", grid_event)
            limitation("Базове порівняння за GAEZ потребує окремої перевірки сівозміни та строків посіву перед прийняттям рішення.")
            if facts.get("fallback"):
                limitation("Основний шар SUX відсутній; використано SXX для всієї площі з додатковою невизначеністю.")
            if facts.get("coverage_valid", 1) < .8:
                limitation(f"Частка поля з даними GAEZ: {facts['coverage_valid']:.0%}; нижня межа оцінки враховує неповне покриття.")
        limitation(f"Оцінка GAEZ має крок {gaez.resolution_deg:.5f}°; площа поля становить {gaez.field_to_cell_ratio:.2%} типової комірки, тому растр не описує неоднорідність усередині поля.", grid_event)
        if gaez.match == "parent_crop":
            limitation(f"GAEZ використовує спільний код {gaez.crop_code} для культури; озимий і ярий варіанти окремо не оцінені.", grid_event)
        limitation("Агрооцінка є індексом придатності, а не ймовірністю успішного врожаю.")

        missing = sorted(name for name, quality in enriched.data_quality.by_source.items() if quality == "missing")
        if missing:
            identifier = event("MISSING_SOURCES", crop.crop_variant, {"sources": missing})
            limitation("Відсутні дані: " + ", ".join(SOURCE_UK.get(name, name) for name in missing) + ".", identifier)
        if crop.economics:
            eco = crop.economics
            price_refs = [eco.price.price_id] if eco.price and eco.price.price_id else []
            identifier = event("ECONOMICS", crop.crop_variant, eco.model_dump(), price_refs)
            if eco.complete and eco.margin_uah_ha:
                reason(f"Сценарна маржа: {eco.margin_uah_ha.low:,.0f}–{eco.margin_uah_ha.high:,.0f} грн/га; базовий сценарій {eco.margin_uah_ha.base:,.0f} грн/га.", identifier)
                if eco.applied_to_rank:
                    reason("Економічні дані враховано для впорядкування всередині агрономічного ярусу.", identifier)
                limitation("Сценарна маржа враховує лише надані статті витрат.", identifier)
            else:
                limitation("Економічних даних недостатньо для порівняння маржі.", identifier)
        stability = crop.stability
        identifier = event("STABILITY", crop.crop_variant, stability.model_dump())
        if stability.label == "unavailable":
            limitation("Стійкість рейтингу до збурень параметрів не оцінена.", identifier)
        else:
            reason(f"У {stability.scenarios_evaluated} сценаріях перевірки культура потрапляє до топ-5 у {stability.p_top5:.0%} випадків; місце змінюється від {stability.rank_range[0]} до {stability.rank_range[1]}.", identifier)
            limitation("Частка потрапляння до топ-5 описує перевірені збурення параметрів, а не ймовірність майбутнього результату.", identifier)
        if enriched.provenance.demo:
            identifier = event("DEMO_DATA", crop.crop_variant, {"demo": True})
            limitation("Демонстраційний режим: використано синтетичні значення й навчальні правила; результат не призначений для агрономічних рішень.", identifier)
        crop.reasons, crop.limitations = reasons, limitations
    return RecommendationResult.model_validate(enriched.model_dump())
