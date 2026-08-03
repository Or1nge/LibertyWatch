from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from liberty_v2.import_dividends import DividendImportError
from liberty_v2.import_dividends_v2 import (
    EXPECTED_BLOCKED_COUNT,
    EXPECTED_COMPANY_COUNT,
    EXPECTED_READY_FACT_COUNT,
    EXPECTED_TARGET_SLOT_COUNT,
    load_controlled_dividend_facts_v2,
)
from liberty_v2.models import DataStatus, RawDataPoint


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_manifest(root: Path) -> None:
    files = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "manifest.json":
            continue
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    _write_json(
        root / "manifest.json",
        {
            "schema_version": "dividend-reconciliation-v2.0",
            "created_at": "2026-08-03T16:00:00+08:00",
            "file_count": len(files),
            "files": files,
            "writes_production": False,
        },
    )


def _distribution(
    company_id: str,
    fiscal_year: int,
    ordinal: int,
    *,
    source_sha256: str,
) -> dict:
    amount = str(1000 + ordinal)
    return {
        "schema_version": "dividend-reconciliation-v2.0",
        "reviewed_at": "2026-08-03T16:00:00+08:00",
        "distribution_id": f"{company_id}-FY{fiscal_year}-ORDINARY-ANNUAL",
        "company_id": company_id,
        "company_name": f"Company {company_id}",
        "fiscal_year": fiscal_year,
        "dividend_kind": "ORDINARY",
        "lifecycle_status": "PAID",
        "calculation_method": "DIRECT_OFFICIAL_IMPLEMENTED_TOTAL",
        "import_scope": "FISCAL_YEAR_TOTAL",
        "ready_for_controlled_ledger_import": True,
        "ordinary_cash_dividend_total": {
            "value": amount,
            "currency": "CNY",
            "unit": "currency",
        },
        "ordinary_components": [
            {
                "component_id": f"FY{fiscal_year}_ANNUAL",
                "component": "ANNUAL",
                "amount_method": "OFFICIAL_TOTAL",
                "value": amount,
                "currency": "CNY",
            }
        ],
        "source_evidence": {
            "official_annual_report_pages": [
                {
                    "source_type": "OFFICIAL_ANNUAL_REPORT",
                    "company_id": company_id,
                    "fiscal_year": fiscal_year + 1,
                    "source_document": f"FY{fiscal_year + 1} annual report",
                    "source_name": "Official exchange disclosure",
                    "source_url": f"https://example.test/{company_id}/{fiscal_year + 1}.pdf",
                    "source_publish_date": f"{fiscal_year + 2}-04-30",
                    "source_local_path": f"companies/{company_id}/FY{fiscal_year + 1}.pdf",
                    "source_sha256": source_sha256,
                    "pdf_pages": 100,
                    "identity_status": "VALID",
                    "verification_status": "FULL_ANNUAL_REPORT_SHA256_AND_PAGE_COUNT_OK",
                    "page": 42,
                    "matched_markers": ["implemented", amount],
                }
            ],
            "secondary_implementation_events": [
                {
                    "source_type": "SECONDARY_CORPORATE_ACTION_FEED",
                    "event_key": f"dividend:{company_id}:{fiscal_year}",
                    "issuer_id": company_id,
                    "payload_hash": "b" * 64,
                    "payload": {
                        "statement": "10派1.00元（含税）",
                        "process": "方案实施",
                        "dividend_payable_date": f"{fiscal_year + 1}/06/01",
                    },
                    "verification_status": "EXACT_EVENT_AND_PAYLOAD_HASH_MATCH",
                }
            ],
        },
        "writes_production": False,
    }


