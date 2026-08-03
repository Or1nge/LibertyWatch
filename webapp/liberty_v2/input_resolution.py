from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .balance_sheet_adapter import BalanceSheetAssessment
from .market_observation import MarketObservation
from .market_value_resolver import MarketValueResolution
from .models import MetricBasis


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
