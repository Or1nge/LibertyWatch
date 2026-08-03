from __future__ import annotations

from pathlib import Path

from liberty_v2.equity_bridge_candidates import (
    extract_equity_bridge_candidate,
    pdftotext_layout,
    reconcile_company_reports,
)


def metadata(year: int = 2025) -> dict:
    return {
        "company_id": "SH600309",
        "company_name": "万华化学",
        "security_id": "SH600309",
        "share_class": "A",
        "fiscal_year": year,
        "fiscal_year_end_date": f"{year}-12-31",
        "source_document": f"{year}年年度报告",
        "source_url": f"https://example.test/{year}.pdf",
        "source_publish_date": f"{year + 1}-04-01",
        "source_fetch_time": "2026-08-02T01:00:00+00:00",
        "local_path": f"/evidence/{year}.pdf",
        "sha256": "a" * 64,
    }


def hk_metadata(year: int = 2025) -> dict:
    result = metadata(year)
    result.update(
        {
            "company_id": "HK0669",
            "company_name": "创科实业",
            "security_id": "HK0669",
            "share_class": "H",
            "market": "HK",
            "currency": "HKD",
        }
    )
    return result


def table(row: str, *, extra: str = "") -> str:
    return f"""第六节 股份变动及股东情况
一、股份变动情况
1、股份变动情况
单位：股
本次变动前             本次变动增减（+，-）             本次变动后
数量 比例(%) 发行新股 其他 小计 数量 比例(%)
{row}
2、股份变动情况说明
{extra}
"""


def test_standard_row_extracts_opening_closing_and_page_line() -> None:
    result = extract_equity_bridge_candidate(
        table("三、股份总数 3,139,746,626 100 -9,275,000 -9,275,000 3,130,471,626 100"),
        metadata(),
    )
    assert result["opening_issued_shares"] == "3139746626"
    assert result["closing_issued_shares"] == "3130471626"
    assert result["reported_net_issued_share_change"] == "-9275000"
    assert result["reported_net_issued_share_change_status"] == "REVIEW"
    assert result["page"] == 1
    assert isinstance(result["text_line"], int)
    assert result["status"] == "REVIEW"
    assert result["cancelled_shares"] is None
    assert result["diluted_total_shares"] is None


def test_percentages_and_non_integral_ratio_are_not_share_counts() -> None:
    result = extract_equity_bridge_candidate(
        table("三、股份总数 868,644,679 100.00 -666,208 -666,208 867,978,471 100.00"),
        metadata(),
    )
    assert result["opening_issued_shares"] == "868644679"
    assert result["closing_issued_shares"] == "867978471"


def test_multiple_table_rows_are_conflict() -> None:
    text = table("三、股份总数 100,000,000 100 99,000,000 100") + "\f" + table(
        "三、股份总数 100,000,000 100 98,000,000 100"
    )
    result = extract_equity_bridge_candidate(text, metadata())
    assert result["status"] == "CONFLICT"
    assert result["opening_issued_shares"] is None
    assert result["row_match_count"] == 2


def test_adjacent_reports_match_then_conflict() -> None:
    previous = extract_equity_bridge_candidate(
        table("三、股份总数 110,000,000 100 100,000,000 100"), metadata(2024)
    )
    current = extract_equity_bridge_candidate(
        table("三、股份总数 100,000,000 100 90,000,000 100"), metadata(2025)
    )
    checked = reconcile_company_reports([current, previous])
    assert checked[0]["closing_reconciliation"] == "MATCH"
    assert checked[1]["opening_reconciliation"] == "MATCH"
    assert {item["status"] for item in checked} == {"VALID"}
    assert {item["reported_net_issued_share_change_status"] for item in checked} == {"VALID"}
    assert all(item["eligible_for_diluted_share_core"] is False for item in checked)

    current["opening_issued_shares"] = "99"
    conflict = reconcile_company_reports([previous, current])
    assert {item["status"] for item in conflict} == {"CONFLICT"}


def test_split_marker_keeps_candidate_in_review() -> None:
    previous = extract_equity_bridge_candidate(
        table("三、股份总数 110,000,000 100 100,000,000 100"), metadata(2024)
    )
    current = extract_equity_bridge_candidate(
        table(
            "三、股份总数 100,000,000 100 200,000,000 100",
            extra="公司本年度实施股份拆细。",
        ),
        metadata(2025),
    )
    checked = reconcile_company_reports([previous, current])
    assert checked[1]["status"] == "REVIEW"
    assert checked[1]["eligible_for_issued_share_candidate"] is False
    assert checked[1]["reported_net_issued_share_change_status"] == "REVIEW"
    assert checked[1]["diluted_net_share_reduction"] is None


