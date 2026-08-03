from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from liberty_v2.calculations import (
    CalculationError,
    InsufficientDataError,
    buyback_persistence_factor,
    company_market_cap,
    conservative_growth_contribution,
    effective_distribution,
    eligible_buyback,
    entry_risk_index,
    historical_conservative_distribution,
    history_recommendation_cap,
    payout_quality_score,
    recommendation_class,
    recommendation_index,
    return_score,
    robust_organic_growth,
    security_prices_at_four_percent,
    shareholder_yield,
    sustainable_distribution_non_financial,
    to_decimal,
    valuation_drag,
)
from liberty_v2.coverage import (
    InsuranceSurplusAdapter,
    InsuranceSurplusInput,
    NonFinancialFCFAdapter,
    ensure_non_financial_adapter,
)
from liberty_v2.models import (
    CoverageStatus,
    CoverageResult,
    DataStatus,
    FCFYear,
    IndustryKind,
    RawDataPoint,
    SecurityClassInput,
)
from liberty_v2.registry import load_metric_definitions, load_policy
from liberty_v2.pipeline import SlowVariables, compute_company_snapshot, compute_fast_variables, compute_slow_variables
from liberty_v2.slow_cache import load_or_compute_slow
from liberty_v2.veto import evaluate_vetoes
from liberty_v2.validation import (
    reconcile_value,
    required_provenance_field_ids,
    select_latest_restatements,
    validate_raw_provenance_records,
    validate_raw_points,
    validate_required_provenance_fields,
)


D = Decimal
NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


def security(
    security_id: str,
    share_class: str,
    *,
    price: str = "10",
    shares: str = "100",
    fx: str = "1",
    timestamp: datetime = NOW,
    rights_verified: bool = True,
    rights_factor: str | None = None,
) -> SecurityClassInput:
    return SecurityClassInput(
        security_id=security_id,
        share_class=share_class,
        price=D(price),
        issued_shares=D(shares),
        currency="CNY" if share_class == "A" else "HKD",
        fx_to_base=D(fx),
        price_timestamp=timestamp,
        quote_status=DataStatus.VALID,
        rights_verified=rights_verified,
        economic_rights_factor=D(rights_factor) if rights_factor is not None else None,
    )


def raw_point(
    *,
    value: Decimal | None = D("100"),
    status: DataStatus = DataStatus.VALID,
    fetched: datetime = NOW,
    restatement: str = "ORIGINAL",
    unit: str = "currency",
) -> RawDataPoint:
    return RawDataPoint(
        company_id="issuer-1",
        field_id="FY2025.test_amount",
        security_id="security-1",
        share_class="A",
        source_name="exchange",
        source_document="announcement-1",
        source_url_or_local_path="https://example.com/a",
        source_publish_date=date(2026, 3, 1),
        source_fetch_time=fetched,
        fiscal_period="FY2025",
        currency="CNY",
        unit=unit,
        value=value,
        data_status=status,
        restatement_status=restatement,
    )


def test_golden_01_ten_year_stable_dividends() -> None:
    assert historical_conservative_distribution([D("100")] * 10) == D("100")


def test_golden_02_latest_dividend_suspension_drops_h_quickly() -> None:
    stable = historical_conservative_distribution([D("100")] * 10)
    suspended = historical_conservative_distribution([D("0"), *([D("100")] * 9)])
    assert suspended < stable
    assert suspended == D("57.7500")


def test_golden_03_latest_surge_is_capped_by_history_median() -> None:
    value = historical_conservative_distribution([D("300"), *([D("100")] * 9)])
    assert value == D("110.00")


def test_golden_04_special_dividend_does_not_enter_h() -> None:
    base = [effective_distribution(D("100"), D("0"), D("0")) for _ in range(5)]
    with_special = list(base)
    assert historical_conservative_distribution(base) == historical_conservative_distribution(with_special)


