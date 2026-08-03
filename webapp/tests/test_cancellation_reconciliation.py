from __future__ import annotations

import json
from pathlib import Path

import pytest

from liberty_v2.cancellation_reconciliation import (
    CancellationReconciliationError,
    pdf_page_text,
    reconcile_one,
    validate_bridge,
)


CONFIG = Path(__file__).resolve().parents[1] / "config" / "cancellation_reconciliation_v1.json"


def test_review_config_has_six_safe_decisions_and_reconciled_bridge_math() -> None:
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    reviews = payload["reviews"]
    assert len(reviews) == 6
    assert sum(item["decision"] == "ACCEPT" for item in reviews) == 5
    assert sum(item["decision"] == "REJECT" for item in reviews) == 1
    assert all(item["cancellation_fact_decision"] == "ACCEPT" for item in reviews)
    assert all(item["diluted_share_bridge_status"] == "REVIEW" for item in reviews)
    assert all(item["net_reduction_factor"] is None for item in reviews)
    assert all(item["b_eligible_authorized"] is False for item in reviews)
    assert [validate_bridge(item)["status"] for item in reviews].count("ACCEPT") == 4

    anta_2025 = next(
        item for item in reviews if item["company_id"] == "HK2020" and item["fiscal_year"] == 2025
    )
    assert anta_2025["candidate_cancelled_shares"] == "26571000"
    assert anta_2025["issued_share_bridge"]["verified_cancelled_shares"] == "26570200"
    bridge = validate_bridge(anta_2025)
    assert bridge["reported_minus_derived_shares"] == "-800"
    assert bridge["status"] == "REVIEW"


def test_accept_bridge_rejects_unreconciled_reported_closing() -> None:
    review = {
        "issued_share_bridge": {
            "opening_issued_shares": "1000",
            "issued_additions": [],
            "verified_cancelled_shares": "100",
            "derived_closing_issued_shares": "900",
            "reported_closing_issued_shares": "899",
            "status": "ACCEPT",
        }
    }
    with pytest.raises(CancellationReconciliationError, match="must equal"):
        validate_bridge(review)


def test_reconcile_one_never_promotes_issued_shares_to_diluted_shares(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"fixture")
    candidate = {
        "company_id": "HK0001",
        "company_name": "Fixture",
        "fiscal_year": 2025,
        "cancelled_shares_candidate": "100",
        "source_sha256": "a" * 64,
        "source_local_path": str(source),
    }
    review = {
        "review_id": "HK0001-FY2025",
        "company_id": "HK0001",
        "fiscal_year": 2025,
        "candidate_cancelled_shares": "100",
        "decision": "ACCEPT",
        "cancellation_fact_decision": "ACCEPT",
        "issued_share_bridge": {
            "opening_issued_shares": "1000",
            "issued_additions": [],
            "verified_cancelled_shares": "100",
            "derived_closing_issued_shares": "900",
            "reported_closing_issued_shares": "900",
            "status": "ACCEPT",
        },
        "diluted_share_bridge_status": "REVIEW",
        "dilution_blockers": ["no endpoint diluted count"],
        "net_reduction_factor": None,
        "b_eligible_authorized": False,
        "evidence_checks": [],
    }
    sources = {
        2025: {
            "sha256": "a" * 64,
            "path": source,
            "pdf_pages": 1,
        }
    }
    result = reconcile_one(candidate, review, sources, [])
    assert result["cancellation_fact_decision"] == "ACCEPT"
    assert result["issued_share_bridge"]["status"] == "ACCEPT"
    assert result["issued_shares_are_not_diluted_shares"] is True
    assert result["diluted_share_bridge_status"] == "REVIEW"
    assert result["net_reduction_factor"] is None
    assert result["b_eligible"] is None
    assert result["b_eligible_authorized"] is False


def test_pdf_page_text_uses_argv_and_no_shell(monkeypatch, tmp_path: Path) -> None:
    observed = {}

    class Completed:
        returncode = 0
        stdout = "page text"
        stderr = ""

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr("subprocess.run", fake_run)
    path = tmp_path / "annual report.pdf"
    assert pdf_page_text(path, 3, 4) == "page text"
    assert observed["argv"] == [
        "pdftotext",
        "-f",
        "3",
        "-l",
        "4",
        "-layout",
        str(path),
        "-",
    ]
    assert "shell" not in observed["kwargs"]

