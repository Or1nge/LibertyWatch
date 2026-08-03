#!/usr/bin/env python3
"""Reconcile the six explicit HK cancellation candidates without production writes."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


WEBAPP_ROOT = Path(__file__).resolve().parents[2]
LIBERTY_ROOT = WEBAPP_ROOT.parent
sys.path.insert(0, str(WEBAPP_ROOT))

from liberty_v2.cancellation_reconciliation import (  # noqa: E402
    SCHEMA_VERSION,
    CancellationReconciliationError,
    discover_cancelled_share_candidates,
    reconcile_one,
    sha256_file,
    verify_review_sources,
)
from liberty_v2.snapshot_store import atomic_write_json  # noqa: E402


DEFAULT_CANDIDATES = (
    LIBERTY_ROOT
    / "data"
    / "shareholder-v2"
    / "backfill-output"
    / "equity-bridge-candidates-v1"
)
DEFAULT_ANNUAL_ROOT = LIBERTY_ROOT / "data" / "raw" / "annual_reports" / "official_backfill_v1"
DEFAULT_REVIEW_CONFIG = WEBAPP_ROOT / "config" / "cancellation_reconciliation_v1.json"
DEFAULT_OUTPUT = LIBERTY_ROOT / "data" / "shareholder-v2" / "reconciliation" / "cancellation-v1"
PRODUCTION_STAGING = LIBERTY_ROOT / "data" / "shareholder-v2" / "staging"


def _safe_output_root(path: Path) -> Path:
    result = path.resolve()
    production = PRODUCTION_STAGING.resolve()
    if result == production or production in result.parents:
        raise CancellationReconciliationError("refusing to write reconciliation under production staging")
    return result


def _listed_files(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix(): path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json" and not path.name.startswith(".")
    }


def verify_manifest(output_root: Path) -> dict[str, Any]:
    root = _safe_output_root(output_root)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise CancellationReconciliationError(f"manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    listed = {str(item["path"]): item for item in manifest.get("files", [])}
    actual = _listed_files(root)
    if set(listed) != set(actual):
        raise CancellationReconciliationError(
            f"manifest file set mismatch: listed={sorted(listed)}, actual={sorted(actual)}"
        )
    for relative, path in actual.items():
        expected = listed[relative]
        if path.stat().st_size != int(expected["size_bytes"]):
            raise CancellationReconciliationError(f"manifest size mismatch: {relative}")
        if sha256_file(path) != str(expected["sha256"]):
            raise CancellationReconciliationError(f"manifest SHA-256 mismatch: {relative}")
    if len(actual) != int(manifest.get("file_count") or -1):
        raise CancellationReconciliationError("manifest file_count mismatch")
    return {
        "status": "VALID",
        "checked_file_count": len(actual),
        "manifest": str(manifest_path),
    }


def run(args: argparse.Namespace) -> int:
    candidate_root = Path(args.candidate_root).resolve()
    annual_root = Path(args.annual_root).resolve()
    review_path = Path(args.review_config).resolve()
    output_root = _safe_output_root(Path(args.output_root))
    config = json.loads(review_path.read_text(encoding="utf-8"))
    reviews = config.get("reviews")
    if not isinstance(reviews, list):
        raise CancellationReconciliationError("review config must contain a reviews array")
    candidates = discover_cancelled_share_candidates(candidate_root)
    review_keys = {
        (str(item.get("company_id") or ""), int(item.get("fiscal_year") or 0))
        for item in reviews
        if isinstance(item, Mapping)
    }
    if review_keys != set(candidates):
        raise CancellationReconciliationError(
            f"review scope does not exactly match candidates: reviews={sorted(review_keys)}, "
            f"candidates={sorted(candidates)}"
        )

    decisions: list[dict[str, Any]] = []
    written: list[Path] = []
    for raw_review in sorted(reviews, key=lambda item: (item["company_id"], item["fiscal_year"])):
        review = dict(raw_review)
        key = (str(review["company_id"]), int(review["fiscal_year"]))
        sources, source_summaries = verify_review_sources(review, annual_root)
        decision = reconcile_one(candidates[key], review, sources, source_summaries)
        path = output_root / "decisions" / f"{decision['review_id']}.json"
        atomic_write_json(path, decision)
        written.append(path)
        decisions.append(decision)

    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate_root": str(candidate_root),
        "annual_report_root": str(annual_root),
        "review_config": str(review_path),
        "review_config_sha256": sha256_file(review_path),
        "candidate_count": len(decisions),
        "candidate_decision_counts": {
            status: sum(item["candidate_decision"] == status for item in decisions)
            for status in ("ACCEPT", "REJECT", "REVIEW")
        },
        "cancellation_fact_counts": {
            status: sum(item["cancellation_fact_decision"] == status for item in decisions)
            for status in ("ACCEPT", "REJECT", "REVIEW")
        },
        "issued_share_bridge_counts": {
            status: sum(item["issued_share_bridge"]["status"] == status for item in decisions)
            for status in ("ACCEPT", "REJECT", "REVIEW")
        },
        "diluted_share_bridge_counts": {
            status: sum(item["diluted_share_bridge_status"] == status for item in decisions)
            for status in ("ACCEPT", "REJECT", "REVIEW")
        },
        "net_reduction_factor_calculated_count": sum(
            item["net_reduction_factor"] is not None for item in decisions
        ),
        "b_eligible_authorized_count": sum(
            item["b_eligible_authorized"] is True for item in decisions
        ),
        "writes_production": False,
        "summary": [
            {
                "review_id": item["review_id"],
                "company_id": item["company_id"],
                "company_name": item["company_name"],
                "fiscal_year": item["fiscal_year"],
                "candidate_decision": item["candidate_decision"],
                "cancellation_fact_decision": item["cancellation_fact_decision"],
                "candidate_cancelled_shares": item["candidate_cancelled_shares"],
                "verified_cancelled_shares": item["verified_cancelled_shares"],
                "issued_share_bridge_status": item["issued_share_bridge"]["status"],
                "diluted_share_bridge_status": item["diluted_share_bridge_status"],
                "net_reduction_factor": None,
            }
            for item in decisions
        ],
        "safety": [
            "Cancellation facts are kept separate from issued-share and diluted-share bridges.",
            "A rounded thousand-share table is never promoted to an exact share count without corroboration.",
            "No net_reduction_factor or B_eligible is calculated without an endpoint diluted-share bridge.",
            "Outputs are private reconciliation artifacts outside production staging.",
        ],
    }
    report_path = output_root / "report.json"
    atomic_write_json(report_path, report)
    written.append(report_path)
    basis_path = output_root / "review_basis.json"
    atomic_write_json(basis_path, config)
    written.append(basis_path)

    manifest = {
        "schema_version": "cancellation-reconciliation-manifest-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(written),
        "files": [
            {
                "path": path.relative_to(output_root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in sorted(written)
        ],
    }
    atomic_write_json(output_root / "manifest.json", manifest)
    verification = verify_manifest(output_root)
    print(json.dumps({**report, "manifest_verification": verification}, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--candidate-root", default=str(DEFAULT_CANDIDATES))
    result.add_argument("--annual-root", default=str(DEFAULT_ANNUAL_ROOT))
    result.add_argument("--review-config", default=str(DEFAULT_REVIEW_CONFIG))
    result.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    result.add_argument("--verify-only", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.verify_only:
            print(json.dumps(verify_manifest(Path(args.output_root)), ensure_ascii=False, indent=2))
            return 0
        return run(args)
    except (CancellationReconciliationError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"cancellation reconciliation error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
