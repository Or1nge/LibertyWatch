#!/usr/bin/env python3
"""Build isolated ordinary/special dividend evidence candidates from official PDFs."""

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

from liberty_v2.dividend_candidates import (  # noqa: E402
    SCHEMA_VERSION,
    DividendCandidateError,
    extract_dividend_report_candidates,
    pdftotext_layout,
    verify_pdf_sha256,
)
from liberty_v2.snapshot_store import atomic_write_json  # noqa: E402


DEFAULT_INPUT = LIBERTY_ROOT / "data" / "raw" / "annual_reports" / "official_backfill_v1"
DEFAULT_OUTPUT = (
    LIBERTY_ROOT
    / "data"
    / "shareholder-v2"
    / "backfill-output"
    / "dividend-candidates-v1"
)
PRODUCTION_STAGING = LIBERTY_ROOT / "data" / "shareholder-v2" / "staging"


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _safe_output_root(path: Path) -> Path:
    resolved = path.resolve()
    source = DEFAULT_INPUT.resolve()
    staging = PRODUCTION_STAGING.resolve()
    if resolved == source or source in resolved.parents:
        raise DividendCandidateError("refusing to write into immutable official annual-report evidence")
    if resolved == staging or staging in resolved.parents:
        raise DividendCandidateError("refusing to write under production staging")
    return resolved


