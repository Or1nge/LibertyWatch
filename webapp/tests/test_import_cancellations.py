from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from liberty_v2.import_cancellations import (
    CancellationImportError,
    load_confirmed_cancellation_points,
)
from liberty_v2.models import DataStatus, RawDataPoint


FACTS = (
    ("HK0669", "创科实业", 2016, "1500000"),
    ("HK0669", "创科实业", 2023, "500000"),
    ("HK0669", "创科实业", 2024, "3000000"),
    ("HK0669", "创科实业", 2025, "3500000"),
    ("HK2020", "安踏体育", 2024, "9400000"),
    ("HK2020", "安踏体育", 2025, "26570200"),
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_bundle_manifest(root: Path) -> None:
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    )
    _write_json(
        root / "manifest.json",
        {
            "schema_version": "cancellation-reconciliation-manifest-v1",
            "file_count": len(files),
            "files": [
                {
                    "path": path.relative_to(root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha(path),
                }
                for path in files
            ],
        },
    )


def _bundle(tmp_path: Path) -> tuple[Path, Path]:
    reconciliation = tmp_path / "reconciliation"
    annual_root = tmp_path / "annual"
    reviews = []
    summaries = []
    annual_documents: dict[str, list[dict]] = {"HK0669": [], "HK2020": []}
    for company_id, company_name, fiscal_year, value in FACTS:
        pdf = (
            annual_root
            / "companies"
            / f"{company_id}_{company_name}"
            / "documents"
            / f"FY{fiscal_year}"
            / "annual_report.pdf"
        )
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(f"official {company_id} FY{fiscal_year}".encode())
        relative_pdf = pdf.relative_to(annual_root).as_posix()
        source_document = f"FY{fiscal_year} annual report"
        source_url = f"https://www1.hkexnews.hk/{company_id}/{fiscal_year}.pdf"
        source_sha = _sha(pdf)
        source = {
            "company_id": company_id,
            "company_name": company_name,
            "security_id": company_id,
            "share_class": "H",
            "fiscal_year": fiscal_year,
            "fiscal_year_end_date": f"{fiscal_year}-12-31",
            "source_document": source_document,
            "source_url": source_url,
            "source_publish_date": f"{fiscal_year + 1}-03-31",
            "source_fetch_time": "2026-08-02T01:35:32Z",
            "restatement_status": "ORIGINAL",
            "selection_status": "SELECTED_CURRENT",
            "data_status": "VERIFIED",
            "local_path": relative_pdf,
            "sha256": source_sha,
            "pdf_pages": 100,
        }
        annual_documents[company_id].append(source)
        review = {
            "review_id": f"{company_id}-FY{fiscal_year}",
            "company_id": company_id,
            "company_name": company_name,
            "fiscal_year": fiscal_year,
            "candidate_cancelled_shares": value,
            "decision": "ACCEPT",
            "cancellation_fact_decision": "ACCEPT",
            "issued_share_bridge": {"verified_cancelled_shares": value},
            "diluted_share_bridge_status": "REVIEW",
            "net_reduction_factor": None,
            "b_eligible_authorized": False,
        }
        reviews.append(review)
        decision = {
            "schema_version": "cancellation-reconciliation-v1.0",
            "review_id": review["review_id"],
            "company_id": company_id,
            "company_name": company_name,
            "fiscal_year": fiscal_year,
            "candidate_decision": "ACCEPT",
            "cancellation_fact_decision": "ACCEPT",
            "verified_cancelled_shares": value,
            "diluted_share_bridge_status": "REVIEW",
            "net_reduction_factor": None,
            "b_eligible": None,
            "b_eligible_authorized": False,
            "writes_production": False,
            "source_checks": [
                {
                    "fiscal_year": fiscal_year,
                    "source_document": source_document,
                    "source_url": source_url,
                    "source_publish_date": source["source_publish_date"],
                    "source_local_path": str(pdf.resolve()),
                    "sha256": source_sha,
                    "pdf_pages": 100,
                    "identity_status": "VALID",
                }
            ],
        }
        _write_json(reconciliation / "decisions" / f"{review['review_id']}.json", decision)
        summaries.append(
            {
                "company_id": company_id,
                "fiscal_year": fiscal_year,
                "verified_cancelled_shares": value,
                "cancellation_fact_decision": "ACCEPT",
                "diluted_share_bridge_status": "REVIEW",
                "net_reduction_factor": None,
            }
        )

    for company_id, documents in annual_documents.items():
        company_name = next(item[1] for item in FACTS if item[0] == company_id)
        _write_json(
            annual_root / "companies" / f"{company_id}_{company_name}" / "manifest.json",
            {"company_id": company_id, "documents": documents},
        )
    basis = {
        "schema_version": "cancellation-reconciliation-review-v1.0",
        "policy": {"production_write": False},
        "reviews": reviews,
    }
    _write_json(reconciliation / "review_basis.json", basis)
    report = {
        "schema_version": "cancellation-reconciliation-v1.0",
        "candidate_count": 6,
        "cancellation_fact_counts": {"ACCEPT": 6},
        "diluted_share_bridge_counts": {"ACCEPT": 0, "REVIEW": 6},
        "net_reduction_factor_calculated_count": 0,
        "b_eligible_authorized_count": 0,
        "writes_production": False,
        "review_config_sha256": _sha(reconciliation / "review_basis.json"),
        "summary": summaries,
    }
    _write_json(reconciliation / "report.json", report)
    _rewrite_bundle_manifest(reconciliation)
    return reconciliation, annual_root


def test_loads_only_six_confirmed_cancelled_share_raw_points(tmp_path: Path) -> None:
    reconciliation, annual_root = _bundle(tmp_path)
    points = load_confirmed_cancellation_points(reconciliation, annual_root)
    assert len(points) == 6
    assert all(isinstance(point, RawDataPoint) for point in points)
    assert {(point.company_id, point.field_id) for point in points} == {
        (company_id, f"FY{year}.cancelled_shares")
        for company_id, _, year, _ in FACTS
    }
    assert all(point.unit == "shares" and point.currency is None for point in points)
    assert all(point.data_status is DataStatus.VALID for point in points)
    assert all(point.source_name == "HKEX official annual report" for point in points)
    assert all(point.source_fetch_time.tzinfo is not None for point in points)
    assert all(point.restatement_status == "ORIGINAL" for point in points)
    anta = next(
        point for point in points if point.company_id == "HK2020" and point.fiscal_period == "FY2025"
    )
    assert anta.value == Decimal("26570200")


def test_reconciliation_manifest_tamper_is_rejected(tmp_path: Path) -> None:
    reconciliation, annual_root = _bundle(tmp_path)
    path = reconciliation / "decisions" / "HK0669-FY2016.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(CancellationImportError, match="manifest size mismatch"):
        load_confirmed_cancellation_points(reconciliation, annual_root)


def test_numeric_b_eligible_is_rejected_even_with_a_fresh_manifest(tmp_path: Path) -> None:
    reconciliation, annual_root = _bundle(tmp_path)
    path = reconciliation / "decisions" / "HK0669-FY2016.json"
    decision = json.loads(path.read_text(encoding="utf-8"))
    decision["b_eligible"] = "1"
    _write_json(path, decision)
    _rewrite_bundle_manifest(reconciliation)
    with pytest.raises(CancellationImportError, match="contains or authorizes B_eligible"):
        load_confirmed_cancellation_points(reconciliation, annual_root)


def test_anta_2025_uncorrected_rounded_value_is_rejected(tmp_path: Path) -> None:
    reconciliation, annual_root = _bundle(tmp_path)
    decision_path = reconciliation / "decisions" / "HK2020-FY2025.json"
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    decision["verified_cancelled_shares"] = "26571000"
    _write_json(decision_path, decision)
    report_path = reconciliation / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    for item in report["summary"]:
        if item["company_id"] == "HK2020" and item["fiscal_year"] == 2025:
            item["verified_cancelled_shares"] = "26571000"
    _write_json(report_path, report)
    _rewrite_bundle_manifest(reconciliation)
    with pytest.raises(CancellationImportError, match="decision and review basis disagree"):
        load_confirmed_cancellation_points(reconciliation, annual_root)
