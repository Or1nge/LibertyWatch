from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from liberty_v2 import controlled_import as module
from liberty_v2.controlled_import import (
    ControlledImportError,
    ControlledImportInputs,
    _ensure_global_missing_points,
    _merge_annual_facts,
    apply_import,
    rollback_import,
    sha256_file,
    verify_run,
)
from liberty_v2.models import DataStatus, RawDataPoint
from liberty_v2.snapshot_store import atomic_write_json


def _inputs(tmp_path: Path) -> ControlledImportInputs:
    return ControlledImportInputs(
        staging_dir=tmp_path / "staging",
        futu_ledger_root=tmp_path / "futu",
        cashflow_root=tmp_path / "cashflow",
        cashflow_v2_root=tmp_path / "cashflow-v2",
        dividend_root=tmp_path / "dividend",
        dividend_v2_root=tmp_path / "dividend-v2",
        cancellation_root=tmp_path / "cancellation",
        share_capital_root=tmp_path / "share-capital",
        official_annual_root=tmp_path / "official",
    )


def test_annual_facts_keep_unknowns_null_and_never_authorize_buyback(tmp_path: Path) -> None:
    record = {
        "company_id": "HK2020",
        "securities": [
            {"security_id": "HK2020", "share_class": "H", "currency": "HKD"}
        ],
        "annual_distributions": [],
        "raw_data_points": [],
    }
    point = RawDataPoint(
        company_id="HK2020",
        field_id="FY2025.cancelled_shares",
        security_id="HK2020",
        share_class="H",
        source_name="Official annual report",
        source_document="annual.pdf",
        source_url_or_local_path="https://example.com/annual.pdf",
        source_publish_date=date(2026, 3, 1),
        source_fetch_time=datetime(2026, 8, 1, tzinfo=timezone.utc),
        fiscal_period="FY2025",
        currency=None,
        unit="shares",
        value=Decimal("26570200"),
        data_status=DataStatus.VALID,
        restatement_status="ORIGINAL",
    )
    source = tmp_path / "manifest.json"
    source.write_text("{}", encoding="utf-8")
    _merge_annual_facts(
        record,
        [point],
        reviewed_at="2026-08-03T04:00:00+00:00",
        source_path=source,
    )
    row = record["annual_distributions"][0]
    assert row["cancelled_shares"] == "26570200"
    assert row["ordinary_dividend"] is None
    assert row["gross_cancelled_buyback"] is None
    assert row["diluted_net_share_reduction"] is None
    assert row["ordinary_dividend_status"] == "NOT_DISCLOSED"
    points = {item["field_id"]: item for item in record["raw_data_points"]}
    assert points["FY2025.cancelled_shares"]["data_status"] == "VALID"
    assert points["FY2025.gross_cancelled_buyback"]["data_status"] == "NOT_DISCLOSED"
    assert points["FY2025.gross_cancelled_buyback"]["value"] is None


def test_global_reconciliation_absences_are_explicit_not_zero(tmp_path: Path) -> None:
    record = {
        "company_id": "C1",
        "securities": [{"security_id": "S1", "share_class": "A", "currency": "CNY"}],
        "raw_data_points": [],
    }
    source = tmp_path / "manifest.json"
    source.write_text("{}", encoding="utf-8")
    _ensure_global_missing_points(
        record,
        reviewed_at="2026-08-03T04:00:00+00:00",
        source_path=source,
    )
    points = {item["field_id"]: item for item in record["raw_data_points"]}
    assert set(points) == {
        "VALUATION.current",
        "VALUATION.historical_median",
        "RECONCILIATION.dividend_per_share_times_entitled_shares",
        "RECONCILIATION.repurchased_shares_times_average_price",
        "RECONCILIATION.opening_minus_closing_shares",
        "RECONCILIATION.cancelled_minus_issued_and_converted",
    }
    assert all(item["value"] is None for item in points.values())
    assert all(item["data_status"] == "NOT_DISCLOSED" for item in points.values())


def test_apply_verify_and_guarded_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = _inputs(tmp_path)
    company_path = inputs.staging_dir / "companies" / "C1.json"
    original = {"company_id": "C1", "value": "before"}
    desired = {"company_id": "C1", "value": "after"}
    atomic_write_json(company_path, original)
    pre_sha = sha256_file(company_path)
    post_sha = module.canonical_sha256(desired)
    plan = {
        "schema_version": "controlled-import-plan-v1",
        "mode": "dry-run",
        "changed_company_count": 1,
        "changes": [
            {
                "company_id": "C1",
                "path": str(company_path.resolve()),
                "pre_sha256": pre_sha,
                "post_sha256": post_sha,
                "pre_size_bytes": company_path.stat().st_size,
                "post_size_bytes": len(module.canonical_bytes(desired)),
            }
        ],
        "writes_staging": False,
    }
    monkeypatch.setattr(module, "build_plan", lambda _inputs: ({"C1": desired}, plan))
    applied = apply_import(
        inputs,
        backup_root=tmp_path / "backups",
        run_root=tmp_path / "runs",
        run_id="test-run",
    )
    assert applied["applied"] is True
    assert json.loads(company_path.read_text())["value"] == "after"
    assert verify_run(tmp_path / "runs", "test-run")["verified"] is True

    company_path.write_text('{"company_id":"C1","value":"drift"}\n', encoding="utf-8")
    with pytest.raises(ControlledImportError, match="drifted"):
        rollback_import(tmp_path / "runs", "test-run")
    atomic_write_json(company_path, desired)
    rolled_back = rollback_import(tmp_path / "runs", "test-run")
    assert rolled_back["restored_file_count"] == 1
    assert json.loads(company_path.read_text()) == original


def test_apply_is_noop_when_plan_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    monkeypatch.setattr(
        module,
        "build_plan",
        lambda _inputs: (
            {},
            {
                "schema_version": "controlled-import-plan-v1",
                "mode": "dry-run",
                "changes": [],
                "changed_company_count": 0,
                "writes_staging": False,
            },
        ),
    )
    result = apply_import(
        inputs,
        backup_root=tmp_path / "backups",
        run_root=tmp_path / "runs",
    )
    assert result["applied"] is False
    assert result["run_id"] is None
    assert not (tmp_path / "backups").exists()
