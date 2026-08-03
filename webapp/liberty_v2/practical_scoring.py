"""Practical, missing-aware shareholder-return v2.1 scoring primitives.

This module is intentionally independent from the current strict publication
pipeline.  It provides deterministic building blocks for the v2.1 migration:
unknown raw values remain unknown, while explicit policy defaults are carried
as a separate calculation basis.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum
from typing import Mapping, Sequence

from .calculations import (
    CalculationError,
    InsufficientDataError,
    clip,
    piecewise_score,
    to_decimal,
)


ZERO = Decimal("0")
ONE = Decimal("1")
FIFTY = Decimal("50")
SIXTY = Decimal("60")


class CalculationBasis(str, Enum):
    DIRECT = "DIRECT"
    DERIVED = "DERIVED"
    PROXY = "PROXY"
    CONSERVATIVE_DEFAULT = "CONSERVATIVE_DEFAULT"
    UNAVAILABLE = "UNAVAILABLE"


class PracticalDataTier(str, Enum):
    BLOCKED = "BLOCKED"
    ESTIMATED = "ESTIMATED"
    CALCULABLE = "CALCULABLE"
    VERIFIED = "VERIFIED"


class ValuationBasis(str, Enum):
    COMPARABLE_HISTORY = "COMPARABLE_HISTORY"
    INDUSTRY_PERCENTILE = "INDUSTRY_PERCENTILE"
    PEER_MEDIAN = "PEER_MEDIAN"
    CURRENT_ONLY = "CURRENT_ONLY"


@dataclass(frozen=True)
class PolicyValue:
    value: Decimal
    basis: CalculationBasis
    warning_code: str | None = None


@dataclass(frozen=True)
class ScoreResult:
    value: Decimal
    imputed_components: tuple[str, ...] = ()
    uplift: Decimal = ZERO


@dataclass(frozen=True)
class ConfidenceAdjustment:
    code: str
    points: Decimal
    reason_zh: str

    def __post_init__(self) -> None:
        if to_decimal(self.points) < ZERO:
            raise CalculationError("confidence deduction cannot be negative")


RETURN_SCORE_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("0.00"), Decimal("0")),
    (Decimal("0.01"), Decimal("20")),
    (Decimal("0.02"), Decimal("40")),
    (Decimal("0.03"), Decimal("60")),
    (Decimal("0.04"), Decimal("75")),
    (Decimal("0.05"), Decimal("88")),
    (Decimal("0.06"), Decimal("100")),
)

PAYOUT_QUALITY_WEIGHTS: Mapping[str, Decimal] = {
    "coverage": Decimal("0.35"),
    "recent_trend": Decimal("0.20"),
    "history_stability": Decimal("0.20"),
    "balance_sheet": Decimal("0.15"),
    "buyback_quality": Decimal("0.10"),
}

RECOMMENDATION_WEIGHTS: Mapping[str, Decimal] = {
    "return_score": Decimal("0.45"),
    "payout_quality": Decimal("0.30"),
    "business_durability": Decimal("0.15"),
    "governance_capital_allocation": Decimal("0.10"),
}

ENTRY_RISK_WEIGHTS: Mapping[str, Decimal] = {
    "distribution_deterioration": Decimal("0.20"),
    "coverage": Decimal("0.20"),
    "balance_sheet": Decimal("0.15"),
    "structural_cycle": Decimal("0.15"),
    "policy_asset_life": Decimal("0.12"),
    "valuation_trap": Decimal("0.10"),
    "governance": Decimal("0.08"),
}

CONFIDENCE_DEDUCTIONS: Mapping[str, tuple[Decimal, str]] = {
    "VENDOR_TOTAL_MARKET_CAP": (Decimal("4"), "公司总市值使用行情商代理。"),
    "COVERED_CLASS_GROSS_UP": (Decimal("8"), "公司总市值由已覆盖股份类别保守放大。"),
    "SIMPLIFIED_FCF": (Decimal("5"), "租赁本金缺失，FCF使用经营现金流减资本开支。"),
    "BUYBACK_NOT_CREDITED": (Decimal("3"), "稀释后股本桥缺失，回购未计入评分。"),
    "ONLY_TWO_DIVIDEND_YEARS": (Decimal("8"), "普通分红历史只有两个完整财年。"),
    "THREE_TO_FOUR_DIVIDEND_YEARS": (Decimal("4"), "普通分红历史少于五个完整财年。"),
    "ONLY_TWO_COVERAGE_YEARS": (Decimal("6"), "覆盖能力历史只有两个完整财年。"),
    "ONLY_THREE_COVERAGE_YEARS": (Decimal("3"), "覆盖能力历史只有三个完整财年。"),
    "SHORT_GROWTH_SERIES": (Decimal("5"), "增长序列只有两至三个完整财年。"),
    "GROWTH_NOT_CREDITED": (Decimal("8"), "没有可用增长序列，评分不计正增长。"),
    "VALUATION_INDUSTRY_PROXY": (Decimal("4"), "估值使用行业分位或同业中位数。"),
    "VALUATION_CURRENT_ONLY": (Decimal("7"), "只有当前估值，缺少可比基准。"),
    "BUSINESS_DURABILITY_DEFAULT": (Decimal("5"), "业务耐久度使用中性默认值。"),
    "GOVERNANCE_DEFAULT": (Decimal("4"), "治理与资本配置使用默认值。"),
    "MISSING_ERI_COMPONENT": (Decimal("3"), "一个ERI定性分项使用谨慎默认值。"),
    "RECONCILIATION_NOT_RUN": (Decimal("1"), "一项会计对账尚未运行。"),
    "RECONCILIATION_WARNING": (Decimal("1"), "一项会计对账存在2%至5%差异。"),
    "RECONCILIATION_CONSERVATIVE": (Decimal("3"), "一项会计对账存在5%至10%差异。"),
    "UNKNOWN_VETO": (Decimal("1"), "一项否决状态未知。"),
    "SINGLE_NON_OFFICIAL_SOURCE": (Decimal("2"), "一个核心领域只有单一非官方来源。"),
    "STALE_QUOTE_24_72H": (Decimal("5"), "核心行情已超过24小时但不超过72小时。"),
    "BALANCE_SHEET_PROXY": (Decimal("4"), "资产负债表风险使用替代指标。"),
}


HIGH_IMPACT_PROXY_CODES = {
    "COVERED_CLASS_GROSS_UP",
    "GROWTH_NOT_CREDITED",
    "VALUATION_CURRENT_ONLY",
}


def _weighted_with_defaults(
    values: Mapping[str, Decimal | None],
    weights: Mapping[str, Decimal],
    defaults: Mapping[str, Decimal],
) -> ScoreResult:
    missing_keys = set(weights) - set(values)
    extra_keys = set(values) - set(weights)
    if missing_keys or extra_keys:
        raise CalculationError(
            f"score component keys mismatch: missing={sorted(missing_keys)} extra={sorted(extra_keys)}"
        )
    total_weight = sum((to_decimal(weight) for weight in weights.values()), ZERO)
    if total_weight <= ZERO:
        raise CalculationError("score weights must be positive")
    imputed: list[str] = []
    numerator = ZERO
    for name, weight in weights.items():
        raw_value = values[name]
        if raw_value is None:
            if name not in defaults:
                raise InsufficientDataError(f"required score component is missing: {name}")
            value = to_decimal(defaults[name])
            imputed.append(name)
        else:
            value = to_decimal(raw_value)
        numerator += clip(value) * to_decimal(weight)
    return ScoreResult(
        value=clip(numerator / total_weight),
        imputed_components=tuple(imputed),
    )


def credited_buyback_for_score(eligible_buyback: Decimal | None) -> PolicyValue:
    """Do not convert the raw unknown into zero; only default the scored contribution."""

    if eligible_buyback is None:
        return PolicyValue(
            value=ZERO,
            basis=CalculationBasis.CONSERVATIVE_DEFAULT,
            warning_code="BUYBACK_NOT_CREDITED",
        )
    value = to_decimal(eligible_buyback)
    if value < ZERO:
        raise CalculationError("eligible buyback cannot be negative")
    return PolicyValue(value=value, basis=CalculationBasis.DERIVED)


def conservative_market_cap_from_coverage(
    covered_market_cap: Decimal,
    verified_coverage_ratio: Decimal,
    *,
    denominator_buffer: Decimal = Decimal("1.05"),
) -> PolicyValue:
    covered = to_decimal(covered_market_cap)
    ratio = to_decimal(verified_coverage_ratio)
    buffer = to_decimal(denominator_buffer)
    if covered <= ZERO:
        raise CalculationError("covered market cap must be positive")
    if ratio < Decimal("0.80") or ratio > ONE:
        raise InsufficientDataError("verified covered-class ratio must be within 0.80..1.00")
    if buffer < ONE:
        raise CalculationError("market-cap denominator buffer cannot be below one")
    return PolicyValue(
        value=covered / ratio * buffer,
        basis=CalculationBasis.PROXY,
        warning_code="COVERED_CLASS_GROSS_UP",
    )


def non_financial_fcf_value(
    operating_cash_flow: Decimal,
    capital_expenditure: Decimal,
    lease_principal: Decimal | None,
) -> PolicyValue:
    ocf = to_decimal(operating_cash_flow)
    capex = to_decimal(capital_expenditure)
    if capex < ZERO:
        raise CalculationError("capital expenditure must be normalized as a positive cash outflow")
    if lease_principal is None:
        return PolicyValue(
            value=ocf - capex,
            basis=CalculationBasis.PROXY,
            warning_code="SIMPLIFIED_FCF",
        )
    lease = to_decimal(lease_principal)
    if lease < ZERO:
        raise CalculationError("lease principal repayment cannot be negative")
    return PolicyValue(value=ocf - capex - lease, basis=CalculationBasis.DERIVED)


def fcf_capacity_haircut(*, simplified: bool) -> Decimal:
    return Decimal("0.85") if simplified else Decimal("0.90")


def return_score_practical(cr10: Decimal) -> Decimal:
    return clip(piecewise_score(to_decimal(cr10), RETURN_SCORE_BANDS))


def payout_quality_practical(
    *,
    coverage: Decimal | None,
    recent_trend: Decimal | None,
    history_stability: Decimal | None,
    balance_sheet: Decimal | None,
    buyback_quality: Decimal | None,
) -> ScoreResult:
    return _weighted_with_defaults(
        {
            "coverage": coverage,
            "recent_trend": recent_trend,
            "history_stability": history_stability,
            "balance_sheet": balance_sheet,
            "buyback_quality": buyback_quality,
        },
        PAYOUT_QUALITY_WEIGHTS,
        {
            "balance_sheet": FIFTY,
            "buyback_quality": FIFTY,
        },
    )


def practical_history_cap(history_years: int) -> Decimal:
    if history_years < 2:
        raise InsufficientDataError("at least two complete dividend years are required")
    if history_years == 2:
        return Decimal("70")
    if history_years <= 4:
        return Decimal("85")
    return Decimal("100")


def recommendation_index_practical(
    *,
    return_score_value: Decimal | None,
    payout_quality_value: Decimal | None,
    business_durability: Decimal | None,
    governance_capital_allocation: Decimal | None,
    history_years: int,
) -> ScoreResult:
    raw = _weighted_with_defaults(
        {
            "return_score": return_score_value,
            "payout_quality": payout_quality_value,
            "business_durability": business_durability,
            "governance_capital_allocation": governance_capital_allocation,
        },
        RECOMMENDATION_WEIGHTS,
        {
            "business_durability": FIFTY,
            "governance_capital_allocation": FIFTY,
        },
    )
    return ScoreResult(
        value=min(raw.value, practical_history_cap(history_years)),
        imputed_components=raw.imputed_components,
    )


def entry_risk_index_practical(
    components: Mapping[str, Decimal | None],
    *,
    unknown_veto_count: int = 0,
    triggered_non_major_veto_count: int = 0,
) -> ScoreResult:
    if unknown_veto_count < 0 or triggered_non_major_veto_count < 0:
        raise CalculationError("veto counts cannot be negative")
    base = _weighted_with_defaults(
        components,
        ENTRY_RISK_WEIGHTS,
        {name: SIXTY for name in ENTRY_RISK_WEIGHTS},
    )
    unknown_uplift = min(
        Decimal("8"),
        Decimal("1.25") * Decimal(unknown_veto_count),
    )
    warning_uplift = min(
        Decimal("10"),
        Decimal("5") * Decimal(triggered_non_major_veto_count),
    )
    uplift = unknown_uplift + warning_uplift
    return ScoreResult(
        value=clip(base.value + uplift),
        imputed_components=base.imputed_components,
        uplift=uplift,
    )


def valuation_adjustment_practical(
    basis: ValuationBasis,
    *,
    current: Decimal | None = None,
    reference: Decimal | None = None,
    percentile: Decimal | None = None,
) -> PolicyValue:
    if basis in {ValuationBasis.COMPARABLE_HISTORY, ValuationBasis.PEER_MEDIAN}:
        if current is None or reference is None:
            raise InsufficientDataError("current and reference valuations are required")
        present = to_decimal(current)
        comparison = to_decimal(reference)
        if present <= ZERO or comparison <= ZERO:
            raise InsufficientDataError("valuation multiples must be positive")
        with localcontext() as context:
            context.prec = 36
            adjustment = context.power(comparison / present, ONE / Decimal("10")) - ONE
        warning = (
            "VALUATION_INDUSTRY_PROXY"
            if basis is ValuationBasis.PEER_MEDIAN
            else None
        )
        return PolicyValue(
            value=min(ZERO, adjustment),
            basis=(
                CalculationBasis.PROXY
                if basis is ValuationBasis.PEER_MEDIAN
                else CalculationBasis.DERIVED
            ),
            warning_code=warning,
        )
    if basis is ValuationBasis.INDUSTRY_PERCENTILE:
        if percentile is None:
            raise InsufficientDataError("industry valuation percentile is required")
        point = to_decimal(percentile)
        if not ZERO <= point <= ONE:
            raise CalculationError("valuation percentile must be within 0..1")
        adjustment = -min(
            Decimal("0.03"),
            max(ZERO, point - Decimal("0.50")) * Decimal("0.06"),
        )
        return PolicyValue(
            value=adjustment,
            basis=CalculationBasis.PROXY,
            warning_code="VALUATION_INDUSTRY_PROXY",
        )
    if basis is ValuationBasis.CURRENT_ONLY:
        if current is None or to_decimal(current) <= ZERO:
            raise InsufficientDataError("a positive current valuation is required")
        return PolicyValue(
            value=Decimal("-0.005"),
            basis=CalculationBasis.CONSERVATIVE_DEFAULT,
            warning_code="VALUATION_CURRENT_ONLY",
        )
    raise CalculationError(f"unsupported valuation basis: {basis}")


def valuation_trap_risk_practical(
    basis: ValuationBasis,
    *,
    current: Decimal | None = None,
    reference: Decimal | None = None,
    percentile: Decimal | None = None,
) -> Decimal:
    if basis is ValuationBasis.INDUSTRY_PERCENTILE:
        if percentile is None:
            raise InsufficientDataError("industry valuation percentile is required")
        point = to_decimal(percentile)
        if not ZERO <= point <= ONE:
            raise CalculationError("valuation percentile must be within 0..1")
        return clip(Decimal("20") + Decimal("80") * point)
    if basis in {ValuationBasis.COMPARABLE_HISTORY, ValuationBasis.PEER_MEDIAN}:
        if current is None or reference is None:
            raise InsufficientDataError("current and reference valuations are required")
        present = to_decimal(current)
        comparison = to_decimal(reference)
        if present <= ZERO or comparison <= ZERO:
            raise InsufficientDataError("valuation multiples must be positive")
        ratio = present / comparison
        return clip(
            piecewise_score(
                ratio,
                (
                    (Decimal("0"), Decimal("20")),
                    (Decimal("0.70"), Decimal("20")),
                    (Decimal("1.00"), Decimal("45")),
                    (Decimal("1.30"), Decimal("65")),
                    (Decimal("1.60"), Decimal("80")),
                    (Decimal("2.00"), Decimal("100")),
                ),
            )
        )
    if basis is ValuationBasis.CURRENT_ONLY:
        if current is None or to_decimal(current) <= ZERO:
            raise InsufficientDataError("a positive current valuation is required")
        return SIXTY
    raise CalculationError(f"unsupported valuation basis: {basis}")


def confidence_adjustment(
    code: str,
    *,
    count: int = 1,
    cap: Decimal | None = None,
) -> ConfidenceAdjustment:
    if code not in CONFIDENCE_DEDUCTIONS:
        raise CalculationError(f"unknown confidence adjustment: {code}")
    if count < 1:
        raise CalculationError("confidence adjustment count must be positive")
    per_item, reason = CONFIDENCE_DEDUCTIONS[code]
    points = per_item * Decimal(count)
    if cap is not None:
        points = min(points, to_decimal(cap))
    return ConfidenceAdjustment(code=code, points=points, reason_zh=reason)


def data_confidence(adjustments: Sequence[ConfidenceAdjustment]) -> Decimal:
    total = sum((to_decimal(item.points) for item in adjustments), ZERO)
    return clip(Decimal("100") - total)


def has_high_impact_proxy(adjustments: Sequence[ConfidenceAdjustment]) -> bool:
    return any(item.code in HIGH_IMPACT_PROXY_CODES for item in adjustments)


def determine_data_tier(
    confidence: Decimal,
    *,
    blockers: Sequence[str] = (),
    high_impact_proxy: bool = False,
    verified_core: bool = False,
) -> PracticalDataTier:
    score = clip(to_decimal(confidence))
    if blockers or score < Decimal("35"):
        return PracticalDataTier.BLOCKED
    if verified_core and not high_impact_proxy and score >= Decimal("85"):
        return PracticalDataTier.VERIFIED
    if high_impact_proxy or score < Decimal("60"):
        return PracticalDataTier.ESTIMATED
    return PracticalDataTier.CALCULABLE


def investment_observation_class(
    ri: Decimal | None,
    eri: Decimal | None,
    confidence: Decimal,
    tier: PracticalDataTier,
    *,
    major_investment_veto: bool = False,
    unknown_major_veto: bool = False,
) -> str | None:
    if tier is PracticalDataTier.BLOCKED or ri is None or eri is None:
        return None
    recommendation = to_decimal(ri)
    risk = to_decimal(eri)
    trust = clip(to_decimal(confidence))
    if major_investment_veto or recommendation < Decimal("50") or risk > Decimal("65"):
        return "D"
    if tier is PracticalDataTier.ESTIMATED:
        return "C"
    if (
        recommendation >= Decimal("78")
        and risk <= Decimal("30")
        and trust >= Decimal("80")
        and not unknown_major_veto
    ):
        return "A"
    if recommendation >= Decimal("65") and risk <= Decimal("45") and trust >= Decimal("60"):
        return "B"
    return "C"
