from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .balance_sheet_adapter import BalanceSheetAssessment
from .capital_structure import CapitalStructureAuthorization
from .confidence import ConfidenceResult, calculate_data_confidence
from .input_resolution import SelectedInputPlan, build_selected_input_plan
from .market_observation import MarketObservation
from .market_value_resolver import (
    MarketValueResolution,
    resolve_selected_security_equivalent_value,
)
from .models import CompanyDataTier, Freshness, MetricBasis, ReleaseValidity


FATAL_SOURCE_STATUSES = {"CONFLICT", "CALCULATION_FAILED"}
AVAILABLE_SOURCE_STATUSES = {"VALID", "KNOWN_ZERO"}


@dataclass(frozen=True)
class CompanyAssessment:
    company_id: str
    data_tier: CompanyDataTier
    data_confidence: ConfidenceResult
    freshness: Freshness
    input_plan: SelectedInputPlan
    market_value: MarketValueResolution
    balance_sheet: BalanceSheetAssessment
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    missing_source_field_ids: tuple[str, ...]
    invalid_source_field_ids: tuple[str, ...]

    @property
    def publishes_scores(self) -> bool:
        return self.data_tier is not CompanyDataTier.BLOCKED

    def public_dict(self) -> dict[str, Any]:
        return {
            "data_tier": self.data_tier.value,
            "data_confidence": self.data_confidence.public_dict(),
            "freshness": self.freshness.value,
            "selected_input_plan": self.input_plan.public_dict(),
            "selected_security_equivalent_value": self.market_value.public_dict(),
            "balance_sheet_adapter": self.balance_sheet.public_dict(),
            "warnings": list(self.warnings),
            "blockers": list(self.blockers),
            "source_summary": {
                "required_field_count": len(self.input_plan.required_source_field_ids),
                "missing_field_ids": list(self.missing_source_field_ids),
                "invalid_field_ids": list(self.invalid_source_field_ids),
            },
        }


@dataclass(frozen=True)
class ReleaseAssessment:
    validity: ReleaseValidity
    errors: tuple[str, ...] = ()


def _latest_year_lag(years: tuple[int, ...], on_date: date) -> int | None:
    return (on_date.year - 1) - max(years) if years else None


