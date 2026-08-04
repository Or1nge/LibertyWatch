#!/usr/bin/env python3
"""Build v2 annual source ledgers without mutating production snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


WEBAPP_ROOT = Path(__file__).resolve().parents[1]
LIBERTY_ROOT = WEBAPP_ROOT.parent
sys.path.insert(0, str(WEBAPP_ROOT))

from liberty_v2.snapshot_store import atomic_write_json  # noqa: E402
from liberty_v2.source_ledger import (  # noqa: E402
    KNOWN_MULTI_MARKET_SECURITIES,
    SourceLedgerError,
    apply_ledger_to_staging_record,
    build_futu_financial_ledger,
    extract_official_pdf_text_candidates,
    load_json,
    load_sqlite_buyback_evidence,
    normalize_futu_statement_payload,
)


DEFAULT_COMPANIES = LIBERTY_ROOT / "data" / "source" / "companies.json"
DEFAULT_ANNUAL_REPORTS = LIBERTY_ROOT / "data" / "raw" / "annual_reports"
DEFAULT_EVIDENCE_ROOT = (
    LIBERTY_ROOT / "data" / "shareholder-v2" / "source-evidence" / "futu-financials"
)
DEFAULT_STAGING = LIBERTY_ROOT / "data" / "shareholder-v2" / "staging" / "companies"
DEFAULT_EVENT_DB = LIBERTY_ROOT / "data" / "monitor" / "liberty_monitor.sqlite3"


def dump(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def load_companies(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    companies = payload.get("companies")
    if not isinstance(companies, list):
        raise SourceLedgerError("company source has no companies array")
    result = [dict(item) for item in companies if isinstance(item, Mapping)]
    if len(result) != 67:
        raise SourceLedgerError(f"formal company source must contain 67 companies, got {len(result)}")
    return result


def annual_report_covered_ids(
    companies: Sequence[Mapping[str, Any]],
    annual_root: Path,
) -> set[str]:
    # Freeze the exclusion to the 11 legacy ten-year archives that existed
    # before this backfill started.  Newly downloaded official_backfill_v1
    # directories are outputs of the current job, not reasons to shrink its
    # original 56-company scope while it is running.
    legacy_root = annual_root / "十年候选_2016_2025分红年报PDF"
    directory_names = [path.name for path in legacy_root.iterdir() if path.is_dir()]
    covered: set[str] = set()
    for company in companies:
        name = str(company.get("name") or "").replace("-S", "").replace("-W", "")
        if any(name and name in directory for directory in directory_names):
            covered.add(str(company["issuerId"]))
    return covered


def target_companies(args: argparse.Namespace) -> tuple[list[dict[str, Any]], set[str]]:
    companies = load_companies(Path(args.companies))
    covered = annual_report_covered_ids(companies, Path(args.annual_reports))
    selected = companies if args.include_annual_report_covered else [
        company for company in companies if str(company["issuerId"]) not in covered
    ]
    wanted = set(args.company_id or [])
    if wanted:
        selected = [company for company in selected if str(company["issuerId"]) in wanted]
        missing = wanted - {str(company["issuerId"]) for company in selected}
        if missing:
            raise SourceLedgerError(f"requested company ids are outside the selected target: {sorted(missing)}")
    return selected, covered


def command_targets(args: argparse.Namespace) -> int:
    selected, covered = target_companies(args)
    known_cross_listed = [
        {
            "company_id": str(company["issuerId"]),
            "security_id": str(company["securityId"]),
            "company_name": str(company["name"]),
            "known_market_classes": list(KNOWN_MULTI_MARKET_SECURITIES[str(company["securityId"])]),
        }
        for company in selected
        if str(company["securityId"]) in KNOWN_MULTI_MARKET_SECURITIES
    ]
    dump(
        {
            "formal_company_count": 67,
            "annual_report_covered_count": len(covered),
            "target_count": len(selected),
            "target_company_ids": [str(company["issuerId"]) for company in selected],
            "known_cross_listing_review_required": known_cross_listed,
            "share_class_scope_note": (
                "All targets remain rights_verified=false until every material ordinary share class is verified."
            ),
        }
    )
    return 0


class RollingLimiter:
    def __init__(self, maximum: int = 28, window_seconds: float = 30.0) -> None:
        self.maximum = maximum
        self.window_seconds = window_seconds
        self.calls: deque[float] = deque()

    def wait(self) -> None:
        now = time.monotonic()
        while self.calls and now - self.calls[0] >= self.window_seconds:
            self.calls.popleft()
        if len(self.calls) >= self.maximum:
            time.sleep(max(self.window_seconds - (now - self.calls[0]) + 0.1, 0.1))
            now = time.monotonic()
            while self.calls and now - self.calls[0] >= self.window_seconds:
                self.calls.popleft()
        self.calls.append(time.monotonic())


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def file_record(path: Path, root: Path) -> dict[str, Any]:
    content = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _query_statement(context: Any, limiter: RollingLimiter, code: str, statement_type: int, years: int) -> dict[str, Any]:
    last_error = ""
    for attempt in range(3):
        limiter.wait()
        ret, data = context.get_financials_statements(
            code,
            statement_type=statement_type,
            financial_type=7,
            num=years,
        )
        if ret == 0 and isinstance(data, Mapping):
            return normalize_futu_statement_payload(data)
        last_error = str(data)
        if "频率太高" in last_error and attempt < 2:
            time.sleep(30.5)
            continue
        break
    raise SourceLedgerError(f"Futu financial statement query failed: {last_error}")


def _share_class(company: Mapping[str, Any]) -> str:
    market = str(company.get("market") or "")
    return "A" if market == "CN" else "H" if market == "HK" else market


def command_collect_futu(args: argparse.Namespace) -> int:
    selected, _covered = target_companies(args)
    plan = {
        "mode": "apply-evidence" if args.apply else "dry-run",
        "target_count": len(selected),
        "calls_planned": len(selected) * 3,
        "max_full_years": args.max_years,
        "output_root": str(Path(args.output_root)),
        "writes_production_snapshots": False,
    }
    if not args.apply:
        dump(plan)
        return 0

    try:
        from futu import OpenQuoteContext
    except ImportError as error:
        raise SourceLedgerError(
            "futu-api is unavailable; use tools/futu-opend/.venv/bin/python"
        ) from error

    output_root = Path(args.output_root)
    limiter = RollingLimiter()
    context = OpenQuoteContext(host=args.host, port=args.port)
    completed: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    try:
        for index, company in enumerate(selected, start=1):
            issuer_id = str(company["issuerId"])
            fetched_at = datetime.now(timezone.utc)
            statements: dict[str, Any] = {}
            errors: dict[str, str] = {}
            for statement_name, statement_type in (
                ("income_statement", 1),
                ("cash_flow", 3),
                ("balance_sheet", 2),
            ):
                try:
                    statements[statement_name] = _query_statement(
                        context,
                        limiter,
                        str(company["quoteCode"]),
                        statement_type,
                        args.max_years,
                    )
                except SourceLedgerError as error:
                    errors[statement_name] = str(error)
                    statements[statement_name] = {
                        "next_key": "-1",
                        "structure_list": [],
                        "report_list": [],
                    }
            payload: dict[str, Any] = {
                "schema_version": "futu-financial-evidence-v1",
                "fetched_at": fetched_at.isoformat(),
                "company": {
                    "issuer_id": issuer_id,
                    "security_id": str(company["securityId"]),
                    "quote_code": str(company["quoteCode"]),
                    "name": str(company["name"]),
                    "share_class": _share_class(company),
                    "security_currency": str(company["currency"]),
                },
                "statements": statements,
                "errors": errors,
            }
            payload["sha256"] = canonical_sha256(payload)
            stamp = fetched_at.strftime("%Y%m%dT%H%M%SZ")
            company_root = output_root / issuer_id
            snapshot = company_root / "snapshots" / f"{stamp}-{payload['sha256'][:12]}.json"
            atomic_write_json(snapshot, payload)
            atomic_write_json(company_root / "latest.json", payload)
            summary = {
                "position": index,
                "company_id": issuer_id,
                "snapshot": str(snapshot),
                "errors": errors,
            }
            completed.append(summary)
            if errors:
                failures.append({"company_id": issuer_id, "error": json.dumps(errors, ensure_ascii=False)})
            print(
                json.dumps(
                    {
                        "progress": f"{index}/{len(selected)}",
                        "company_id": issuer_id,
                        "status": "PARTIAL" if errors else "COLLECTED",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        context.close()
    manifest = {
        **plan,
        "completed_count": len(completed),
        "failure_count": len(failures),
        "companies": completed,
    }
    atomic_write_json(output_root / "collection_manifest.json", manifest)
    dump({"completed_count": len(completed), "failures": failures})
    return 0 if not failures else 2


def _latest_evidence_path(root: Path, company_id: str) -> Path:
    return root / company_id / "latest.json"


def command_build_ledger(args: argparse.Namespace) -> int:
    selected, _covered = target_companies(args)
    evidence_root = Path(args.evidence_root)
    staging_root = Path(args.staging_dir)
    output_root = Path(args.output_dir) if args.output_dir else None
    if args.apply and output_root is None:
        raise SourceLedgerError("--apply requires a separate --output-dir")
    if output_root and output_root.resolve() == staging_root.resolve():
        raise SourceLedgerError("refusing to overwrite the staging input directory")

    summaries: list[dict[str, Any]] = []
    missing_evidence: list[str] = []
    tracked_fields = (
        "operating_cash_flow",
        "capital_expenditure",
        "lease_principal_repayment",
        "reported_share_capital_amount",
        "diluted_total_shares",
        "cancelled_shares",
        "diluted_net_share_reduction",
    )
    field_coverage = {
        field: {"VALID_OR_KNOWN_ZERO": 0, "MISSING": 0, "CONFLICT": 0}
        for field in tracked_fields
    }
    company_status_counts = {"VALID": 0, "PARTIAL": 0, "INVALID": 0}
    buyback_event_count = 0
    buyback_company_count = 0
    written_paths: list[Path] = []
    for company in selected:
        company_id = str(company["issuerId"])
        evidence_path = _latest_evidence_path(evidence_root, company_id)
        staging_path = staging_root / f"{company_id}.json"
        if not evidence_path.is_file():
            missing_evidence.append(company_id)
            continue
        if not staging_path.is_file():
            raise SourceLedgerError(f"staging input missing: {staging_path}")
        ledger = build_futu_financial_ledger(
            load_json(evidence_path),
            evidence_path=evidence_path,
            max_years=args.max_years,
        )
        patched = apply_ledger_to_staging_record(load_json(staging_path), ledger)
        buyback_evidence = load_sqlite_buyback_evidence(Path(args.sqlite_db), company_id)
        patched["buyback_event_evidence"] = buyback_evidence
        if buyback_evidence:
            buyback_company_count += 1
            buyback_event_count += len(buyback_evidence)
        annual = ledger["annual_source_ledger"]
        coverage_rows = patched.get("coverage", {}).get("fcf_years", [])
        numeric_cfo = sum(item.get("operating_cash_flow") is not None for item in coverage_rows)
        numeric_capex = sum(item.get("capital_expenditure") is not None for item in coverage_rows)
        summary = patched.get("source_summary", {}).get("share_class_coverage", {})
        has_conflict = False
        latest_five = annual[:5]
        for annual_row in latest_five:
            values = annual_row.get("values", {})
            for field in tracked_fields:
                item = values.get(field, {}) if isinstance(values, Mapping) else {}
                status = str(item.get("data_status") or "MISSING")
                bucket = (
                    "VALID_OR_KNOWN_ZERO"
                    if status in {"VALID", "KNOWN_ZERO"}
                    else "CONFLICT"
                    if status == "CONFLICT"
                    else "MISSING"
                )
                field_coverage[field][bucket] += 1
                has_conflict = has_conflict or bucket == "CONFLICT"
        core_complete = all(
            field_coverage_name in {"VALID", "KNOWN_ZERO"}
            for annual_row in latest_five
            for field_name in (
                "operating_cash_flow",
                "capital_expenditure",
                "lease_principal_repayment",
                "diluted_total_shares",
            )
            for field_coverage_name in [
                str(annual_row.get("values", {}).get(field_name, {}).get("data_status") or "MISSING")
            ]
        ) and len(latest_five) == 5
        has_any_numeric = any(
            item.get("data_status") in {"VALID", "KNOWN_ZERO"}
            for annual_row in latest_five
            for item in annual_row.get("values", {}).values()
            if isinstance(item, Mapping)
        )
        if has_conflict or not has_any_numeric:
            ledger_status = "INVALID"
        elif core_complete and summary.get("rights_verified") is True:
            ledger_status = "VALID"
        else:
            ledger_status = "PARTIAL"
        company_status_counts[ledger_status] += 1
        summaries.append(
            {
                "company_id": company_id,
                "company_name": str(company["name"]),
                "full_years_indexed": len(annual),
                "fcf_years_with_cfo": numeric_cfo,
                "fcf_years_with_capex": numeric_capex,
                "lease_principal_status": "MISSING",
                "issued_share_count_status": "MISSING",
                "share_class_scope_status": summary.get("status"),
                "source_ledger_status": ledger_status,
                "company_level_yield_eligible": False,
            }
        )
        if args.apply and output_root is not None:
            output_path = output_root / "companies" / f"{company_id}.json"
            atomic_write_json(output_path, patched)
            written_paths.append(output_path)
    report = {
        "mode": "apply-separate-output" if args.apply else "dry-run",
        "target_count": len(selected),
        "ledger_built_count": len(summaries),
        "missing_evidence_count": len(missing_evidence),
        "missing_evidence_company_ids": missing_evidence,
        "coverage_window": "latest up to 5 provider-labelled FULL_YEAR periods per company",
        "company_year_denominator": sum(min(5, item["full_years_indexed"]) for item in summaries),
        "field_coverage_company_years": field_coverage,
        "source_ledger_status_counts": company_status_counts,
        "company_level_yield_eligible_count": 0,
        "buyback_event_evidence": {
            "company_count": buyback_company_count,
            "event_count": buyback_event_count,
            "core_eligible_count": 0,
            "status": "REVIEW_REQUIRED",
        },
        "companies": summaries,
        "data_gaps": [
            "Futu does not isolate lease-principal repayment.",
            "Share-capital book amount is not issued/diluted share count.",
            "Official filings are required for cancellation and diluted share bridges.",
            "All material A/H/other ordinary classes must be verified before company-level yield is VALID.",
        ],
    }
    if args.apply and output_root is not None:
        report_path = output_root / "backfill_report.json"
        atomic_write_json(report_path, report)
        written_paths.append(report_path)
        manifest = {
            "schema_version": "source-ledger-output-manifest-v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ledger_company_count": len(summaries),
            "files": [file_record(path, output_root) for path in sorted(written_paths)],
        }
        atomic_write_json(output_root / "manifest.json", manifest)
    dump(report)
    return 0


def command_pdf_candidates(args: argparse.Namespace) -> int:
    text_path = Path(args.text)
    metadata = load_json(Path(args.metadata))
    candidates = extract_official_pdf_text_candidates(
        text_path.read_text(encoding="utf-8", errors="replace"),
        metadata,
    )
    payload = {
        "schema_version": "official-pdf-ledger-candidates-v1",
        "source_text": str(text_path),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    if args.apply:
        if not args.output:
            raise SourceLedgerError("--apply requires --output")
        atomic_write_json(Path(args.output), payload)
    dump(payload)
    return 0


def add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--companies", default=str(DEFAULT_COMPANIES))
    parser.add_argument("--annual-reports", default=str(DEFAULT_ANNUAL_REPORTS))
    parser.add_argument("--include-annual-report-covered", action="store_true")
    parser.add_argument("--company-id", action="append")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    targets = commands.add_parser("targets", help="show the 56-company source-backfill scope")
    add_scope_arguments(targets)
    targets.set_defaults(func=command_targets)

    collect = commands.add_parser("collect-futu", help="collect immutable Futu statement evidence")
    add_scope_arguments(collect)
    collect.add_argument("--host", default="127.0.0.1")
    collect.add_argument("--port", type=int, default=11111)
    collect.add_argument("--max-years", type=int, default=10, choices=range(1, 11))
    collect.add_argument("--output-root", default=str(DEFAULT_EVIDENCE_ROOT))
    collect.add_argument("--apply", action="store_true")
    collect.set_defaults(func=command_collect_futu)

    build = commands.add_parser("build-ledger", help="convert evidence into staged v2 ledger copies")
    add_scope_arguments(build)
    build.add_argument("--evidence-root", default=str(DEFAULT_EVIDENCE_ROOT))
    build.add_argument("--staging-dir", default=str(DEFAULT_STAGING))
    build.add_argument("--sqlite-db", default=str(DEFAULT_EVENT_DB))
    build.add_argument("--output-dir")
    build.add_argument("--max-years", type=int, default=10, choices=range(1, 11))
    build.add_argument("--apply", action="store_true")
    build.set_defaults(func=command_build_ledger)

    pdf = commands.add_parser("pdf-candidates", help="extract review candidates from pdftotext -layout")
    pdf.add_argument("--text", required=True)
    pdf.add_argument("--metadata", required=True)
    pdf.add_argument("--output")
    pdf.add_argument("--apply", action="store_true")
    pdf.set_defaults(func=command_pdf_candidates)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.func(args))
    except SourceLedgerError as error:
        print(f"source-ledger error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
