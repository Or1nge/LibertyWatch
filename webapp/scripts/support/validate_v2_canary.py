#!/usr/bin/env python3
"""Fail closed unless the v2.2 screening index passes global approval."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


WEBAPP_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WEBAPP_ROOT))

from liberty_v2.public_contract import (  # noqa: E402
    V2ContractError,
    validate_activation_canary,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--companies", type=Path, required=True)
    parser.add_argument(
        "--reviews",
        type=Path,
        default=WEBAPP_ROOT / "config" / "shareholder_v2_activation_reviews.json",
    )
    args = parser.parse_args()
    companies = json.loads(args.companies.read_text(encoding="utf-8"))
    reviews = json.loads(args.reviews.read_text(encoding="utf-8"))
    try:
        summary = validate_activation_canary(
            companies,
            approval=reviews,
        )
    except V2ContractError as error:
        print(f"CANARY_REJECTED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary.public_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
