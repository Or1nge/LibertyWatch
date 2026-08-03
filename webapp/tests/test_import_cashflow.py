from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from liberty_v2.cashflow_reconciliation import SCHEMA_VERSION, sha256_file
from liberty_v2.import_cashflow import (
    CashflowImportError,
    _load_reviewed_cashflow_imports,
    _point_from_decision,
    cashflow_import_payloads,
    load_reviewed_cashflow_imports,
)
from liberty_v2.models import DataStatus


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def manifest_set_sha(path: Path) -> str:
    mapping = {str(path.resolve()): sha256_file(path)}
    encoded = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def write_reconciliation_manifest(root: Path) -> None:
    ledger = root / "ledger.json"
    write_json(
        root / "manifest.json",
        {
            "schema_version": "cashflow-reviewed-decision-manifest-v1",
            "file_count": 1,
            "files": [
                {
                    "path": "ledger.json",
                    "size_bytes": ledger.stat().st_size,
                    "sha256": sha256_file(ledger),
                }
            ],
        },
    )


def fixture_tree(tmp_path: Path, *, value: str = "100") -> tuple[Path, Path, dict]:
    official_root = tmp_path / "official"
    pdf = official_root / "companies" / "C1_Test" / "documents" / "FY2025" / "annual.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.7 reviewed annual report")
    pdf_sha = sha256_file(pdf)
    company_manifest = official_root / "companies" / "C1_Test" / "manifest.json"
    document = {
        "company_id": "C1",
        "company_name": "测试公司",
        "security_id": "SH600001",
        "share_class": "A",
        "ticker": "600001",
        "market": "CN",
        "currency": "CNY",
        "fiscal_year": 2025,
        "fiscal_year_end_date": "2025-12-31",
        "source_document": "测试公司2025年年度报告",
        "source_name": "巨潮资讯网（法定信息披露平台）",
        "source_url": "https://static.cninfo.com.cn/finalpage/2026-04-01/test.PDF",
        "source_publish_date": "2026-04-01",
        "source_fetch_time": "2026-08-02T01:00:00Z",
        "restatement_status": "ORIGINAL",
        "selection_status": "SELECTED_CURRENT",
        "data_status": "VERIFIED",
        "local_path": pdf.relative_to(official_root).as_posix(),
        "sha256": pdf_sha,
    }
    write_json(company_manifest, {"company_id": "C1", "documents": [document]})
    company_manifest_sha = sha256_file(company_manifest)

    candidate_manifest = tmp_path / "candidates" / "manifest.json"
    write_json(candidate_manifest, {"schema_version": "candidate-test"})
    candidate_manifest_sha = sha256_file(candidate_manifest)
    reconciliation_root = tmp_path / "reconciliation"
    decision = {
        "decision_id": "C1:2025:operating_cash_flow",
        "company_id": "C1",
        "company_name": "测试公司",
        "security_id": "SH600001",
        "fiscal_year": 2025,
        "field": "operating_cash_flow",
        "decision": "ACCEPT",
        "reason_codes": ["ALL_EXACT_CHECKS_PASSED"],
        "accepted_value": value,
        "currency": "CNY",
        "checks": {"official_exact": True, "futu_exact": True},
        "current_official_source": {
            "source_document": document["source_document"],
            "source_url": document["source_url"],
            "source_publish_date": document["source_publish_date"],
            "source_local_path": str(pdf.resolve()),
            "source_sha256": pdf_sha,
            "source_manifest_path": str(company_manifest.resolve()),
            "source_manifest_sha256": company_manifest_sha,
            "fiscal_year": 2025,
            "fiscal_year_end_date": "2025-12-31",
            "currency": "CNY",
            "value": value,
            "evidence_rows": [{"page": 100, "page_line": 20}],
        },
        "futu_source": {
            "field_value": value,
            "field_currency": "CNY",
            "fiscal_period": "2025/FY",
        },
        "candidate_only": True,
        "eligible_for_core_write": False,
    }
    ledger = {
        "schema_version": SCHEMA_VERSION,
        "candidate_input_manifest": {
            "path": str(candidate_manifest.resolve()),
            "sha256": candidate_manifest_sha,
        },
        "official_source_manifest_set_sha256": manifest_set_sha(company_manifest),
        "decision_count": 1,
        "decisions": [decision],
    }
    write_json(reconciliation_root / "ledger.json", ledger)
    write_reconciliation_manifest(reconciliation_root)
    return reconciliation_root, official_root, ledger


