"""Exercise deployed HTTP, worker, live sources, saved evidence and replay.

The geometry/history are examples, not an actual farm declaration. The optional
near-sowing plan is explicitly synthetic and only tests application of signals.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--request", type=Path, default=Path("examples/request.json"))
    parser.add_argument("--output", type=Path, default=Path("/tmp/sivay-http-smoke.json"))
    parser.add_argument("--require-sources", action="store_true")
    parser.add_argument("--synthetic-near-sowing", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.request.read_text())
    today = datetime.now(timezone.utc).date()
    payload["as_of_date"] = today.isoformat()
    payload.pop("economics", None)
    if args.synthetic_near_sowing:
        sowing = today + timedelta(days=3)
        payload["target_season"] = {"harvest_year": sowing.year + 1, "include": ["winter"]}
        payload["personalization"] = {"sowing_dates": {
            crop: sowing.isoformat() for crop in
            ("wheat_winter", "barley_winter", "rapeseed_winter", "rye_winter")}}
    headers = {}
    if token := os.environ.get("SIVAY_API_KEY"):
        headers["Authorization"] = "Bearer " + token
    start = time.monotonic()
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=300) as client:
        def get(path):
            response = client.get(path)
            response.raise_for_status()
            return response.json()

        health = get("/v1/health")
        response = client.post(f"/v1/fields/{payload['field']['field_id']}/prepare", json=payload)
        response.raise_for_status()
        job_id = response.json()["job_id"]
        while True:
            job = get("/v1/jobs/" + job_id)
            if job["status"] in {"completed", "partial", "failed"}:
                break
            if time.monotonic() - start > 600:
                raise RuntimeError("Worker did not finish within 600 seconds")
            time.sleep(1)
        if job["status"] == "failed":
            raise RuntimeError("Worker failed: " + str(job.get("error")))
        response = client.post("/v1/recommendations", json=payload)
        response.raise_for_status()
        result = response.json()
        saved = get("/v1/recommendations/" + result["recommendation_id"])
        analysis = get("/v1/recommendations/" + result["recommendation_id"] + "/analysis")
        replay = client.post("/v1/recommendations", json=payload,
                             params={"snapshot_id": result["snapshot_id"]})
        replay.raise_for_status()
    checks = {
        "ready": health["recommendation_ready"],
        "real_data": not result["demo"],
        "recommendations": bool(result["results"]),
        "worker_snapshot_matches": job["snapshot_id"] == result["snapshot_id"],
        "saved_identical": saved == result,
        "replay_identical": replay.json() == result,
        "weather": "weather.precip_30d" in analysis["metrics"],
        "forecast": "forecast.tmin_7d" in analysis["metrics"],
        "sentinel": bool(analysis["images"]) and {"NDVI", "NDMI"} <= analysis["indices"].keys(),
        "sources_complete": not analysis["source_errors"],
        "economics_empty": all(all(value is None for value in crop["economics"].values())
                               for crop in result["results"]),
    }
    args.output.write_text(json.dumps({"example_field": True,
        "synthetic_sowing_plan": args.synthetic_near_sowing,
        "checks": checks, "health": health, "request": payload,
        "job": job, "response": result, "analysis": analysis}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "seconds": round(time.monotonic()-start, 2),
        "checks": checks, "status": result["status"], "source_errors": analysis["source_errors"],
        "scene_count": len(analysis["images"]), "metrics": analysis["metrics"],
        "crops": [{"crop": crop["name_uk"], "score": crop["score"]} for crop in result["results"]]},
        ensure_ascii=False, indent=2))
    required = set(checks) if args.require_sources else set(checks) - {"weather", "forecast", "sentinel", "sources_complete"}
    return 0 if all(checks[name] for name in required) else 2


if __name__ == "__main__":
    raise SystemExit(main())