def test_golden_05_announced_but_unexecuted_buyback_is_zero() -> None:
    assert eligible_buyback(D("0"), D("0"), D("0")) == 0


def test_golden_06_executed_but_not_cancelled_buyback_is_zero() -> None:
    assert eligible_buyback(D("100"), D("0"), D("0")) == 0


def test_golden_07_cancelled_buyback_fully_offset_by_dilution_is_zero() -> None:
    assert eligible_buyback(D("100"), D("10"), D("0")) == 0


def test_golden_08_partial_net_reduction_is_proportional() -> None:
    assert eligible_buyback(D("100"), D("10"), D("4")) == D("40")


def test_golden_09_two_cyclical_peak_years_cannot_overrun_median_cap() -> None:
    value = historical_conservative_distribution([D("200"), D("200"), *([D("100")] * 8)])
    assert value <= D("110")


def test_golden_10_fcf_cannot_cover_distribution() -> None:
    sustainable = sustainable_distribution_non_financial(D("100"), D("60"))
    assert sustainable == D("54.0")
    assert sustainable <= D("100")


def test_golden_11_negative_fcf_closes_sustainable_distribution() -> None:
    assert sustainable_distribution_non_financial(D("100"), D("-20")) == 0


def test_golden_12_listing_under_three_years_is_not_recommendable() -> None:
    assert historical_conservative_distribution([D("100"), D("80")]) == D("93.0")
    assert history_recommendation_cap(2) == (D("50"), False)


def test_golden_13_five_to_seven_year_history_caps_recommendation() -> None:
    score, complete = recommendation_index(
        return_score_value=D("100"),
        payout_quality_value=D("100"),
        business_durability=D("100"),
        governance_capital_allocation=D("100"),
        history_years=6,
    )
    assert score == D("80")
    assert complete is True


def test_golden_14_negative_valuation_is_not_comparable() -> None:
    with pytest.raises(InsufficientDataError):
        valuation_drag(D("10"), D("-2"))


def test_golden_15_bank_must_not_use_nonfinancial_fcf() -> None:
    adapter = NonFinancialFCFAdapter(
        [FCFYear(2025, D("10"), D("2"), D("1")), FCFYear(2024, D("10"), D("2"), D("1"))]
    )
    with pytest.raises(InsufficientDataError):
        ensure_non_financial_adapter(IndustryKind.BANK, adapter)


def test_golden_16_insurer_missing_free_surplus_returns_insufficient() -> None:
    adapter = InsuranceSurplusAdapter(
        InsuranceSurplusInput(None, None, None, None, None, None, None, None, None)
    )
    result = adapter.calculate(D("100"))
    assert result.status is CoverageStatus.INSUFFICIENT_DATA
    assert result.sustainable_distribution is None


def test_golden_17_ah_dividend_cannot_use_h_market_cap_only() -> None:
    with pytest.raises(InsufficientDataError):
        company_market_cap([security("h", "H", fx="0.9")], expected_share_classes=["A", "H"], now=NOW)


def test_golden_18_stale_ah_quote_blocks_company_yield() -> None:
    rows = [
        security("a", "A"),
        security("h", "H", fx="0.9", timestamp=NOW - timedelta(hours=25)),
    ]
    with pytest.raises(InsufficientDataError, match="stale"):
        company_market_cap(rows, expected_share_classes=["A", "H"], now=NOW)


def test_golden_19_yuan_and_hundred_million_yuan_mismatch_is_rejected() -> None:
    issue = reconcile_value(D("1"), D("100000000"), field="amount", absolute_tolerance=D("0"))
    assert issue is not None and issue.code == "RECONCILIATION_MISMATCH"


def test_golden_20_currency_missing_or_double_conversion_is_detected() -> None:
    market_cap = company_market_cap(
        [security("h", "H", price="10", shares="100", fx="0.9")],
        expected_share_classes=["H"],
        now=NOW,
    )
    assert market_cap == D("900")
    assert reconcile_value(D("810"), market_cap, field="market_cap", absolute_tolerance=D("0")) is not None