def load_one(reconciliation_root: Path, official_root: Path):
    return _load_reviewed_cashflow_imports(
        reconciliation_root,
        official_root,
        required_decision_count=1,
        expected_reconciliation_manifest_sha256=None,
        expected_candidate_manifest_sha256=None,
    )


def rewrite_ledger(root: Path, ledger: dict) -> None:
    write_json(root / "ledger.json", ledger)
    write_reconciliation_manifest(root)


def test_verified_decision_becomes_coverage_and_raw_data_point(tmp_path: Path) -> None:
    reconciliation, official, _ledger = fixture_tree(tmp_path)
    imports = load_one(reconciliation, official)
    reviewed = imports["C1"]
    assert reviewed.coverage_rows == (
        {
            "fiscal_year": 2025,
            "fiscal_year_end_date": "2025-12-31",
            "fiscal_period": "2025/FY",
            "period_type": "FULL_YEAR",
            "operating_cash_flow": "100",
            "capital_expenditure": None,
            "lease_principal_repayment": None,
        },
    )
    point = reviewed.raw_data_points[0]
    assert point.field_id == "FY2025.operating_cash_flow"
    assert point.source_fetch_time.isoformat() == "2026-08-02T01:00:00+00:00"
    assert point.restatement_status == "ORIGINAL"
    payload = cashflow_import_payloads(imports)["C1"]
    assert payload["raw_data_points"][0]["data_status"] == "VALID"
    assert payload["raw_data_points"][0]["source_name"].startswith("巨潮资讯网")


def test_real_zero_is_explicit_known_zero(tmp_path: Path) -> None:
    reconciliation, official, _ledger = fixture_tree(tmp_path, value="0")
    point = load_one(reconciliation, official)["C1"].raw_data_points[0]
    assert point.value == 0
    assert point.data_status is DataStatus.KNOWN_ZERO


def test_reporting_currency_can_differ_from_security_trading_currency(tmp_path: Path) -> None:
    _reconciliation, official, ledger = fixture_tree(tmp_path)
    metadata = json.loads(next(official.glob("companies/*/manifest.json")).read_text())["documents"][0]
    metadata["currency"] = "HKD"
    point, _fiscal_end = _point_from_decision(ledger["decisions"][0], metadata)
    assert point.currency == "CNY"


def test_official_futu_amount_conflict_is_rejected(tmp_path: Path) -> None:
    reconciliation, official, ledger = fixture_tree(tmp_path)
    ledger["decisions"][0]["futu_source"]["field_value"] = "100.0001"
    rewrite_ledger(reconciliation, ledger)
    with pytest.raises(CashflowImportError, match="official/Futu amount conflict"):
        load_one(reconciliation, official)


def test_any_false_reconciliation_check_is_rejected(tmp_path: Path) -> None:
    reconciliation, official, ledger = fixture_tree(tmp_path)
    ledger["decisions"][0]["checks"]["futu_exact"] = False
    rewrite_ledger(reconciliation, ledger)
    with pytest.raises(CashflowImportError, match="not all reconciliation checks are true"):
        load_one(reconciliation, official)


def test_changed_candidate_manifest_is_rejected(tmp_path: Path) -> None:
    reconciliation, official, ledger = fixture_tree(tmp_path)
    candidate_path = Path(ledger["candidate_input_manifest"]["path"])
    candidate_path.write_text("changed", encoding="utf-8")
    with pytest.raises(CashflowImportError, match="changed after reconciliation"):
        load_one(reconciliation, official)


def test_changed_official_company_manifest_is_rejected(tmp_path: Path) -> None:
    reconciliation, official, _ledger = fixture_tree(tmp_path)
    company_manifest = next(official.glob("companies/*/manifest.json"))
    company_manifest.write_text(company_manifest.read_text() + " ", encoding="utf-8")
    with pytest.raises(CashflowImportError, match="manifest set changed"):
        load_one(reconciliation, official)


def test_public_loader_never_relaxes_fixed_268_scope(tmp_path: Path) -> None:
    reconciliation, official, _ledger = fixture_tree(tmp_path)
    with pytest.raises(CashflowImportError, match="exactly 268 decisions"):
        load_reviewed_cashflow_imports(reconciliation, official)
