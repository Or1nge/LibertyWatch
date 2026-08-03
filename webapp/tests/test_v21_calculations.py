from __future__ import annotations

from decimal import Decimal

from liberty_v2.calculations import (
    conservative_growth_contribution_v21,
    effective_distribution_v21,
    entry_risk_index_v21,
    payout_quality_score_v21,
    return_score_v21,
    sustainable_distribution_non_financial_v21,
)


D = Decimal


def test_v21_distribution_has_no_qb_and_missing_buyback_credits_zero() -> None:
    assert effective_distribution_v21(D("100"), None) == D("100")
    assert effective_distribution_v21(D("100"), D("12")) == D("112")


def test_v21_simplified_fcf_uses_85_percent_capacity() -> None:
    sustainable, capacity = sustainable_distribution_non_financial_v21(
        D("200"),
        [D("100"), D("120")],
        simplified_fcf=True,
    )
    assert capacity == D("110")
    assert sustainable == D("93.50")


def test_v21_growth_caps_depend_on_continuous_history_length() -> None:
    assert conservative_growth_contribution_v21(D("0.08"), year_count=5) == D("0.03")
    assert conservative_growth_contribution_v21(D("0.08"), year_count=4) == D("0.02")
    assert conservative_growth_contribution_v21(D("0.08"), year_count=2) == 0
    assert conservative_growth_contribution_v21(D("-0.06"), year_count=2) == D("-0.06")


def test_v21_return_score_uses_the_fixed_piecewise_knots() -> None:
    expected = {
        "0": "0",
        "0.01": "20",
        "0.02": "40",
        "0.03": "60",
        "0.04": "75",
        "0.05": "88",
        "0.06": "100",
    }
    assert {point: str(return_score_v21(D(point))) for point in expected} == expected
    assert return_score_v21(D("0.045")) == D("81.5")


def test_v21_payout_and_eri_have_no_data_quality_or_buyback_weight() -> None:
    payout = payout_quality_score_v21(
        {
            "coverage": D("100"),
            "recent_trend": D("80"),
            "history_stability": D("60"),
            "balance_sheet": D("40"),
        }
    )
    assert payout == D("78")
    eri = entry_risk_index_v21(
        {
            "distribution_deterioration": D("10"),
            "coverage": D("20"),
            "balance_sheet": D("30"),
            "structural_cycle": D("40"),
            "policy_asset_life": D("50"),
            "valuation": D("60"),
            "governance": D("70"),
        },
        unknown_veto_uplift=D("5"),
    )
    assert eri == D("39.5")
