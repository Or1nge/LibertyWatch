from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .balance_sheet_adapter import BalanceSheetAssessment
from .market_observation import MarketObservation
from .market_value_resolver import MarketValueResolution
from .models import MetricBasis
from .screening import filter_recent_weekly_prices


ELIGIBLE_DIVIDEND_STATUSES = {
    "PAID",
    "SHAREHOLDER_APPROVED",
    "LEGAL_COMMITMENT",
    "NO_DISTRIBUTION",
}


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


@dataclass(frozen=True)
class SelectedInputPlan:
    company_id: str
    distribution_basis: MetricBasis
    distribution_years: tuple[int, ...]
    coverage_basis: MetricBasis
    coverage_years: tuple[int, ...]
    market_value_basis: MetricBasis
    valuation_basis: MetricBasis
    valuation_metric: str | None
    balance_sheet_basis: MetricBasis
    balance_sheet_kind: str
    growth_basis: MetricBasis
    growth_years: tuple[int, ...]
    required_source_field_ids: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        return {
            "distribution_basis": self.distribution_basis.value,
            "distribution_years": list(self.distribution_years),
            "coverage_basis": self.coverage_basis.value,
            "coverage_years": list(self.coverage_years),
            "market_value_basis": self.market_value_basis.value,
            "valuation_basis": self.valuation_basis.value,
            "valuation_metric": self.valuation_metric,
            "balance_sheet_basis": self.balance_sheet_basis.value,
            "balance_sheet_kind": self.balance_sheet_kind,
            "growth_basis": self.growth_basis.value,
            "growth_years": list(self.growth_years),
            "required_source_field_ids": list(self.required_source_field_ids),
            "warnings": list(self.warnings),
        }


def eligible_distribution_rows(raw: Mapping[str, Any], *, on_date: date) -> list[Mapping[str, Any]]:
    selected: list[Mapping[str, Any]] = []
    rows = raw.get("annual_distributions")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return selected
    for row in rows:
        if not isinstance(row, Mapping) or row.get("period_type") != "FULL_YEAR":
            continue
        fiscal_end = _date(row.get("fiscal_year_end_date"))
        if fiscal_end is None or fiscal_end > on_date:
            continue
        status = str(row.get("ordinary_dividend_status") or "")
        value = _decimal(row.get("ordinary_dividend"))
        if status not in ELIGIBLE_DIVIDEND_STATUSES:
            continue
        if status == "NO_DISTRIBUTION" and value is None:
            value = Decimal("0")
        if value is None or value < 0:
            continue
        selected.append(row)
    return sorted(selected, key=lambda item: int(item["fiscal_year"]), reverse=True)[:10]


def eligible_coverage_rows(raw: Mapping[str, Any], *, on_date: date) -> list[Mapping[str, Any]]:
    coverage = raw.get("coverage") if isinstance(raw.get("coverage"), Mapping) else {}
    rows = coverage.get("fcf_years")
    selected: list[Mapping[str, Any]] = []
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return selected
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        fiscal_end = _date(row.get("fiscal_year_end_date"))
        if fiscal_end is not None and fiscal_end > on_date:
            continue
        ocf = _decimal(row.get("operating_cash_flow"))
        capex = _decimal(row.get("capital_expenditure"))
        if ocf is None or capex is None or capex < 0:
            continue
        selected.append(row)
    return sorted(selected, key=lambda item: int(item["fiscal_year"]), reverse=True)[:5]


def _valuation_selection(
    observation: MarketObservation | None,
) -> tuple[MetricBasis, str | None, tuple[str, ...]]:
    if observation is None:
        return MetricBasis.UNAVAILABLE, None, ()
    candidates = (
        ("PE_TTM", observation.pe_ttm, "pe_ttm"),
        ("PE", observation.pe, "pe"),
        ("PB", observation.pb, "pb"),
    )
    for metric, value, field in candidates:
        if value is not None and value > 0:
            return (
                MetricBasis.VENDOR_AUTHORIZED,
                metric,
                (f"MARKET.{observation.security_id}.{field}",),
            )
    return MetricBasis.UNAVAILABLE, None, ()


