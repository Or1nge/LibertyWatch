from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from liberty_v2.dividend_candidates import (
    extract_dividend_report_candidates,
    pdftotext_layout,
    resolve_candidate_slot,
)
from scripts.support.extract_dividend_candidates import verify_output_manifest


def metadata(*, market: str = "CN", year: int = 2024) -> dict:
    return {
        "company_id": "SH603288",
        "company_name": "海天味业",
        "security_id": "SH603288",
        "share_class": "A" if market == "CN" else "H",
        "market": market,
        "fiscal_year": year,
        "fiscal_year_end_date": f"{year}-12-31",
        "source_name": "法定信息披露平台",
        "source_document": f"{year}年年度报告",
        "source_url": f"https://example.test/{year}.pdf",
        "source_publish_date": f"{year + 1}-04-01",
        "source_fetch_time": "2026-08-02T01:00:00Z",
        "local_path": f"/evidence/{year}.pdf",
        "sha256": "a" * 64,
    }


def test_cn_proposed_per_share_and_total_are_separate_and_never_core() -> None:
    result = extract_dividend_report_candidates(
        """董事会决议通过的本报告期利润分配预案
拟以总股本为基数，向全体股东按每 10 股派发现金股利 8.60 元（含税），拟派发现金红利 4,773,267,505.58 元。""",
        metadata(),
    )
    assert {(item["amount_kind"], item["value"]) for item in result["candidates"]} == {
        ("PER_SHARE", "8.60"),
        ("TOTAL", "4773267505.58"),
    }
    assert {item["lifecycle_status"] for item in result["candidates"]} == {"PROPOSED"}
    assert all(item["core_import_allowed"] is False for item in result["candidates"])
    assert all(item["eligible_after_manual_review"] is False for item in result["candidates"])
    assert result["ledger"]["ordinary"]["proposed"]["total"]["status"] == "REVIEW"


def test_approved_ordinary_candidate_is_only_manual_review_eligible() -> None:
    result = extract_dividend_report_candidates(
        "2024年度利润分配方案经股东大会审议通过，向全体股东每10股派发现金红利5.00元。",
        metadata(),
    )
    candidate = result["candidates"][0]
    assert candidate["dividend_kind"] == "ORDINARY"
    assert candidate["lifecycle_status"] == "APPROVED"
    assert candidate["eligible_after_manual_review"] is True
    assert candidate["core_import_allowed"] is False
    assert candidate["source_url"].endswith("2024.pdf")
    assert candidate["source_sha256"] == "a" * 64
    assert candidate["page"] == 1 and candidate["page_line_start"] == 1


@pytest.mark.parametrize(
    "text",
    [
        "2024年度利润分配预案需提交公司股东大会审议通过后实施，每10股派发现金红利5元。",
        "2024年度利润分配预案须经2024年年度股东大会审议通过后实施，每10股派发现金红利5元。",
        "2024年度利润分配预案尚待本公司股东大会审议通过，每10股派发现金红利5元。",
        "2024年度利润分配预案待股东大会审议批准，每10股派发现金红利5元。",
        "2024年度利润分配预案经股东大会审议通过后实施，每10股派发现金红利5元。",
        "2024年度每10股派发现金红利5元，自董事会及股东大会审议通过利润分配方案预案之日起实施。",
        "2024年度每10股派发现金红利5元，自董事会及股东大会审议通过利润分配方案预案之",
        "二零二四年度利潤分配預案須經股東大會審議通過後實施，每10股派發現金股息5港元。",
    ],
)
def test_pending_or_conditional_approval_is_proposed_not_approved(text: str) -> None:
    result = extract_dividend_report_candidates(text, metadata())
    candidate = result["candidates"][0]
    assert candidate["lifecycle_status"] == "PROPOSED"
    assert candidate["lifecycle_basis"] == "CONDITIONAL_OR_PENDING_APPROVAL"
    assert candidate["eligible_after_manual_review"] is False


def test_explicit_dated_past_approval_is_not_mistaken_for_future_condition() -> None:
    result = extract_dividend_report_candidates(
        "2016年度利润分配方案每10股派发现金红利1元。上述方案2017年4月27日经股东大会审议通过后实施。",
        metadata(year=2016),
    )
    candidate = result["candidates"][0]
    assert candidate["lifecycle_status"] == "APPROVED"


def test_already_submitted_and_approved_is_approved_not_pending() -> None:
    result = extract_dividend_report_candidates(
        "2024年度利润分配方案已提交股东大会审议通过，每10股派发现金红利5元。",
        metadata(),
    )
    candidate = result["candidates"][0]
    assert candidate["lifecycle_status"] == "APPROVED"
    assert candidate["eligible_after_manual_review"] is True


