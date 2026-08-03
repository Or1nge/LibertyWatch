#!/usr/bin/env python3
"""Build a recent-five-year official-first CFO/capex coverage ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


WEBAPP_ROOT = Path(__file__).resolve().parents[2]
LIBERTY_ROOT = WEBAPP_ROOT.parent
sys.path.insert(0, str(WEBAPP_ROOT))

from liberty_v2.cashflow_reconciliation import (  # noqa: E402
    evidence_rows,
    official_document_checks,
    pdfinfo_pages,
    sha256_file,
    statement_year_present,
    verify_futu_payload,
)
from liberty_v2.cashflow_reconciliation_v2 import (  # noqa: E402
    FIELD_NAMES,
    SCHEMA_VERSION,
    CashflowCoverageReconciliationError,
    accepted_status,
    adjacent_reconciliations,
    classify_coverage_decision,
    exact_decimal_equal,
    extract_official_first_cashflow_report,
)
from liberty_v2.official_cashflow_candidates import (  # noqa: E402
    build_futu_reference,
    pdftotext_layout,
)
from liberty_v2.snapshot_store import atomic_write_json  # noqa: E402


DEFAULT_CANDIDATES = (
    LIBERTY_ROOT
    / "data"
    / "shareholder-v2"
    / "backfill-output"
    / "official-cashflow-candidates-v1"
)
DEFAULT_V1 = LIBERTY_ROOT / "data" / "shareholder-v2" / "reconciliation" / "cashflow-v1"
DEFAULT_OFFICIAL = LIBERTY_ROOT / "data" / "raw" / "annual_reports" / "official_backfill_v1"
DEFAULT_FUTU = (
    LIBERTY_ROOT / "data" / "shareholder-v2" / "source-evidence" / "futu-financials"
)
DEFAULT_OUTPUT = LIBERTY_ROOT / "data" / "shareholder-v2" / "reconciliation" / "cashflow-v2"
PRODUCTION_STAGING = LIBERTY_ROOT / "data" / "shareholder-v2" / "staging"


def _load_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CashflowCoverageReconciliationError(f"expected JSON object: {path}")
    return payload


def _safe_output(path: Path) -> Path:
    resolved = path.resolve()
    protected = (
        PRODUCTION_STAGING.resolve(),
        DEFAULT_CANDIDATES.resolve(),
        DEFAULT_V1.resolve(),
        DEFAULT_OFFICIAL.resolve(),
        DEFAULT_FUTU.resolve(),
    )
    if any(resolved == root or root in resolved.parents for root in protected):
        raise CashflowCoverageReconciliationError(
            "refusing to write into staging, v1 reconciliation, or source evidence"
        )
    return resolved


def _verify_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    manifest = _load_object(manifest_path)
    declared: set[str] = set()
    for raw in manifest.get("files", []):
        if not isinstance(raw, Mapping):
            raise CashflowCoverageReconciliationError("manifest file entry is invalid")
        path = (root / str(raw.get("path") or "")).resolve()
        if root not in path.parents or not path.is_file():
            raise CashflowCoverageReconciliationError(f"unsafe/missing manifest input: {path}")
        if path.stat().st_size != int(raw.get("size_bytes") or -1):
            raise CashflowCoverageReconciliationError(f"manifest size mismatch: {path}")
        if sha256_file(path) != str(raw.get("sha256") or ""):
            raise CashflowCoverageReconciliationError(f"manifest SHA mismatch: {path}")
        declared.add(path.relative_to(root).as_posix())
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if declared != actual or len(declared) != int(manifest.get("file_count") or -1):
        raise CashflowCoverageReconciliationError("manifest does not exactly cover input files")
    return {
        "path": str(manifest_path.resolve()),
        "sha256": sha256_file(manifest_path),
        "file_count": len(declared),
    }


def _candidate_company_ids(root: Path) -> set[str]:
    result = {path.stem for path in (root / "candidates").glob("*.json")}
    if len(result) != 56:
        raise CashflowCoverageReconciliationError(
            f"expected 56 companies, got {len(result)}"
        )
    return result


def _official_index(
    root: Path,
) -> tuple[dict[tuple[str, int], dict[str, Any]], dict[str, str]]:
    selected: dict[tuple[str, int], dict[str, Any]] = {}
    manifest_hashes: dict[str, str] = {}
    for manifest_path in sorted(root.glob("companies/*/manifest.json")):
        manifest = _load_object(manifest_path)
        manifest_sha = sha256_file(manifest_path)
        manifest_hashes[str(manifest_path.resolve())] = manifest_sha
        for raw in manifest.get("documents", []):
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            if (
                item.get("selection_status") != "SELECTED_CURRENT"
                or item.get("data_status") != "VERIFIED"
            ):
                continue
            item["source_manifest_path"] = str(manifest_path.resolve())
            item["source_manifest_sha256"] = manifest_sha
            item["resolved_local_path"] = str(
                (root / str(item.get("local_path") or "")).resolve()
            )
            key = (str(item["company_id"]), int(item["fiscal_year"]))
            if key in selected:
                raise CashflowCoverageReconciliationError(
                    f"duplicate selected report: {key}"
                )
            selected[key] = item
    return selected, manifest_hashes


def _futu_inputs(
    root: Path, company_ids: set[str]
) -> tuple[
    dict[str, dict[int, dict[str, Any]]],
    dict[str, dict[str, Any]],
    dict[str, list[int]],
]:
    references: dict[str, dict[int, dict[str, Any]]] = {}
    verifications: dict[str, dict[str, Any]] = {}
    recent_years: dict[str, list[int]] = {}
    for company_id in sorted(company_ids):
        path = root / company_id / "latest.json"
        payload = _load_object(path)
        verified = verify_futu_payload(payload, path)
        if verified.get("issuer_id") != company_id or not verified.get("all_passed"):
            raise CashflowCoverageReconciliationError(
                f"Futu evidence integrity/identity failed: {company_id}"
            )
        reference = build_futu_reference(payload, evidence_path=path)
        years = sorted(reference, reverse=True)[:5]
        if len(years) != 5:
            raise CashflowCoverageReconciliationError(
                f"Futu recent-five scope incomplete: {company_id}/{years}"
            )
        references[company_id] = reference
        verifications[company_id] = verified
        recent_years[company_id] = years
    return references, verifications, recent_years


def _v1_accepts(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    manifest = _verify_manifest(root)
    ledger = _load_object(root / "ledger.json")
    decisions = ledger.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != 268:
        raise CashflowCoverageReconciliationError("cashflow-v1 ledger scope is not 268")
    accepted: dict[str, dict[str, Any]] = {}
    for raw in decisions:
        item = dict(raw)
        if item.get("decision") != "ACCEPT" or any(
            value is not True for value in (item.get("checks") or {}).values()
        ):
            raise CashflowCoverageReconciliationError(
                "cashflow-v1 contains a non-ACCEPT or failed check"
            )
        accepted[str(item["decision_id"])] = item
    return accepted, manifest


def _extract_one(
    metadata: Mapping[str, Any], timeout: int
) -> dict[str, Any]:
    pdf_path = Path(str(metadata["resolved_local_path"]))
    actual_sha = sha256_file(pdf_path)
    pages = pdfinfo_pages(pdf_path, timeout_seconds=timeout)
    text = pdftotext_layout(pdf_path, timeout_seconds=timeout)
    try:
        document = official_document_checks(
            text,
            metadata,
            pdf_path,
            actual_pages=pages,
            actual_sha256=actual_sha,
        )
        extracted = extract_official_first_cashflow_report(
            text, {**metadata, "local_path": str(pdf_path)}
        )
        year_checks = {
            field_name: statement_year_present(
                text,
                [
                    int(row["page"])
                    for row in evidence_rows(
                        field_name, extracted["fields"][field_name]
                    )
                ],
                int(metadata["fiscal_year"]),
            )
            for field_name in FIELD_NAMES
        }
        return {
            "metadata": dict(metadata),
            "document_validation": document,
            "statement_year_checks": year_checks,
            "extracted": extracted,
        }
    finally:
        del text


def _source_record(
    processed: Mapping[str, Any], field_name: str
) -> dict[str, Any]:
    metadata = processed["metadata"]
    field = processed["extracted"]["fields"][field_name]
    return {
        "source_name": metadata["source_name"],
        "source_document": metadata["source_document"],
        "source_url": metadata["source_url"],
        "source_publish_date": metadata["source_publish_date"],
        "source_fetch_time": metadata["source_fetch_time"],
        "restatement_status": metadata["restatement_status"],
        "security_id": metadata["security_id"],
        "share_class": metadata["share_class"],
        "source_local_path": metadata["resolved_local_path"],
        "source_sha256": metadata["sha256"],
        "source_manifest_path": metadata["source_manifest_path"],
        "source_manifest_sha256": metadata["source_manifest_sha256"],
        "fiscal_year": metadata["fiscal_year"],
        "fiscal_year_end_date": metadata["fiscal_year_end_date"],
        "currency": field.get("currency"),
        "unit": field.get("unit"),
        "current_value": field.get("current_value"),
        "comparative_value": field.get("comparative_value"),
        "definition_basis": field.get("definition_basis"),
        "evidence_rows": [
            {
                "page": row.get("page"),
                "page_line": row.get("page_line"),
                "text_line": row.get("text_line"),
                "line_excerpt": row.get("line_excerpt"),
                "source_unit_label": row.get("source_unit_label"),
                "unit_multiplier": row.get("unit_multiplier"),
                "wrapped_line_count": row.get("wrapped_line_count"),
            }
            for row in evidence_rows(field_name, field)
        ],
    }


def _write_manifest(output_root: Path, paths: list[Path]) -> None:
    manifest = {
        "schema_version": "cashflow-coverage-reconciliation-manifest-v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(paths),
        "files": [
            {
                "path": path.relative_to(output_root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in sorted(paths)
        ],
    }
    atomic_write_json(output_root / "manifest.json", manifest)


def run(args: argparse.Namespace) -> int:
    candidate_root = Path(args.candidate_root).resolve()
    v1_root = Path(args.v1_root).resolve()
    official_root = Path(args.official_root).resolve()
    futu_root = Path(args.futu_root).resolve()
    output_root = _safe_output(Path(args.output_root))
    candidate_manifest = _verify_manifest(candidate_root)
    company_ids = _candidate_company_ids(candidate_root)
    v1_accepted, v1_manifest = _v1_accepts(v1_root)
    official, official_manifest_hashes = _official_index(official_root)
    references, futu_verifications, recent_years = _futu_inputs(
        futu_root, company_ids
    )

    needed: set[tuple[str, int]] = set()
    for company_id, years in recent_years.items():
        for year in years:
            for adjacent_year in (year - 1, year, year + 1):
                if (company_id, adjacent_year) in official:
                    needed.add((company_id, adjacent_year))
    processed: dict[tuple[str, int], dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {
            executor.submit(_extract_one, official[key], args.timeout): key
            for key in needed
        }
        for position, future in enumerate(as_completed(future_map), start=1):
            key = future_map[future]
            try:
                processed[key] = future.result()
                status = "REEXTRACTED"
            except Exception as error:
                errors.append(
                    {
                        "company_id": key[0],
                        "fiscal_year": key[1],
                        "error": str(error)[:1000],
                    }
                )
                status = "ERROR"
            if position == 1 or position % 25 == 0 or position == len(future_map):
                print(
                    json.dumps(
                        {
                            "progress": f"{position}/{len(future_map)}",
                            "company_id": key[0],
                            "fiscal_year": key[1],
                            "status": status,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    decisions: list[dict[str, Any]] = []
    matrix_companies: list[dict[str, Any]] = []
    for company_id in sorted(company_ids):
        company_reports = {
            year: item["extracted"]
            for (item_company, year), item in processed.items()
            if item_company == company_id
        }
        matrix_rows = []
        for fiscal_year in recent_years[company_id]:
            row = {
                "fiscal_year": fiscal_year,
                "fiscal_year_end_date": references[company_id][fiscal_year][
                    "fiscal_year_end_date"
                ],
                "fiscal_period": references[company_id][fiscal_year][
                    "fiscal_period"
                ],
                "period_type": "FULL_YEAR",
                "lease_principal_repayment": None,
                "lease_principal_repayment_status": "NOT_EXTRACTED_DO_NOT_ASSUME_ZERO",
            }
            for field_name in FIELD_NAMES:
                decision_id = f"{company_id}:{fiscal_year}:{field_name}"
                processed_report = processed.get((company_id, fiscal_year))
                official_field = (
                    processed_report["extracted"]["fields"][field_name]
                    if processed_report is not None
                    else None
                )
                adjacent = adjacent_reconciliations(
                    company_reports, fiscal_year, field_name
                )
                status, accepted_value, reasons = classify_coverage_decision(
                    official_field,
                    references[company_id][fiscal_year]["fields"][field_name],
                    adjacent,
                    accepted_by_v1=decision_id in v1_accepted,
                )
                document_checks: dict[str, bool] = {}
                statement_year_check = False
                source = None
                if processed_report is not None:
                    document_checks = dict(
                        processed_report["document_validation"]["checks"]
                    )
                    statement_year_check = bool(
                        processed_report["statement_year_checks"][field_name]
                    )
                    source = _source_record(processed_report, field_name)
                    if not all(document_checks.values()) or (
                        isinstance(official_field, Mapping)
                        and official_field.get("current_value") is not None
                        and not statement_year_check
                    ):
                        status, accepted_value, reasons = (
                            "REVIEW",
                            None,
                            ["OFFICIAL_DOCUMENT_OR_STATEMENT_YEAR_CHECK_FAILED"],
                        )
                futu_field = references[company_id][fiscal_year]["fields"][field_name]
                futu_exact = bool(
                    official_field
                    and official_field.get("current_value") is not None
                    and futu_field.get("data_status") in {"VALID", "KNOWN_ZERO"}
                    and official_field.get("currency") == futu_field.get("currency")
                    and exact_decimal_equal(
                        official_field.get("current_value"), futu_field.get("value")
                    )
                )
                if decision_id in v1_accepted and accepted_status(status):
                    if not exact_decimal_equal(
                        accepted_value, v1_accepted[decision_id]["accepted_value"]
                    ):
                        status, accepted_value, reasons = (
                            "CONFLICT",
                            None,
                            ["V1_AND_V2_OFFICIAL_VALUE_CONFLICT"],
                        )
                decisions.append(
                    {
                        "decision_id": decision_id,
                        "company_id": company_id,
                        "company_name": (
                            processed_report["metadata"].get("company_name")
                            if processed_report is not None
                            else None
                        ),
                        "fiscal_year": fiscal_year,
                        "field": field_name,
                        "status": status,
                        "accepted_value": accepted_value,
                        "currency": source.get("currency") if source else None,
                        "reason_codes": reasons,
                        "official_source": source,
                        "official_document_checks": document_checks,
                        "statement_fiscal_year_check": statement_year_check,
                        "adjacent_reconciliation": adjacent,
                        "futu_reconciliation": (
                            "MATCH"
                            if futu_exact
                            else "MISSING"
                            if futu_field.get("data_status") not in {"VALID", "KNOWN_ZERO"}
                            else "MISMATCH_OR_OFFICIAL_UNAVAILABLE"
                        ),
                        "futu_source": {
                            **futu_verifications[company_id],
                            "fiscal_year": fiscal_year,
                            "fiscal_year_end_date": references[company_id][fiscal_year][
                                "fiscal_year_end_date"
                            ],
                            "fiscal_period": references[company_id][fiscal_year][
                                "fiscal_period"
                            ],
                            "field_value": futu_field.get("value"),
                            "field_currency": futu_field.get("currency"),
                            "field_data_status": futu_field.get("data_status"),
                            "provider_fields": futu_field.get("provider_fields", []),
                        },
                        "accepted_by_cashflow_v1": decision_id in v1_accepted,
                        "eligible_for_read_only_import": accepted_status(status),
                        "writes_production": False,
                    }
                )
                row[field_name] = accepted_value if accepted_status(status) else None
                row[f"{field_name}_status"] = status
            matrix_rows.append(row)
        matrix_companies.append(
            {
                "company_id": company_id,
                "recent_five_fiscal_years": matrix_rows,
            }
        )

    status_counts = {
        field_name: dict(
            Counter(
                item["status"]
                for item in decisions
                if item["field"] == field_name
            )
        )
        for field_name in FIELD_NAMES
    }
    official_complete = {}
    for field_name in FIELD_NAMES:
        official_complete[field_name] = sum(
            all(
                accepted_status(row[f"{field_name}_status"])
                for row in company["recent_five_fiscal_years"]
            )
            for company in matrix_companies
        )
    complete_both = sum(
        all(
            accepted_status(row["operating_cash_flow_status"])
            and accepted_status(row["capital_expenditure_status"])
            for row in company["recent_five_fiscal_years"]
        )
        for company in matrix_companies
    )
    blockers = {
        field_name: {
            "FUTU_ONLY": status_counts[field_name].get("FUTU_ONLY", 0),
            "CONFLICT": status_counts[field_name].get("CONFLICT", 0),
            "REVIEW": status_counts[field_name].get("REVIEW", 0),
            "INSUFFICIENT_DATA": status_counts[field_name].get(
                "INSUFFICIENT_DATA", 0
            ),
        }
        for field_name in FIELD_NAMES
    }
    manifest_set_sha = hashlib.sha256(
        json.dumps(
            official_manifest_hashes, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    created_at = datetime.now(timezone.utc).isoformat()
    ledger = {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at,
        "policy": {
            "recent_complete_fiscal_year_count": 5,
            "accepted_statuses": [
                "ACCEPT_V1",
                "ACCEPT_OFFICIAL_ADJACENT",
                "ACCEPT_OFFICIAL_PLUS_FUTU",
            ],
            "amount_comparison": "Decimal exact equality; no tolerance",
            "lease_principal_repayment": "NOT_EXTRACTED_DO_NOT_ASSUME_ZERO",
        },
        "candidate_input_manifest": candidate_manifest,
        "cashflow_v1_input_manifest": v1_manifest,
        "official_source_manifest_set_sha256": manifest_set_sha,
        "company_count": len(company_ids),
        "decision_count": len(decisions),
        "decisions": decisions,
        "writes_production": False,
    }
    matrix = {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at,
        "company_count": len(matrix_companies),
        "company_year_count": sum(
            len(item["recent_five_fiscal_years"]) for item in matrix_companies
        ),
        "companies": matrix_companies,
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at,
        "company_count": len(company_ids),
        "company_year_count": 280,
        "decision_count": len(decisions),
        "reextracted_pdf_count": len(processed),
        "reextraction_error_count": len(errors),
        "reextraction_errors": errors,
        "status_counts": status_counts,
        "blocker_counts": blockers,
        "companies_with_complete_official_recent_five": official_complete,
        "companies_with_complete_official_cfo_and_capex_recent_five": complete_both,
        "lease_principal_repayment": {
            "accepted_count": 0,
            "status": "NOT_EXTRACTED_DO_NOT_ASSUME_ZERO",
        },
        "safety": [
            "FUTU_ONLY values remain secondary evidence and are never imported as official points.",
            "Any official/Futu or adjacent-report mismatch remains CONFLICT.",
            "Missing lease principal repayment is never converted to zero.",
            "This reconciliation never writes production staging or cashflow-v1.",
        ],
    }
    paths = []
    for name, payload in (
        ("ledger.json", ledger),
        ("coverage_matrix.json", matrix),
        ("report.json", report),
    ):
        path = output_root / name
        atomic_write_json(path, payload)
        paths.append(path)
    _write_manifest(output_root, paths)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


def verify(output_root: Path) -> dict[str, Any]:
    descriptor = _verify_manifest(output_root.resolve())
    result = {"status": "VALID", **descriptor}
    print(json.dumps(result, ensure_ascii=False))
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("run", "verify"), nargs="?", default="run")
    result.add_argument("--candidate-root", default=str(DEFAULT_CANDIDATES))
    result.add_argument("--v1-root", default=str(DEFAULT_V1))
    result.add_argument("--official-root", default=str(DEFAULT_OFFICIAL))
    result.add_argument("--futu-root", default=str(DEFAULT_FUTU))
    result.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    result.add_argument("--workers", type=int, default=2)
    result.add_argument("--timeout", type=int, default=120)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "verify":
        verify(Path(args.output_root))
        return 0
    if args.workers < 1 or args.timeout < 1:
        raise SystemExit("workers and timeout must be positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