def _raw_source_index(raw: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    for item in raw.get("raw_data_points", []):
        if not isinstance(item, Mapping):
            continue
        field_id = str(item.get("field_id") or "")
        if field_id:
            result.setdefault(field_id, []).append(item)
    return result


def _identity_blockers(
    raw: Mapping[str, Any],
    authorization: CapitalStructureAuthorization,
    observation: MarketObservation | None,
) -> list[str]:
    blockers: list[str] = []
    company_id = str(raw.get("company_id") or "")
    if not company_id or not str(raw.get("company_name") or "").strip():
        blockers.append("COMPANY_OR_SECURITY_IDENTITY_MISSING")
    if company_id != authorization.company_id:
        blockers.append("CAPITAL_STRUCTURE_COMPANY_IDENTITY_CONFLICT")
    securities = raw.get("securities")
    rows = (
        securities
        if isinstance(securities, Sequence) and not isinstance(securities, (str, bytes))
        else ()
    )
    known_security_ids = {
        str(item.get("security_id") or "")
        for item in rows
        if isinstance(item, Mapping)
    }
    if known_security_ids and authorization.selected_security_id not in known_security_ids:
        blockers.append("SELECTED_SECURITY_ISSUER_MAPPING_CONFLICT")
    if observation is not None and observation.company_id != company_id:
        blockers.append("MARKET_OBSERVATION_COMPANY_IDENTITY_CONFLICT")
    return blockers


def _fiscal_structure_blockers(raw: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    for key in ("annual_distributions",):
        rows = raw.get(key)
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            continue
        years: list[int] = []
        for row in rows:
            if not isinstance(row, Mapping) or row.get("fiscal_year") is None:
                blockers.append("FISCAL_PERIOD_IDENTITY_INVALID")
                continue
            try:
                years.append(int(row["fiscal_year"]))
            except (TypeError, ValueError):
                blockers.append("FISCAL_PERIOD_IDENTITY_INVALID")
        if len(years) != len(set(years)):
            blockers.append("DUPLICATE_FISCAL_YEAR")
    return blockers


def assess_company(
    raw: Mapping[str, Any],
    *,
    authorization: CapitalStructureAuthorization,
    market_observation: MarketObservation | None,
    balance_sheet: BalanceSheetAssessment,
    now: datetime,
) -> CompanyAssessment:
    market_value = resolve_selected_security_equivalent_value(
        authorization,
        market_observation,
    )
    plan = build_selected_input_plan(
        raw,
        on_date=now.date(),
        market_observation=market_observation,
        market_value=market_value,
        balance_sheet=balance_sheet,
    )
    confidence = calculate_data_confidence(
        raw,
        plan,
        market_value,
        balance_sheet,
        on_date=now.date(),
    )
    blockers = [
        *_identity_blockers(raw, authorization, market_observation),
        *_fiscal_structure_blockers(raw),
        *market_value.blockers,
    ]
    if len(plan.distribution_years) < 2:
        blockers.append("DIVIDEND_HISTORY_LT_2Y")
    dividend_lag = _latest_year_lag(plan.distribution_years, now.date())
    if dividend_lag is None or dividend_lag > 1:
        blockers.append("DIVIDEND_HISTORY_NOT_RECENT")
    if len(plan.coverage_years) < 2:
        blockers.append("COVERAGE_HISTORY_LT_2Y")
    coverage_lag = _latest_year_lag(plan.coverage_years, now.date())
    if coverage_lag is None or coverage_lag > 1:
        blockers.append("COVERAGE_HISTORY_NOT_RECENT")
    if market_observation is None or market_observation.price is None or market_observation.fx_to_base is None:
        blockers.append("CURRENT_PRICE_OR_FX_MISSING")
    if plan.valuation_basis is MetricBasis.UNAVAILABLE:
        blockers.append("CURRENT_VALUATION_MISSING")
    if not balance_sheet.available:
        blockers.append("BALANCE_SHEET_MINIMUM_MISSING")

    source_index = _raw_source_index(raw)
    missing_sources = tuple(
        field_id
        for field_id in plan.required_source_field_ids
        if field_id not in source_index
    )
    invalid_sources: list[str] = []
    for field_id in plan.required_source_field_ids:
        records = source_index.get(field_id, [])
        if records and not any(str(item.get("data_status") or "") in AVAILABLE_SOURCE_STATUSES for item in records):
            invalid_sources.append(field_id)
        if any(str(item.get("data_status") or "") in FATAL_SOURCE_STATUSES for item in records):
            blockers.append("CORE_SOURCE_CONFLICT")
    if missing_sources:
        blockers.append("SELECTED_INPUT_SOURCE_MISSING")
    if invalid_sources:
        blockers.append("SELECTED_INPUT_SOURCE_INVALID")
    if confidence.total < 35:
        blockers.append("DATA_CONFIDENCE_LT_35")

    warnings = [
        *plan.warnings,
        *confidence.caveats,
    ]
    if market_observation is not None and market_observation.freshness is Freshness.STALE_LAST_GOOD:
        warnings.append("STALE_LAST_GOOD")
    blockers = list(dict.fromkeys(blockers))
    warnings = list(dict.fromkeys(warnings))
    qualitative_score = confidence.domains.get("qualitative_and_veto_coverage", 0)
    important_proxy = any(
        (
            plan.coverage_basis is MetricBasis.PROXY,
            plan.balance_sheet_basis is MetricBasis.PROXY,
            plan.growth_basis is MetricBasis.CONSERVATIVE_DEFAULT,
            market_value.estimated,
            qualitative_score == 0,
        )
    )
    if blockers:
        tier = CompanyDataTier.BLOCKED
    elif confidence.total < 60 or important_proxy:
        tier = CompanyDataTier.ESTIMATED
    else:
        tier = CompanyDataTier.CALCULABLE
    verified_ready = bool(
        tier is not CompanyDataTier.BLOCKED
        and confidence.total >= 85
        and len(plan.distribution_years) >= 5
        and dividend_lag == 0
        and len(plan.coverage_years) >= 4
        and coverage_lag == 0
        and market_value.relative_difference is not None
        and market_value.relative_difference <= Decimal("0.02")
        and balance_sheet.basis is MetricBasis.DIRECT
        and plan.valuation_basis is not MetricBasis.VENDOR_AUTHORIZED
        and qualitative_score == 10
    )
    if verified_ready:
        tier = CompanyDataTier.VERIFIED
    freshness = (
        market_observation.freshness
        if market_observation is not None
        else Freshness.STALE_LAST_GOOD
    )
    return CompanyAssessment(
        company_id=str(raw.get("company_id") or ""),
        data_tier=tier,
        data_confidence=confidence,
        freshness=freshness,
        input_plan=plan,
        market_value=market_value,
        balance_sheet=balance_sheet,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        missing_source_field_ids=missing_sources,
        invalid_source_field_ids=tuple(invalid_sources),
    )


def _contains_non_finite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, Decimal):
        return not value.is_finite()
    if isinstance(value, Mapping):
        return any(_contains_non_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite(item) for item in value)
    return False


def assess_release_records(records: Iterable[Mapping[str, Any]]) -> ReleaseAssessment:
    errors: list[str] = []
    company_ids: set[str] = set()
    rows = list(records)
    if not rows:
        errors.append("EMPTY_RELEASE")
    for index, row in enumerate(rows):
        company_id = str(row.get("company_id") or "")
        if not company_id:
            errors.append(f"MISSING_COMPANY_ID:{index}")
        elif company_id in company_ids:
            errors.append(f"DUPLICATE_COMPANY_ID:{company_id}")
        company_ids.add(company_id)
        if _contains_non_finite(row):
            errors.append(f"NON_FINITE_VALUE:{company_id or index}")
        if str(row.get("data_tier") or "") not in {item.value for item in CompanyDataTier}:
            errors.append(f"INVALID_DATA_TIER:{company_id or index}")
    return ReleaseAssessment(
        validity=(
            ReleaseValidity.REJECTED_RELEASE
            if errors
            else ReleaseValidity.VALID_RELEASE
        ),
        errors=tuple(errors),
    )
