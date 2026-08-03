#!/usr/bin/env python3
"""Build the read-only dividend-v2 reconciliation bundle.

The command validates official annual-report PDFs, exact Futu implementation
events and Decimal calculations.  It never writes the production staging tree.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping


WEBAPP_ROOT = Path(__file__).resolve().parents[2]
LIBERTY_ROOT = WEBAPP_ROOT.parent
sys.path.insert(0, str(WEBAPP_ROOT))

from liberty_v2.dividend_reconciliation import (  # noqa: E402
    AnnualReportIndex,
    DividendReconciliationError,
    load_candidate_inventory,
    public_candidate,
    sha256_file,
    validate_futu_event,
    verify_file_manifest,
)
from liberty_v2.dividend_reconciliation_v2 import (  # noqa: E402
    SCHEMA_VERSION,
    blocker_for_candidates,
    distribution_total,
    load_recent_fiscal_year_targets,
    validate_v2_review_config,
)
from liberty_v2.snapshot_store import atomic_write_json  # noqa: E402


DEFAULT_CONFIG = WEBAPP_ROOT / "config" / "dividend_reconciliation_v2.json"
DEFAULT_CANDIDATES = (
    LIBERTY_ROOT / "data" / "shareholder-v2" / "backfill-output" / "dividend-candidates-v1"
)
DEFAULT_ANNUAL_REPORTS = (
    LIBERTY_ROOT / "data" / "raw" / "annual_reports" / "official_backfill_v1"
)
DEFAULT_FUTU_DATABASE = LIBERTY_ROOT / "data" / "monitor" / "liberty_monitor.sqlite3"
DEFAULT_PRIOR_RECONCILIATION = (
    LIBERTY_ROOT / "data" / "shareholder-v2" / "reconciliation" / "dividend-v1"
)
DEFAULT_OUTPUT = (
    LIBERTY_ROOT / "data" / "shareholder-v2" / "reconciliation" / "dividend-v2"
)
PRODUCTION_STAGING = LIBERTY_ROOT / "data" / "shareholder-v2" / "staging"


def _load_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DividendReconciliationError(f"cannot load JSON {path}: {error}") from error
    if not isinstance(payload, dict):
        raise DividendReconciliationError(f"JSON root must be an object: {path}")
    return payload


def _candidate_company_ids(candidate_root: Path) -> set[str]:
    company_ids: set[str] = set()
    for path in sorted((candidate_root / "candidates").glob("*.json")):
        payload = _load_object(path)
        company_id = str(payload.get("company_id") or "")
        if not company_id or company_id in company_ids:
            raise DividendReconciliationError(f"invalid duplicate candidate company: {company_id}")
        company_ids.add(company_id)
    return company_ids


def _safe_empty_output_root(
    output_root: Path,
    *,
    candidate_root: Path,
    annual_root: Path,
    prior_root: Path,
) -> Path:
    output = output_root.resolve()
    forbidden = (
        PRODUCTION_STAGING.resolve(),
        candidate_root.resolve(),
        annual_root.resolve(),
        prior_root.resolve(),
    )
    for root in forbidden:
        if output == root or root in output.parents:
            raise DividendReconciliationError(f"refusing unsafe output path: {output}")
    if output.exists() and any(output.iterdir()):
        raise DividendReconciliationError(
            f"output directory is not empty; use a new path to preserve prior evidence: {output}"
        )
    return output


def _manifest_payload(output_root: Path, *, reviewed_at: str) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(item for item in output_root.rglob("*") if item.is_file()):
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


def _assert_source_matches_current_document(
    source: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    label: str,
) -> None:
    for field in ("company_id", "fiscal_year", "source_sha256", "source_local_path"):
        if str(source.get(field) or "") != str(current.get(field) or ""):
            raise DividendReconciliationError(f"{label} source drifted at {field}")


def _validate_candidate_component(
    annual_index: AnnualReportIndex,
    inventory: Mapping[str, Mapping[str, Any]],
    component: Mapping[str, Any],
    *,
    company_id: str,
    expected_security_id: str,
    database: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence_id = str(component["source_candidate_id"])
    candidate = inventory[evidence_id]
    if str(candidate.get("security_id") or "") != expected_security_id:
        raise DividendReconciliationError(f"candidate security mapping mismatch: {evidence_id}")
    report_fiscal_year = int(candidate.get("report_fiscal_year") or 0)
    document = annual_index.get(company_id, report_fiscal_year)
    if Path(str(candidate.get("source_local_path") or "")).resolve() != document.path:
        raise DividendReconciliationError(f"candidate source path drifted: {evidence_id}")
    if str(candidate.get("source_sha256") or "") != str(document.metadata.get("sha256") or ""):
        raise DividendReconciliationError(f"candidate source SHA-256 drifted: {evidence_id}")
    official = annual_index.validate_candidate_page(
        company_id=company_id,
        fiscal_year=report_fiscal_year,
        page=int(candidate.get("page") or 0),
        markers=list(component.get("official_page_markers") or []),
    )
    official.update(
        {
            "evidence_id": evidence_id,
            "evidence_role": "OFFICIAL_IMPLEMENTED_ORDINARY_DIVIDEND_COMPONENT",
            "candidate_disposition": component["candidate_disposition"],
            "candidate_original_value": str(component["candidate_original_value"]),
            "accepted_component_value": str(component["value"]),
            "accepted_component_currency": str(component["currency"]),
            "original_candidate": public_candidate(candidate),
        }
    )
    event = validate_futu_event(database, component["futu_event"])
    return official, event


def _revalidate_carried_official_source(
    annual_index: AnnualReportIndex,
    source: Mapping[str, Any],
    *,
    page_required: bool,
) -> dict[str, Any]:
    company_id = str(source.get("company_id") or "")
    fiscal_year = int(source.get("fiscal_year") or 0)
    markers = list(source.get("matched_markers") or [])
    if page_required:
        validated = annual_index.validate_candidate_page(
            company_id=company_id,
            fiscal_year=fiscal_year,
            page=int(source.get("page") or 0),
            markers=markers,
        )
    else:
        validated = annual_index.validate_markers(
            company_id=company_id,
            fiscal_year=fiscal_year,
            markers=markers,
        )
    _assert_source_matches_current_document(source, validated, label="carry-forward")
    validated["evidence_role"] = (
        "CARRIED_OFFICIAL_CANDIDATE_PAGE" if page_required else "CARRIED_SUPPORTING_ANNUAL_REPORT"
    )
    return validated


def _carry_forward_distribution(
    raw: Mapping[str, Any],
    *,
    annual_index: AnnualReportIndex,
    database: Path,
    reviewed_at: str,
    prior_manifest_sha256: str,
) -> dict[str, Any]:
    distribution_id = str(raw.get("distribution_id") or "")
    if raw.get("ready_for_controlled_ledger_import") is not True:
        raise DividendReconciliationError(f"carry-forward row is not import-ready: {distribution_id}")
    if raw.get("lifecycle_status") != "PAID" or raw.get("dividend_kind") != "ORDINARY":
        raise DividendReconciliationError(f"carry-forward row is not a paid ordinary dividend: {distribution_id}")
    total = raw.get("ordinary_cash_dividend_total")
    if not isinstance(total, Mapping):
        raise DividendReconciliationError(f"carry-forward total is missing: {distribution_id}")
    old_evidence = raw.get("source_evidence")
    if not isinstance(old_evidence, Mapping):
        raise DividendReconciliationError(f"carry-forward evidence is missing: {distribution_id}")
    candidate_sources = [
        _revalidate_carried_official_source(annual_index, item, page_required=True)
        for item in old_evidence.get("candidate_annual_report_pages") or []
    ]
    support_sources = [
        _revalidate_carried_official_source(annual_index, item, page_required=False)
        for item in old_evidence.get("supporting_annual_reports") or []
    ]
    if not candidate_sources or not support_sources:
        raise DividendReconciliationError(
            f"carry-forward requires candidate-page and later-report evidence: {distribution_id}"
        )
    events = [
        validate_futu_event(
            database,
            {
                "event_key": item.get("event_key"),
                "issuer_id": item.get("issuer_id"),
                "payload_hash": item.get("payload_hash"),
                "expected_payload": item.get("payload"),
            },
        )
        for item in old_evidence.get("secondary_implementation_events") or []
    ]
    if not events:
        raise DividendReconciliationError(f"carry-forward event is missing: {distribution_id}")
    per_share = list(raw.get("per_share_components") or [])
    component: dict[str, Any] = {
        "component_id": f"FY{raw['fiscal_year']}_ANNUAL",
        "component": "ANNUAL",
        "amount_method": "OFFICIAL_TOTAL",
        "value": str(total["value"]),
        "currency": str(total["currency"]),
        "source_reconciliation": "dividend-v1",
    }
    if per_share:
        component.update(
            {
                "per_share_value": str(per_share[0].get("value") or ""),
                "share_basis": per_share[0].get("share_basis"),
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "reviewed_at": reviewed_at,
        "distribution_id": distribution_id,
        "company_id": raw["company_id"],
        "company_name": raw["company_name"],
        "fiscal_year": int(raw["fiscal_year"]),
        "dividend_kind": "ORDINARY",
        "lifecycle_status": "PAID",
        "calculation_method": "DIRECT_OFFICIAL_IMPLEMENTED_TOTAL",
        "import_scope": "FISCAL_YEAR_TOTAL",
        "ready_for_controlled_ledger_import": True,
        "ordinary_cash_dividend_total": dict(total),
        "ordinary_components": [component],
        "source_candidate_ids": list(raw.get("source_candidate_ids") or []),
        "source_evidence": {
            "official_annual_report_pages": candidate_sources + support_sources,
            "secondary_implementation_events": events,
        },
        "source_reconciliation": {
            "schema_version": raw.get("schema_version"),
            "distribution_id": distribution_id,
            "manifest_sha256": prior_manifest_sha256,
            "revalidated_against_current_annual_report_manifests": True,
        },
        "notes_zh": list(raw.get("notes_zh") or []),
        "writes_production": False,
    }
    distribution_total(payload)
    return payload


def build(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config).resolve()
    candidate_root = Path(args.candidate_root).resolve()
    annual_root = Path(args.annual_root).resolve()
    database = Path(args.futu_database).resolve()
    prior_root = Path(args.prior_reconciliation).resolve()
    output_root = _safe_empty_output_root(
        Path(args.output_root),
        candidate_root=candidate_root,
        annual_root=annual_root,
        prior_root=prior_root,
    )
    config = _load_object(config_path)
    source_candidate_manifest = verify_file_manifest(
        candidate_root,
        expected_manifest_sha256=str(config["expected_candidate_manifest_sha256"]),
    )
    prior_manifest = verify_file_manifest(
        prior_root,
        expected_manifest_sha256=str(config["expected_prior_reconciliation_manifest_sha256"]),
    )
    inventory = load_candidate_inventory(candidate_root)
    company_ids = _candidate_company_ids(candidate_root)
    targets = load_recent_fiscal_year_targets(
        annual_root,
        company_ids,
        maximum_years=int(config.get("maximum_target_fiscal_years") or 5),
    )
    validate_v2_review_config(config, inventory, targets)
    annual_index = AnnualReportIndex(
        annual_root,
        minimum_pages=int(config.get("minimum_annual_report_pages") or 60),
        identity_fragments=config.get("identity_fragments") or {},
    )
    reviewed_at = str(config["reviewed_at"])

    prior_by_id: dict[str, dict[str, Any]] = {}
    for path in sorted((prior_root / "distributions").glob("*.json")):
        item = _load_object(path)
        prior_by_id[str(item.get("distribution_id") or "")] = item
    distribution_outputs: list[dict[str, Any]] = []
    for distribution_id in config.get("carry_forward_distribution_ids") or []:
        if distribution_id not in prior_by_id:
            raise DividendReconciliationError(f"carry-forward distribution missing: {distribution_id}")
        distribution_outputs.append(
            _carry_forward_distribution(
                prior_by_id[distribution_id],
                annual_index=annual_index,
                database=database,
                reviewed_at=reviewed_at,
                prior_manifest_sha256=prior_manifest["manifest_sha256"],
            )
        )

    expected_security_ids = dict(config["expected_security_ids"])
    for raw in config.get("new_distributions") or []:
        official_sources: list[dict[str, Any]] = []
        implementation_events: list[dict[str, Any]] = []
        components: list[dict[str, Any]] = []
        for component_raw in raw.get("ordinary_components") or []:
            component = dict(component_raw)
            official, event = _validate_candidate_component(
                annual_index,
                inventory,
                component,
                company_id=str(raw["company_id"]),
                expected_security_id=str(expected_security_ids[raw["company_id"]]),
                database=database,
            )
            official_sources.append(official)
            implementation_events.append(event)
            component.pop("official_page_markers", None)
            component.pop("futu_event", None)
            components.append(component)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "reviewed_at": reviewed_at,
            **{key: value for key, value in raw.items() if key != "ordinary_components"},
            "ordinary_components": components,
            "source_candidate_ids": [item["source_candidate_id"] for item in components],
            "source_evidence": {
                "official_annual_report_pages": official_sources,
                "secondary_implementation_events": implementation_events,
            },
            "writes_production": False,
        }
        distribution_total(payload)
        distribution_outputs.append(payload)

    accepted_keys = {
        (str(item["company_id"]), int(item["fiscal_year"])) for item in distribution_outputs
    }
    candidates_by_company: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for candidate in inventory.values():
        candidates_by_company[str(candidate.get("company_id") or "")].append(candidate)
    blocked_outputs: dict[str, dict[str, Any]] = {}
    blocker_counts: Counter[str] = Counter()
    for company_id, target in sorted(targets.items()):
        blocked = []
        for fiscal_year in target.fiscal_years:
            if (company_id, fiscal_year) in accepted_keys:
                continue
            row = blocker_for_candidates(
                company_id, fiscal_year, candidates_by_company.get(company_id, [])
            )
            blocked.append(row)
            blocker_counts[str(row["reason_code"])] += 1
        blocked_outputs[company_id] = {
            "schema_version": SCHEMA_VERSION,
            "company_id": company_id,
            "company_name": target.company_name,
            "security_id": target.security_id,
            "market": target.market,
            "target_fiscal_years": list(target.fiscal_years),
            "blocked": blocked,
            "writes_production": False,
        }

    expected_ready = int(config["expected_ready_count"])
    expected_blocked = int(config["expected_blocked_count"])
    blocked_count = sum(len(item["blocked"]) for item in blocked_outputs.values())
    if len(distribution_outputs) != expected_ready or blocked_count != expected_blocked:
        raise DividendReconciliationError(
            f"ready/blocked balance changed: ready={len(distribution_outputs)}, blocked={blocked_count}"
        )

    for item in distribution_outputs:
        atomic_write_json(
            output_root / "distributions" / f"{item['distribution_id']}.json", item
        )
    for company_id, item in blocked_outputs.items():
        atomic_write_json(output_root / "blocked" / f"{company_id}.json", item)

    report = {
        "schema_version": SCHEMA_VERSION,
        "reviewed_at": reviewed_at,
        "scope": {
            "company_count": len(targets),
            "target_fiscal_year_slot_count": sum(
                len(item.fiscal_years) for item in targets.values()
            ),
            "maximum_fiscal_years_per_company": int(config["maximum_target_fiscal_years"]),
            "ready_for_controlled_ledger_import_count": len(distribution_outputs),
            "newly_reconciled_ready_count": len(config.get("new_distributions") or []),
            "carried_forward_ready_count": len(
                config.get("carry_forward_distribution_ids") or []
            ),
            "blocked_count": blocked_count,
        },
        "source_manifests": {
            "dividend_candidates": source_candidate_manifest,
            "prior_dividend_reconciliation": prior_manifest,
        },
        "target_fiscal_years": {
            company_id: list(item.fiscal_years) for company_id, item in sorted(targets.items())
        },
        "ready_for_controlled_ledger_import": [
            {
                "distribution_id": item["distribution_id"],
                "company_id": item["company_id"],
                "company_name": item["company_name"],
                "fiscal_year": item["fiscal_year"],
                "ordinary_cash_dividend_total": item["ordinary_cash_dividend_total"],
                "calculation_method": item["calculation_method"],
            }
            for item in sorted(
                distribution_outputs,
                key=lambda value: (str(value["company_id"]), int(value["fiscal_year"])),
            )
        ],
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "corrected_candidate_amounts": [
            {
                "distribution_id": item["distribution_id"],
                "candidate_value": item["ordinary_components"][0]["candidate_original_value"],
                "implemented_value": item["ordinary_components"][0]["value"],
                "reason": "proposal amount changed before implementation",
            }
            for item in distribution_outputs
            if item.get("ordinary_components")
            and item["ordinary_components"][0].get("candidate_disposition")
            == "REJECT_PROPOSAL_AMOUNT_USE_IMPLEMENTED_TOTAL_SAME_PAGE"
        ],
        "safety": {
            "production_staging_modified": False,
            "unknown_values_are_not_zero": True,
            "proposed_values_are_not_importable": True,
            "special_dividends_are_excluded": True,
            "component_only_values_are_not_full_year_totals": True,
            "all_accepted_amounts_use_decimal": True,
            "official_pdf_sha256_and_page_evidence_revalidated": True,
            "secondary_events_are_auxiliary_not_primary": True,
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--candidate-root", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--annual-root", default=str(DEFAULT_ANNUAL_REPORTS))
    parser.add_argument("--futu-database", default=str(DEFAULT_FUTU_DATABASE))
    parser.add_argument("--prior-reconciliation", default=str(DEFAULT_PRIOR_RECONCILIATION))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        result = build(parse_args(argv))
    except DividendReconciliationError as error:
        print(json.dumps({"status": "ERROR", "error": str(error)}, ensure_ascii=False))
        return 1
    report = result["report"]
    print(
        json.dumps(
            {
                "status": "VALID",
                "scope": report["scope"],
                "blocker_counts": report["blocker_counts"],
                "manifest": result["verification"],
                "writes_production": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
