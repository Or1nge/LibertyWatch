from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from liberty_v2.dividend_reconciliation import DividendReconciliationError
from liberty_v2.dividend_reconciliation_v2 import (
    blocker_for_candidates,
    component_amount,
    distribution_total,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "dividend_reconciliation_v2.json"


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def test_review_config_accounts_for_277_recent_year_slots_without_guessing() -> None:
    config = load_config()
    assert config["expected_company_count"] == 56
    assert config["expected_target_slot_count"] == 277
    assert config["expected_ready_count"] == 16
    assert config["expected_new_ready_count"] == 14
    assert config["expected_blocked_count"] == 261
    assert 16 + 261 == 277


def test_huichuan_uses_implemented_amount_not_the_proposal_candidate() -> None:
    config = load_config()
    by_id = {item["distribution_id"]: item for item in config["new_distributions"]}
    fy2023 = by_id["SZ300124-FY2023-ORDINARY-ANNUAL"]
    fy2024 = by_id["SZ300124-FY2024-ORDINARY-ANNUAL"]
    assert fy2023["ordinary_cash_dividend_total"]["value"] == "1204746677.55"
    assert fy2023["ordinary_components"][0]["candidate_original_value"] == "1204384000.50"
    assert fy2024["ordinary_cash_dividend_total"]["value"] == "1104397656.61"
    assert fy2024["ordinary_components"][0]["candidate_original_value"] == "1104385494.78"
    assert all(
        item["ordinary_components"][0]["candidate_disposition"]
        == "REJECT_PROPOSAL_AMOUNT_USE_IMPLEMENTED_TOTAL_SAME_PAGE"
        for item in (fy2023, fy2024)
    )


def test_supor_fy2022_is_an_exact_decimal_sum_of_two_paid_ordinary_components() -> None:
    distribution = next(
        item
        for item in load_config()["new_distributions"]
        if item["distribution_id"] == "SZ002032-FY2022-ORDINARY-ANNUAL"
    )
    assert distribution_total(distribution) == Decimal("3446165986.96")
    assert [item["component"] for item in distribution["ordinary_components"]] == [
        "ANNUAL",
        "Q3_INTERIM",
    ]


def test_direct_total_cannot_hide_multiple_components() -> None:
    distribution = {
        "calculation_method": "DIRECT_OFFICIAL_IMPLEMENTED_TOTAL",
        "ordinary_cash_dividend_total": {
            "value": "3",
            "currency": "CNY",
            "unit": "currency",
        },
        "ordinary_components": [
            {"amount_method": "OFFICIAL_TOTAL", "value": "1", "currency": "CNY"},
            {"amount_method": "OFFICIAL_TOTAL", "value": "2", "currency": "CNY"},
        ],
    }
    with pytest.raises(DividendReconciliationError, match="exactly one"):
        distribution_total(distribution)


def test_per_share_derivation_requires_two_distinct_sources_and_decimal_identity() -> None:
    component = {
        "amount_method": "PER_SHARE_TIMES_ENTITLED_SHARES",
        "value": "125.00",
        "per_share_value": "2.50",
        "share_basis": "10",
        "entitled_shares": "500",
        "derivation_source_ids": ["per-share-page", "entitled-shares-page"],
    }
    assert component_amount(component, field="component") == Decimal("125.00")
    missing_source = copy.deepcopy(component)
    missing_source["derivation_source_ids"] = ["one-source"]
    with pytest.raises(DividendReconciliationError, match="distinct"):
        component_amount(missing_source, field="component")
    wrong_total = copy.deepcopy(component)
    wrong_total["value"] = "125.01"
    with pytest.raises(DividendReconciliationError, match="mismatch"):
        component_amount(wrong_total, field="component")


def test_blocked_year_has_no_number_and_explains_the_gap_in_plain_chinese() -> None:
    row = blocker_for_candidates("SH600001", 2024, [])
    assert row["status"] == "BLOCKED"
    assert row["ordinary_cash_dividend_total"] is None
    assert row["unknown_is_not_zero"] is True
    assert "没有找到" in row["reason_zh"]


def test_special_candidate_never_becomes_an_ordinary_total() -> None:
    candidates = [
        {
            "report_fiscal_year": 2025,
            "associated_fiscal_year": 2024,
            "amount_kind": "TOTAL",
            "dividend_kind": "ORDINARY",
            "lifecycle_status": "PAID",
        },
        {
            "report_fiscal_year": 2025,
            "associated_fiscal_year": 2024,
            "amount_kind": "TOTAL",
            "dividend_kind": "SPECIAL",
            "lifecycle_status": "PAID",
        },
    ]
    row = blocker_for_candidates("SH600001", 2024, candidates)
    assert row["reason_code"] == "ORDINARY_AND_SPECIAL_NOT_SEPARATED"
    assert row["ordinary_cash_dividend_total"] is None