def build_selected_input_plan(
    raw: Mapping[str, Any],
    *,
    on_date: date,
    market_observation: MarketObservation | None,
    market_value: MarketValueResolution,
    balance_sheet: BalanceSheetAssessment,
) -> SelectedInputPlan:
    company_id = str(raw.get("company_id") or "")
    distributions = eligible_distribution_rows(raw, on_date=on_date)
    distribution_years = tuple(int(row["fiscal_year"]) for row in distributions)
    distribution_sources = tuple(f"FY{year}.ordinary_dividend" for year in distribution_years)
    distribution_basis = MetricBasis.DIRECT if distributions else MetricBasis.UNAVAILABLE

    coverage = eligible_coverage_rows(raw, on_date=on_date)
    coverage_years = tuple(int(row["fiscal_year"]) for row in coverage)
    simplified = any(_decimal(row.get("lease_principal_repayment")) is None for row in coverage)
    coverage_basis = (
        MetricBasis.PROXY
        if coverage and simplified
        else MetricBasis.DIRECT
        if coverage
        else MetricBasis.UNAVAILABLE
    )
    coverage_sources: list[str] = []
    for row in coverage:
        year = int(row["fiscal_year"])
        coverage_sources.extend(
            (f"FY{year}.operating_cash_flow", f"FY{year}.capital_expenditure")
        )
        if _decimal(row.get("lease_principal_repayment")) is not None:
            coverage_sources.append(f"FY{year}.lease_principal_repayment")

    valuation_basis, valuation_metric, valuation_sources = _valuation_selection(market_observation)
    growth_years = tuple(sorted(coverage_years))
    growth_basis = (
        MetricBasis.DERIVED if len(growth_years) >= 2 else MetricBasis.CONSERVATIVE_DEFAULT
    )
    warnings: list[str] = []
    if simplified:
        warnings.append("SIMPLIFIED_FCF")
    if valuation_metric is not None:
        warnings.append("CURRENT_VALUATION_WITHOUT_COMPARABLE_HISTORY")
    if growth_basis is MetricBasis.CONSERVATIVE_DEFAULT:
        warnings.append("GROWTH_DEFAULT_ZERO")
    warnings.extend(market_value.warnings)
    warnings.extend(balance_sheet.warnings)
    required = tuple(
        dict.fromkeys(
            (
                *distribution_sources,
                *coverage_sources,
                *market_value.source_field_ids,
                *valuation_sources,
                *balance_sheet.source_field_ids,
            )
        )
    )
    return SelectedInputPlan(
        company_id=company_id,
        distribution_basis=distribution_basis,
        distribution_years=distribution_years,
        coverage_basis=coverage_basis,
        coverage_years=coverage_years,
        market_value_basis=market_value.basis,
        valuation_basis=valuation_basis,
        valuation_metric=valuation_metric,
        balance_sheet_basis=balance_sheet.basis,
        balance_sheet_kind=balance_sheet.kind,
        growth_basis=growth_basis,
        growth_years=growth_years,
        required_source_field_ids=required,
        warnings=tuple(dict.fromkeys(warnings)),
    )


# The functions below form the independent v2.2 screening input path.  The
# legacy SelectedInputPlan above remains available for v2.1 replay/tests.

