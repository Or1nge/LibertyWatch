#!/usr/bin/env python3
"""Reconcile the 13 narrow dividend candidates without writing production staging."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


WEBAPP_ROOT = Path(__file__).resolve().parents[2]
LIBERTY_ROOT = WEBAPP_ROOT.parent
sys.path.insert(0, str(WEBAPP_ROOT))

from liberty_v2.dividend_reconciliation import (  # noqa: E402
    SCHEMA_VERSION,
    AnnualReportIndex,
    DividendReconciliationError,
    load_candidate_inventory,
    public_candidate,
    sha256_file,
    validate_futu_event,
    validate_review_config,
    verify_file_manifest,
)
from liberty_v2.snapshot_store import atomic_write_json  # noqa: E402


DEFAULT_CONFIG = WEBAPP_ROOT / "config" / "dividend_reconciliation_v1.json"
DEFAULT_CANDIDATES = (
    LIBERTY_ROOT / "data" / "shareholder-v2" / "backfill-output" / "dividend-candidates-v1"
)
DEFAULT_ANNUAL_REPORTS = (
    LIBERTY_ROOT / "data" / "raw" / "annual_reports" / "official_backfill_v1"
)
DEFAULT_FUTU_DATABASE = LIBERTY_ROOT / "data" / "monitor" / "liberty_monitor.sqlite3"
DEFAULT_OUTPUT = (
    LIBERTY_ROOT / "data" / "shareholder-v2" / "reconciliation" / "dividend-v1"
)
PRODUCTION_STAGING = LIBERTY_ROOT / "data" / "shareholder-v2" / "staging"


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DividendReconciliationError(f"JSON root must be an object: {path}")
    return value


def _safe_output_root(path: Path, *, candidate_root: Path, annual_root: Path) -> Path:
    output = path.resolve()
    forbidden = (PRODUCTION_STAGING.resolve(), candidate_root.resolve(), annual_root.resolve())
    for root in forbidden:
        if output == root or root in output.parents:
            raise DividendReconciliationError(f"refusing unsafe reconciliation output: {output}")
    return output


def _manifest_payload(output_root: Path, *, reviewed_at: str) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(output_root.rglob("*.json")):
        if path.name == "manifest.json":
            continue
        files.append(
            {
                "path": path.relative_to(output_root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": reviewed_at,
        "file_count": len(files),
        "files": files,
        "writes_production": False,
    }


def _candidate_source_validation(
    annual_index: AnnualReportIndex,
    candidate: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    company_id = str(candidate.get("company_id") or "")
    fiscal_year = int(candidate.get("report_fiscal_year") or 0)
    document = annual_index.get(company_id, fiscal_year)
    expected_path = Path(str(candidate.get("source_local_path") or "")).resolve()
    if document.path != expected_path:
        raise DividendReconciliationError(
            f"candidate source path differs from current official report: {candidate.get('evidence_id')}"
        )
    if str(candidate.get("source_sha256") or "") != str(document.metadata.get("sha256") or ""):
        raise DividendReconciliationError(
            f"candidate source SHA differs from annual-report manifest: {candidate.get('evidence_id')}"
        )
    return annual_index.validate_candidate_page(
        company_id=company_id,
        fiscal_year=fiscal_year,
        page=int(candidate.get("page") or 0),
        markers=list(decision.get("page_markers") or []),
    )


def build(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config).resolve()
    candidate_root = Path(args.candidate_root).resolve()
    annual_root = Path(args.annual_root).resolve()
    database = Path(args.futu_database).resolve()
    output_root = _safe_output_root(
        Path(args.output_root), candidate_root=candidate_root, annual_root=annual_root
    )
    config = _load_json(config_path)
    source_manifest = verify_file_manifest(
        candidate_root,
        expected_manifest_sha256=str(config["expected_candidate_manifest_sha256"]),
    )
    inventory = load_candidate_inventory(candidate_root)
    validate_review_config(config, inventory)
    annual_index = AnnualReportIndex(
        annual_root,
        minimum_pages=int(config.get("minimum_annual_report_pages") or 60),
        identity_fragments=config.get("identity_fragments") or {},
    )

    reviewed_at = str(config["reviewed_at"])
    decision_outputs: dict[str, dict[str, Any]] = {}
    candidate_source_evidence: dict[str, dict[str, Any]] = {}
    decisions = list(config["candidate_decisions"])
    for decision in decisions:
        evidence_id = str(decision["evidence_id"])
        candidate = inventory[evidence_id]
        source_validation = _candidate_source_validation(annual_index, candidate, decision)
        candidate_source_evidence[evidence_id] = source_validation
        payload = {
            "schema_version": SCHEMA_VERSION,
            "reviewed_at": reviewed_at,
            "evidence_id": evidence_id,
            "selection_basis": decision["selection_basis"],
            "decision": decision["decision"],
            "reason_code": decision["reason_code"],
            "explanation_zh": decision["explanation_zh"],
            "distribution_id": decision.get("distribution_id"),
            "replacement_distribution_id": decision.get("replacement_distribution_id"),
            "accepted_role": decision.get("accepted_role"),
            "original_candidate": public_candidate(candidate),
            "candidate_source_validation": source_validation,
            "writes_production": False,
        }
        decision_outputs[evidence_id] = payload
        atomic_write_json(output_root / "decisions" / f"{evidence_id}.json", payload)

    distribution_outputs: list[dict[str, Any]] = []
    for raw in config["reconciled_distributions"]:
        distribution = dict(raw)
        annual_evidence: list[dict[str, Any]] = []
        for support in distribution.pop("support_annual_reports", []):
            annual_evidence.append(
                annual_index.validate_markers(
                    company_id=str(support["company_id"]),
                    fiscal_year=int(support["fiscal_year"]),
                    markers=list(support.get("markers") or []),
                )
            )
        futu_evidence = [
            validate_futu_event(database, event)
            for event in distribution.pop("futu_events", [])
        ]
        candidate_evidence = [
            candidate_source_evidence[evidence_id]
            for evidence_id in distribution.get("source_candidate_ids", [])
        ]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "reviewed_at": reviewed_at,
            **distribution,
            "source_evidence": {
                "candidate_annual_report_pages": candidate_evidence,
                "supporting_annual_reports": annual_evidence,
                "secondary_implementation_events": futu_evidence,
            },
            "writes_production": False,
        }
        distribution_outputs.append(payload)
        atomic_write_json(
            output_root / "distributions" / f"{payload['distribution_id']}.json", payload
        )

    counts = Counter(str(item["decision"]) for item in decisions)
    selection_counts = Counter(str(item["selection_basis"]) for item in decisions)
    current_eligible_ids = sorted(
        evidence_id
        for evidence_id, candidate in inventory.items()
        if candidate.get("eligible_after_manual_review") is True
    )
    ready = [
        item for item in distribution_outputs if item["ready_for_controlled_ledger_import"] is True
    ]
    component_only = [
        item for item in distribution_outputs if item["import_scope"] == "COMPONENT_ONLY"
    ]
    decision_by_id = {
        str(item["evidence_id"]): str(item["decision"]) for item in decisions
    }
    replacement_distributions = [
        item
        for item in distribution_outputs
        if any(
            decision_by_id[evidence_id] == "REJECT"
            for evidence_id in item.get("source_candidate_ids", [])
        )
    ]
    report = {
        "schema_version": SCHEMA_VERSION,
        "reviewed_at": reviewed_at,
        "source_candidate_manifest": source_manifest,
        "scope": {
            "candidate_count": len(decisions),
            "historical_review_candidate_count": len(decisions),
            "source_extractor_current_eligible_count": len(current_eligible_ids),
            "company_count": len({item["company_id"] for item in distribution_outputs}),
            "distribution_count": len(distribution_outputs),
        },
        "source_extractor_current_eligible": {
            "count": len(current_eligible_ids),
            "evidence_ids": current_eligible_ids,
        },
        "historical_candidate_review": {
            "count": len(decisions),
            "evidence_ids": [str(item["evidence_id"]) for item in decisions],
            "decision_counts": dict(sorted(counts.items())),
            "selection_basis_counts": dict(sorted(selection_counts.items())),
        },
        "candidate_decision_counts": dict(sorted(counts.items())),
        "reconciled_complete_fiscal_year_total_count": len(ready),
        "replacement_distribution_count": len(replacement_distributions),
        "replacement_distribution_ids": [
            str(item["distribution_id"]) for item in replacement_distributions
        ],
        "ready_for_controlled_ledger_import_count": len(ready),
        "component_only_count": len(component_only),
        "ready_for_controlled_ledger_import": [
            {
                "distribution_id": item["distribution_id"],
                "company_id": item["company_id"],
                "company_name": item["company_name"],
                "fiscal_year": item["fiscal_year"],
                "ordinary_cash_dividend_total": item["ordinary_cash_dividend_total"],
                "per_share_components": item["per_share_components"],
            }
            for item in ready
        ],
        "remaining_gaps": [
            {
                "distribution_id": item["distribution_id"],
                "company_id": item["company_id"],
                "fiscal_year": item["fiscal_year"],
                "missing": "official final cash amount for the implemented interim dividend and complete FY2023 ordinary-dividend total",
            }
            for item in component_only
        ],
        "safety": {
            "candidate_values_written_to_production": False,
            "production_staging_modified": False,
            "rejected_candidates_are_not_importable": True,
            "per_share_and_total_representations_are_not_added": True,
            "unknown_values_are_not_zero": True,
        },
        "writes_production": False,
    }
    atomic_write_json(output_root / "report.json", report)
    atomic_write_json(
        output_root / "manifest.json",
        _manifest_payload(output_root, reviewed_at=reviewed_at),
    )
    verification = verify_file_manifest(output_root)
    return {"report": report, "verification": verification}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify"), nargs="?", default="build")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--candidate-root", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--annual-root", default=str(DEFAULT_ANNUAL_REPORTS))
    parser.add_argument("--futu-database", default=str(DEFAULT_FUTU_DATABASE))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "verify":
        result = verify_file_manifest(Path(args.output_root))
    else:
        result = build(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
