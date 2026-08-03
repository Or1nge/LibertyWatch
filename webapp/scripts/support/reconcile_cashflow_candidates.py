#!/usr/bin/env python3
"""Independently review official CFO/capex candidates without production writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


WEBAPP_ROOT = Path(__file__).resolve().parents[2]
LIBERTY_ROOT = WEBAPP_ROOT.parent
sys.path.insert(0, str(WEBAPP_ROOT))

from liberty_v2.cashflow_reconciliation import (  # noqa: E402
    FIELD_NAMES,
    SCHEMA_VERSION,
    CashflowReconciliationError,
    compare_reextracted_field,
    decision_from_checks,
    evidence_rows,
    metadata_matches_candidate,
    metadata_semantic_anomaly,
    official_document_checks,
    pdfinfo_pages,
    sha256_file,
    statement_year_present,
    verify_futu_payload,
)
from liberty_v2.official_cashflow_candidates import (  # noqa: E402
    build_futu_reference,
    extract_official_cashflow_report,
    pdftotext_layout,
    reconcile_company_reports,
)
from liberty_v2.snapshot_store import atomic_write_json  # noqa: E402


DEFAULT_CANDIDATES = (
    LIBERTY_ROOT
    / "data"
    / "shareholder-v2"
    / "backfill-output"
    / "official-cashflow-candidates-v1"
)
DEFAULT_OFFICIAL = LIBERTY_ROOT / "data" / "raw" / "annual_reports" / "official_backfill_v1"
DEFAULT_FUTU = (
    LIBERTY_ROOT / "data" / "shareholder-v2" / "source-evidence" / "futu-financials"
)
DEFAULT_OUTPUT = LIBERTY_ROOT / "data" / "shareholder-v2" / "reconciliation" / "cashflow-v1"
PRODUCTION_STAGING = LIBERTY_ROOT / "data" / "shareholder-v2" / "staging"


def _safe_output_root(path: Path) -> Path:
    resolved = path.resolve()
    protected = (
        PRODUCTION_STAGING.resolve(),
        DEFAULT_CANDIDATES.resolve(),
        DEFAULT_OFFICIAL.resolve(),
        DEFAULT_FUTU.resolve(),
    )
    if any(resolved == root or root in resolved.parents for root in protected):
        raise CashflowReconciliationError(
            "refusing to write into production staging, candidates, or immutable evidence"
        )
    return resolved


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CashflowReconciliationError(f"expected JSON object: {path}")
    return payload


def _verify_input_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    manifest = _load_json(manifest_path)
    declared: set[str] = set()
    for raw in manifest.get("files", []):
        item = dict(raw)
        relative = str(item.get("path") or "")
        path = (root / relative).resolve()
        if not relative or root.resolve() not in path.parents or not path.is_file():
            raise CashflowReconciliationError(f"unsafe or missing candidate file: {path}")
        if path.stat().st_size != int(item.get("size_bytes") or -1):
            raise CashflowReconciliationError(f"candidate size mismatch: {path}")
        if sha256_file(path) != str(item.get("sha256") or ""):
            raise CashflowReconciliationError(f"candidate SHA-256 mismatch: {path}")
        declared.add(relative)
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if declared != actual or len(declared) != int(manifest.get("file_count") or -1):
        raise CashflowReconciliationError("candidate manifest does not exactly cover its files")
    return {
        "path": str(manifest_path.resolve()),
        "sha256": sha256_file(manifest_path),
        "file_count": len(declared),
    }


def _official_documents(
    root: Path,
) -> tuple[dict[tuple[str, int], dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    selected: dict[tuple[str, int], dict[str, Any]] = {}
    all_documents: list[dict[str, Any]] = []
    manifest_hashes: dict[str, str] = {}
    for manifest_path in sorted(root.glob("companies/*/manifest.json")):
        manifest = _load_json(manifest_path)
        manifest_hash = sha256_file(manifest_path)
        manifest_hashes[str(manifest_path.resolve())] = manifest_hash
        for raw in manifest.get("documents", []):
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            item["source_manifest_path"] = str(manifest_path.resolve())
            item["source_manifest_sha256"] = manifest_hash
            item["resolved_local_path"] = str((root / str(item["local_path"])).resolve())
            all_documents.append(item)
            if (
                item.get("selection_status") == "SELECTED_CURRENT"
                and item.get("data_status") == "VERIFIED"
            ):
                key = (str(item["company_id"]), int(item["fiscal_year"]))
                if key in selected:
                    raise CashflowReconciliationError(f"duplicate selected official report: {key}")
                selected[key] = item
    return selected, all_documents, manifest_hashes


def _candidate_payloads(root: Path) -> dict[str, dict[str, Any]]:
    payloads = {
        path.stem: _load_json(path) for path in sorted((root / "candidates").glob("*.json"))
    }
    if len(payloads) != 56:
        raise CashflowReconciliationError(f"expected 56 candidate companies, got {len(payloads)}")
    return payloads


def _futu_sources(
    root: Path, company_ids: set[str]
) -> tuple[dict[str, dict[int, dict[str, Any]]], dict[str, dict[str, Any]]]:
    references: dict[str, dict[int, dict[str, Any]]] = {}
    verifications: dict[str, dict[str, Any]] = {}
    for company_id in sorted(company_ids):
        path = root / company_id / "latest.json"
        payload = _load_json(path)
        verified = verify_futu_payload(payload, path)
        if str(verified.get("issuer_id")) != company_id:
            verified["checks"]["issuer_id_exact"] = False
            verified["all_passed"] = False
        else:
            verified["checks"]["issuer_id_exact"] = True
        references[company_id] = build_futu_reference(payload, evidence_path=path)
        verifications[company_id] = verified
    return references, verifications


def _extract_one(
    metadata: Mapping[str, Any],
    futu_year: Mapping[str, Any] | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    pdf_path = Path(str(metadata["resolved_local_path"]))
    actual_sha = sha256_file(pdf_path)
    actual_pages = pdfinfo_pages(pdf_path, timeout_seconds=timeout_seconds)
    text = pdftotext_layout(pdf_path, timeout_seconds=timeout_seconds)
    try:
        document = official_document_checks(
            text,
            metadata,
            pdf_path,
            actual_pages=actual_pages,
            actual_sha256=actual_sha,
        )
        enriched = {**metadata, "local_path": str(pdf_path)}
        extracted = extract_official_cashflow_report(text, enriched, futu_year)
        year_checks = {
            field_name: statement_year_present(
                text,
                [row["page"] for row in evidence_rows(field_name, extracted["fields"][field_name])],
                int(metadata["fiscal_year"]),
            )
            for field_name in FIELD_NAMES
        }
        return {
            "metadata": dict(metadata),
            "document_validation": document,
            "extracted": extracted,
            "statement_year_checks": year_checks,
        }
    finally:
        del text


def _source_record(
    processed: Mapping[str, Any], field_name: str, field: Mapping[str, Any]
) -> dict[str, Any]:
    metadata = processed["metadata"]
    return {
        "source_document": metadata["source_document"],
        "source_url": metadata["source_url"],
        "source_publish_date": metadata["source_publish_date"],
        "source_local_path": metadata["resolved_local_path"],
        "source_sha256": metadata["sha256"],
        "source_manifest_path": metadata["source_manifest_path"],
        "source_manifest_sha256": metadata["source_manifest_sha256"],
        "fiscal_year": metadata["fiscal_year"],
        "fiscal_year_end_date": metadata["fiscal_year_end_date"],
        "currency": field.get("currency"),
        "value": field.get("current_value"),
        "evidence_rows": [
            {
                "page": row.get("page"),
                "page_line": row.get("page_line"),
                "text_line": row.get("text_line"),
                "line_excerpt": row.get("line_excerpt"),
                "source_unit_label": row.get("source_unit_label"),
                "unit_multiplier": row.get("unit_multiplier"),
            }
            for row in evidence_rows(field_name, field)
        ],
    }


def _write_manifest(output_root: Path, paths: list[Path]) -> None:
    manifest = {
        "schema_version": "cashflow-reviewed-decision-manifest-v1",
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
    official_root = Path(args.official_root).resolve()
    futu_root = Path(args.futu_root).resolve()
    output_root = _safe_output_root(Path(args.output_root))
    candidate_manifest = _verify_input_manifest(candidate_root)
    candidate_payloads = _candidate_payloads(candidate_root)
    selected_official, all_official, source_manifest_hashes = _official_documents(official_root)
    references, futu_verifications = _futu_sources(futu_root, set(candidate_payloads))

    targets: list[tuple[str, int, str, dict[str, Any], dict[str, Any]]] = []
    candidate_reports: dict[tuple[str, int], dict[str, Any]] = {}
    for company_id, payload in candidate_payloads.items():
        for report in payload.get("reports", []):
            key = (company_id, int(report["fiscal_year"]))
            candidate_reports[key] = report
            for field_name in FIELD_NAMES:
                field = report["fields"][field_name]
                if field.get("status") == "VALID":
                    targets.append((company_id, key[1], field_name, report, field))

    observed = {
        field_name: sum(target[2] == field_name for target in targets)
        for field_name in FIELD_NAMES
    }
    if args.expected_cfo_valid is not None and observed["operating_cash_flow"] != args.expected_cfo_valid:
        raise CashflowReconciliationError(
            f"expected {args.expected_cfo_valid} CFO VALID candidates, got {observed['operating_cash_flow']}"
        )
    if args.expected_capex_valid is not None and observed["capital_expenditure"] != args.expected_capex_valid:
        raise CashflowReconciliationError(
            f"expected {args.expected_capex_valid} capex VALID candidates, got {observed['capital_expenditure']}"
        )

    needed: set[tuple[str, int]] = set()
    for company_id, year, _field_name, _report, _field in targets:
        needed.add((company_id, year))
        needed.add((company_id, year - 1))
    missing = sorted(key for key in needed if key not in selected_official or key not in candidate_reports)
    if missing:
        raise CashflowReconciliationError(f"adjacent official/candidate reports are missing: {missing[:10]}")

    processed: dict[tuple[str, int], dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {
            executor.submit(
                _extract_one,
                selected_official[key],
                references[key[0]].get(key[1]),
                args.timeout,
            ): key
            for key in needed
        }
        for position, future in enumerate(as_completed(future_map), start=1):
            key = future_map[future]
            try:
                processed[key] = future.result()
                status = "REEXTRACTED"
            except Exception as error:  # retain every other independent decision
                errors.append(
                    {"company_id": key[0], "fiscal_year": key[1], "error": str(error)[:1000]}
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
    hard_reject_suffixes = {
        "manifest_sha256_exact",
        "manifest_file_size_exact",
        "manifest_page_count_exact",
        "official_https_url",
        "official_source_level",
        "selected_current",
        "manifest_data_verified",
        "annual_report_title",
        "not_correction_summary_or_circular",
        "full_report_page_count",
        "candidate_value_exact",
        "current_value_exact",
        "comparative_value_exact",
        "currency_exact",
        "futu_match_repeated",
        "adjacent_report_match_repeated",
        "futu_payload_sha256",
        "futu_file_sha256_exact",
        "futu_payload_sha256_exact",
    }
    for company_id, year, field_name, original_report, original_field in targets:
        current_key = (company_id, year)
        prior_key = (company_id, year - 1)
        if current_key not in processed or prior_key not in processed:
            decisions.append(
                {
                    "decision_id": f"{company_id}:{year}:{field_name}",
                    "company_id": company_id,
                    "fiscal_year": year,
                    "field": field_name,
                    "decision": "REVIEW",
                    "reason_codes": ["PDF_REEXTRACTION_ERROR"],
                    "accepted_value": None,
                }
            )
            continue

        current = processed[current_key]
        prior = processed[prior_key]
        reviewed_pair = reconcile_company_reports(
            [prior["extracted"], current["extracted"]]
        )
        reviewed_current = next(row for row in reviewed_pair if int(row["fiscal_year"]) == year)
        reviewed_prior = next(row for row in reviewed_pair if int(row["fiscal_year"]) == year - 1)
        reviewed_field = reviewed_current["fields"][field_name]
        reviewed_prior_field = reviewed_prior["fields"][field_name]
        futu = futu_verifications[company_id]
        candidate_futu = original_report.get("futu_source") or {}
        current_metadata_checks = metadata_matches_candidate(
            original_report, current["metadata"]
        )
        field_checks = compare_reextracted_field(original_field, reviewed_field)
        checks: dict[str, bool] = {}
        checks.update({f"candidate_manifest_{key}": value for key, value in current_metadata_checks.items()})
        checks.update(
            {f"current_document_{key}": value for key, value in current["document_validation"]["checks"].items()}
        )
        checks.update(
            {f"prior_document_{key}": value for key, value in prior["document_validation"]["checks"].items()}
        )
        checks.update({f"field_{key}": value for key, value in field_checks.items()})
        checks.update(
            {
                "current_statement_fiscal_year": current["statement_year_checks"][field_name],
                "prior_statement_fiscal_year": prior["statement_year_checks"][field_name],
                "futu_payload_sha256": bool(futu["checks"].get("payload_sha256")),
                "futu_company_identity": bool(futu["checks"].get("issuer_id_exact")),
                "futu_file_sha256_exact": str(candidate_futu.get("source_file_sha256"))
                == str(futu.get("source_file_sha256")),
                "futu_payload_sha256_exact": str(candidate_futu.get("source_payload_sha256"))
                == str(futu.get("declared_payload_sha256")),
                "futu_fiscal_year_end_exact": str(candidate_futu.get("fiscal_year_end_date"))
                == str(original_report.get("fiscal_year_end_date")),
            }
        )
        hard_reject_keys = [
            key for key in checks if any(key.endswith(suffix) for suffix in hard_reject_suffixes)
        ]
        decision, reasons = decision_from_checks(checks, hard_reject_keys=hard_reject_keys)
        current_source = _source_record(current, field_name, reviewed_field)
        prior_source = _source_record(prior, field_name, reviewed_prior_field)
        decisions.append(
            {
                "decision_id": f"{company_id}:{year}:{field_name}",
                "company_id": company_id,
                "company_name": original_report["company_name"],
                "security_id": original_report["security_id"],
                "fiscal_year": year,
                "field": field_name,
                "decision": decision,
                "reason_codes": reasons,
                "accepted_value": str(reviewed_field["value"]) if decision == "ACCEPT" else None,
                "currency": reviewed_field.get("currency") if decision == "ACCEPT" else None,
                "checks": checks,
                "current_official_source": current_source,
                "adjacent_prior_official_source": prior_source,
                "futu_source": {
                    **futu,
                    "fiscal_year": year,
                    "fiscal_year_end_date": candidate_futu.get("fiscal_year_end_date"),
                    "fiscal_period": candidate_futu.get("fiscal_period"),
                    "field_value": reviewed_field.get("futu_reference", {}).get("value"),
                    "field_currency": reviewed_field.get("futu_reference", {}).get("currency"),
                    "provider_fields": reviewed_field.get("futu_reference", {}).get("provider_fields", []),
                },
                "candidate_only": True,
                "eligible_for_core_write": False,
            }
        )

    anomalies = []
    for metadata in all_official:
        reasons = metadata_semantic_anomaly(metadata)
        if reasons:
            target_count = sum(
                company_id == str(metadata["company_id"])
                and year == int(metadata["fiscal_year"])
                and str(_report.get("source_sha256")) == str(metadata["sha256"])
                for company_id, year, _field_name, _report, _field in targets
            )
            anomalies.append(
                {
                    "company_id": metadata["company_id"],
                    "fiscal_year": metadata["fiscal_year"],
                    "source_document": metadata["source_document"],
                    "selection_status": metadata["selection_status"],
                    "pdf_pages": metadata["pdf_pages"],
                    "source_url": metadata["source_url"],
                    "source_sha256": metadata["sha256"],
                    "source_manifest_path": metadata["source_manifest_path"],
                    "source_manifest_sha256": metadata["source_manifest_sha256"],
                    "decision": "EXCLUDE_DOCUMENT",
                    "reason_codes": reasons,
                    "target_valid_candidate_count": target_count,
                }
            )

    repaired_selections = []
    for anomaly in anomalies:
        if not str(anomaly["selection_status"]).startswith("SUPERSEDED"):
            continue
        replacement = selected_official.get(
            (str(anomaly["company_id"]), int(anomaly["fiscal_year"]))
        )
        if replacement is None:
            continue
        repaired_selections.append(
            {
                "company_id": anomaly["company_id"],
                "fiscal_year": anomaly["fiscal_year"],
                "excluded_source_document": anomaly["source_document"],
                "excluded_source_sha256": anomaly["source_sha256"],
                "replacement_source_document": replacement["source_document"],
                "replacement_source_url": replacement["source_url"],
                "replacement_source_sha256": replacement["sha256"],
                "replacement_pdf_pages": replacement["pdf_pages"],
                "source_manifest_path": replacement["source_manifest_path"],
                "source_manifest_sha256": replacement["source_manifest_sha256"],
            }
        )

    summary = {
        status: sum(item["decision"] == status for item in decisions)
        for status in ("ACCEPT", "REJECT", "REVIEW")
    }
    field_summary = {
        field_name: {
            status: sum(
                item["field"] == field_name and item["decision"] == status
                for item in decisions
            )
            for status in ("ACCEPT", "REJECT", "REVIEW")
        }
        for field_name in FIELD_NAMES
    }
    source_manifest_digest = hashlib.sha256(
        json.dumps(source_manifest_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    ledger = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate_only": True,
        "writes_production": False,
        "candidate_input_manifest": candidate_manifest,
        "official_source_manifest_set_sha256": source_manifest_digest,
        "decision_count": len(decisions),
        "decisions": decisions,
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "writes_production": False,
        "scope": {
            "company_count": len(candidate_payloads),
            "candidate_valid_counts": observed,
            "reviewed_decision_count": len(decisions),
            "reextracted_pdf_count": len(processed),
        },
        "decision_summary": summary,
        "field_summary": field_summary,
        "reextraction_error_count": len(errors),
        "reextraction_errors": errors,
        "annual_report_semantic_anomalies": anomalies,
        "repaired_source_selections": repaired_selections,
        "selected_current_semantic_anomaly_count": sum(
            item["selection_status"] == "SELECTED_CURRENT" for item in anomalies
        ),
        "excluded_or_superseded_semantic_anomaly_count": len(anomalies),
        "candidate_input_manifest": candidate_manifest,
        "official_source_manifest_set_sha256": source_manifest_digest,
        "safety": [
            "ACCEPT means exact official PDF, Futu, and adjacent-report checks passed without tolerance.",
            "The reviewed ledger remains isolated and does not write production staging.",
            "REJECT and REVIEW values remain null; missing data is never converted to zero.",
            "A source-ledger ACCEPT is not a company-level shareholder-return VALID status.",
        ],
    }
    ledger_path = output_root / "ledger.json"
    report_path = output_root / "report.json"
    atomic_write_json(ledger_path, ledger)
    atomic_write_json(report_path, report)
    _write_manifest(output_root, [ledger_path, report_path])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


def verify_output(output_root: Path) -> dict[str, Any]:
    root = output_root.resolve()
    manifest = _load_json(root / "manifest.json")
    declared = set()
    for raw in manifest.get("files", []):
        item = dict(raw)
        path = (root / str(item["path"])).resolve()
        if root not in path.parents or not path.is_file():
            raise CashflowReconciliationError(f"unsafe or missing output: {path}")
        if path.stat().st_size != int(item["size_bytes"]) or sha256_file(path) != item["sha256"]:
            raise CashflowReconciliationError(f"output manifest mismatch: {path}")
        declared.add(path.relative_to(root).as_posix())
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if actual != declared or len(declared) != int(manifest.get("file_count") or -1):
        raise CashflowReconciliationError("output manifest coverage mismatch")
    result = {"status": "VALID", "checked_file_count": len(declared)}
    print(json.dumps(result, ensure_ascii=False))
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("run", "verify"), nargs="?", default="run")
    result.add_argument("--candidate-root", default=str(DEFAULT_CANDIDATES))
    result.add_argument("--official-root", default=str(DEFAULT_OFFICIAL))
    result.add_argument("--futu-root", default=str(DEFAULT_FUTU))
    result.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    result.add_argument("--workers", type=int, default=2)
    result.add_argument("--timeout", type=int, default=120)
    result.add_argument("--expected-cfo-valid", type=int)
    result.add_argument("--expected-capex-valid", type=int)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.workers < 1 or args.timeout < 1:
        raise SystemExit("workers and timeout must be positive")
    if args.command == "verify":
        verify_output(Path(args.output_root))
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
