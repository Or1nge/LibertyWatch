from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from .balance_sheet_adapter import BalanceSheetAssessment
from .input_resolution import SelectedInputPlan
from .market_value_resolver import MarketValueResolution
from .models import MetricBasis


@dataclass(frozen=True)
class ConfidenceResult:
    total: int
    domains: Mapping[str, int]
    caveats: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        return {
            "value": self.total,
            "domains": dict(self.domains),
            "caveats": list(self.caveats),
        }


def _latest_lag(years: tuple[int, ...], on_date: date) -> int | None:
    return (on_date.year - 1) - max(years) if years else None


def _current_structured_field(raw: Mapping[str, Any], field: str, on_date: date) -> bool:
    for container_name in ("structured_scores", "reviewed_overlay_scores", "risk_scores"):
        container = raw.get(container_name)
        item = container.get(field) if isinstance(container, Mapping) else None
        if not isinstance(item, Mapping) or item.get("value") is None:
            continue
        try:
            as_of = date.fromisoformat(str(item.get("as_of_date")))
            expires = date.fromisoformat(str(item.get("expires_at")))
        except ValueError:
            continue
        if (
            str(item.get("source") or "").strip()
            and str(item.get("reason") or "").strip()
            and as_of <= on_date <= expires
        ):
            return True
    return False


def _veto_coverage(raw: Mapping[str, Any], on_date: date) -> bool:
    required = (
        "core_asset_expires_within_10y",
        "major_committed_capex_or_ma",
        "qualified_audit_opinion",
        "material_internal_control_weakness",
        "controlling_shareholder_fund_occupation",
        "major_related_party_alert",
        "major_regulatory_penalty",
    )
    values = raw.get("veto_inputs") if isinstance(raw.get("veto_inputs"), Mapping) else {}
    for field in required:
        item = values.get(field)
        if not isinstance(item, Mapping) or not isinstance(item.get("value"), bool):
            return False
        try:
            as_of = date.fromisoformat(str(item.get("as_of_date")))
            expires = date.fromisoformat(str(item.get("expires_at")))
        except ValueError:
            return False
        if not str(item.get("source") or "").strip() or not as_of <= on_date <= expires:
            return False
    return True


def calculate_data_confidence(
    raw: Mapping[str, Any],
    plan: SelectedInputPlan,
    market_value: MarketValueResolution,
    balance_sheet: BalanceSheetAssessment,
    *,
    on_date: date,
) -> ConfidenceResult:
    caveats: list[str] = []
    points = {
        str(item.get("field_id")): item
        for item in raw.get("raw_data_points", [])
        if isinstance(item, Mapping) and item.get("field_id")
    }
    selected = [points[field] for field in plan.required_source_field_ids if field in points]
    selected_sources = " ".join(
        f"{item.get('source_name', '')} {item.get('source_document', '')}".lower()
        for item in selected
    )
    official_tokens = ("annual report", "年报", "exchange", "stock exchange", "hkex", "巨潮")
    identity_and_provenance = 15 if selected and any(token in selected_sources for token in official_tokens) else 12 if selected else 0

    dividend_count = len(plan.distribution_years)
    distribution_history = 25 if dividend_count >= 5 else 20 if dividend_count >= 3 else 15 if dividend_count >= 2 else 0
    dividend_lag = _latest_lag(plan.distribution_years, on_date)
    if dividend_lag == 1:
        distribution_history = max(0, distribution_history - 3)
        caveats.append("LATEST_DIVIDEND_YEAR_LAGS_ONE_YEAR")

    coverage_count = len(plan.coverage_years)
    coverage_score = 18 if coverage_count >= 4 else 15 if coverage_count == 3 else 12 if coverage_count >= 2 else 0
    balance_score = 7 if balance_sheet.basis is MetricBasis.DIRECT else 4 if balance_sheet.basis is MetricBasis.PROXY else 0
    coverage_and_balance = coverage_score + balance_score
    if plan.coverage_basis is MetricBasis.PROXY:
        coverage_and_balance = max(0, coverage_and_balance - 2)
        caveats.append("SIMPLIFIED_FCF")

    market_score = (
        12
        if market_value.basis is MetricBasis.DERIVED
        else 10
        if market_value.basis is MetricBasis.VENDOR_AUTHORIZED
        else 0
    )
    valuation_score = 4 if plan.valuation_basis is MetricBasis.VENDOR_AUTHORIZED else 0
    market_and_valuation = market_score + valuation_score

    growth_count = len(plan.growth_years)
    growth_evidence = 5 if growth_count >= 5 else 4 if growth_count >= 3 else 2 if growth_count == 2 else 0

    business = _current_structured_field(raw, "business_durability", on_date)
    governance = _current_structured_field(raw, "governance_capital_allocation", on_date)
    structural = _current_structured_field(raw, "structural_cycle", on_date)
    policy = _current_structured_field(raw, "policy_asset_life", on_date)
    vetoes = _veto_coverage(raw, on_date)
    qualitative = 2 * int(business) + 2 * int(governance) + 2 * int(structural and policy) + 4 * int(vetoes)
    if qualitative == 0:
        caveats.append("QUALITATIVE_AND_VETO_COVERAGE_MISSING")

    domains = {
        "identity_and_provenance": identity_and_provenance,
        "distribution_history": distribution_history,
        "coverage_and_balance_sheet": coverage_and_balance,
        "market_and_valuation": market_and_valuation,
        "growth_evidence": growth_evidence,
        "qualitative_and_veto_coverage": qualitative,
    }
    return ConfidenceResult(
        total=sum(domains.values()),
        domains=domains,
        caveats=tuple(dict.fromkeys(caveats)),
    )
