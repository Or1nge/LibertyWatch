from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from liberty_v2.import_dividends import (
    DividendImportError,
    convert_ready_distribution,
    load_controlled_dividend_facts,
)
from liberty_v2.models import DataStatus, RawDataPoint


ROOT = Path(__file__).resolve().parents[1]
REAL_RECONCILIATION = (
    ROOT.parent / "data" / "shareholder-v2" / "reconciliation" / "dividend-v1"
)
REVIEWED_AT = "2026-08-03T12:00:00+08:00"


def _official_source(company_id: str, fiscal_year: int, *, supporting: bool) -> dict:
    source_year = fiscal_year + 1 if supporting else fiscal_year
    return {
        "source_type": "OFFICIAL_ANNUAL_REPORT",
        "company_id": company_id,
        "fiscal_year": source_year,
        "source_document": f"FY{source_year} annual report",
        "source_name": "Official exchange disclosure",
        "source_url": f"https://example.invalid/{company_id}/FY{source_year}.pdf",
        "source_local_path": f"companies/{company_id}/FY{source_year}.pdf",
        "source_publish_date": f"{source_year + 1}-04-30",
        "source_sha256": hashlib.sha256(
            f"{company_id}:{source_year}".encode("utf-8")
        ).hexdigest(),
        "identity_status": "VALID",
        "verification_status": "FULL_ANNUAL_REPORT_SHA256_AND_PAGE_COUNT_OK",
    }


def _ready_distribution(index: int) -> dict:
    company_id = f"TEST{index:02d}"
    fiscal_year = 2010 + index
    return {
        "schema_version": "dividend-reconciliation-v1.0",
        "reviewed_at": REVIEWED_AT,
        "distribution_id": f"{company_id}-FY{fiscal_year}-ORDINARY-ANNUAL",
        "company_id": company_id,
        "company_name": f"Test Company {index}",
        "fiscal_year": fiscal_year,
        "dividend_kind": "ORDINARY",
        "component": "ANNUAL",
        "lifecycle_status": "PAID",
        "ordinary_cash_dividend_total": {
            "value": str(index * 1000),
            "currency": "CNY",
            "unit": "currency",
        },
        "per_share_components": [],
        "import_scope": "FISCAL_YEAR_TOTAL",
        "ready_for_controlled_ledger_import": True,
        "source_candidate_ids": [f"candidate-{index}"],
        "source_evidence": {
            "candidate_annual_report_pages": [
                _official_source(company_id, fiscal_year, supporting=False)
            ],
            "supporting_annual_reports": [
                _official_source(company_id, fiscal_year, supporting=True)
            ],
            "secondary_implementation_events": [
                {
                    "source_type": "SECONDARY_CORPORATE_ACTION_FEED",
                    "event_key": f"dividend:{index}",
                    "source": "Futu OpenD corporate actions",
                    "verification_status": "EXACT_EVENT_AND_PAYLOAD_HASH_MATCH",
                }
            ],
        },
        "writes_production": False,
    }


def _component_only_distribution() -> dict:
    return {
        "schema_version": "dividend-reconciliation-v1.0",
        "reviewed_at": REVIEWED_AT,
        "distribution_id": "SZ002430-FY2023-ORDINARY-INTERIM",
        "company_id": "SZ002430",
        "company_name": "杭氧股份",
        "fiscal_year": 2023,
        "dividend_kind": "ORDINARY",
        "component": "INTERIM",
        "lifecycle_status": "PAID",
        "ordinary_cash_dividend_total": None,
        "per_share_components": [
            {
                "component": "INTERIM",
                "value": "2.00",
                "currency": "CNY",
                "share_basis": 10,
            }
        ],
        "import_scope": "COMPONENT_ONLY",
        "ready_for_controlled_ledger_import": False,
        "source_candidate_ids": ["component-candidate"],
        "source_evidence": {
            "candidate_annual_report_pages": [
                _official_source("SZ002430", 2023, supporting=False)
            ],
            "supporting_annual_reports": [
                _official_source("SZ002430", 2023, supporting=True)
            ],
            "secondary_implementation_events": [],
        },
        "writes_production": False,
    }


def _dataset() -> tuple[dict, list[dict]]:
    distributions = [_ready_distribution(index) for index in range(1, 12)]
    distributions.append(_component_only_distribution())
    ready = [
        {"distribution_id": item["distribution_id"]}
        for item in distributions
        if item["ready_for_controlled_ledger_import"]
    ]
    report = {
        "schema_version": "dividend-reconciliation-v1.0",
        "reviewed_at": REVIEWED_AT,
        "source_candidate_manifest": {"status": "VALID"},
        "scope": {"distribution_count": len(distributions)},
        "ready_for_controlled_ledger_import_count": len(ready),
        "reconciled_complete_fiscal_year_total_count": len(ready),
        "ready_for_controlled_ledger_import": ready,
        "component_only_count": 1,
        "safety": {
            "production_staging_modified": False,
            "candidate_values_written_to_production": False,
        },
        "writes_production": False,
    }
    return report, distributions


