"""Run ``python -m app.rules [rules-path]`` to validate a review bundle."""

import argparse
import json

from .loader import load_rule_bundle, load_rules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="rules")
    parser.add_argument("--allow-demo", action="store_true")
    args = parser.parse_args()
    bundle = load_rule_bundle(args.path, allow_demo=args.allow_demo)
    runtime = load_rules(args.path, allow_demo=args.allow_demo)
    print(json.dumps({
        "version": bundle.version, "demo": runtime.demo,
        "catalog_count": len(bundle.catalog), "oracle_pairs": len(bundle.oracle),
        "approved_rules": len(bundle.active_rotation),
        "readiness": {region: bundle.readiness_issues(region) for region in bundle.manifest.regions},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