def test_paid_special_is_separate_and_not_eligible() -> None:
    result = extract_dividend_report_candidates(
        "截至二零二四年十二月三十一日止年度已派付特别股息每股普通股港币50分。",
        metadata(market="HK"),
    )
    candidate = result["candidates"][0]
    assert candidate["dividend_kind"] == "SPECIAL"
    assert candidate["lifecycle_status"] == "PAID"
    assert candidate["associated_fiscal_year"] == 2024
    assert candidate["currency"] == "HKD"
    assert candidate["unit"] == "分_per_1_shares"
    assert candidate["eligible_after_manual_review"] is False
    assert result["ledger"]["ordinary"]["paid_or_approved"]["per_share"]["candidate"] is None


def test_spaced_traditional_chinese_year_is_recognized() -> None:
    result = extract_dividend_report_candidates(
        "董 事 會 已 宣 派 截 至 二 零 二 四 年 止 年 度 末 期 股 息 每 股 0.20 港 元。",
        metadata(market="HK"),
    )
    assert result["candidates"][0]["associated_fiscal_year"] == 2024


def test_conversion_price_near_dividend_is_not_a_dividend_candidate() -> None:
    result = extract_dividend_report_candidates(
        "派發中期股息後，換股價被調整為每股38.85港元。",
        metadata(market="HK"),
    )
    assert result["candidate_count"] == 0


def test_paid_interim_and_proposed_final_are_not_mixed() -> None:
    text = """截至二零二四年十二月三十一日止年度中期股息每股普通股港币118分已派付。
董事会建议截至二零二四年十二月三十一日止年度末期股息每股普通股港币115分。"""
    result = extract_dividend_report_candidates(text, metadata(market="HK"))
    statuses = {(item["component"], item["lifecycle_status"], item["value"]) for item in result["candidates"]}
    assert ("INTERIM", "PAID", "118") in statuses
    assert ("FINAL", "PROPOSED", "115") in statuses
    assert all(item["core_import_allowed"] is False for item in result["candidates"])


def test_multiple_same_component_values_are_conflict_and_null() -> None:
    candidates = [
        {
            "evidence_id": "one",
            "dividend_kind": "ORDINARY",
            "lifecycle_status": "PAID",
            "amount_kind": "TOTAL",
            "associated_fiscal_year": 2024,
            "value": "100",
            "currency": "CNY",
            "unit": "currency",
            "share_basis": None,
            "component": "ANNUAL_TOTAL",
        },
        {
            "evidence_id": "two",
            "dividend_kind": "ORDINARY",
            "lifecycle_status": "APPROVED",
            "amount_kind": "TOTAL",
            "associated_fiscal_year": 2024,
            "value": "101",
            "currency": "CNY",
            "unit": "currency",
            "share_basis": None,
            "component": "ANNUAL_TOTAL",
        },
    ]
    resolved = resolve_candidate_slot(
        candidates,
        fiscal_year=2024,
        dividend_kind="ORDINARY",
        lifecycle_group="PAID_OR_APPROVED",
        amount_kind="TOTAL",
    )
    assert resolved["status"] == "CONFLICT"
    assert resolved["candidate"] is None
    assert resolved["core_import_allowed"] is False


def test_missing_is_null_not_zero() -> None:
    result = extract_dividend_report_candidates("本年度没有可唯一识别的分配数字。", metadata())
    assert result["candidate_count"] == 0
    slot = result["ledger"]["ordinary"]["paid_or_approved"]["total"]
    assert slot["status"] == "MISSING"
    assert slot["candidate"] is None


def test_pdftotext_uses_argv_without_shell(monkeypatch, tmp_path: Path) -> None:
    observed = {}

    class Completed:
        returncode = 0
        stdout = "layout"
        stderr = ""

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr("subprocess.run", fake_run)
    path = tmp_path / "annual report.pdf"
    assert pdftotext_layout(path) == "layout"
    assert observed["argv"] == ["pdftotext", "-layout", str(path), "-"]
    assert "shell" not in observed["kwargs"]


def test_output_manifest_verification(tmp_path: Path) -> None:
    candidate = tmp_path / "candidates" / "x.json"
    candidate.parent.mkdir(parents=True)
    candidate.write_text(json.dumps({"candidate": True}), encoding="utf-8")
    content = candidate.read_bytes()
    manifest = {
        "file_count": 1,
        "files": [
            {
                "path": "candidates/x.json",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_output_manifest(tmp_path)["checked_file_count"] == 1
