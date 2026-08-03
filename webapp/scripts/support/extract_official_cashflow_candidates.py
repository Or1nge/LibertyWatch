#!/usr/bin/env python3
"""Reconcile official annual-report CFO/capex candidates with Futu evidence."""

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

from liberty_v2.official_cashflow_candidates import (  # noqa: E402
    FIELD_NAMES,
    SCHEMA_VERSION,
    OfficialCashflowCandidateError,
    build_futu_reference,
    extract_official_cashflow_report,
    pdftotext_layout,
    reconcile_company_reports,
    verify_pdf_sha256,
)
from liberty_v2.snapshot_store import atomic_write_json  # noqa: E402


DEFAULT_INPUT = LIBERTY_ROOT / "data" / "raw" / "annual_reports" / "official_backfill_v1"
DEFAULT_FUTU = LIBERTY_ROOT / "data" / "shareholder-v2" / "source-evidence" / "futu-financials"
DEFAULT_OUTPUT = LIBERTY_ROOT / "data" / "shareholder-v2" / "backfill-output" / "official-cashflow-candidates-v1"
PRODUCTION_STAGING = LIBERTY_ROOT / "data" / "shareholder-v2" / "staging"


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _safe_output_root(path: Path) -> Path:
    resolved = path.resolve()
    protected = (PRODUCTION_STAGING.resolve(), DEFAULT_INPUT.resolve(), DEFAULT_FUTU.resolve())
    if any(resolved == root or root in resolved.parents for root in protected):
        raise OfficialCashflowCandidateError("refusing to write into production staging or immutable evidence")
    return resolved


