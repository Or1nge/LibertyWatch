from __future__ import annotations

from decimal import Decimal

import pytest

from liberty_v2.calculations import InsufficientDataError
from liberty_v2.practical_scoring import (
    CalculationBasis,
    PracticalDataTier,
    ValuationBasis,
    confidence_adjustment,
    conservative_market_cap_from_coverage,
    credited_buyback_for_score,
    data_confidence,
    determine_data_tier,
    entry_risk_index_practical,
    fcf_capacity_haircut,
    has_high_impact_proxy,
    investment_observation_class,
    non_financial_fcf_value,
    payout_quality_practical,
    recommendation_index_practical,
    return_score_practical,
    valuation_adjustment_practical,
    valuation_trap_risk_practical,
)


D = Decimal


def test_unverified_buyback_is_not_disguised_as_raw_zero() -> None:
    scored = credited_buyback_for_score(None)
    assert scored.value == D("0")
    assert scored.basis is CalculationBasis.CONSERVATIVE_DEFAULT
    assert scored.warning_code == "BUYBACK_NOT_CREDITED"


def test_covered_share_class_market_cap_uses_conservative_buffer() -> None:
    result = conservative_market_cap_from_coverage(D("800"), D("0.80"))
    assert result.value == D("1050.00")
    assert result.basis is CalculationBasis.PROXY
    with pytest.raises(InsufficientDataError):
        conservative_market_cap_from_coverage(D("800"), D("0.79"))


def test_missing_lease_principal_uses_simplified_fcf_and_lower_haircut() -> None:
    result = non_financial_fcf_value(D("160"), D("20"), None)
    assert result.value == D("140")
    assert result.basis is CalculationBasis.PROXY
    assert result.warning_code == "SIMPLIFIED_FCF"
    assert fcf_capacity_haircut(simplified=True) == D("0.85")
    assert fcf_capacity_haircut(simplified=False) == D("0.90")


def test_return_score_uses_practical_piecewise_bands() -> None:
    assert return_score_practical(D("0")) == D("0")
    assert return_score_practical(D("0.04")) == D("75")
    assert return_score_practical(D("0.06")) == D("100")
    assert return_score_practical(D("0.035")) == D("67.5")


def test_payout_quality_defaults_only_non_core_components() -> None:
    result = payout_quality_practical(
        coverage=D("80"),
        recent_trend=D("70"),
        history_stability=D("90"),
        balance_sheet=None,
        buyback_quality=None,
    )
    assert result.value == D("72.5")
    assert result.imputed_components == ("balance_sheet", "buyback_quality")
    with pytest.raises(InsufficientDataError):
        payout_quality_practical(
            coverage=None,
            recent_trend=D("70"),
            history_stability=D("90"),
            balance_sheet=D("80"),
            buyback_quality=D("50"),
        )


def test_ri_uses_fixed_neutral_defaults_and_history_cap() -> None:
    result = recommendation_index_practical(
        return_score_value=D("100"),
        payout_quality_value=D("100"),
        business_durability=None,
        governance_capital_allocation=None,
        history_years=2,
    )
    assert result.value == D("70")
    assert result.imputed_components == (
        "business_durability",
        "governance_capital_allocation",
    )


def test_eri_uses_cautious_defaults_and_unknown_veto_uplift() -> None:
    result = entry_risk_index_practical(
        {
            "distribution_deterioration": D("20"),
            "coverage": D("20"),
            "balance_sheet": D("30"),
            "structural_cycle": None,
            "policy_asset_life": None,
            "valuation_trap": D("50"),
            "governance": None,
        },
        unknown_veto_count=7,
    )
    assert set(result.imputed_components) == {
        "structural_cycle",
        "policy_asset_life",
        "governance",
    }
    assert result.uplift == D("8")
    assert D("0") <= result.value <= D("100")


def test_valuation_proxies_never_add_positive_expansion() -> None:
    expensive = valuation_adjustment_practical(
        ValuationBasis.INDUSTRY_PERCENTILE,
        percentile=D("1"),
    )
    cheap = valuation_adjustment_practical(
        ValuationBasis.INDUSTRY_PERCENTILE,
        percentile=D("0.20"),
    )
    current_only = valuation_adjustment_practical(
        ValuationBasis.CURRENT_ONLY,
        current=D("15"),
    )
    assert expensive.value == D("-0.030")
    assert cheap.value == D("0")
    assert current_only.value == D("-0.005")
    assert valuation_trap_risk_practical(
        ValuationBasis.INDUSTRY_PERCENTILE,
        percentile=D("0.50"),
    ) == D("60.00")


def test_confidence_deductions_are_explicit_and_versionable() -> None:
    adjustments = [
        confidence_adjustment("VENDOR_TOTAL_MARKET_CAP"),
        confidence_adjustment("SIMPLIFIED_FCF"),
        confidence_adjustment("UNKNOWN_VETO", count=7, cap=D("5")),
    ]
    assert data_confidence(adjustments) == D("86")
    assert has_high_impact_proxy(adjustments) is False
    high_impact = [confidence_adjustment("VALUATION_CURRENT_ONLY")]
    assert has_high_impact_proxy(high_impact) is True


def test_data_tier_separates_blocked_estimated_calculable_verified() -> None:
    assert determine_data_tier(D("90"), blockers=["IDENTITY_CONFLICT"]) is PracticalDataTier.BLOCKED
    assert determine_data_tier(D("34")) is PracticalDataTier.BLOCKED
    assert determine_data_tier(D("80"), high_impact_proxy=True) is PracticalDataTier.ESTIMATED
    assert determine_data_tier(D("59")) is PracticalDataTier.ESTIMATED
    assert determine_data_tier(D("80")) is PracticalDataTier.CALCULABLE
    assert determine_data_tier(D("90"), verified_core=True) is PracticalDataTier.VERIFIED


def test_investment_class_does_not_confuse_bad_data_with_bad_company() -> None:
    assert investment_observation_class(
        D("90"), D("20"), D("90"), PracticalDataTier.BLOCKED
    ) is None
    assert investment_observation_class(
        D("90"), D("20"), D("55"), PracticalDataTier.ESTIMATED
    ) == "C"
    assert investment_observation_class(
        D("80"), D("25"), D("82"), PracticalDataTier.CALCULABLE
    ) == "A"
    assert investment_observation_class(
        D("70"), D("40"), D("70"), PracticalDataTier.CALCULABLE
    ) == "B"
    assert investment_observation_class(
        D("80"), D("70"), D("90"), PracticalDataTier.VERIFIED
    ) == "D"