def test_golden_21_split_and_consolidation_preserve_market_cap() -> None:
    before = company_market_cap([security("a", "A", price="10", shares="100")], expected_share_classes=["A"], now=NOW)
    after_split = company_market_cap([security("a", "A", price="5", shares="200")], expected_share_classes=["A"], now=NOW)
    assert before == after_split


def test_golden_22_duplicate_announcement_is_detected() -> None:
    result = validate_raw_points([raw_point(), raw_point()])
    assert result.status.value == "INVALID"
    assert any(issue.code == "DUPLICATE_SOURCE_RECORD" for issue in result.issues)


def test_golden_23_latest_restatement_replaces_original() -> None:
    original = raw_point(value=D("100"), fetched=NOW, restatement="ORIGINAL")
    restated = raw_point(value=D("90"), fetched=NOW + timedelta(hours=1), restatement="RESTATED")
    selected = select_latest_restatements([original, restated])
    assert len(selected) == 1
    assert selected[0].value == D("90")
    assert selected[0].restatement_status == "RESTATED"


def test_golden_24_missing_value_cannot_be_disguised_as_zero() -> None:
    with pytest.raises(ValueError):
        raw_point(value=None, status=DataStatus.VALID)
    with pytest.raises(ValueError):
        raw_point(value=D("0"), status=DataStatus.VALID)
    zero = raw_point(value=D("0"), status=DataStatus.KNOWN_ZERO)
    assert zero.value == 0


def test_golden_25_price_changes_only_change_fast_yield() -> None:
    distribution = D("100")
    low_market_cap = D("1000")
    high_market_cap = D("2000")
    assert shareholder_yield(distribution, high_market_cap) < shareholder_yield(distribution, low_market_cap)
    assert historical_conservative_distribution([distribution] * 5) == distribution


def test_invariants_for_growth_drag_scores_and_determinism() -> None:
    positive = conservative_growth_contribution(robust_organic_growth([D("100"), D("106")]))
    negative = conservative_growth_contribution(D("-0.05"))
    assert 0 <= positive <= D("0.03")
    assert negative == D("-0.05")
    assert valuation_drag(D("10"), D("20")) <= 0
    assert valuation_drag(D("20"), D("10")) == 0
    assert 0 <= return_score(D("0.04")) <= 100
    components = {
        "coverage": D("70"),
        "recent_trend": D("80"),
        "history_stability": D("90"),
        "balance_sheet": D("50"),
        "buyback_quality": D("70"),
        "data_completeness": D("100"),
    }
    assert payout_quality_score(components) == payout_quality_score(components)
    risk = entry_risk_index(
        {
            "distribution_deterioration": D("10"),
            "coverage": D("20"),
            "balance_sheet": D("30"),
            "structural_cycle": D("40"),
            "policy_asset_life": D("50"),
            "valuation_trap": D("60"),
            "governance": D("70"),
            "data_quality": D("80"),
        }
    )
    assert 0 <= risk <= 100
    for bad in (D("NaN"), D("Infinity")):
        with pytest.raises(CalculationError):
            to_decimal(bad)
    assert (
        recommendation_class(
            D("90"),
            D("20"),
            unresolved_veto=False,
            major_veto=False,
            data_complete=False,
        )
        == "C"
    )


def test_dilution_never_increases_eligible_buyback() -> None:
    full = eligible_buyback(D("100"), D("10"), D("10"))
    partial = eligible_buyback(D("100"), D("10"), D("5"))
    diluted = eligible_buyback(D("100"), D("10"), D("-1"))
    assert full >= partial >= diluted == 0