def verify_output_manifest(output_root: Path) -> dict[str, Any]:
    root = output_root.resolve()
    path = root / "manifest.json"
    if not path.is_file():
        raise OfficialCashflowCandidateError(f"candidate manifest is missing: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    checked = 0
    for item in manifest.get("files", []):
        candidate = (root / str(item["path"])).resolve()
        if root not in candidate.parents or not candidate.is_file():
            raise OfficialCashflowCandidateError(f"unsafe or missing manifest file: {candidate}")
        if candidate.stat().st_size != int(item["size_bytes"]) or _sha256(candidate) != str(item["sha256"]):
            raise OfficialCashflowCandidateError(f"candidate manifest mismatch: {candidate}")
        checked += 1
    if checked != int(manifest.get("file_count") or -1):
        raise OfficialCashflowCandidateError("candidate manifest file_count mismatch")
    return {"status": "VALID", "checked_file_count": checked, "manifest": str(path)}


def _selected_documents(input_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    documents: list[tuple[Path, dict[str, Any]]] = []
    for manifest_path in sorted(input_root.glob("companies/*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for raw in manifest.get("documents", []):
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            if item.get("data_status") != "VERIFIED" or item.get("selection_status") != "SELECTED_CURRENT":
                continue
            documents.append((input_root / str(item["local_path"]), item))
    return documents


def _futu_references(futu_root: Path, company_ids: set[str]) -> dict[str, dict[int, dict[str, Any]]]:
    result: dict[str, dict[int, dict[str, Any]]] = {}
    for company_id in sorted(company_ids):
        path = futu_root / company_id / "latest.json"
        if not path.is_file():
            raise OfficialCashflowCandidateError(f"Futu evidence is missing: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        result[company_id] = build_futu_reference(payload, evidence_path=path)
    return result


def _extract_one(
    pair: tuple[Path, dict[str, Any]],
    references: Mapping[str, Mapping[int, Mapping[str, Any]]],
    timeout: int,
) -> dict[str, Any]:
    pdf_path, metadata = pair
    verify_pdf_sha256(pdf_path, str(metadata["sha256"]))
    text = pdftotext_layout(pdf_path, timeout_seconds=timeout)
    try:
        company_id = str(metadata["company_id"])
        fiscal_year = int(metadata["fiscal_year"])
        enriched = {**metadata, "local_path": str(pdf_path.resolve())}
        return extract_official_cashflow_report(
            text,
            enriched,
            references.get(company_id, {}).get(fiscal_year),
        )
    finally:
        del text


def run(args: argparse.Namespace) -> int:
    input_root = Path(args.input_root).resolve()
    futu_root = Path(args.futu_root).resolve()
    output_root = _safe_output_root(Path(args.output_root))
    selected = _selected_documents(input_root)
    company_ids = {str(metadata["company_id"]) for _path, metadata in selected}
    if len(company_ids) != 56 or len(selected) != 526:
        raise OfficialCashflowCandidateError(
            f"expected fixed 56-company/526-PDF scope, got {len(company_ids)}/{len(selected)}"
        )
    references = _futu_references(futu_root, company_ids)

    extracted: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_extract_one, pair, references, args.timeout): pair for pair in selected
        }
        for position, future in enumerate(as_completed(futures), start=1):
            path, metadata = futures[future]
            try:
                extracted.append(future.result())
                status = "EXTRACTED"
            except Exception as error:  # one damaged PDF must not erase other evidence
                status = "ERROR"
                errors.append(
                    {
                        "company_id": str(metadata.get("company_id") or ""),
                        "fiscal_year": int(metadata.get("fiscal_year") or 0),
                        "source_local_path": str(path),
                        "error": str(error)[:1000],
                    }
                )
            if position == 1 or position % 25 == 0 or position == len(selected):
                print(
                    json.dumps(
                        {
                            "progress": f"{position}/{len(selected)}",
                            "company_id": metadata.get("company_id"),
                            "fiscal_year": metadata.get("fiscal_year"),
                            "status": status,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    by_company: dict[str, list[dict[str, Any]]] = {}
    for item in extracted:
        by_company.setdefault(str(item["company_id"]), []).append(item)
    reconciled = {
        company_id: reconcile_company_reports(items)
        for company_id, items in sorted(by_company.items())
    }

    written: list[Path] = []
    company_summaries: list[dict[str, Any]] = []
    for company_id, reports in reconciled.items():
        payload = {
            "schema_version": SCHEMA_VERSION,
            "company_id": company_id,
            "company_name": reports[-1]["company_name"],
            "candidate_only": True,
            "writes_production": False,
            "reports": reports,
        }
        path = output_root / "candidates" / f"{company_id}.json"
        atomic_write_json(path, payload)
        written.append(path)
        fields = [report["fields"][name] for report in reports for name in FIELD_NAMES]
        company_summaries.append(
            {
                "company_id": company_id,
                "company_name": reports[-1]["company_name"],
                "report_count": len(reports),
                "field_status_counts": {
                    status: sum(item["status"] == status for item in fields)
                    for status in ("VALID", "REVIEW", "CONFLICT")
                },
            }
        )

    all_fields = [
        (name, report["fields"][name])
        for reports in reconciled.values()
        for report in reports
        for name in FIELD_NAMES
    ]
    field_summary: dict[str, Any] = {}
    for name in FIELD_NAMES:
        items = [item for item_name, item in all_fields if item_name == name]
        field_summary[name] = {
            "candidate_count": len(items),
            "status_counts": {
                status: sum(item["status"] == status for item in items)
                for status in ("VALID", "REVIEW", "CONFLICT")
            },
            "official_numeric_extracted_count": sum(item.get("current_value") is not None for item in items),
            "futu_match_count": sum(item.get("futu_reconciliation") == "MATCH" for item in items),
            "comparative_report_match_count": sum(
                item.get("comparative_report_reconciliation") == "MATCH" for item in items
            ),
            "core_write_eligible_count": 0,
        }
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_root": str(input_root),
        "futu_evidence_root": str(futu_root),
        "output_root": str(output_root),
        "company_count": len(reconciled),
        "selected_pdf_count": len(selected),
        "processed_pdf_count": len(extracted),
        "error_count": len(errors),
        "field_summary": field_summary,
        "companies": company_summaries,
        "errors": errors,
        "safety": [
            "Official values remain isolated candidates and never write core fields.",
            "VALID requires a unique statement row, explicit unit/currency, exact Futu match, and prior-report comparative match.",
            "Multiple rows, unknown units, missing components, or HK format ambiguity remain REVIEW/CONFLICT with public candidate value null.",
            "Missing values are never converted to zero.",
        ],
    }
    report_path = output_root / "report.json"
    atomic_write_json(report_path, report)
    written.append(report_path)
    manifest = {
        "schema_version": "official-cashflow-candidate-manifest-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(written),
        "files": [
            {
                "path": path.relative_to(output_root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in sorted(written)
        ],
    }
    atomic_write_json(output_root / "manifest.json", manifest)
    verification = verify_output_manifest(output_root)
    print(
        json.dumps(
            {
                "company_count": report["company_count"],
                "selected_pdf_count": report["selected_pdf_count"],
                "processed_pdf_count": report["processed_pdf_count"],
                "error_count": report["error_count"],
                "field_summary": report["field_summary"],
                "manifest_verification": verification,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0 if not errors else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--input-root", default=str(DEFAULT_INPUT))
    result.add_argument("--futu-root", default=str(DEFAULT_FUTU))
    result.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    result.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    result.add_argument("--timeout", type=int, default=120)
    result.add_argument("--verify-only", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        output_root = _safe_output_root(Path(args.output_root))
        if args.verify_only:
            print(json.dumps(verify_output_manifest(output_root), ensure_ascii=False, indent=2))
            return 0
        return run(args)
    except OfficialCashflowCandidateError as error:
        print(f"official cash-flow candidate error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
