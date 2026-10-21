"""python -m app.validation {backtest,ablation,blind} cases.json --output report.json"""
import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from app.validation.offline import ValidationCase, ablate, backtest, blind_package


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline snapshot validation; no expert results inferred")
    parser.add_argument("command", choices=("backtest", "ablation", "blind"))
    parser.add_argument("cases", type=Path, help="JSON object with a cases array")
    parser.add_argument("--profile", choices=("gaez_baseline", "validated"), default="gaez_baseline")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-output", type=Path, help="Separate private assignment key for blind review")
    parser.add_argument("--seed", default="sivay-review-v1")
    args = parser.parse_args()
    if args.command == "blind" and args.key_output is None:
        parser.error("blind requires --key-output, kept separate from the reviewer package")
    if args.key_output and args.output.resolve() == args.key_output.resolve():
        parser.error("reviewer output and assignment key must have different paths")
    try:
        raw = json.loads(args.cases.read_text())
        cases = [ValidationCase.model_validate(row) for row in raw["cases"]]
        if args.command == "blind":
            result, key = blind_package(cases, args.profile, args.seed)
            args.key_output.write_text(json.dumps(key, ensure_ascii=False, indent=2) + "\n")
        else:
            result = (backtest if args.command == "backtest" else ablate)(cases, args.profile)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
        parser.exit(2, f"validation failed: {exc}\n")


if __name__ == "__main__":
    main()
