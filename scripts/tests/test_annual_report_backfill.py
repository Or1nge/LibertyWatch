from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "support" / "annual_report_backfill.py"
SPEC = importlib.util.spec_from_file_location("annual_report_backfill", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_parse_fiscal_year_supports_calendar_and_split_year_labels() -> None:
    assert MODULE.parse_fiscal_year("2025年年度报告") == 2025
    assert MODULE.parse_fiscal_year("2025/26年度年报") == 2026
    assert MODULE.parse_fiscal_year("ANNUAL REPORT 2024") == 2024
    assert MODULE.parse_fiscal_year("年報2025") == 2025
    assert MODULE.parse_fiscal_year("二零二五年年報") == 2025


def test_summary_and_notification_are_not_full_reports() -> None:
    assert MODULE.is_full_annual_report("2025年年度报告")
    assert not MODULE.is_full_annual_report("2025年年度报告摘要")
    assert not MODULE.is_full_annual_report("2025年年报发布通知信函及回条")
    assert not MODULE.is_full_annual_report("2025年环境、社会及管治年度报告")
    assert not MODULE.is_full_annual_report("2016年年报更正公告")
    assert MODULE.is_full_annual_report("2016年年度报告（修订版）")


def test_select_latest_ten_prefers_restatement_and_never_future() -> None:
    documents = []
    for year in range(2014, 2026):
        documents.append(
            {
                "fiscal_year": year,
                "fiscal_year_end_date": f"{year}-12-31",
                "publish_date": f"{year + 1}-04-01",
                "source_document": f"{year}年年度报告",
            }
        )
    documents.append(
        {
            "fiscal_year": 2024,
            "fiscal_year_end_date": "2024-12-31",
            "publish_date": "2025-05-01",
            "source_document": "2024年年度报告（修订版）",
        }
    )
    selected = MODULE.select_latest_ten(documents)
    assert len(selected) == 10
    assert [row["fiscal_year"] for row in selected] == list(range(2025, 2015, -1))
    revised = next(row for row in selected if row["fiscal_year"] == 2024)
    assert revised["restatement_status"] == "RESTATED"


def test_duplicate_title_prefers_larger_official_attachment() -> None:
    base = {
        "fiscal_year": 2020,
        "fiscal_year_end_date": "2020-12-31",
        "publish_date": "2021-04-29",
        "source_document": "公牛集团2020年年度报告",
    }
    selected = MODULE.select_latest_ten(
        [
            {**base, "source_announcement_id": "short", "source_size_hint_kb": 447},
            {**base, "source_announcement_id": "full", "source_size_hint_kb": 3378},
        ]
    )
    assert selected[0]["source_announcement_id"] == "full"


def test_discovery_supersedes_old_selected_document_without_deleting_it(
    tmp_path, monkeypatch
) -> None:
    company = MODULE.Company(
        issuer_id="SH600529",
        security_id="SH600529",
        ticker="600529",
        market="CN",
        name="山东药玻",
        currency="CNY",
    )
    monkeypatch.setattr(
        MODULE, "manifest_path", lambda output, company: output / "manifest.json"
    )
    old = {
        "fiscal_year": 2016,
        "source_url": "https://static.cninfo.com.cn/old.pdf",
        "selection_status": "SELECTED_CURRENT",
        "data_status": "VERIFIED",
    }
    MODULE.atomic_write_json(
        tmp_path / "manifest.json", {**MODULE.base_manifest(company), "documents": [old]}
    )
    new = {
        "fiscal_year": 2016,
        "source_url": "https://static.cninfo.com.cn/full.pdf",
    }
    result = MODULE.update_discovery_manifest(tmp_path, company, [new], {})
    assert result["documents"][0]["selection_status"] == "SUPERSEDED_SOURCE_SELECTION"


def test_hkex_parser_uses_official_pdf_and_end_year(tmp_path, monkeypatch) -> None:
    company = MODULE.Company(
        issuer_id="HK0179",
        security_id="HK0179",
        ticker="00179",
        market="HK",
        name="德昌电机控股",
        currency="HKD",
    )
    monkeypatch.setattr(MODULE, "CALENDARS_PATH", tmp_path / "calendar.json")
    (tmp_path / "calendar.json").write_text(
        '{"defaults":{"HK":{"fiscal_year_end_month_day":"12-31"}},'
        '"company_overrides":{"HK0179":{"fiscal_year_end_month_day":"03-31",'
        '"status":"VERIFIED"}}}',
        encoding="utf-8",
    )
    body = """
    <tr><td class="release-time"><span>x</span>15/06/2026 12:00</td>
    <td><a href="/listedco/listconews/sehk/2026/0615/a_c.pdf">2025/26年度年報</a></td></tr>
    <tr><td class="release-time"><span>x</span>15/06/2026 12:01</td>
    <td><a href="/listedco/listconews/sehk/2026/0615/n_c.pdf">2025/26年度年報發布通知信函</a></td></tr>
    """
    rows = MODULE.parse_hkex_rows(body, company)
    assert len(rows) == 1
    assert rows[0]["fiscal_year"] == 2026
    assert rows[0]["fiscal_year_end_date"] == "2026-03-31"
    assert rows[0]["source_url"].startswith("https://www1.hkexnews.hk/")


def test_allowed_source_hosts_are_narrow() -> None:
    assert "static.cninfo.com.cn" in MODULE.PDF_ALLOWED_HOSTS
    assert "www1.hkexnews.hk" in MODULE.PDF_ALLOWED_HOSTS
    assert "example.com" not in MODULE.PDF_ALLOWED_HOSTS


def test_selected_document_semantics_rejects_short_or_correction_notice() -> None:
    full = {"source_document": "2024年年度报告"}
    MODULE.validate_selected_document_semantics(full, {"pdf_pages": 120})
    try:
        MODULE.validate_selected_document_semantics(full, {"pdf_pages": 9})
    except MODULE.BackfillError as error:
        assert "ANNUAL_REPORT_TOO_SHORT" in str(error)
    else:
        raise AssertionError("short report must be rejected")
    try:
        MODULE.validate_selected_document_semantics(
            {"source_document": "2024年年报更正公告"}, {"pdf_pages": 120}
        )
    except MODULE.BackfillError as error:
        assert "NOT_FULL_ANNUAL_REPORT_TITLE" in str(error)
    else:
        raise AssertionError("correction notice must be rejected")