def _dataset(root: Path) -> tuple[list[Path], list[Path], Path]:
    annual_root = root.parent / f"{root.name}-official-annual-reports"
    target_map: dict[str, list[int]] = {}
    for ordinal in range(EXPECTED_COMPANY_COUNT):
        company_id = f"C{ordinal:02d}"
        target_map[company_id] = [2025, 2024] if ordinal == 55 else [2025, 2024, 2023, 2022, 2021]
    assert sum(len(years) for years in target_map.values()) == EXPECTED_TARGET_SLOT_COUNT

    ready_keys = {(f"C{ordinal:02d}", 2025) for ordinal in range(EXPECTED_READY_FACT_COUNT)}
    distribution_paths: list[Path] = []
    blocked_paths: list[Path] = []
    ready_report = []
    blocked_count = 0
    for ordinal, (company_id, years) in enumerate(sorted(target_map.items())):
        blocked = []
        for fiscal_year in years:
            if (company_id, fiscal_year) in ready_keys:
                source_path = (
                    annual_root / "companies" / company_id / f"FY{fiscal_year + 1}.pdf"
                )
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_bytes(f"official-{company_id}-{fiscal_year}".encode("utf-8"))
                distribution = _distribution(
                    company_id,
                    fiscal_year,
                    ordinal,
                    source_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
                )
                path = root / "distributions" / f"{distribution['distribution_id']}.json"
                _write_json(path, distribution)
                distribution_paths.append(path)
                ready_report.append(
                    {
                        "distribution_id": distribution["distribution_id"],
                        "company_id": company_id,
                        "fiscal_year": fiscal_year,
                    }
                )
            else:
                blocked.append(
                    {
                        "company_id": company_id,
                        "fiscal_year": fiscal_year,
                        "status": "BLOCKED",
                        "ordinary_cash_dividend_total": None,
                        "reason_code": "NOT_RECONCILED",
                        "reason_zh": "尚未核清完整已实施总额。",
                        "unknown_is_not_zero": True,
                        "writes_production": False,
                    }
                )
                blocked_count += 1
        blocked_payload = {
            "schema_version": "dividend-reconciliation-v2.0",
            "company_id": company_id,
            "company_name": f"Company {company_id}",
            "security_id": company_id,
            "market": "CN",
            "target_fiscal_years": years,
            "blocked": blocked,
            "writes_production": False,
        }
        path = root / "blocked" / f"{company_id}.json"
        _write_json(path, blocked_payload)
        blocked_paths.append(path)
    assert blocked_count == EXPECTED_BLOCKED_COUNT
    report = {
        "schema_version": "dividend-reconciliation-v2.0",
        "reviewed_at": "2026-08-03T16:00:00+08:00",
        "scope": {
            "company_count": EXPECTED_COMPANY_COUNT,
            "target_fiscal_year_slot_count": EXPECTED_TARGET_SLOT_COUNT,
            "ready_for_controlled_ledger_import_count": EXPECTED_READY_FACT_COUNT,
            "blocked_count": EXPECTED_BLOCKED_COUNT,
        },
        "source_manifests": {
            "dividend_candidates": {"status": "VALID"},
            "prior_dividend_reconciliation": {"status": "VALID"},
        },
        "target_fiscal_years": target_map,
        "ready_for_controlled_ledger_import": ready_report,
        "safety": {
            "production_staging_modified": False,
            "unknown_values_are_not_zero": True,
            "proposed_values_are_not_importable": True,
            "special_dividends_are_excluded": True,
            "component_only_values_are_not_full_year_totals": True,
            "official_pdf_sha256_and_page_evidence_revalidated": True,
        },
        "writes_production": False,
    }
    _write_json(root / "report.json", report)
    _write_manifest(root)
    return distribution_paths, blocked_paths, annual_root


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_v2_loader_is_read_only_and_returns_exactly_sixteen_facts(tmp_path: Path) -> None:
    root = tmp_path / "reconciliation"
    _, _, annual_root = _dataset(root)
    before = _tree_hashes(root)
    facts = load_controlled_dividend_facts_v2(
        root, official_annual_root=annual_root
    )
    assert _tree_hashes(root) == before
    assert len(facts) == EXPECTED_READY_FACT_COUNT
    fact = facts[("C00", 2025)]
    assert fact.ordinary_dividend == Decimal("1000")
    assert isinstance(fact.raw_data_point, RawDataPoint)
    assert fact.raw_data_point.data_status is DataStatus.VALID
    assert fact.raw_data_point.restatement_status == "RECONCILED_FROM_OFFICIAL_ANNUAL_REPORT_V2"


def test_v2_loader_rejects_a_proposed_distribution(tmp_path: Path) -> None:
    root = tmp_path / "reconciliation"
    distribution_paths, _, annual_root = _dataset(root)
    distribution = json.loads(distribution_paths[0].read_text(encoding="utf-8"))
    distribution["lifecycle_status"] = "PROPOSED"
    _write_json(distribution_paths[0], distribution)
    _write_manifest(root)
    with pytest.raises(DividendImportError, match="not paid"):
        load_controlled_dividend_facts_v2(root, official_annual_root=annual_root)


def test_v2_loader_rejects_a_total_that_differs_from_its_components(tmp_path: Path) -> None:
    root = tmp_path / "reconciliation"
    distribution_paths, _, annual_root = _dataset(root)
    distribution = json.loads(distribution_paths[0].read_text(encoding="utf-8"))
    distribution["ordinary_cash_dividend_total"]["value"] = "999999"
    _write_json(distribution_paths[0], distribution)
    _write_manifest(root)
    with pytest.raises(DividendImportError, match="invalid Decimal calculation"):
        load_controlled_dividend_facts_v2(root, official_annual_root=annual_root)


def test_v2_loader_rejects_a_number_in_a_blocked_unknown_row(tmp_path: Path) -> None:
    root = tmp_path / "reconciliation"
    _, blocked_paths, annual_root = _dataset(root)
    payload = json.loads(blocked_paths[-1].read_text(encoding="utf-8"))
    payload["blocked"][0]["ordinary_cash_dividend_total"] = "0"
    _write_json(blocked_paths[-1], payload)
    _write_manifest(root)
    with pytest.raises(DividendImportError, match="must not contain a dividend number"):
        load_controlled_dividend_facts_v2(root, official_annual_root=annual_root)


def test_v2_loader_rejects_manifest_tampering(tmp_path: Path) -> None:
    root = tmp_path / "reconciliation"
    distribution_paths, _, annual_root = _dataset(root)
    distribution_paths[0].write_text("{}\n", encoding="utf-8")
    with pytest.raises(DividendImportError, match="manifest validation failed"):
        load_controlled_dividend_facts_v2(root, official_annual_root=annual_root)


def test_v2_loader_rejects_a_replaced_official_pdf(tmp_path: Path) -> None:
    root = tmp_path / "reconciliation"
    _, _, annual_root = _dataset(root)
    official_pdf = next(annual_root.rglob("*.pdf"))
    official_pdf.write_bytes(b"replaced after reconciliation")
    with pytest.raises(DividendImportError, match="official PDF SHA-256 mismatch"):
        load_controlled_dividend_facts_v2(root, official_annual_root=annual_root)


def test_v2_loader_rejects_an_official_path_outside_the_annual_root(tmp_path: Path) -> None:
    root = tmp_path / "reconciliation"
    distribution_paths, _, annual_root = _dataset(root)
    distribution = json.loads(distribution_paths[0].read_text(encoding="utf-8"))
    distribution["source_evidence"]["official_annual_report_pages"][0][
        "source_local_path"
    ] = "../outside.pdf"
    _write_json(distribution_paths[0], distribution)
    _write_manifest(root)
    with pytest.raises(DividendImportError, match="stay under annual root"):
        load_controlled_dividend_facts_v2(root, official_annual_root=annual_root)
