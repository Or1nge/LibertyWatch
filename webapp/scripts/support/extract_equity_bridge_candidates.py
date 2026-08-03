#!/usr/bin/env python3
"""Extract isolated issued-share bridge candidates from official annual reports."""

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

from liberty_v2.equity_bridge_candidates import (  # noqa: E402
    SCHEMA_VERSION,
    EquityBridgeCandidateError,
    extract_equity_bridge_candidate,
    pdftotext_layout,
    reconcile_company_reports,
    verify_pdf_sha256,
)
from liberty_v2.snapshot_store import atomic_write_json  # noqa: E402


DEFAULT_INPUT = LIBERTY_ROOT / "data" / "raw" / "annual_reports" / "official_backfill_v1"
DEFAULT_OUTPUT = (
    LIBERTY_ROOT
    / "data"
    / "shareholder-v2"
    / "backfill-output"
    / "equity-bridge-candidates-v1"
)
PRODUCTION_STAGING = LIBERTY_ROOT / "data" / "shareholder-v2" / "staging"


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_output_manifest(output_root: Path) -> dict[str, Any]:
    root = output_root.resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise EquityBridgeCandidateError(f"candidate manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checked = 0
    for item in manifest.get("files", []):
        path = (root / str(item["path"])).resolve()
        if root not in path.parents:
            raise EquityBridgeCandidateError("candidate manifest path escapes output root")
        if not path.is_file():
            raise EquityBridgeCandidateError(f"candidate manifest file is missing: {path}")
        if path.stat().st_size != int(item["size_bytes"]):
            raise EquityBridgeCandidateError(f"candidate manifest size mismatch: {path}")
        if _sha256(path) != str(item["sha256"]):
            raise EquityBridgeCandidateError(f"candidate manifest SHA-256 mismatch: {path}")
        checked += 1
    if checked != int(manifest.get("file_count") or -1):
        raise EquityBridgeCandidateError("candidate manifest file_count mismatch")
    return {"status": "VALID", "checked_file_count": checked, "manifest": str(manifest_path)}


def _selected_documents(
    input_root: Path,
    *,
    fiscal_years: set[int],
    market: str,
) -> list[tuple[Path, dict[str, Any]]]:
    documents: list[tuple[Path, dict[str, Any]]] = []
    for manifest_path in sorted(input_root.glob("companies/*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for raw in manifest.get("documents", []):
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            if market != "ALL" and str(item.get("market")) != market:
                continue
            if int(item.get("fiscal_year") or 0) not in fiscal_years:
                continue
            if item.get("data_status") != "VERIFIED" or item.get("selection_status") != "SELECTED_CURRENT":
                continue
            pdf_path = input_root / str(item["local_path"])
            documents.append((pdf_path, item))
    return documents


def _extract_one(pair: tuple[Path, dict[str, Any]], timeout: int) -> dict[str, Any]:
    pdf_path, metadata = pair
    verify_pdf_sha256(pdf_path, str(metadata["sha256"]))
    text = pdftotext_layout(pdf_path, timeout_seconds=timeout)
    try:
        enriched = {**metadata, "local_path": str(pdf_path.resolve())}
        return extract_equity_bridge_candidate(text, enriched)
    finally:
        # The full pdftotext output is never persisted and is released after
        # one report; only page/line excerpts survive in the candidate JSON.
        del text


def _safe_output_root(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == PRODUCTION_STAGING.resolve() or PRODUCTION_STAGING.resolve() in resolved.parents:
        raise EquityBridgeCandidateError("refusing to write candidates under production staging")
    if resolved == DEFAULT_INPUT.resolve() or DEFAULT_INPUT.resolve() in resolved.parents:
        raise EquityBridgeCandidateError("refusing to write candidates into immutable source evidence")
    return resolved


def run(args: argparse.Namespace) -> int:
    input_root = Path(args.input_root).resolve()
    output_root = _safe_output_root(Path(args.output_root))
    fiscal_years = {int(value) for value in args.fiscal_year}
    selected = _selected_documents(input_root, fiscal_years=fiscal_years, market=args.market)
    if not selected:
        raise EquityBridgeCandidateError("no verified annual reports matched the requested scope")

    extracted: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(_extract_one, pair, args.timeout): pair for pair in selected}
        for position, future in enumerate(as_completed(futures), start=1):
            path, metadata = futures[future]
            try:
                extracted.append(future.result())
                status = extracted[-1]["status"]
            except Exception as error:  # isolate one damaged/source-conflict PDF
                status = "ERROR"
                errors.append(
                    {
                        "company_id": str(metadata.get("company_id") or ""),
                        "fiscal_year": int(metadata.get("fiscal_year") or 0),
                        "source_local_path": str(path),
                        "error": str(error)[:1000],
                    }
                )
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
            "issued_shares_are_not_diluted_shares": True,
            "reports": reports,
        }
        path = output_root / "candidates" / f"{company_id}.json"
        atomic_write_json(path, payload)
        written.append(path)
        counts = {name: sum(item["status"] == name for item in reports) for name in ("VALID", "REVIEW", "CONFLICT")}
        company_summaries.append(
            {
                "company_id": company_id,
                "company_name": reports[-1]["company_name"],
                "report_count": len(reports),
                "status_counts": counts,
            }
        )

    all_reports = [report for reports in reconciled.values() for report in reports]
    status_counts = {
        name: sum(item["status"] == name for item in all_reports)
        for name in ("VALID", "REVIEW", "CONFLICT")
    }
    market_pdf_counts = {
        market: sum(str(item.get("market")) == market for item in all_reports)
        for market in ("CN", "HK")
    }
    market_status_counts = {
        market: {
            status: sum(
                str(item.get("market")) == market and item["status"] == status
                for item in all_reports
            )
            for status in ("VALID", "REVIEW", "CONFLICT")
        }
        for market in ("CN", "HK")
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_root": str(input_root),
        "output_root": str(output_root),
        "scope": {"market": args.market, "fiscal_years": sorted(fiscal_years)},
        "selected_pdf_count": len(selected),
        "processed_pdf_count": len(all_reports),
        "error_count": len(errors),
        "status_counts": status_counts,
        "market_pdf_counts": market_pdf_counts,
        "market_status_counts": market_status_counts,
        "numeric_row_count": sum(item.get("opening_issued_shares") is not None for item in all_reports),
        "issued_candidate_valid_count": sum(item.get("eligible_for_issued_share_candidate") is True for item in all_reports),
        "diluted_core_eligible_count": 0,
        "cancelled_shares_autofilled_count": 0,
        "explicit_cancelled_candidate_valid_count": sum(
            item.get("eligible_for_cancelled_shares_candidate") is True for item in all_reports
        ),
        "companies": company_summaries,
        "errors": errors,
        "safety": [
            "All outputs are candidates outside production staging.",
            "Issued-share counts are never treated as diluted shares.",
            "Cancelled shares and diluted net reduction are never inferred.",
            "A cancelled-share candidate requires a unique official row explicitly stating actual cancellation.",
            "Missing or unchanged-without-numbers remains unknown, not zero.",
        ],
    }
    report_path = output_root / "report.json"
    atomic_write_json(report_path, report)
    written.append(report_path)
    manifest = {
        "schema_version": "equity-bridge-candidate-manifest-v1",
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
    report["manifest_verification"] = verification
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if not errors else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--input-root", default=str(DEFAULT_INPUT))
    result.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    result.add_argument("--market", choices=("CN", "HK", "ALL"), default="CN")
    result.add_argument("--fiscal-year", action="append", default=[])
    result.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    result.add_argument("--timeout", type=int, default=120)
    result.add_argument("--verify-only", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.verify_only:
        try:
            print(
                json.dumps(
                    verify_output_manifest(_safe_output_root(Path(args.output_root))),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        except EquityBridgeCandidateError as error:
            print(f"equity-bridge candidate error: {error}", file=sys.stderr)
            return 2
    if not args.fiscal_year:
        args.fiscal_year = ["2024", "2025"]
    try:
        return run(args)
    except EquityBridgeCandidateError as error:
        print(f"equity-bridge candidate error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
