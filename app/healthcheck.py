"""Container readiness, including local data and the configured bearer key."""
import json
import os
from urllib.request import Request, urlopen


def main():
    headers = {}
    if token := os.environ.get("SIVAY_API_KEY"):
        headers["Authorization"] = "Bearer " + token
    with urlopen(Request("http://127.0.0.1:8000/v1/health", headers=headers), timeout=10) as response:
        result = json.load(response)
    if not result.get("recommendation_ready"):
        raise SystemExit("Recommendation data is not ready")


if __name__ == "__main__":
    main()