def test_security_four_percent_price_requires_verified_ah_rights() -> None:
    rows = [security("a", "A", rights_verified=False), security("h", "H", fx="0.9")]
    with pytest.raises(InsufficientDataError):
        security_prices_at_four_percent(D("100"), rows)
    with pytest.raises(InsufficientDataError, match="rights factors"):
        security_prices_at_four_percent(
            D("100"),
            [security("a", "A", rights_verified=True)],
        )
    prices = security_prices_at_four_percent(
        D("100"),
        [security("a", "A", rights_factor="1")],
    )
    assert prices["a"] == D("25")


def test_registry_is_version_locked_and_complete() -> None:
    definitions = load_metric_definitions()
    policy = load_policy()
    assert definitions["definition_version"] == policy["metric_definition_version"]
    assert len(definitions["metrics"]) >= 20
    codex = policy["codex"]
    assert codex["model"] == "gpt-5.6-sol"
    assert codex["reasoning_effort"] == "xhigh"
    assert codex["sandbox"] == "read-only"
    assert codex["reviewed_overlay_max_validity_days"] == 365
    rubric = codex["reviewed_overlay_score_rubric"]
    assert rubric["version"] == "qualitative-score-rubric-v1.0.0"
    assert len(rubric["business_durability"]["dimensions"]) == 4
    assert len(rubric["governance_capital_allocation"]["dimensions"]) == 4


def test_pipeline_marks_only_qualitative_overlay_gap_analysis_eligible() -> None:
    slow = SlowVariables(
        annual_effective_distributions=((2025, D("100")), (2024, D("100")), (2023, D("100"))),
        q_b=D("0"),
        r2=D("100"),
        m5=D("100"),
        t10=D("100"),
        historical_distribution=D("100"),
        coverage=CoverageResult(
            status=CoverageStatus.VALID,
            adapter="NonFinancialFCFAdapter",
            sustainable_distribution=D("90"),
            coverage_ratio=D("1.5"),
            capacity=D("135"),
        ),
        organic_growth=D("0.02"),
        conservative_growth=D("0.01"),
        payout_quality=D("80"),
        eri=None,
        veto_flags=(),
        business_durability=None,
        governance=None,
        qualitative_overlay_pending=True,
        errors=("entry_risk:structured risk component missing or expired",),
    )
    raw = {
        "industry_kind": "NON_FINANCIAL",
        "expected_share_classes": ["A"],
        "share_classes": [
            {
                "security_id": "a",
                "share_class": "A",
                "price": "10",
                "issued_shares": "100",
                "currency": "CNY",
                "fx_to_base": "1",
                "price_timestamp": NOW.isoformat(),
                "quote_status": "VALID",
                "rights_verified": True,
                "economic_rights_factor": "1",
            }
        ],
        "annual_distributions": [
            {"ordinary_dividend": "100", "gross_cancelled_buyback": "0"}
        ],
        "reconciliation_inputs": {
            "dividend_per_share_times_entitled_shares": "100",
            "repurchased_shares_times_average_price": "0",
            "opening_minus_closing_shares": "0",
            "cancelled_minus_issued_and_converted": "0",
        },
        "valuation": {
            "metric": "P_FCF",
            "historical_median": "10",
            "current": "10",
            "basis_consistent": True,
        },
    }
    fast = compute_fast_variables(raw, slow, now=NOW)
    assert fast["data_status"].value == "PARTIAL"
    assert fast["analysis_eligibility"]["status"] == "CORE_VALID_QUALITATIVE_OVERLAY_PENDING"
    assert fast["analysis_eligibility"]["eligible"] is True

    invalid_core = compute_fast_variables(
        raw,
        replace(slow, errors=(*slow.errors, "provenance:RAW_SOURCE_CONFLICT")),
        now=NOW,
    )
    assert invalid_core["analysis_eligibility"]["status"] == "NOT_ELIGIBLE"


