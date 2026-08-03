from __future__ import annotations

from pathlib import Path

from liberty_v2.official_cashflow_candidates import (
    extract_official_cashflow_report,
    pdftotext_layout,
    reconcile_company_reports,
)


def metadata(year: int, *, market: str = "CN", currency: str = "CNY") -> dict:
    return {
        "company_id": "SZ002032" if market == "CN" else "HK0179",
        "company_name": "苏泊尔" if market == "CN" else "德昌电机控股",
        "security_id": "SZ002032" if market == "CN" else "HK0179",
        "share_class": "A" if market == "CN" else "H",
        "market": market,
        "currency": currency,
        "fiscal_year": year,
        "fiscal_year_end_date": f"{year}-12-31",
        "source_document": f"FY{year} annual report",
        "source_url": f"https://example.test/{year}.pdf",
        "source_publish_date": f"{year + 1}-04-01",
        "source_fetch_time": "2026-08-02T01:00:00Z",
        "local_path": f"/evidence/{year}.pdf",
        "sha256": "a" * 64,
    }


def futu_year(year: int, cfo: str, capex: str, *, currency: str = "CNY", ifrs: bool = False) -> dict:
    capex_fields = (
        [
            {"field_id": 5071, "display_name": "购买固定资产", "value": "-195504000"},
            {"field_id": 5073, "display_name": "购买无形资产", "value": "-1749000"},
        ]
        if ifrs
        else [{"field_id": 3043, "display_name": "购建固定资产、无形资产和其他长期资产支付的现金", "value": capex}]
    )
    return {
        "fiscal_year": year,
        "fiscal_year_end_date": f"{year}-12-31",
        "fiscal_period": f"{year}/FY",
        "source_local_path": "/evidence/futu.json",
        "source_file_sha256": "b" * 64,
        "source_payload_sha256": "c" * 64,
        "fields": {
            "operating_cash_flow": {
                "value": cfo,
                "currency": currency,
                "data_status": "VALID",
                "provider_fields": [{"field_id": 3001 if not ifrs else 5001, "value": cfo}],
            },
            "capital_expenditure": {
                "value": capex,
                "currency": currency,
                "data_status": "VALID",
                "provider_fields": capex_fields,
            },
        },
    }


def cn_statement(cfo: str, prior_cfo: str, capex: str, prior_capex: str) -> str:
    return f"""5、合并现金流量表
单位：元
项目                            本年金额            上年金额
经营活动产生的现金流量净额       {cfo}              {prior_cfo}
购建固定资产、无形资产和其他长期资产支付的现金 {capex} {prior_capex}
"""


def test_cn_candidate_requires_futu_and_prior_report_reconciliation() -> None:
    previous = extract_official_cashflow_report(
        cn_statement("90", "80", "18", "17"),
        metadata(2024),
        futu_year(2024, "90", "18"),
    )
    current = extract_official_cashflow_report(
        cn_statement("100", "90", "20", "18"),
        metadata(2025),
        futu_year(2025, "100", "20"),
    )
    checked = reconcile_company_reports([current, previous])
    assert checked[0]["fields"]["operating_cash_flow"]["status"] == "REVIEW"
    assert checked[1]["fields"]["operating_cash_flow"]["status"] == "VALID"
    assert checked[1]["fields"]["operating_cash_flow"]["value"] == "100"
    assert checked[1]["fields"]["capital_expenditure"]["status"] == "VALID"
    assert all(
        field["eligible_for_core_write"] is False
        for report in checked
        for field in report["fields"].values()
    )


def test_statement_context_continues_across_page_and_stops_at_parent_table() -> None:
    text = """5、合并现金流量表
单位：元
项目 本年金额 上年金额
\f经营活动产生的现金流量净额 100 90
购建固定资产、无形资产和其他长期资产支付的现金 20 18
6、母公司现金流量表
经营活动产生的现金流量净额 9 8
购建固定资产、无形资产和其他长期资产支付的现金 2 1
"""
    result = extract_official_cashflow_report(
        text,
        metadata(2025),
        futu_year(2025, "100", "20"),
    )
    assert result["fields"]["operating_cash_flow"]["current_value"] == "100"
    assert result["fields"]["operating_cash_flow"]["match_count"] == 1
    assert result["fields"]["capital_expenditure"]["current_value"] == "20"


def test_hk_components_keep_explicit_usd_thousand_unit() -> None:
    text = """綜合現金流量表
截至2025年3月31日止年度
                                  2025       2024
                                  千美元      千美元
經營活動所得之現金淨額             448,087    581,883
購買物業、廠房及機器設備           (195,504)  (184,917)
工程開發成本之資本化開支             (1,749)    (1,237)
"""
    result = extract_official_cashflow_report(
        text,
        metadata(2025, market="HK", currency="HKD"),
        futu_year(2025, "448087000", "197253000", currency="USD", ifrs=True),
    )
    assert result["fields"]["operating_cash_flow"]["current_value"] == "448087000"
    assert result["fields"]["operating_cash_flow"]["currency"] == "USD"
    assert result["fields"]["capital_expenditure"]["current_value"] == "197253000"
    assert len(result["fields"]["capital_expenditure"]["components"]) == 2


def test_unknown_or_conflicting_unit_never_emits_candidate_value() -> None:
    text = """綜合現金流量表
千美元 人民幣百萬元
經營活動所得之現金淨額 100 90
購買固定資產 20 18
"""
    result = extract_official_cashflow_report(
        text,
        metadata(2025, market="HK", currency="HKD"),
        futu_year(2025, "100", "20", currency="USD", ifrs=True),
    )
    checked = reconcile_company_reports([result])[0]
    assert checked["fields"]["operating_cash_flow"]["status"] == "REVIEW"
    assert checked["fields"]["operating_cash_flow"]["value"] is None
    assert checked["fields"]["capital_expenditure"]["value"] is None


def test_multiple_statement_rows_are_conflict_and_null() -> None:
    page = cn_statement("100", "90", "20", "18")
    result = extract_official_cashflow_report(
        page + "\f" + page,
        metadata(2025),
        futu_year(2025, "100", "20"),
    )
    checked = reconcile_company_reports([result])[0]
    assert checked["fields"]["operating_cash_flow"]["status"] == "CONFLICT"
    assert checked["fields"]["operating_cash_flow"]["value"] is None
    assert checked["fields"]["operating_cash_flow"]["match_count"] == 2


def test_pdftotext_uses_argument_array_without_shell(monkeypatch, tmp_path: Path) -> None:
    observed = {}

    class Completed:
        returncode = 0
        stdout = "layout text"
        stderr = ""

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr("subprocess.run", fake_run)
    path = tmp_path / "report.pdf"
    assert pdftotext_layout(path) == "layout text"
    assert observed["argv"] == ["pdftotext", "-layout", str(path), "-"]
    assert "shell" not in observed["kwargs"]
