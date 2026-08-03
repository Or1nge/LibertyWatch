from __future__ import annotations

from liberty_v2.cashflow_reconciliation_v2 import (
    adjacent_reconciliations,
    classify_coverage_decision,
    extract_official_first_cashflow_report,
)


def metadata(year: int = 2025) -> dict:
    return {
        "company_id": "A600025",
        "company_name": "华能水电",
        "security_id": "SH600025",
        "share_class": "A",
        "market": "CN",
        "currency": "CNY",
        "fiscal_year": year,
        "fiscal_year_end_date": f"{year}-12-31",
        "source_document": f"{year}年年度报告",
        "source_url": "https://static.cninfo.com.cn/report.pdf",
        "source_publish_date": f"{year + 1}-04-01",
        "source_fetch_time": "2026-08-02T01:00:00Z",
        "local_path": "/official/report.pdf",
        "sha256": "a" * 64,
    }


def wrapped_statement(cfo: str, prior_cfo: str, capex: str, prior_capex: str) -> str:
    return f"""合并现金流量表
{metadata()['fiscal_year']} 年 1—12 月
单位：元 币种：人民币
项目 本期 上期
经营活动产生的现金流
                         {cfo} {prior_cfo}
量净额
购建固定资产、无形资产和其他长
                         {capex} {prior_capex}
期资产支付的现金
"""


def test_wrapped_cfo_and_capex_rows_are_extracted_without_futu_definition() -> None:
    report = extract_official_first_cashflow_report(
        wrapped_statement("100", "90", "20", "18"), metadata()
    )
    assert report["fields"]["operating_cash_flow"]["current_value"] == "100"
    capex = report["fields"]["capital_expenditure"]
    assert capex["current_value"] == "20"
    assert capex["definition_basis"] == "DIRECT_COMPREHENSIVE_CASHFLOW_ROW"
    assert capex["components"][0]["evidence_rows"][0]["wrapped_line_count"] == 3


def test_preceding_operating_subtotal_does_not_duplicate_target_row() -> None:
    text = wrapped_statement("100", "90", "20", "18").replace(
        "经营活动产生的现金流\n",
        "经营活动现金流出小计 500 490\n经营活动产生的现金流\n",
    )
    report = extract_official_first_cashflow_report(text, metadata())
    field = report["fields"]["operating_cash_flow"]
    assert field["match_count"] == 1
    assert field["current_value"] == "100"


def test_adjacent_comparison_checks_both_directions_exactly() -> None:
    reports = {
        2024: extract_official_first_cashflow_report(
            wrapped_statement("90", "80", "18", "17"), metadata(2024)
        ),
        2025: extract_official_first_cashflow_report(
            wrapped_statement("100", "90", "20", "18"), metadata(2025)
        ),
    }
    assert adjacent_reconciliations(reports, 2024, "operating_cash_flow") == {
        "backward": "UNAVAILABLE",
        "forward": "MATCH",
    }
    assert adjacent_reconciliations(reports, 2025, "capital_expenditure") == {
        "backward": "MATCH",
        "forward": "UNAVAILABLE",
    }


def test_unique_official_and_adjacent_match_can_accept_without_futu() -> None:
    status, value, reasons = classify_coverage_decision(
        {"current_value": "100", "currency": "CNY", "unit": "currency"},
        {"value": None, "currency": "CNY", "data_status": "MISSING"},
        {"backward": "MATCH", "forward": "UNAVAILABLE"},
        accepted_by_v1=False,
    )
    assert status == "ACCEPT_OFFICIAL_ADJACENT"
    assert value == "100"
    assert reasons == ["UNIQUE_OFFICIAL_ROW_AND_EXACT_ADJACENT_REPORT"]


def test_unique_official_and_exact_futu_can_accept_without_adjacent() -> None:
    status, value, _reasons = classify_coverage_decision(
        {"current_value": "100", "currency": "CNY", "unit": "currency"},
        {"value": "100.0", "currency": "CNY", "data_status": "VALID"},
        {"backward": "UNAVAILABLE", "forward": "UNAVAILABLE"},
        accepted_by_v1=False,
    )
    assert status == "ACCEPT_OFFICIAL_PLUS_FUTU"
    assert value == "100"


def test_any_exact_source_mismatch_stays_conflict() -> None:
    status, value, _reasons = classify_coverage_decision(
        {"current_value": "100", "currency": "CNY", "unit": "currency"},
        {"value": "101", "currency": "CNY", "data_status": "VALID"},
        {"backward": "MATCH", "forward": "UNAVAILABLE"},
        accepted_by_v1=False,
    )
    assert status == "CONFLICT"
    assert value is None


def test_futu_only_never_becomes_an_official_accept() -> None:
    assert classify_coverage_decision(
        None,
        {"value": "100", "currency": "CNY", "data_status": "VALID"},
        {"backward": "UNAVAILABLE", "forward": "UNAVAILABLE"},
        accepted_by_v1=False,
    )[0] == "FUTU_ONLY"