def test_price_only_refresh_reuses_slow_cache_and_keeps_history(tmp_path) -> None:
    score = {
        "value": "80",
        "source": "exchange structured proxy",
        "as_of_date": "2026-01-01",
        "expires_at": "2026-12-31",
        "reason": "test fixture",
    }
    raw = {
        "company_id": "issuer-cache",
        "company_name": "快慢变量测试",
        "industry_kind": "NON_FINANCIAL",
        "expected_share_classes": ["A"],
        "securities": [{"security_id": "a", "ticker": "000001", "market": "CN"}],
        "share_classes": [
            {
                "security_id": "a",
                "share_class": "A",
                "price": "10",
                "issued_shares": "100",
                "currency": "CNY",
                "fx_to_base": "1",
                "price_timestamp": NOW.isoformat(),
                "quote_status": "VALID",
                "rights_verified": True,
            }
        ],
        "annual_distributions": [
            {
                "fiscal_year": year,
                "fiscal_year_end_date": f"{year}-12-31",
                "period_type": "FULL_YEAR",
                "ordinary_dividend_status": "PAID",
                "ordinary_dividend": "100",
                "special_dividend": "0",
                "gross_cancelled_buyback": "0",
                "cancelled_shares": "0",
                "diluted_net_share_reduction": "0",
            }
            for year in range(2025, 2020, -1)
        ],
        "coverage": {
            "fcf_years": [
                {
                    "fiscal_year": year,
                    "operating_cash_flow": "160",
                    "capital_expenditure": "20",
                    "lease_principal_repayment": "5",
                }
                for year in range(2025, 2020, -1)
            ]
        },
        "organic_growth_metric": "normalized_fcf",
        "organic_growth_series": [
            {
                "fiscal_year": year,
                "fiscal_year_end_date": f"{year}-12-31",
                "period_type": "FULL_YEAR",
                "value": value,
            }
            for year, value in zip(range(2021, 2026), ("100", "102", "104", "106", "108"))
        ],
        "valuation": {
            "metric": "P_FCF",
            "historical_median": "10",
            "current": "10",
            "basis_consistent": True,
        },
        "structured_scores": {
            "balance_sheet": score,
            "data_completeness": score,
            "business_durability": score,
            "governance_capital_allocation": score,
        },
        "risk_scores": {
            "structural_cycle": score,
            "policy_asset_life": score,
            "valuation_trap": score,
        },
        "raw_data_points": [
            {
                "field_id": field_id,
                "company_id": "issuer-cache",
                "security_id": "a",
                "share_class": "A",
                "source_name": "futu-opend",
                "source_document": "quote-snapshot.json",
                "source_url_or_local_path": "/home/private/quote-snapshot.json",
                "source_publish_date": "2026-08-01",
                "source_fetch_time": NOW.isoformat(),
                "fiscal_period": "MARKET_AS_OF_2026-08-01",
                "currency": "CNY",
                "unit": unit,
                "value": value,
                "data_status": "VALID",
                "restatement_status": "CURRENT_MARKET_SNAPSHOT",
            }
            for field_id, unit, value in (
                ("MARKET.a.price", "currency_per_share", "10"),
                ("MARKET.a.fx_to_base", "ratio", "1"),
            )
        ],
        "veto_inputs": {},
        "source_summary": {
            "annual_report": "exchange",
            "source_url_or_local_path": "/home/private/report.pdf",
        },
    }
    cache = tmp_path / "slow.json"
    slow_first, hit_first = load_or_compute_slow(raw, cache, on_date=NOW.date())
    first = compute_company_snapshot(raw, now=NOW, slow_variables=slow_first)
    raw["share_classes"][0]["price"] = "20"
    for point in raw["raw_data_points"]:
        if point["field_id"] == "MARKET.a.price":
            point["value"] = "20"
        point["source_fetch_time"] = (NOW + timedelta(minutes=2)).isoformat()
    slow_second, hit_second = load_or_compute_slow(raw, cache, on_date=NOW.date())
    second = compute_company_snapshot(raw, now=NOW, slow_variables=slow_second)
    assert hit_first is False and hit_second is True
    assert first["metrics"]["historical_conservative_distribution"] == second["metrics"]["historical_conservative_distribution"]
    assert D(second["metrics"]["sustainable_shareholder_yield"]["value"]) == D(first["metrics"]["sustainable_shareholder_yield"]["value"]) / 2
    assert first["distribution_history"][0]["special_dividend"]["status"] == "KNOWN_ZERO"
    assert "/home/" not in str(first["source_summary"])
    raw["balance_sheet_history"] = [
        {"fiscal_year": 2025, "net_debt": "20"},
        {"fiscal_year": 2024, "net_debt": "10"},
    ]
    _, hit_after_slow_veto_change = load_or_compute_slow(raw, cache, on_date=NOW.date())
    assert hit_after_slow_veto_change is False