FINANCIAL_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": ("营业收入", "营业总收入", "收入合计"),
    "net_profit": ("净利润",),
    "operating_cash_flow": ("经营活动产生的现金流量净额",),
    "capital_expenditure": ("购建固定资产、无形资产和其他长期资产支付的现金",),
    "total_assets": ("资产合计",),
    "total_liabilities": ("负债合计",),
    "total_equity": ("股东权益合计", "所有者权益合计"),
    "cash": ("货币资金", "现金及现金等价物", "现金及等价物"),
}
INTEREST_BEARING_DEBT_ALIASES = (
    "短期借款",
    "一年内到期的非流动负债",
    "长期借款",
    "应付债券",
    "租赁负债",
    "银行借款",
    "计息银行及其他借款",
)
PROVIDER_STATEMENT_BY_FIELD = {
    "revenue": "income_statement",
    "net_profit": "income_statement",
    "operating_cash_flow": "cash_flow",
    "capital_expenditure": "cash_flow",
    "total_assets": "balance_sheet",
    "total_liabilities": "balance_sheet",
    "total_equity": "balance_sheet",
    "cash": "balance_sheet",
    "interest_bearing_debt": "balance_sheet",
}


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _valid_provider_evidence(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != "futu-financial-evidence-v1":
        return None
    expected = str(payload.get("sha256") or "")
    unsigned = dict(payload)
    unsigned.pop("sha256", None)
    if len(expected) != 64 or _canonical_sha256(unsigned) != expected:
        return None
    return payload


def _report_index(payload: Mapping[str, Any]) -> dict[tuple[str, int], Mapping[str, Any]]:
    result: dict[tuple[str, int], Mapping[str, Any]] = {}
    statements = payload.get("statements") if isinstance(payload.get("statements"), Mapping) else {}
    for statement_name, statement in statements.items():
        if not isinstance(statement, Mapping):
            continue
        reports = statement.get("report_list")
        if not isinstance(reports, Sequence) or isinstance(reports, (str, bytes)):
            continue
        for report in reports:
            if not isinstance(report, Mapping) or not str(report.get("period_text") or "").endswith("/FY"):
                continue
            try:
                year = int(report["fiscal_year"])
            except (KeyError, TypeError, ValueError):
                continue
            result[(str(statement_name), year)] = report
    return result


def _field_from_report(report: Mapping[str, Any] | None, aliases: Sequence[str]) -> tuple[Decimal | None, str | None]:
    if not isinstance(report, Mapping):
        return None, None
    items = report.get("item_list")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return None, None
    exact = {
        str(item.get("display_name") or "").strip(): item
        for item in items
        if isinstance(item, Mapping)
    }
    for alias in aliases:
        item = exact.get(alias)
        if item is None:
            continue
        value = _decimal(item.get("data"))
        if value is not None:
            return value, str(item.get("field_id") or alias)
    return None, None


def _interest_bearing_debt(report: Mapping[str, Any] | None) -> tuple[Decimal | None, list[str]]:
    if not isinstance(report, Mapping):
        return None, []
    items = report.get("item_list")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return None, []
    values: list[Decimal] = []
    field_ids: list[str] = []
    for item in items:
        if not isinstance(item, Mapping) or str(item.get("display_name") or "").strip() not in INTEREST_BEARING_DEBT_ALIASES:
            continue
        value = _decimal(item.get("data"))
        if value is None:
            continue
        values.append(value)
        field_ids.append(str(item.get("field_id") or item.get("display_name")))
    return (sum(values, Decimal("0")), field_ids) if values else (None, [])


def _official_field_index(raw: Mapping[str, Any]) -> dict[tuple[int, str], list[Mapping[str, Any]]]:
    result: dict[tuple[int, str], list[Mapping[str, Any]]] = {}
    points = raw.get("raw_data_points")
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes)):
        return result
    for point in points:
        if not isinstance(point, Mapping):
            continue
        field_id = str(point.get("field_id") or "")
        suffix = field_id.rsplit(".", 1)[-1]
        if suffix not in PROVIDER_STATEMENT_BY_FIELD:
            continue
        fiscal = str(point.get("fiscal_period") or "")
        try:
            year = int(fiscal[2:6]) if fiscal.startswith("FY") else int(fiscal[:4])
        except ValueError:
            continue
        result.setdefault((year, suffix), []).append(point)
    return result


def _is_official_source(point: Mapping[str, Any]) -> bool:
    source = str(point.get("source_name") or "").lower()
    return "futu" not in source and "provider" not in source


def _source_record(
    *,
    source: str,
    field_id: str | Sequence[str] | None,
    fiscal_year: int,
    currency: str | None,
    unit: str,
    fetched_at: str | None,
    basis: str,
    warning: str | None = None,
) -> dict[str, Any]:
    return {
        "source_name": source,
        "source_field_id": field_id,
        "fiscal_year": fiscal_year,
        "currency": currency,
        "unit": unit,
        "fetched_at": fetched_at,
        "basis": basis,
        "warning": warning,
    }