def verify_output_manifest(output_root: Path) -> dict[str, Any]:
    root = output_root.resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise DividendCandidateError(f"candidate manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checked = 0
    for item in manifest.get("files", []):
        path = (root / str(item["path"])).resolve()
        if root not in path.parents:
            raise DividendCandidateError("candidate manifest path escapes output root")
        if not path.is_file():
            raise DividendCandidateError(f"candidate manifest file is missing: {path}")
        if path.stat().st_size != int(item["size_bytes"]):
            raise DividendCandidateError(f"candidate manifest size mismatch: {path}")
        if _sha256(path) != str(item["sha256"]):
            raise DividendCandidateError(f"candidate manifest SHA-256 mismatch: {path}")
        checked += 1
    if checked != int(manifest.get("file_count") or -1):
        raise DividendCandidateError("candidate manifest file_count mismatch")
    return {"status": "VALID", "checked_file_count": checked, "manifest": str(manifest_path)}


def _selected_documents(
    input_root: Path,
    *,
    markets: set[str],
    fiscal_years: set[int],
) -> tuple[list[tuple[Path, dict[str, Any]]], list[dict[str, Any]]]:
    documents: list[tuple[Path, dict[str, Any]]] = []
    manifests: list[dict[str, Any]] = []
    for manifest_path in sorted(input_root.glob("companies/*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        relative_manifest = manifest_path.relative_to(input_root).as_posix()
        manifest_record = {
            "company_id": str(manifest.get("company_id") or ""),
            "path": relative_manifest,
            "sha256": _sha256(manifest_path),
        }
        selected_for_company = 0
        for raw in manifest.get("documents", []):
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            market = str(item.get("market") or "")
            year = int(item.get("fiscal_year") or 0)
            if markets and market not in markets:
                continue
            if fiscal_years and year not in fiscal_years:
                continue
            if item.get("data_status") != "VERIFIED" or item.get("selection_status") != "SELECTED_CURRENT":
                continue
            item["source_manifest_path"] = relative_manifest
            item["source_manifest_sha256"] = manifest_record["sha256"]
            documents.append((input_root / str(item["local_path"]), item))
            selected_for_company += 1
        if selected_for_company:
            manifest_record["selected_document_count"] = selected_for_company
            manifests.append(manifest_record)
    return documents, manifests


def _extract_one(pair: tuple[Path, dict[str, Any]], timeout: int) -> dict[str, Any]:
    pdf_path, metadata = pair
    verify_pdf_sha256(pdf_path, str(metadata["sha256"]))
    text = pdftotext_layout(pdf_path, timeout_seconds=timeout)
    try:
        return extract_dividend_report_candidates(
            text, {**metadata, "local_path": str(pdf_path.resolve())}
        )
    finally:
        del text


def run(args: argparse.Namespace) -> int:
    input_root = Path(args.input_root).resolve()
    output_root = _safe_output_root(Path(args.output_root))
    markets = set(args.market or [])
    fiscal_years = {int(value) for value in args.fiscal_year}
    selected, source_manifests = _selected_documents(
        input_root, markets=markets, fiscal_years=fiscal_years
    )
    if not selected:
        raise DividendCandidateError("no verified official annual reports matched the requested scope")

    extracted: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(_extract_one, pair, args.timeout): pair for pair in selected}
        for position, future in enumerate(as_completed(futures), start=1):
            pdf_path, metadata = futures[future]
            try:
                extracted.append(future.result())
                status = "EXTRACTED"
            except Exception as error:  # one malformed PDF must not block other companies
                status = "ERROR"
                errors.append(
                    {
                        "company_id": str(metadata.get("company_id") or ""),
                        "fiscal_year": int(metadata.get("fiscal_year") or 0),
                        "source_local_path": str(pdf_path),
                        "error": str(error)[:1000],
                    }
                )
            if position == 1 or position % 25 == 0 or position == len(selected):
                print(
                    json.dumps(
                        {"progress": f"{position}/{len(selected)}", "last_status": status},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    by_company: dict[str, list[dict[str, Any]]] = {}
    for report in extracted:
        by_company.setdefault(str(report["company_id"]), []).append(report)

    written: list[Path] = []
    company_summaries: list[dict[str, Any]] = []
    for company_id, reports in sorted(by_company.items()):
        reports.sort(key=lambda item: int(item["fiscal_year"]))
        source_manifest = next(
            item for item in source_manifests if item["company_id"] == company_id
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "company_id": company_id,
            "company_name": reports[-1]["company_name"],
            "candidate_only": True,
            "writes_production": False,
            "source_manifest": source_manifest,
            "reports": reports,
        }
        path = output_root / "candidates" / f"{company_id}.json"
        atomic_write_json(path, payload)
        written.append(path)
        all_candidates = [candidate for report in reports for candidate in report["candidates"]]
        company_summaries.append(
            {
                "company_id": company_id,
                "company_name": reports[-1]["company_name"],
                "report_count": len(reports),
                "candidate_count": len(all_candidates),
                "exact_fiscal_year_candidate_count": sum(
                    report["exact_fiscal_year_candidate_count"] for report in reports
                ),
                "ordinary_candidate_count": sum(
                    item["dividend_kind"] == "ORDINARY" for item in all_candidates
                ),
                "special_candidate_count": sum(
                    item["dividend_kind"] == "SPECIAL" for item in all_candidates
                ),
                "eligible_after_manual_review_count": sum(
                    item["eligible_after_manual_review"] is True for item in all_candidates
                ),
            }
        )

    all_candidates = [candidate for report in extracted for candidate in report["candidates"]]
    ledger_slots = [
        slot
        for report in extracted
        for kind in report["ledger"].values()
        for lifecycle in kind.values()
        for slot in lifecycle.values()
    ]
    report_payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_root": str(input_root),
        "output_root": str(output_root),
        "scope": {
            "markets": sorted(markets) if markets else ["CN", "HK"],
            "fiscal_years": sorted(fiscal_years) if fiscal_years else "ALL_AVAILABLE",
        },
        "source_company_manifest_count": len(source_manifests),
        "source_manifests": source_manifests,
        "selected_pdf_count": len(selected),
        "processed_pdf_count": len(extracted),
        "error_count": len(errors),
        "company_count": len(by_company),
        "candidate_count": len(all_candidates),
        "exact_fiscal_year_candidate_count": sum(
            report["exact_fiscal_year_candidate_count"] for report in extracted
        ),
        "ordinary_candidate_count": sum(
            item["dividend_kind"] == "ORDINARY" for item in all_candidates
        ),
        "special_candidate_count": sum(
            item["dividend_kind"] == "SPECIAL" for item in all_candidates
        ),
        "ambiguous_kind_candidate_count": sum(
            item["dividend_kind"] == "AMBIGUOUS" for item in all_candidates
        ),
        "paid_or_approved_candidate_count": sum(
            item["lifecycle_status"] in {"PAID", "APPROVED"} for item in all_candidates
        ),
        "proposed_candidate_count": sum(
            item["lifecycle_status"] == "PROPOSED" for item in all_candidates
        ),
        "eligible_after_manual_review_count": sum(
            item["eligible_after_manual_review"] is True for item in all_candidates
        ),
        "ledger_slot_status_counts": {
            status: sum(slot["status"] == status for slot in ledger_slots)
            for status in ("REVIEW", "CONFLICT", "MISSING")
        },
        "core_import_allowed_count": 0,
        "candidate_coverage": {
            "reports_with_any_candidate": sum(
                report["candidate_count"] > 0 for report in extracted
            ),
            "reports_without_any_candidate": sum(
                report["candidate_count"] == 0 for report in extracted
            ),
            "reports_with_exact_fiscal_year_candidate": sum(
                report["exact_fiscal_year_candidate_count"] > 0 for report in extracted
            ),
            "reports_without_exact_fiscal_year_candidate": sum(
                report["exact_fiscal_year_candidate_count"] == 0 for report in extracted
            ),
            "companies_with_any_candidate": sum(
                item["candidate_count"] > 0 for item in company_summaries
            ),
            "companies_without_any_candidate": [
                item["company_id"] for item in company_summaries if item["candidate_count"] == 0
            ],
            "companies_with_exact_fiscal_year_candidate": sum(
                item["exact_fiscal_year_candidate_count"] > 0 for item in company_summaries
            ),
            "companies_without_exact_fiscal_year_candidate": [
                item["company_id"]
                for item in company_summaries
                if item["exact_fiscal_year_candidate_count"] == 0
            ],
        },
        "companies": company_summaries,
        "errors": errors,
        "safety": [
            "Outputs are review candidates outside production staging.",
            "Proposed ordinary dividends and all special dividends are never core-import eligible.",
            "Multiple components are not summed; ambiguous/conflicting slots keep a null candidate.",
            "Missing extraction is unknown, never financial zero.",
            "No OCR is attempted and full pdftotext output is never persisted.",
        ],
    }
    report_path = output_root / "report.json"
    atomic_write_json(report_path, report_payload)
    written.append(report_path)
    manifest = {
        "schema_version": "dividend-candidate-manifest-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(input_root),
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
                "status": "COMPLETE" if not errors else "PARTIAL",
                "company_count": len(by_company),
                "processed_pdf_count": len(extracted),
                "error_count": len(errors),
                "candidate_count": len(all_candidates),
                "exact_fiscal_year_candidate_count": report_payload[
                    "exact_fiscal_year_candidate_count"
                ],
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
    result.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    result.add_argument("--market", action="append", choices=("CN", "HK"), default=[])
    result.add_argument("--fiscal-year", action="append", default=[])
    result.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    result.add_argument("--timeout", type=int, default=120)
    result.add_argument("--verify-only", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        output = _safe_output_root(Path(args.output_root))
        if args.verify_only:
            print(json.dumps(verify_output_manifest(output), ensure_ascii=False, indent=2))
            return 0
        return run(args)
    except DividendCandidateError as error:
        print(f"dividend candidate error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