def test_raw_provenance_contract_requires_unit_currency_period_and_source() -> None:
    valid = {
        "field_id": "FY2025.ordinary_dividend",
        "company_id": "issuer-1",
        "security_id": None,
        "share_class": None,
        "source_name": "exchange",
        "source_document": "FY2025 annual report",
        "source_url_or_local_path": "https://example.com/report",
        "source_publish_date": "2026-03-31",
        "source_fetch_time": "2026-04-01T00:00:00Z",
        "fiscal_period": "FY2025",
        "currency": "CNY",
        "unit": "currency",
        "value": "100",
        "data_status": "VALID",
        "restatement_status": "ORIGINAL",
    }
    assert validate_raw_provenance_records([valid], expected_company_id="issuer-1").status.value == "VALID"
    invalid = {**valid, "field_id": "bad", "currency": None}
    result = validate_raw_provenance_records([invalid], expected_company_id="issuer-1")
    assert result.status.value == "INVALID"


def test_vetoes_are_derived_from_sorted_structured_history() -> None:
    raw = {
        "company_id": "issuer-veto",
        "industry_kind": "NON_FINANCIAL",
        "annual_distributions": [
            {
                "fiscal_year": 2024,
                "fiscal_year_end_date": "2024-12-31",
                "period_type": "FULL_YEAR",
                "ordinary_dividend_status": "PAID",
                "ordinary_dividend": "90",
                "special_dividend": "50",
                "gross_cancelled_buyback": "0",
                "cancelled_shares": "0",
                "diluted_net_share_reduction": "0",
                "asset_sale_distribution": "0",
                "one_off_buyback": "0",
            },
            {
                "fiscal_year": 2025,
                "fiscal_year_end_date": "2025-12-31",
                "period_type": "FULL_YEAR",
                "ordinary_dividend_status": "PAID",
                "ordinary_dividend": "100",
                "special_dividend": "50",
                "gross_cancelled_buyback": "0",
                "cancelled_shares": "0",
                "diluted_net_share_reduction": "0",
                "asset_sale_distribution": "0",
                "one_off_buyback": "0",
            },
        ],
        "coverage": {
            "fcf_years": [
                {
                    "fiscal_year": year,
                    "operating_cash_flow": "120",
                    "capital_expenditure": "10",
                    "lease_principal_repayment": "10",
                }
                for year in (2025, 2024)
            ]
        },
        "balance_sheet_history": [
            {"fiscal_year": 2024, "net_debt": "50"},
            {"fiscal_year": 2025, "net_debt": "80"},
        ],
    }
    slow = compute_slow_variables(raw, on_date=NOW.date())
    triggered = {flag.code for flag in slow.veto_flags if flag.triggered}
    assert "DISTRIBUTION_OVER_FCF_AND_DEBT_RISING" in triggered
    assert "ONE_OFF_DISTRIBUTION_OVER_30PCT" in triggered


