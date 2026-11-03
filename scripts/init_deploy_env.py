"""Generate a private server configuration without printing or overwriting credentials."""
import argparse
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".env"))
    args = parser.parse_args()
    text = (
        f"POSTGRES_PASSWORD={secrets.token_hex(24)}\n"
        f"SIVAY_API_KEY={secrets.token_urlsafe(36)}\n"
        "SIVAY_DEMO=false\n"
        "SIVAY_RANKING_PROFILE=personalized\n"
        "SIVAY_RULES_PATH=rules\n"
        "SIVAY_GAEZ_ROOT=data/gaez\n"
        "SIVAY_REGIONS_PATH=data/regions.geojson\n"
        "SIVAY_PERSONALIZATION_POLICY_PATH=rules/personalization.json\n"
        "SIVAY_AUTOMATIC_SOURCES_ENABLED=true\n"
        "SIVAY_AUTOMATIC_WEATHER_TTL_SECONDS=3600\n"
        "SIVAY_AUTOMATIC_INDICES_TTL_SECONDS=21600\n"
        "SIVAY_JOB_LEASE_SECONDS=900\n"
        "SIVAY_CORS_ORIGINS=[]\n"
    )
    with open(args.output, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
        output.write(text)
    print(f"Created {args.output}; credentials were not printed.")


if __name__ == "__main__":
    main()
