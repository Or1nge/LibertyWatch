from __future__ import annotations

import json
from pathlib import Path

from liberty_v2.share_capital_reconciliation import (
    exact_issued_candidate,
    pdf_page_text,
)


WEBAPP_ROOT = Path(__file__).resolve().parents[1]
CONFIG = WEBAPP_ROOT / "config" / "share_capital_reconciliation_v1.json"
OUTPUT = (
    WEBAPP_ROOT.parent
    / "data"
    / "shareholder-v2"
    / "reconciliation"
    / "share-capital-v1"
)


def test_review_config_has_controlled_counts_and_explicit_multi_class_issuers() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    policy = config["policy"]
    assert policy["expected_company_count"] == 56
    assert policy["expected_latest_five_company_years"] == 277
    assert policy["expected_material_class_count"] == 60
    assert policy["expected_accepted_current_class_facts"] == 21
    assert policy["expected_rights_verified_class_facts"] == 17
    assert policy["production_write"] is False
    assert len(config["direct_exact_current_company_ids"]) == 15
    assert set(config["material_class_overrides"]) == {
        "SH600600",
        "SH600660",
        "SH688235",
        "SZ002352",
    }
    assert all(len(rows) == 2 for rows in config["material_class_overrides"].values())
    assert len(config["manual_current_facts"]) == 6
    assert len(config["review_only_cases"]) == 1
    assert config["review_only_cases"][0]["company_id"] == "SH688235"


def test_exact_candidate_rejects_rounded_thousand_share_row() -> None:
    base = {
        "status": "VALID",
        "eligible_for_issued_share_candidate": True,
        "unit": "shares",
        "closing_issued_shares": "2796653000",
        "closing_evidence": {"reported_unit_multiplier": "1000"},
    }
    assert exact_issued_candidate(base) is False
    base["closing_evidence"]["reported_unit_multiplier"] = "1"
    assert exact_issued_candidate(base) is True


def test_pdf_page_text_uses_argument_array_and_no_shell(monkeypatch, tmp_path: Path) -> None:
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
    assert pdf_page_text(path, 2, 4) == "page text"
    assert observed["argv"] == [
        "pdftotext",
        "-f",
        "2",
        "-l",
        "4",
        "-layout",
        str(path),
        "-",
    ]
    assert "shell" not in observed["kwargs"]


def test_actual_bundle_keeps_diluted_endpoints_null_and_ah_classes_separate() -> None:
    if not (OUTPUT / "manifest.json").is_file():
        return
    report = json.loads((OUTPUT / "report.json").read_text(encoding="utf-8"))
    assert report["company_count"] == 56
    assert report["latest_five_company_year_count"] == 277
    assert report["latest_five_exact_issued_candidate_count"] == 90
    assert report["material_share_class_count"] == 60
    assert report["accepted_current_class_fact_count"] == 21
    assert report["rights_verified_class_fact_count"] == 17
    assert report["company_denominator_authorized_count"] == 0
    assert report["diluted_total_shares_non_null_count"] == 0
    assert report["diluted_net_share_reduction_non_null_count"] == 0

    for company_id in ("SH600600", "SH600660", "SH688235", "SZ002352"):
        company = json.loads(
            (OUTPUT / "companies" / f"{company_id}.json").read_text(encoding="utf-8")
        )
        assert company["material_share_class_count"] == 2
        assert len({item["security_id"] for item in company["material_share_classes"]}) == 2
        assert company["company_market_value_denominator_authorized"] is False
        assert all(
            row["diluted_total_shares"] is None
            and row["diluted_net_share_reduction"] is None
            for row in company["latest_five_fiscal_years"]
        )

    beigen = json.loads(
        (OUTPUT / "companies" / "SH688235.json").read_text(encoding="utf-8")
    )
    assert all(item["decision"] == "REVIEW" for item in beigen["material_share_classes"])
    assert all(item["issued_shares"] is None for item in beigen["material_share_classes"])