def _write_dataset(root: Path, report: dict, distributions: list[dict]) -> None:
    distribution_root = root / "distributions"
    distribution_root.mkdir(parents=True)
    (root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    for distribution in distributions:
        path = distribution_root / f"{distribution['distribution_id']}.json"
        path.write_text(
            json.dumps(distribution, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
    files = []
    for path in sorted(root.rglob("*.json")):
        if path.name == "manifest.json":
            continue
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    manifest = {
        "schema_version": "dividend-reconciliation-v1.0",
        "created_at": REVIEWED_AT,
        "file_count": len(files),
        "files": files,
        "writes_production": False,
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_loads_exactly_eleven_paid_facts_without_writing(tmp_path: Path) -> None:
    report, distributions = _dataset()
    _write_dataset(tmp_path, report, distributions)
    before = _tree_hashes(tmp_path)
    facts = load_controlled_dividend_facts(tmp_path)
    assert _tree_hashes(tmp_path) == before
    assert len(facts) == 11
    assert ("SZ002430", 2023) not in facts
    fact = facts[("TEST01", 2011)]
    assert fact.ordinary_dividend_status == "PAID"
    assert fact.ordinary_dividend == Decimal("1000")
    assert fact.currency == "CNY"
    assert isinstance(fact.raw_data_point, RawDataPoint)
    assert fact.raw_data_point.field_id == "FY2011.ordinary_dividend"
    assert fact.raw_data_point.value == Decimal("1000")
    assert fact.raw_data_point.data_status is DataStatus.VALID
    assert fact.raw_data_point.source_document == "FY2012 annual report"
    assert fact.raw_data_point.source_name == "Official exchange disclosure"
    assert fact.auxiliary_sources[0]["source"] == "Futu OpenD corporate actions"


def test_component_only_hangyang_is_not_convertible() -> None:
    with pytest.raises(DividendImportError, match="not approved for controlled import"):
        convert_ready_distribution(_component_only_distribution())


def test_loader_requires_exactly_eleven_ready_facts(tmp_path: Path) -> None:
    report, distributions = _dataset()
    distributions[0]["ready_for_controlled_ledger_import"] = False
    report["ready_for_controlled_ledger_import"] = report[
        "ready_for_controlled_ledger_import"
    ][1:]
    report["ready_for_controlled_ledger_import_count"] = 10
    report["reconciled_complete_fiscal_year_total_count"] = 10
    _write_dataset(tmp_path, report, distributions)
    with pytest.raises(DividendImportError, match="exactly 11"):
        load_controlled_dividend_facts(tmp_path)


def test_loader_rejects_duplicate_company_fiscal_year(tmp_path: Path) -> None:
    report, distributions = _dataset()
    distributions[1]["company_id"] = distributions[0]["company_id"]
    distributions[1]["fiscal_year"] = distributions[0]["fiscal_year"]
    for group in (
        "candidate_annual_report_pages",
        "supporting_annual_reports",
    ):
        for source in distributions[1]["source_evidence"][group]:
            source["company_id"] = distributions[0]["company_id"]
    _write_dataset(tmp_path, report, distributions)
    with pytest.raises(DividendImportError, match="duplicate company/fiscal-year"):
        load_controlled_dividend_facts(tmp_path)


def test_loader_rejects_invalid_official_source_identity(tmp_path: Path) -> None:
    report, distributions = _dataset()
    distributions[0]["source_evidence"]["supporting_annual_reports"][0][
        "identity_status"
    ] = "INVALID"
    _write_dataset(tmp_path, report, distributions)
    with pytest.raises(DividendImportError, match="identity is not VALID"):
        load_controlled_dividend_facts(tmp_path)


def test_futu_cannot_replace_the_primary_official_annual_report(tmp_path: Path) -> None:
    report, distributions = _dataset()
    distributions[0]["source_evidence"]["supporting_annual_reports"] = []
    _write_dataset(tmp_path, report, distributions)
    with pytest.raises(DividendImportError, match="official annual report is required"):
        load_controlled_dividend_facts(tmp_path)


def test_loader_rejects_an_artifact_that_can_write_production(tmp_path: Path) -> None:
    report, distributions = _dataset()
    distributions[0]["writes_production"] = True
    _write_dataset(tmp_path, report, distributions)
    with pytest.raises(DividendImportError, match="not explicitly read-only"):
        load_controlled_dividend_facts(tmp_path)


@pytest.mark.skipif(
    not (REAL_RECONCILIATION / "manifest.json").is_file(),
    reason="local dividend reconciliation artifact is not present",
)
def test_current_dividend_v1_artifact_converts_to_eleven_facts() -> None:
    facts = load_controlled_dividend_facts(REAL_RECONCILIATION)
    assert len(facts) == 11
    assert ("SZ002430", 2023) not in facts
    assert all(fact.ordinary_dividend_status == "PAID" for fact in facts.values())
    assert all(isinstance(fact.ordinary_dividend, Decimal) for fact in facts.values())
