from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from liberty_v2.assessment import assess_company, assess_release_records
from liberty_v2.balance_sheet_adapter import (
    BalanceSheetAssessment,
    adapt_balance_sheet_payload,
)
from liberty_v2.capital_structure import (
    CapitalStructureAuthorization,
    StructureKind,
    load_capital_structure_registry,
)
from liberty_v2.market_observation import (
    MarketObservation,
    determine_freshness,
    market_source_records,
    overlay_market_observation,
)
from liberty_v2.market_value_resolver import resolve_selected_security_equivalent_value
from liberty_v2.models import (
    CompanyDataTier,
    Freshness,
    MetricBasis,
    ReleaseValidity,
)


NOW = datetime(2026, 8, 3, 6, tzinfo=timezone.utc)


def observation(*, market_value: str = "1000") -> MarketObservation:
    return MarketObservation(
        company_id="issuer",
        security_id="security",
        market="CN",
        currency="CNY",
        price=Decimal("10"),
        quote_timestamp=NOW,
        market_state="open",
        total_market_value=Decimal(market_value),
        pe=Decimal("12"),
        pe_ttm=Decimal("11"),
        pb=Decimal("2"),
        dividend_yield_ttm_pct=Decimal("3"),
        earnings_per_share=Decimal("1"),
        book_value_per_share=Decimal("5"),
        fx_to_base=Decimal("1"),
        provider="futu-opend",
        snapshot_collected_at=NOW,
        snapshot_sha256="a" * 64,
        freshness=Freshness.CURRENT,
        source_path="/tmp/latest_snapshot.json",
    )


def authorization(*, vendor: bool = True, direct: bool = True) -> CapitalStructureAuthorization:
    return CapitalStructureAuthorization(
        company_id="issuer",
        selected_security_id="security",
        structure_kind=StructureKind.SINGLE_CLASS,
        material_share_classes=("A",),
        distribution_rights_equal=True,
        selected_security_rights_factor=Decimal("1"),
        vendor_total_market_value_semantics="VENDOR_COMPANY_MARKET_VALUE",
        vendor_value_authorized=vendor,
        direct_equivalent_shares_authorized=direct,
        authorization_source_ids=("SECURITY.security.issued_shares",),
        as_of_date=date(2026, 3, 1),
        official_equivalent_shares=Decimal("100"),
        observed_relative_difference=Decimal("0"),
    )


def source(field_id: str, value: str = "1", *, status: str = "VALID") -> dict:
    return {
        "field_id": field_id,
        "source_name": "official annual report",
        "source_document": "FY2025 annual report",
        "data_status": status,
        "value": value,
    }


def test_registry_covers_the_formal_67_companies() -> None:
    root = Path(__file__).resolve().parents[1]
    watchlist = json.loads((root / "config" / "watchlist.json").read_text(encoding="utf-8"))
    expected = {item["issuerId"] for item in watchlist["securities"]}
    registry = load_capital_structure_registry(
        root / "config" / "issuer_capital_structure_v1.json",
        expected_company_ids=expected,
    )
    assert len(registry) == 67
    assert sum(item.vendor_value_authorized for item in registry.values()) == 14
    assert registry["SH600660"].structure_kind is StructureKind.A_H
    assert registry["SH600600"].vendor_value_authorized is False


def test_seev_uses_authorized_vendor_value_and_rechecks_current_implied_shares() -> None:
    resolved = resolve_selected_security_equivalent_value(authorization(), observation())
    assert resolved.value == Decimal("1000")
    assert resolved.basis is MetricBasis.VENDOR_AUTHORIZED
    assert resolved.relative_difference == 0

    mismatch = resolve_selected_security_equivalent_value(
        authorization(),
        observation(market_value="1060"),
    )
    assert mismatch.value is None
    assert mismatch.blockers == ("VENDOR_EQUIVALENT_SHARES_DEVIATION_GT_5PCT",)


def test_seev_can_derive_from_authorized_equivalent_shares_when_vendor_is_rejected() -> None:
    resolved = resolve_selected_security_equivalent_value(
        authorization(vendor=False),
        observation(market_value="1450"),
    )
    assert resolved.value == Decimal("1000")
    assert resolved.basis is MetricBasis.DERIVED
    assert f"MARKET.security.total_market_value" not in resolved.source_field_ids


def test_market_overlay_is_non_mutating_and_carries_fast_metrics() -> None:
    raw = {
        "company_id": "issuer",
        "valuation": {},
        "share_classes": [{"security_id": "security", "price": None}],
        "raw_data_points": [],
    }
    overlaid = overlay_market_observation(raw, observation())
    assert raw["share_classes"][0]["price"] is None
    assert overlaid["share_classes"][0]["price"] == "10"
    assert overlaid["valuation"]["current_market_metrics"]["pe_ttm"] == "11"
    assert {item["field_id"] for item in overlaid["raw_data_points"]} >= {
        "MARKET.security.price",
        "MARKET.security.total_market_value",
        "MARKET.security.pe_ttm",
    }