def test_layout_columns_do_not_create_false_reverse_split_marker() -> None:
    result = extract_equity_bridge_candidate(
        table(
            "三、股份总数 100,000,000 100 100,000,000 100",
            extra="综合    股    的利得不会构成并股事项",
        ).replace("不会构成并股事项", "并非资本操作"),
        metadata(),
    )
    assert result["corporate_action_markers"] == []


def test_no_numeric_table_unknown_is_not_zero() -> None:
    result = extract_equity_bridge_candidate(
        "一、股份变动情况\n报告期内，公司股份总数及股本结构未发生变化。",
        metadata(),
    )
    assert result["status"] == "REVIEW"
    assert result["opening_issued_shares"] is None
    assert result["closing_issued_shares"] is None


def test_pdftotext_uses_argv_without_shell(monkeypatch, tmp_path: Path) -> None:
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
    assert pdftotext_layout(tmp_path / "report.pdf") == "layout text"
    assert observed["argv"] == ["pdftotext", "-layout", str(tmp_path / "report.pdf"), "-"]
    assert "shell" not in observed["kwargs"]


def test_hk_exact_share_table_and_explicit_cancellation_candidate() -> None:
    text = """39. 股本
                         二零二五年       二零二四年       二零二五年 二零二四年
                          股份數目        股份數目          千美元   千美元
普通股
已發行及繳足股本：
 於年初                 1,832,304,941  1,834,317,941     689,684 685,392
 因行使認股權發行之股份       405,000        987,000       2,203   4,292
 回購股份                 (3,500,000)    (3,000,000)          –       –
於年末                  1,829,209,941  1,832,304,941     691,887 689,684

於二零二五年，本公司透過聯交所回購及註銷其股份如下：
"""
    result = extract_equity_bridge_candidate(text, hk_metadata())
    assert result["opening_issued_shares"] == "1832304941"
    assert result["closing_issued_shares"] == "1829209941"
    assert result["reported_net_issued_share_change"] == "-3095000"
    assert result["cancelled_shares_candidate"] == "3500000"
    assert result["cancelled_shares_candidate_status"] == "VALID"
    assert result["cancelled_shares"] is None
    assert result["diluted_total_shares"] is None
    assert result["opening_evidence"]["page"] == 1


def test_hk_thousand_share_unit_and_current_year_cancellation() -> None:
    text = """25. 股本
                              票面值    股份數目 普通股面值
                              港幣元      千股   港幣百萬元
本公司已發行股本變動如下：
已發行及繳足：
於二零二四年一月一日                0.10 2,832,624 283
購回和註銷股份                     0.10 (9,400) (1)
於二零二四年十二月三十一日及二零二五年一月一日 0.10 2,823,224 282
購回和註銷股份                     0.10 (26,571) (2)
於二零二五年十二月三十一日           0.10 2,796,653 280
"""
    result = extract_equity_bridge_candidate(text, hk_metadata())
    assert result["opening_issued_shares"] == "2823224000"
    assert result["closing_issued_shares"] == "2796653000"
    assert result["reported_unit_multiplier"] == "1000"
    assert result["cancelled_shares_candidate"] == "26571000"
    assert result["cancelled_shares_candidate_status"] == "VALID"


def test_hk_small_unitless_or_rounded_figures_remain_unknown() -> None:
    text = """29. 股本
二零二五年 二零二四年
股份數目 面值 股份數目 面值
已發行及繳足股本
於一月一日 3,244 14,090 3,244 14,090
於十二月三十一日 3,244 14,090 3,244 14,090
"""
    result = extract_equity_bridge_candidate(text, hk_metadata())
    assert result["opening_issued_shares"] is None
    assert result["closing_issued_shares"] is None
    assert result["status"] == "REVIEW"
    assert result["cancelled_shares_candidate"] is None


def test_hk_buyback_without_explicit_cancellation_is_not_cancelled_candidate() -> None:
    text = """39. 股本
股份數目
已發行及繳足股本：
於年初 1,000,000,000
回購股份 (5,000,000)
於年末 995,000,000
本公司於年內購回股份並持作庫存股份。
"""
    result = extract_equity_bridge_candidate(text, hk_metadata())
    assert result["cancelled_shares_candidate"] is None
    assert result["cancelled_shares_candidate_status"] == "MISSING"


def test_hk_duplicate_opening_rows_are_conflict() -> None:
    text = """39. 股本
股份數目
已發行及繳足股本：
於年初 1,000,000,000
於年初 999,000,000
於年末 995,000,000
"""
    result = extract_equity_bridge_candidate(text, hk_metadata())
    assert result["status"] == "CONFLICT"
    assert result["opening_issued_shares"] is None
