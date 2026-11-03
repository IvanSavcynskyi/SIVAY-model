"""Run a current-day public-source recommendation with a clearly synthetic sowing plan."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.domain import RecommendationRequest  # noqa: E402
from app.service import RecommendationService  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, default=Path("examples/request.json"))
    parser.add_argument("--output", type=Path, default=Path("/tmp/sivay-weighted-live.json"))
    parser.add_argument("--weather-only", action="store_true")
    parser.add_argument("--indices-only", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.request.read_text())
    if args.indices_only:
        from app.adapters.public_indices import PublicIndices
        from app.geo import validate_geometry
        data = PublicIndices().collect(validate_geometry(payload["field"]["geometry"], payload["field"]["crs"]))
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if args.weather_only:
        from app.adapters.public_weather import PublicWeather
        from app.geo import validate_geometry
        data = PublicWeather().collect(validate_geometry(payload["field"]["geometry"], payload["field"]["crs"]))
        print(json.dumps({key: value for key, value in data.items() if key != "weather"}, indent=2))
        print("weather_records", len(data["weather"]["records"]))
        return 0 if not data["forecast_error"] else 2
    today = datetime.now(timezone.utc).date()
    sowing = today+timedelta(days=3)
    payload["as_of_date"] = today.isoformat()
    payload["target_season"] = {"harvest_year": sowing.year+1, "include": ["winter"]}
    payload["personalization"] = {"sowing_dates": {
        crop: sowing.isoformat() for crop in ("wheat_winter", "barley_winter", "rapeseed_winter", "rye_winter")}}
    with TemporaryDirectory(prefix="sivay-weighted-live-") as directory:
        service = RecommendationService(Settings(database_url=f"sqlite:///{directory}/smoke.db"))
        try:
            request = RecommendationRequest.model_validate(payload)
            result = service.recommend(request)
            replay = service.replay(request, result.provenance.snapshot_id)
            args.output.write_text(json.dumps({"synthetic_sowing_plan": True,
                "request": payload, "response": result.model_dump(mode="json")}, ensure_ascii=False, indent=2)+"\n")
            print(json.dumps({"output": str(args.output), "synthetic_sowing_plan": True,
                "algorithm": result.provenance.algorithm_version, "mode": result.mode,
                "source_errors": [e.model_dump() for e in result.provenance.source_errors],
                "replay_identical": replay.model_dump() == result.model_dump(),
                "candidates": [{"crop": c.name_uk, "base": c.agro_score.mid,
                    "weighted": c.ranking_score.model_dump(), "metrics": c.personalization.metrics,
                    "images": len(c.personalization.evidence["images"])} for c in result.results]},
                ensure_ascii=False, indent=2))
            return 0 if result.results and not result.provenance.source_errors else 2
        finally:
            service.storage.close()


if __name__ == "__main__":
    raise SystemExit(main())