def test_freshness_accepts_closed_weekend_but_not_old_open_quote() -> None:
    friday = datetime(2026, 8, 7, 7, tzinfo=timezone.utc)
    sunday = datetime(2026, 8, 9, 6, tzinfo=timezone.utc)
    assert determine_freshness(
        quote_timestamp=friday,
        snapshot_collected_at=friday,
        market_state="closed",
        market="CN",
        now=sunday,
    ) is Freshness.MARKET_CLOSED_CURRENT
    assert determine_freshness(
        quote_timestamp=NOW,
        snapshot_collected_at=NOW,
        market_state="open",
        market="CN",
        now=NOW.replace(minute=11),
    ) is Freshness.STALE_LAST_GOOD


def balance_payload(*, direct: bool) -> dict:
    items = [
        {"field_id": 3001, "display_name": "资产合计", "data": "1000"},
        {"field_id": 3055, "display_name": "负债合计", "data": "400"},
        {"field_id": 3003, "display_name": "货币资金", "data": "100"},
        {"field_id": 3097, "display_name": "股东权益合计", "data": "600"},
    ]
    if direct:
        items.extend(
            [
                {"field_id": 3067, "display_name": "短期借款", "data": "20"},
                {"field_id": 3075, "display_name": "一年内到期的非流动负债", "data": "10"},
                {"field_id": 3084, "display_name": "长期借款", "data": "150"},
                {"field_id": 3085, "display_name": "应付债券", "data": "20"},
            ]
        )
    return {
        "company": {"issuer_id": "issuer"},
        "fetched_at": NOW.isoformat(),
        "statements": {
            "balance_sheet": {
                "report_list": [
                    {
                        "fiscal_year": 2025,
                        "date_time_str": "2025-12-31",
                        "currency_code": "CNY",
                        "item_list": items,
                    }
                ]
            }
        },
    }


def test_balance_sheet_adapter_prefers_direct_net_debt_and_falls_back_to_proxy() -> None:
    direct = adapt_balance_sheet_payload(balance_payload(direct=True), expected_company_id="issuer")
    assert direct.kind == "NET_DEBT_TO_EQUITY"
    assert direct.net_debt == Decimal("100")
    assert direct.value == Decimal("100") / Decimal("600")
    assert direct.basis is MetricBasis.DIRECT

    proxy = adapt_balance_sheet_payload(balance_payload(direct=False), expected_company_id="issuer")
    assert proxy.kind == "DEBT_TO_ASSETS_PROXY"
    assert proxy.value == Decimal("0.4")
    assert proxy.basis is MetricBasis.PROXY


def test_unified_assessment_requires_only_selected_inputs_not_buyback_or_four_reconciliations() -> None:
    obs = observation()
    balance = BalanceSheetAssessment(
        company_id="issuer",
        fiscal_year=2025,
        fiscal_year_end_date=date(2025, 12, 31),
        currency="CNY",
        kind="DEBT_TO_ASSETS_PROXY",
        value=Decimal("0.4"),
        basis=MetricBasis.PROXY,
        net_debt=None,
        interest_bearing_debt=None,
        cash_and_cash_equivalents=None,
        total_equity=None,
        total_assets=Decimal("1000"),
        total_liabilities=Decimal("400"),
        source_field_ids=(
            "FY2025.balance_sheet.total_liabilities",
            "FY2025.balance_sheet.total_assets",
        ),
        primitives=(),
        warnings=("BALANCE_SHEET_PROXY",),
    )
    raw = {
        "company_id": "issuer",
        "company_name": "样本公司",
        "industry_kind": "NON_FINANCIAL",
        "securities": [{"security_id": "security"}],
        "annual_distributions": [
            {
                "fiscal_year": year,
                "fiscal_year_end_date": f"{year}-12-31",
                "period_type": "FULL_YEAR",
                "ordinary_dividend_status": "PAID",
                "ordinary_dividend": "100",
            }
            for year in (2025, 2024)
        ],
        "coverage": {
            "fcf_years": [
                {
                    "fiscal_year": year,
                    "fiscal_year_end_date": f"{year}-12-31",
                    "operating_cash_flow": "160",
                    "capital_expenditure": "20",
                    "lease_principal_repayment": None,
                }
                for year in (2025, 2024)
            ]
        },
        "raw_data_points": [
            source("SECURITY.security.issued_shares", "100"),
            source("FY2025.ordinary_dividend", "100"),
            source("FY2024.ordinary_dividend", "100"),
            source("FY2025.operating_cash_flow", "160"),
            source("FY2025.capital_expenditure", "20"),
            source("FY2024.operating_cash_flow", "160"),
            source("FY2024.capital_expenditure", "20"),
            source("FY2025.balance_sheet.total_liabilities", "400"),
            source("FY2025.balance_sheet.total_assets", "1000"),
            *market_source_records(obs),
        ],
    }
    result = assess_company(
        raw,
        authorization=authorization(),
        market_observation=obs,
        balance_sheet=balance,
        now=NOW,
    )
    assert result.data_tier is CompanyDataTier.ESTIMATED
    assert result.blockers == ()
    assert result.data_confidence.total >= 35
    required = set(result.input_plan.required_source_field_ids)
    assert not any("buyback" in item.lower() for item in required)
    assert not any(item.startswith("RECONCILIATION.") for item in required)


def test_release_validity_is_independent_of_company_tier() -> None:
    release = assess_release_records(
        [
            {"company_id": "a", "data_tier": "ESTIMATED", "scores": {}},
            {"company_id": "b", "data_tier": "BLOCKED", "blockers": ["DATA_GAP"]},
        ]
    )
    assert release.validity is ReleaseValidity.VALID_RELEASE