def test_bank_capital_veto_is_derived_without_fcf_fallback() -> None:
    raw = {
        "company_id": "issuer-bank",
        "industry_kind": "BANK",
        "annual_distributions": [
            {
                "fiscal_year": year,
                "fiscal_year_end_date": f"{year}-12-31",
                "period_type": "FULL_YEAR",
                "ordinary_dividend_status": "PAID",
                "ordinary_dividend": "100",
                "special_dividend": "0",
                "gross_cancelled_buyback": "0",
                "cancelled_shares": "0",
                "diluted_net_share_reduction": "0",
            }
            for year in (2025, 2024)
        ],
        "coverage": {"cet1_ratio": "0.105", "cet1_regulatory_minimum": "0.10"},
    }
    slow = compute_slow_variables(raw, on_date=NOW.date())
    assert any(
        flag.code == "FINANCIAL_CAPITAL_BUFFER_NEAR_MINIMUM" and flag.triggered
        for flag in slow.veto_flags
    )
    assert slow.coverage.adapter.startswith("BankCapitalAdapter")
    assert slow.coverage.status is CoverageStatus.INSUFFICIENT_DATA


def test_major_regulatory_penalty_is_a_structured_veto() -> None:
    flags = evaluate_vetoes({"major_regulatory_penalty": True})
    assert any(flag.code == "MAJOR_REGULATORY_PENALTY" and flag.triggered for flag in flags)


def test_incomplete_ah_snapshot_emits_major_veto_and_class_d() -> None:
    snapshot = compute_company_snapshot(
        {
            "company_id": "issuer-ah-incomplete",
            "company_name": "A/H口径测试",
            "industry_kind": "UNSUPPORTED",
            "expected_share_classes": ["A", "H"],
            "share_classes": [
                {
                    "security_id": "h",
                    "share_class": "H",
                    "price": "10",
                    "issued_shares": "100",
                    "currency": "HKD",
                    "fx_to_base": "0.9",
                    "price_timestamp": NOW.isoformat(),
                    "quote_status": "VALID",
                    "rights_verified": True,
                }
            ],
        },
        now=NOW,
    )
    assert snapshot["classification"] == "D"
    assert any(flag["code"] == "INCOMPLETE_AH_MARKET_CAP" for flag in snapshot["veto_flags"])


def test_required_provenance_ledger_covers_numeric_calculation_inputs() -> None:
    raw = {
        "industry_kind": "NON_FINANCIAL",
        "annual_distributions": [{"fiscal_year": 2025}],
        "share_classes": [{"security_id": "a", "material": True}],
        "coverage": {"fcf_years": [{"fiscal_year": 2025}]},
        "organic_growth_series": [{"fiscal_year": 2025, "value": "100"}],
        "balance_sheet_history": [{"fiscal_year": 2025, "net_debt": "10"}],
    }
    required = required_provenance_field_ids(raw)
    assert {
        "FY2025.ordinary_dividend",
        "FY2025.asset_sale_distribution",
        "MARKET.a.price",
        "SECURITY.a.issued_shares",
        "FY2025.operating_cash_flow",
        "GROWTH.FY2025.value",
        "VALUATION.current",
        "RECONCILIATION.opening_minus_closing_shares",
        "FY2025.net_debt",
    } <= required
    missing = validate_required_provenance_fields([], required)
    assert missing.status.value == "INVALID"
    complete = validate_required_provenance_fields(
        [{"field_id": field_id} for field_id in required],
        required,
    )
    assert complete.status.value == "VALID"


def test_incomplete_or_future_fiscal_year_is_rejected() -> None:
    slow = compute_slow_variables(
        {
            "company_id": "issuer-future",
            "industry_kind": "NON_FINANCIAL",
            "annual_distributions": [
                {
                    "fiscal_year": 2026,
                    "fiscal_year_end_date": "2026-12-31",
                    "period_type": "FULL_YEAR",
                    "ordinary_dividend_status": "PAID",
                    "ordinary_dividend": "100",
                    "special_dividend": "0",
                    "gross_cancelled_buyback": "0",
                    "cancelled_shares": "0",
                    "diluted_net_share_reduction": "0",
                }
            ],
        },
        on_date=NOW.date(),
    )
    assert any(error.startswith("distribution:fiscal year end") for error in slow.errors)
    assert slow.historical_distribution is None
