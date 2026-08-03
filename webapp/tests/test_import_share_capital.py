from __future__ import annotations

import hashlib
import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from liberty_v2.import_share_capital import (
    ShareCapitalImportError,
    load_confirmed_issued_share_points,
    load_confirmed_share_capital_facts,
)
from liberty_v2.models import DataStatus, RawDataPoint


WEBAPP_ROOT = Path(__file__).resolve().parents[1]
RECONCILIATION = (
    WEBAPP_ROOT.parent
    / "data"
    / "shareholder-v2"
    / "reconciliation"
    / "share-capital-v1"
)
ANNUAL_ROOT = (
    WEBAPP_ROOT.parent / "data" / "raw" / "annual_reports" / "official_backfill_v1"
)


def _require_actual_bundle() -> None:
    if not (RECONCILIATION / "manifest.json").is_file() or not ANNUAL_ROOT.is_dir():
        pytest.skip("official share-capital reconciliation bundle is not present")


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_manifest(root: Path) -> None:
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    )
    _write_json(
        root / "manifest.json",
        {
            "schema_version": "share-capital-reconciliation-manifest-v1",
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


def _copy_bundle(tmp_path: Path) -> Path:
    _require_actual_bundle()
    target = tmp_path / "share-capital-v1"
    shutil.copytree(RECONCILIATION, target)
    return target


def test_loads_only_21_confirmed_current_class_share_points() -> None:
    _require_actual_bundle()
    facts = load_confirmed_share_capital_facts(RECONCILIATION, ANNUAL_ROOT)
    points = load_confirmed_issued_share_points(RECONCILIATION, ANNUAL_ROOT)
    assert len(facts) == len(points) == 21
    assert all(isinstance(point, RawDataPoint) for point in points)
    assert all(point.unit == "shares" and point.currency is None for point in points)
    assert all(point.data_status is DataStatus.VALID for point in points)
    assert all(point.value is not None and point.value > 0 for point in points)
    assert all(point.field_id == f"SECURITY.{point.security_id}.issued_shares" for point in points)
    assert sum(fact.rights_verified for fact in facts) == 17
    assert sum(fact.company_market_value_denominator_authorized for fact in facts) == 0

    expected_multi = {
        ("SH600600", "SH600600", Decimal("709125943"), False),
        ("SH600600", "HK00168", Decimal("655069178"), False),
        ("SH600660", "SH600660", Decimal("2002986332"), True),
        ("SH600660", "HK03606", Decimal("606757200"), True),
        ("SZ002352", "SZ002352", Decimal("4799430409"), False),
        ("SZ002352", "HK06936", Decimal("240000000"), False),
    }
    actual_multi = {
        (
            fact.point.company_id,
            fact.point.security_id,
            fact.point.value,
            fact.rights_verified,
        )
        for fact in facts
        if fact.point.company_id in {"SH600600", "SH600660", "SZ002352"}
    }
    assert actual_multi == expected_multi
    assert all(fact.economic_rights_factor == Decimal("1") for fact in facts if fact.rights_verified)
    assert all(fact.economic_rights_factor is None for fact in facts if not fact.rights_verified)


def test_manifest_tamper_is_rejected(tmp_path: Path) -> None:
    root = _copy_bundle(tmp_path)
    path = root / "companies" / "A600406.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ShareCapitalImportError, match="manifest size mismatch"):
        load_confirmed_share_capital_facts(root, ANNUAL_ROOT)


def test_review_fact_cannot_carry_a_numeric_share_count(tmp_path: Path) -> None:
    root = _copy_bundle(tmp_path)
    path = root / "companies" / "SH688235.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["material_share_classes"][0]["issued_shares"] = "115055260"
    _write_json(path, payload)
    _rewrite_manifest(root)
    with pytest.raises(ShareCapitalImportError, match="unaccepted class carries issued_shares"):
        load_confirmed_share_capital_facts(root, ANNUAL_ROOT)


def test_company_denominator_cannot_be_authorized_by_this_bundle(tmp_path: Path) -> None:
    root = _copy_bundle(tmp_path)
    path = root / "companies" / "SH600660.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["company_market_value_denominator_authorized"] = True
    _write_json(path, payload)
    _rewrite_manifest(root)
    with pytest.raises(ShareCapitalImportError, match="company denominator is authorized"):
        load_confirmed_share_capital_facts(root, ANNUAL_ROOT)


def test_prohibited_buyback_derived_value_is_rejected(tmp_path: Path) -> None:
    root = _copy_bundle(tmp_path)
    path = root / "companies" / "HK0669.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["gross_buyback"] = "1"
    _write_json(path, payload)
    _rewrite_manifest(root)
    with pytest.raises(ShareCapitalImportError, match="prohibited numeric field"):
        load_confirmed_share_capital_facts(root, ANNUAL_ROOT)
