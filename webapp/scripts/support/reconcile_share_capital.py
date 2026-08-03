#!/usr/bin/env python3
"""Build the read-only 56-company share-capital reconciliation bundle."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WEBAPP_ROOT = Path(__file__).resolve().parents[2]
LIBERTY_ROOT = WEBAPP_ROOT.parent
sys.path.insert(0, str(WEBAPP_ROOT))

from liberty_v2.share_capital_reconciliation import (  # noqa: E402
    SCHEMA_VERSION,
    ShareCapitalReconciliationError,
    build_reconciliation,
    load_candidate_companies,
    load_cancellation_context,
    load_json_object,
    sha256_file,
    verified_manual_facts,
    verify_manual_sources,
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
DEFAULT_CANCELLATIONS = (
    LIBERTY_ROOT / "data" / "shareholder-v2" / "reconciliation" / "cancellation-v1"
)
DEFAULT_REVIEW_CONFIG = WEBAPP_ROOT / "config" / "share_capital_reconciliation_v1.json"
DEFAULT_OUTPUT = (
    LIBERTY_ROOT / "data" / "shareholder-v2" / "reconciliation" / "share-capital-v1"
)
PRODUCTION_STAGING = LIBERTY_ROOT / "data" / "shareholder-v2" / "staging"


def safe_output_root(path: Path) -> Path:
    result = path.resolve()
    staging = PRODUCTION_STAGING.resolve()
    cancellation = DEFAULT_CANCELLATIONS.resolve()
    if result == staging or staging in result.parents:
        raise ShareCapitalReconciliationError("refusing to write under production staging")
    if result == cancellation or cancellation in result.parents:
        raise ShareCapitalReconciliationError("refusing to modify cancellation-v1")
    return result


def listed_files(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix(): path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json" and not path.name.startswith(".")
    }


def verify_manifest(output_root: Path) -> dict[str, Any]:
    root = safe_output_root(output_root)
    manifest_path = root / "manifest.json"
    manifest = load_json_object(manifest_path)
    if manifest.get("schema_version") != "share-capital-reconciliation-manifest-v1":
        raise ShareCapitalReconciliationError("unsupported share-capital manifest")
    listed = {str(item.get("path") or ""): item for item in manifest.get("files", [])}
    actual = listed_files(root)
    if set(listed) != set(actual):
        raise ShareCapitalReconciliationError(
            f"manifest file set mismatch: listed={sorted(listed)}, actual={sorted(actual)}"
        )
    for relative, path in actual.items():
        expected = listed[relative]
        if path.stat().st_size != int(expected.get("size_bytes") or -1):
            raise ShareCapitalReconciliationError(f"manifest size mismatch: {relative}")
        if sha256_file(path) != str(expected.get("sha256") or ""):
            raise ShareCapitalReconciliationError(f"manifest SHA-256 mismatch: {relative}")
    if len(actual) != int(manifest.get("file_count") or -1):
        raise ShareCapitalReconciliationError("manifest file_count mismatch")
    return {
        "status": "VALID",
        "checked_file_count": len(actual),
        "manifest": str(manifest_path),
    }


def run(args: argparse.Namespace) -> int:
    candidate_root = Path(args.candidate_root).resolve()
    annual_root = Path(args.annual_root).resolve()
    cancellation_root = Path(args.cancellation_root).resolve()
    review_path = Path(args.review_config).resolve()
    output_root = safe_output_root(Path(args.output_root))
    config = load_json_object(review_path)
    if config.get("schema_version") != "share-capital-reconciliation-review-v1.0":
        raise ShareCapitalReconciliationError("unsupported review config")
    policy = config.get("policy")
    if not isinstance(policy, dict):
        raise ShareCapitalReconciliationError("review policy is missing")

    candidates, candidate_report = load_candidate_companies(
        candidate_root,
        expected_manifest_sha256=str(policy.get("candidate_manifest_sha256") or ""),
    )
    manual_sources = verify_manual_sources(config, annual_root)
    manual_facts, review_cases = verified_manual_facts(config, manual_sources)
    cancellation_context = load_cancellation_context(cancellation_root, annual_root)
    companies, report = build_reconciliation(
        config,
        candidates,
        candidate_report,
        annual_root,
        cancellation_context,
        manual_facts,
        review_cases,
    )

    created_at = datetime.now(timezone.utc).isoformat()
    written: list[Path] = []
    for company in companies:
        path = output_root / "companies" / f"{company['company_id']}.json"
        atomic_write_json(path, company)
        written.append(path)
    report.update(
        {
            "created_at": created_at,
            "candidate_root": str(candidate_root),
            "candidate_manifest_sha256": sha256_file(candidate_root / "manifest.json"),
            "annual_report_root": str(annual_root),
            "cancellation_root": str(cancellation_root),
            "cancellation_manifest_sha256": sha256_file(cancellation_root / "manifest.json"),
            "review_config": str(review_path),
            "review_config_sha256": sha256_file(review_path),
        }
    )
    report_path = output_root / "report.json"
    atomic_write_json(report_path, report)
    written.append(report_path)
    basis_path = output_root / "review_basis.json"
    atomic_write_json(basis_path, config)
    written.append(basis_path)
    cases_path = output_root / "review_cases.json"
    atomic_write_json(
        cases_path,
        {
            "schema_version": "share-capital-review-cases-v1.0",
            "review_case_count": len(review_cases),
            "review_cases": list(review_cases),
        },
    )
    written.append(cases_path)

    manifest = {
        "schema_version": "share-capital-reconciliation-manifest-v1",
        "created_at": created_at,
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
    result.add_argument("--cancellation-root", default=str(DEFAULT_CANCELLATIONS))
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
    except (ShareCapitalReconciliationError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"share-capital reconciliation error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
