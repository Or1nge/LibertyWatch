#!/usr/bin/env python3
"""Controlled, reversible import of accepted v2 source-ledger facts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


WEBAPP_ROOT = Path(__file__).resolve().parents[1]
LIBERTY_ROOT = WEBAPP_ROOT.parent
sys.path.insert(0, str(WEBAPP_ROOT))

from liberty_v2.controlled_import import (  # noqa: E402
    ControlledImportInputs,
    apply_import,
    build_plan,
    rollback_import,
    verify_run,
)


DATA_ROOT = LIBERTY_ROOT / "data" / "shareholder-v2"
DEFAULT_STAGING = DATA_ROOT / "staging"
DEFAULT_FUTU = DATA_ROOT / "backfill-output" / "futu-ledger-v1"
DEFAULT_CASHFLOW = DATA_ROOT / "reconciliation" / "cashflow-v1"
DEFAULT_CASHFLOW_V2 = DATA_ROOT / "reconciliation" / "cashflow-v2"
DEFAULT_DIVIDEND = DATA_ROOT / "reconciliation" / "dividend-v1"
DEFAULT_DIVIDEND_V2 = DATA_ROOT / "reconciliation" / "dividend-v2"
DEFAULT_CANCELLATION = DATA_ROOT / "reconciliation" / "cancellation-v1"
DEFAULT_SHARE_CAPITAL = DATA_ROOT / "reconciliation" / "share-capital-v1"
DEFAULT_OFFICIAL = LIBERTY_ROOT / "data" / "raw" / "annual_reports" / "official_backfill_v1"
DEFAULT_RUN_ROOT = DATA_ROOT / "controlled-import-runs"


def inputs_from_args(args: argparse.Namespace) -> ControlledImportInputs:
    return ControlledImportInputs(
        staging_dir=Path(args.staging),
        futu_ledger_root=Path(args.futu_ledger_root),
        cashflow_root=Path(args.cashflow_root),
        cashflow_v2_root=Path(args.cashflow_v2_root),
        dividend_root=Path(args.dividend_root),
        dividend_v2_root=Path(args.dividend_v2_root),
        cancellation_root=Path(args.cancellation_root),
        share_capital_root=Path(args.share_capital_root),
        official_annual_root=Path(args.official_annual_root),
    )


def dump(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--staging", default=str(DEFAULT_STAGING))
    common.add_argument("--futu-ledger-root", default=str(DEFAULT_FUTU))
    common.add_argument("--cashflow-root", default=str(DEFAULT_CASHFLOW))
    common.add_argument("--cashflow-v2-root", default=str(DEFAULT_CASHFLOW_V2))
    common.add_argument("--dividend-root", default=str(DEFAULT_DIVIDEND))
    common.add_argument("--dividend-v2-root", default=str(DEFAULT_DIVIDEND_V2))
    common.add_argument("--cancellation-root", default=str(DEFAULT_CANCELLATION))
    common.add_argument("--share-capital-root", default=str(DEFAULT_SHARE_CAPITAL))
    common.add_argument("--official-annual-root", default=str(DEFAULT_OFFICIAL))
    subparsers = result.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan", parents=[common], help="validate and show a read-only plan")
    apply = subparsers.add_parser("apply", parents=[common], help="backup and atomically import")
    apply.add_argument("--backup-root", required=True, help="explicit backup directory")
    apply.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    apply.add_argument("--run-id")
    verify = subparsers.add_parser("verify", help="verify an applied run")
    verify.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    verify.add_argument("--run-id", required=True)
    rollback = subparsers.add_parser("rollback", help="restore an applied run if no drift exists")
    rollback.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    rollback.add_argument("--run-id", required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "plan":
        _desired, plan = build_plan(inputs_from_args(args))
        dump(plan)
    elif args.command == "apply":
        dump(
            apply_import(
                inputs_from_args(args),
                backup_root=Path(args.backup_root),
                run_root=Path(args.run_root),
                run_id=args.run_id,
            )
        )
    elif args.command == "verify":
        result = verify_run(Path(args.run_root), args.run_id)
        dump(result)
        return 0 if result["verified"] else 1
    else:
        dump(rollback_import(Path(args.run_root), args.run_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
