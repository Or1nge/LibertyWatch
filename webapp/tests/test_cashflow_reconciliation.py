from __future__ import annotations

import json
from pathlib import Path

from liberty_v2.cashflow_reconciliation import (
    canonical_payload_sha256,
    decision_from_checks,
    exact_decimal_equal,
    metadata_matches_candidate,
    metadata_semantic_anomaly,
    official_document_checks,
    statement_year_present,
    verify_futu_payload,
)


def official_metadata(path: Path) -> dict:
    return {
        "company_id": "HK0291",
        "company_name": "华润啤酒",
        "security_id": "HK0291",
        "ticker": "00291",
        "fiscal_year": 2025,
        "fiscal_year_end_date": "2025-12-31",
        "source_document": "年報2025",
        "source_url": "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0423/a.pdf",
        "source_publish_date": "2026-04-23",
        "selection_status": "SELECTED_CURRENT",
        "data_status": "VERIFIED",
        "source_level": "OFFICIAL_DISCLOSURE_PLATFORM",
        "pdf_pages": 200,
        "size_bytes": path.stat().st_size,
        "sha256": "a" * 64,
        "resolved_local_path": str(path),
    }


def complete_report_text() -> str:
    return """華潤啤酒有限公司
股份代號 Stock Codes: 00291
2025 ANNUAL REPORT 年報
獨立核數師報告
綜合現金流量表
截至二零二五年十二月三十一日止年度
"""


def test_complete_issuer_report_identity_accepts_traditional_name(tmp_path: Path) -> None:
    pdf = tmp_path / "annual.pdf"
    pdf.write_bytes(b"%PDF-test")
    metadata = official_metadata(pdf)
    checked = official_document_checks(
        complete_report_text(), metadata, pdf, actual_pages=200, actual_sha256="a" * 64
    )
    assert checked["all_passed"] is True
    assert checked["identity_evidence"]["company_name_match"] is True


def test_correction_or_short_document_is_never_full_annual_report(tmp_path: Path) -> None:
    pdf = tmp_path / "notice.pdf"
    pdf.write_bytes(b"%PDF-test")
    metadata = official_metadata(pdf)
    metadata.update(
        {
            "source_document": "2025年年报更正公告",
            "pdf_pages": 1,
            "size_bytes": pdf.stat().st_size,
        }
    )
    checked = official_document_checks(
        complete_report_text(), metadata, pdf, actual_pages=1, actual_sha256="a" * 64
    )
    assert checked["all_passed"] is False
    assert checked["checks"]["not_correction_summary_or_circular"] is False
    assert checked["checks"]["full_report_page_count"] is False
    assert metadata_semantic_anomaly(metadata) == [
        "TITLE_IS_CORRECTION_SUMMARY_OR_CIRCULAR",
        "PDF_TOO_SHORT_FOR_FULL_ANNUAL_REPORT",
    ]


def test_full_corrected_edition_is_not_confused_with_correction_notice(tmp_path: Path) -> None:
    pdf = tmp_path / "corrected-full-report.pdf"
    pdf.write_bytes(b"%PDF-test")
    metadata = official_metadata(pdf)
    metadata["source_document"] = "2025年年度报告（更正版）"
    assert metadata_semantic_anomaly(metadata) == []


def test_futu_payload_requires_exact_canonical_hash(tmp_path: Path) -> None:
    payload = {
        "schema_version": "futu-financial-evidence-v1",
        "company": {"issuer_id": "HK0291", "security_id": "HK0291"},
        "statements": {"cash_flow": {"report_list": []}},
    }
    payload["sha256"] = canonical_payload_sha256(payload)
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert verify_futu_payload(payload, path)["all_passed"] is True
    payload["company"]["issuer_id"] = "WRONG"
    assert verify_futu_payload(payload, path)["checks"]["payload_sha256"] is False


def test_amount_reconciliation_has_no_rounding_tolerance() -> None:
    assert exact_decimal_equal("100.00", "100") is True
    assert exact_decimal_equal("100.0000001", "100") is False


def test_statement_year_supports_arabic_and_chinese_digits() -> None:
    pages = "cover\f綜合現金流量表 截至二零二五年十二月三十一日止年度\fnotes"
    assert statement_year_present(pages, [2], 2025) is True
    assert statement_year_present(pages, [2], 2024) is False


def test_candidate_metadata_must_match_manifest_source(tmp_path: Path) -> None:
    pdf = tmp_path / "annual.pdf"
    pdf.write_bytes(b"%PDF-test")
    metadata = official_metadata(pdf)
    candidate = {
        "company_id": "HK0291",
        "security_id": "HK0291",
        "fiscal_year": 2025,
        "fiscal_year_end_date": "2025-12-31",
        "source_document": "年報2025",
        "source_url": metadata["source_url"],
        "source_sha256": metadata["sha256"],
        "source_local_path": str(pdf),
    }
    assert all(metadata_matches_candidate(candidate, metadata).values())
    candidate["source_url"] = "https://example.test/wrong.pdf"
    assert metadata_matches_candidate(candidate, metadata)["source_url"] is False


def test_failed_hard_check_rejects_while_unresolved_identity_reviews() -> None:
    assert decision_from_checks({"exact": True}) == (
        "ACCEPT",
        ["ALL_EXACT_CHECKS_PASSED"],
    )
    assert decision_from_checks(
        {"hash": False}, hard_reject_keys=["hash"]
    ) == ("REJECT", ["hash"])
    assert decision_from_checks({"issuer_identity": False}) == (
        "REVIEW",
        ["issuer_identity"],
    )