def load_screening_financial_rows(
    raw: Mapping[str, Any],
    *,
    evidence_root: Path,
    maximum_years: int = 5,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve official structured facts over immutable Futu statement evidence."""

    company_id = str(raw.get("company_id") or "")
    payload = _valid_provider_evidence(evidence_root / company_id / "latest.json")
    reports = _report_index(payload or {})
    provider_years = {year for _statement, year in reports}
    official = _official_field_index(raw)
    all_years = sorted(provider_years | {year for year, _field in official}, reverse=True)[:maximum_years]
    rows: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    warnings: list[str] = []
    fetched_at = str((payload or {}).get("fetched_at") or "") or None
    for year in sorted(all_years):
        row: dict[str, Any] = {"fiscal_year": year, "field_sources": {}, "warnings": []}
        currencies: set[str] = set()
        for field, statement_name in PROVIDER_STATEMENT_BY_FIELD.items():
            report = reports.get((statement_name, year))
            currency = str((report or {}).get("currency_code") or "") or None
            if currency:
                currencies.add(currency)
            if field == "interest_bearing_debt":
                provider_value, provider_ids = _interest_bearing_debt(report)
                provider_field_id: str | Sequence[str] | None = provider_ids
            else:
                provider_value, provider_field_id = _field_from_report(report, FINANCIAL_FIELD_ALIASES[field])
            candidates = official.get((year, field), [])
            if any(str(item.get("data_status") or "") == "CONFLICT" for item in candidates):
                row[field] = None
                row["warnings"].append("SOURCE_CONFLICT")
                conflicts.append({"fiscal_year": year, "field_id": field})
                row["field_sources"][field] = _source_record(
                    source="official/provider reconciliation",
                    field_id=field,
                    fiscal_year=year,
                    currency=currency,
                    unit="currency",
                    fetched_at=fetched_at,
                    basis="SOURCE_CONFLICT",
                    warning="SOURCE_CONFLICT",
                )
                continue
            accepted = [
                item for item in candidates
                if _is_official_source(item) and str(item.get("data_status") or "") in {"VALID", "KNOWN_ZERO"}
            ]
            if accepted:
                selected = accepted[-1]
                value = _decimal(selected.get("value"))
                row[field] = value
                row["field_sources"][field] = _source_record(
                    source=str(selected.get("source_name") or "official structured fact"),
                    field_id=str(selected.get("field_id") or field),
                    fiscal_year=year,
                    currency=str(selected.get("currency") or "") or currency,
                    unit=str(selected.get("unit") or "currency"),
                    fetched_at=str(selected.get("source_fetch_time") or "") or None,
                    basis="OFFICIAL_STRUCTURED",
                )
            else:
                row[field] = provider_value
                row["field_sources"][field] = _source_record(
                    source="Futu OpenD detailed financial statements",
                    field_id=provider_field_id,
                    fiscal_year=year,
                    currency=currency,
                    unit="currency",
                    fetched_at=fetched_at,
                    basis="PROVIDER" if provider_value is not None else "MISSING",
                    warning=None if provider_value is not None else "FIELD_MISSING",
                )
        row["currency"] = sorted(currencies)[0] if len(currencies) == 1 else None
        row["source"] = "OFFICIAL_STRUCTURED_AND_PROVIDER" if any(
            item["basis"] == "OFFICIAL_STRUCTURED" for item in row["field_sources"].values()
        ) else "Futu OpenD detailed financial statements"
        warnings.extend(row["warnings"])
        rows.append(row)
    return rows, {
        "provider": "Futu OpenD detailed financial statements" if payload else None,
        "provider_fetched_at": fetched_at,
        "provider_snapshot_sha256": str((payload or {}).get("sha256") or "") or None,
        "fiscal_years": [row["fiscal_year"] for row in rows],
        "source_conflicts": conflicts,
        "warnings": sorted(set(warnings)),
    }


def load_five_year_weekly_prices(
    path: Path,
    *,
    security_id: str,
    as_of: date,
    years: int = 5,
) -> tuple[list[Any], dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], {"source": None, "warning": "PRICE_HISTORY_MISSING"}
    securities = payload.get("securities") if isinstance(payload, Mapping) else None
    series = securities.get(security_id) if isinstance(securities, Mapping) else None
    if not isinstance(series, Mapping) or series.get("adjustment") != "qfq" or series.get("frequency") != "weekly":
        return [], {"source": str(payload.get("provider") or "") or None, "warning": "PRICE_HISTORY_MISSING"}
    points = series.get("points")
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes)):
        points = []
    prices = filter_recent_weekly_prices(
        [point for point in points if isinstance(point, Mapping)],
        as_of=as_of,
        years=years,
    )
    return prices, {
        "source": str(payload.get("provider") or "Futu OpenD"),
        "field_id": "qfq_weekly_close",
        "generated_at": payload.get("generatedAt"),
        "adjustment": "qfq",
        "frequency": "weekly",
        "point_count": len(prices),
        "warning": None if prices else "PRICE_HISTORY_MISSING",
    }


def screening_profile(
    raw: Mapping[str, Any],
    watchlist_security: Mapping[str, Any] | None,
    policy: Mapping[str, Any],
) -> str:
    configured = {str(item) for item in policy.get("financial_company_ids", [])}
    if str(raw.get("company_id") or "") in configured:
        return "FINANCIAL"
    text = " ".join(
        str((watchlist_security or {}).get(key) or "")
        for key in ("sector", "industry", "name")
    )
    return "FINANCIAL" if any(str(keyword) in text for keyword in policy.get("financial_sector_keywords", [])) else "NON_FINANCIAL"
