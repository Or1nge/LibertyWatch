#!/usr/bin/env python3
"""Report practical v2.1 minimum-data readiness for staged company records.

The script is read-only.  It does not mutate staging, caches, snapshots or
published releases.  It is designed to answer the coverage question that
cannot be inferred from the Git repository because production data is ignored.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence


WEBAPP_ROOT = Path(__file__).resolve().parents[1]
LIBERTY_ROOT = WEBAPP_ROOT.parent

ALLOWED_DIVIDEND_STATUSES = {
    "PAID",
    "SHAREHOLDER_APPROVED",
    "LEGAL_COMMITMENT",
    "NO_DISTRIBUTION",
}
FATAL_RAW_STATUSES = {"CONFLICT", "CALCULATION_FAILED"}
BUYBACK_ONLY_TOKENS = {
    "buyback",
    "cancelled_shares",
    "diluted_net_share_reduction",
    "share_count_bridge",
    "one_off_buyback",
    "special_dividend",
    "asset_sale_distribution",
}


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def positive(value: Any) -> bool:
    parsed = decimal_or_none(value)
    return parsed is not None and parsed > 0


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None else result.replace(tzinfo=timezone.utc)


def company_files(path: Path) -> list[Path]:
    if (path / "companies").is_dir():
        path = path / "companies"
    return sorted(path.glob("*.json")) if path.is_dir() else []


def eligible_dividend_years(raw: Mapping[str, Any], *, today: date) -> list[int]:
    years: set[int] = set()
    for item in raw.get("annual_distributions", []):
        if not isinstance(item, Mapping):
            continue
        if item.get("period_type") != "FULL_YEAR":
            continue
        status = str(item.get("ordinary_dividend_status") or "")
        if status not in ALLOWED_DIVIDEND_STATUSES:
            continue
        fiscal_end_raw = item.get("fiscal_year_end_date")
        try:
            fiscal_end = date.fromisoformat(str(fiscal_end_raw))
        except ValueError:
            continue
        if fiscal_end > today:
            continue
        value = decimal_or_none(item.get("ordinary_dividend"))
        if value is None or value < 0:
            continue
        try:
            years.add(int(item["fiscal_year"]))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(years, reverse=True)


def non_financial_coverage_years(raw: Mapping[str, Any]) -> tuple[list[int], bool]:
    coverage = raw.get("coverage") if isinstance(raw.get("coverage"), Mapping) else {}
    years: set[int] = set()
    simplified = False
    for item in coverage.get("fcf_years", []):
        if not isinstance(item, Mapping):
            continue
        if decimal_or_none(item.get("operating_cash_flow")) is None:
            continue
        capex = decimal_or_none(item.get("capital_expenditure"))
        if capex is None or capex < 0:
            continue
        if item.get("lease_principal_repayment") is None:
            simplified = True
        try:
            years.add(int(item["fiscal_year"]))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(years, reverse=True), simplified


def financial_coverage_ready(raw: Mapping[str, Any], industry: str) -> bool:
    coverage = raw.get("coverage") if isinstance(raw.get("coverage"), Mapping) else {}
    if industry == "BANK":
        required = (
            "adjusted_net_income",
            "capital_generation_capacity",
            "cet1_ratio",
            "cet1_regulatory_minimum",
        )
    elif industry == "INSURANCE":
        required = (
            "free_surplus_generation",
            "distributable_surplus_capacity",
            "comprehensive_solvency_ratio",
            "comprehensive_solvency_minimum",
            "core_solvency_ratio",
            "core_solvency_minimum",
        )
    else:
        return False
    return all(decimal_or_none(coverage.get(field)) is not None for field in required)


def direct_market_cap_ready(raw: Mapping[str, Any], *, now: datetime) -> tuple[bool, bool]:
    expected = {str(item) for item in raw.get("expected_share_classes", []) if str(item)}
    rows = [
        item
        for item in raw.get("share_classes", [])
        if isinstance(item, Mapping) and item.get("material") is not False
    ]
    actual = {str(item.get("share_class") or "") for item in rows if item.get("share_class")}
    if not expected or actual != expected:
        return False, False
    stale_24_72 = False
    for item in rows:
        if not (
            positive(item.get("price"))
            and positive(item.get("issued_shares"))
            and positive(item.get("fx_to_base"))
            and str(item.get("quote_status") or "") in {"VALID", "KNOWN_ZERO"}
        ):
            return False, False
        timestamp = parse_datetime(item.get("price_timestamp"))
        if timestamp is None:
            return False, False
        age_hours = (now.astimezone(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds() / 3600
        if age_hours < 0 or age_hours > 72:
            return False, False
        stale_24_72 = stale_24_72 or age_hours > 24
    return True, stale_24_72


def market_cap_proxy_kind(raw: Mapping[str, Any]) -> str | None:
    candidates = (
        raw.get("market_cap_proxy"),
        raw.get("company_market_cap_proxy"),
        raw.get("valuation", {}).get("market_cap_proxy")
        if isinstance(raw.get("valuation"), Mapping)
        else None,
    )
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or item.get("data_status") or "VALID")
        if status not in {"VALID", "ACCEPTED", "PROXY"}:
            continue
        direct_value = item.get("value", item.get("company_total_market_cap"))
        if positive(direct_value) and str(item.get("currency") or "").strip() and (
            str(item.get("source") or item.get("source_name") or "").strip()
        ):
            return "VENDOR_TOTAL_MARKET_CAP"
        if (
            positive(item.get("covered_market_cap"))
            and decimal_or_none(item.get("verified_coverage_ratio")) is not None
        ):
            ratio = decimal_or_none(item.get("verified_coverage_ratio"))
            if ratio is not None and Decimal("0.80") <= ratio <= Decimal("1"):
                return "COVERED_CLASS_GROSS_UP"
    return None


def valuation_kind(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("valuation") if isinstance(raw.get("valuation"), Mapping) else {}
    current = decimal_or_none(value.get("current"))
    historical = decimal_or_none(value.get("historical_median"))
    peer = decimal_or_none(value.get("peer_median"))
    percentile = decimal_or_none(value.get("industry_percentile"))
    if current is not None and current > 0 and historical is not None and historical > 0 and value.get("basis_consistent") is True:
        return "COMPARABLE_HISTORY"
    if percentile is not None and Decimal("0") <= percentile <= Decimal("1"):
        return "INDUSTRY_PERCENTILE"
    if current is not None and current > 0 and peer is not None and peer > 0:
        return "PEER_MEDIAN"
    if current is not None and current > 0 and str(value.get("metric") or ""):
        return "CURRENT_ONLY"
    return None


def balance_sheet_ready(raw: Mapping[str, Any], industry: str) -> tuple[bool, bool]:
    balance = raw.get("balance_sheet") if isinstance(raw.get("balance_sheet"), Mapping) else {}
    direct_fields = (
        "net_debt_ebitda",
        "net_debt",
        "net_cash",
        "cash_and_equivalents",
        "total_debt",
    )
    if any(decimal_or_none(balance.get(field)) is not None for field in direct_fields):
        return True, False
    history = raw.get("balance_sheet_history")
    if isinstance(history, Sequence) and not isinstance(history, (str, bytes)):
        if any(
            isinstance(item, Mapping) and decimal_or_none(item.get("net_debt")) is not None
            for item in history
        ):
            return True, False
    coverage = raw.get("coverage") if isinstance(raw.get("coverage"), Mapping) else {}
    if industry == "BANK" and all(
        decimal_or_none(coverage.get(field)) is not None
        for field in ("cet1_ratio", "cet1_regulatory_minimum")
    ):
        return True, False
    if industry == "INSURANCE" and all(
        decimal_or_none(coverage.get(field)) is not None
        for field in (
            "comprehensive_solvency_ratio",
            "comprehensive_solvency_minimum",
            "core_solvency_ratio",
            "core_solvency_minimum",
        )
    ):
        return True, False
    proxy = raw.get("balance_sheet_proxy")
    if isinstance(proxy, Mapping) and decimal_or_none(proxy.get("value")) is not None and str(proxy.get("source") or ""):
        return True, True
    return False, False


def core_source_envelope_present(raw: Mapping[str, Any]) -> bool:
    points = raw.get("raw_data_points")
    if not isinstance(points, list) or not points:
        return False
    return any(
        isinstance(item, Mapping)
        and str(item.get("field_id") or "")
        and str(item.get("source_name") or "")
        and str(item.get("source_document") or "")
        and str(item.get("source_url_or_local_path") or "")
        and str(item.get("fiscal_period") or "")
        and str(item.get("unit") or "")
        for item in points
    )


def fatal_raw_conflicts(raw: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for item in raw.get("raw_data_points", []):
        if not isinstance(item, Mapping):
            continue
        if str(item.get("data_status") or "") not in FATAL_RAW_STATUSES:
            continue
        field_id = str(item.get("field_id") or "")
        normalized = field_id.lower()
        if any(token in normalized for token in BUYBACK_ONLY_TOKENS):
            continue
        result.append(field_id or "UNKNOWN_CORE_FIELD")
    return sorted(set(result))


def reconciliation_summary(raw: Mapping[str, Any]) -> tuple[int, int]:
    inputs = raw.get("reconciliation_inputs")
    if not isinstance(inputs, Mapping):
        return 0, 4
    fields = (
        "dividend_per_share_times_entitled_shares",
        "repurchased_shares_times_average_price",
        "opening_minus_closing_shares",
        "cancelled_minus_issued_and_converted",
    )
    present = sum(decimal_or_none(inputs.get(field)) is not None for field in fields)
    return present, len(fields) - present


def inspect_company(raw: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    company_id = str(raw.get("company_id") or "")
    blockers: list[str] = []
    if not company_id:
        blockers.append("IDENTITY_MISSING")
    if not str(raw.get("company_name") or ""):
        blockers.append("COMPANY_NAME_MISSING")

    dividends = eligible_dividend_years(raw, today=now.date())
    if len(dividends) < 2:
        blockers.append("DIVIDEND_HISTORY_LT_2Y")

    industry = str(raw.get("industry_kind") or "UNSUPPORTED")
    coverage_years: list[int] = []
    simplified_fcf = False
    if industry == "NON_FINANCIAL":
        coverage_years, simplified_fcf = non_financial_coverage_years(raw)
        coverage_ready = len(coverage_years) >= 2
    else:
        coverage_ready = financial_coverage_ready(raw, industry)
    if not coverage_ready:
        blockers.append("COVERAGE_MINIMUM_MISSING")

    direct_market_cap, stale_quote = direct_market_cap_ready(raw, now=now)
    proxy_market_cap = market_cap_proxy_kind(raw)
    market_cap_ready = direct_market_cap or proxy_market_cap is not None
    if not market_cap_ready:
        blockers.append("MARKET_CAP_BASIS_MISSING")

    valuation = valuation_kind(raw)
    if valuation is None:
        blockers.append("CURRENT_VALUATION_MISSING")

    balance_ready, balance_proxy = balance_sheet_ready(raw, industry)
    if not balance_ready:
        blockers.append("BALANCE_SHEET_MINIMUM_MISSING")

    source_ready = core_source_envelope_present(raw)
    if not source_ready:
        blockers.append("CORE_SOURCE_ENVELOPE_MISSING")

    raw_conflicts = fatal_raw_conflicts(raw)
    if raw_conflicts:
        blockers.append("FATAL_CORE_SOURCE_CONFLICT")

    recons_present, recons_missing = reconciliation_summary(raw)
    high_impact_proxy = proxy_market_cap == "COVERED_CLASS_GROSS_UP" or valuation == "CURRENT_ONLY"
    proxy_codes = [
        code
        for code, active in (
            (proxy_market_cap or "", proxy_market_cap is not None),
            ("SIMPLIFIED_FCF", simplified_fcf),
            ("BALANCE_SHEET_PROXY", balance_proxy),
            ("STALE_QUOTE_24_72H", stale_quote),
            ("VALUATION_INDUSTRY_PROXY", valuation in {"INDUSTRY_PERCENTILE", "PEER_MEDIAN"}),
            ("VALUATION_CURRENT_ONLY", valuation == "CURRENT_ONLY"),
        )
        if active and code
    ]

    minimum_ready = not blockers
    if not minimum_ready:
        estimated_tier = "BLOCKED"
    elif high_impact_proxy:
        estimated_tier = "ESTIMATED"
    else:
        estimated_tier = "CALCULABLE"

    verified_candidate = bool(
        minimum_ready
        and direct_market_cap
        and len(dividends) >= 5
        and (industry != "NON_FINANCIAL" or len(coverage_years) >= 4)
        and not simplified_fcf
        and valuation == "COMPARABLE_HISTORY"
        and not balance_proxy
        and recons_missing == 0
        and not proxy_codes
    )
    if verified_candidate:
        estimated_tier = "VERIFIED"

    return {
        "company_id": company_id,
        "company_name": str(raw.get("company_name") or ""),
        "industry_kind": industry,
        "eligible_dividend_years": dividends,
        "eligible_dividend_year_count": len(dividends),
        "coverage_years": coverage_years,
        "coverage_year_count": len(coverage_years),
        "financial_coverage_ready": coverage_ready if industry != "NON_FINANCIAL" else None,
        "direct_market_cap_ready": direct_market_cap,
        "market_cap_proxy_kind": proxy_market_cap,
        "valuation_kind": valuation,
        "balance_sheet_ready": balance_ready,
        "core_source_envelope_present": source_ready,
        "fatal_core_conflict_fields": raw_conflicts,
        "reconciliations_present": recons_present,
        "reconciliations_missing": recons_missing,
        "proxy_or_warning_codes": proxy_codes,
        "minimum_calculable": minimum_ready,
        "estimated_tier": estimated_tier,
        "blockers": blockers,
    }


def build_report(staging: Path, *, now: datetime) -> dict[str, Any]:
    files = company_files(staging)
    companies: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []
    seen_ids: Counter[str] = Counter()
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise ValueError("company staging record must be an object")
            result = inspect_company(raw, now=now)
            companies.append(result)
            seen_ids[result["company_id"]] += 1
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            parse_errors.append({"path": str(path), "error": f"{type(error).__name__}: {error}"})

    duplicate_ids = sorted(company_id for company_id, count in seen_ids.items() if company_id and count > 1)
    condition_counts = {
        "companies_with_2plus_eligible_dividend_years": sum(
            item["eligible_dividend_year_count"] >= 2 for item in companies
        ),
        "companies_with_2plus_coverage_years_or_financial_capital_inputs": sum(
            item["coverage_year_count"] >= 2 or item["financial_coverage_ready"] is True
            for item in companies
        ),
        "companies_with_direct_or_approved_proxy_market_cap": sum(
            item["direct_market_cap_ready"] or item["market_cap_proxy_kind"] is not None
            for item in companies
        ),
        "companies_with_current_valuation_or_approved_proxy": sum(
            item["valuation_kind"] is not None for item in companies
        ),
        "companies_with_basic_balance_sheet_metric": sum(
            item["balance_sheet_ready"] for item in companies
        ),
        "companies_with_core_source_envelope": sum(
            item["core_source_envelope_present"] for item in companies
        ),
        "companies_with_fatal_core_conflict": sum(
            bool(item["fatal_core_conflict_fields"]) for item in companies
        ),
        "minimum_calculable_candidates": sum(item["minimum_calculable"] for item in companies),
    }
    tier_counts = Counter(item["estimated_tier"] for item in companies)
    blocker_counts = Counter(
        blocker for item in companies for blocker in item["blockers"]
    )
    return {
        "report_version": "shareholder-return-v2.1-readiness-v1",
        "generated_at": now.isoformat(),
        "staging_path": str(staging.resolve()),
        "staging_file_count": len(files),
        "parsed_company_count": len(companies),
        "parse_error_count": len(parse_errors),
        "duplicate_company_ids": duplicate_ids,
        "condition_counts": condition_counts,
        "estimated_tier_counts": dict(sorted(tier_counts.items())),
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "companies": companies,
        "parse_errors": parse_errors,
    }


def parse_args() -> argparse.Namespace:
    default = Path(
        os.getenv(
            "SHAREHOLDER_V2_STAGING_DIR",
            str(LIBERTY_ROOT / "data" / "shareholder-v2" / "staging"),
        )
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path, default=default)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--fail-on-empty", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = datetime.now(timezone.utc)
    report = build_report(args.staging, now=now)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    displayed = report if not args.compact else {
        key: report[key]
        for key in (
            "report_version",
            "generated_at",
            "staging_file_count",
            "parsed_company_count",
            "parse_error_count",
            "duplicate_company_ids",
            "condition_counts",
            "estimated_tier_counts",
            "blocker_counts",
        )
    }
    print(json.dumps(displayed, ensure_ascii=False, indent=2, sort_keys=True))
    if args.fail_on_empty and report["parsed_company_count"] == 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
