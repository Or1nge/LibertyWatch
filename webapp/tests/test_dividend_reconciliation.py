from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from liberty_v2.dividend_reconciliation import (
    DividendReconciliationError,
    is_full_annual_report_title,
    validate_futu_event,
    validate_identity_fragments,
    validate_review_config,
    verify_file_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "dividend_reconciliation_v1.json"


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def review_inventory(config: dict) -> dict:
    companies = {
        item["distribution_id"]: item["company_id"]
        for item in config["reconciled_distributions"]
    }
    return {
        item["evidence_id"]: {
            "eligible_after_manual_review": item["selection_basis"] == "CURRENT_ELIGIBLE",
            "company_id": companies[
                item.get("distribution_id") or item["replacement_distribution_id"]
            ],
        }
        for item in config["candidate_decisions"]
    }


def test_full_revised_annual_report_is_not_mistaken_for_short_correction_notice() -> None:
    assert is_full_annual_report_title("2020年年度报告（更正版）") is True
    assert is_full_annual_report_title("2020年年度报告（修订版）") is True
    assert is_full_annual_report_title("年報2020") is True
    assert is_full_annual_report_title("关于2020年年度报告的更正公告") is False
    assert is_full_annual_report_title("2020年年度报告更正说明") is False
    assert is_full_annual_report_title("2020年年度报告摘要") is False


def test_issuer_and_security_identity_fragments_are_both_required() -> None:
    text = "公司代码：600025 华能澜沧江水电股份有限公司 2022年年度报告"
    assert validate_identity_fragments(
        text,
        ["华能澜沧江水电股份有限公司", "600025"],
        source="annual.pdf",
    ) == ["华能澜沧江水电股份有限公司", "600025"]
    with pytest.raises(DividendReconciliationError, match="identity failed"):
        validate_identity_fragments(
            text,
            ["华能澜沧江水电股份有限公司", "600026"],
            source="annual.pdf",
        )


def test_review_config_preserves_13_historical_decisions_and_covers_current_four() -> None:
    config = load_config()
    decisions = config["candidate_decisions"]
    inventory = review_inventory(config)
    validate_review_config(config, inventory)
    assert len(decisions) == 13
    assert sum(item["decision"] == "ACCEPT" for item in decisions) == 3
    assert sum(item["decision"] == "REJECT" for item in decisions) == 10
    assert sum(item["selection_basis"] == "CURRENT_ELIGIBLE" for item in decisions) == 4
    assert (
        sum(
            item["selection_basis"] == "HISTORICAL_ELIGIBLE_BEFORE_LIFECYCLE_FIX"
            for item in decisions
        )
        == 9
    )
    rejected = {item["evidence_id"] for item in decisions if item["decision"] == "REJECT"}
    direct_import_ids = {
        item["evidence_id"]
        for item in decisions
        if item.get("distribution_id") is not None
    }
    assert rejected.isdisjoint(direct_import_ids)


def test_review_config_rejects_an_uncovered_current_eligible_candidate() -> None:
    config = load_config()
    inventory = review_inventory(config)
    inventory["new-current-candidate"] = {
        "eligible_after_manual_review": True,
        "company_id": "SH000001",
    }
    with pytest.raises(DividendReconciliationError, match="currently eligible"):
        validate_review_config(config, inventory)


def test_historical_candidate_requires_explicit_selection_basis() -> None:
    config = copy.deepcopy(load_config())
    inventory = review_inventory(config)
    historical = next(
        item
        for item in config["candidate_decisions"]
        if item["selection_basis"] == "HISTORICAL_ELIGIBLE_BEFORE_LIFECYCLE_FIX"
    )
    historical["selection_basis"] = "CURRENT_ELIGIBLE"
    with pytest.raises(DividendReconciliationError, match="does not match current source state"):
        validate_review_config(config, inventory)


def test_rejected_original_candidate_cannot_be_presented_as_direct_distribution() -> None:
    config = copy.deepcopy(load_config())
    inventory = review_inventory(config)
    rejected = next(
        item for item in config["candidate_decisions"] if item["decision"] == "REJECT"
    )
    rejected["distribution_id"] = rejected.pop("replacement_distribution_id")
    with pytest.raises(DividendReconciliationError, match="replacement distribution"):
        validate_review_config(config, inventory)


def test_component_only_distribution_cannot_claim_controlled_import_readiness() -> None:
    config = load_config()
    candidate = next(
        item
        for item in config["reconciled_distributions"]
        if item["import_scope"] == "COMPONENT_ONLY"
    )
    candidate["ready_for_controlled_ledger_import"] = True
    inventory = review_inventory(config)
    with pytest.raises(DividendReconciliationError, match="fiscal-year total"):
        validate_review_config(config, inventory)


def test_futu_event_requires_exact_payload_hash_and_expected_fields(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE events (
            event_key TEXT PRIMARY KEY,
            issuer_id TEXT,
            event_type TEXT,
            event_date TEXT,
            source TEXT,
            source_url TEXT,
            payload_hash TEXT,
            payload_json TEXT
        );
        """
    )
    payload = {
        "statement": "10派2.00元（含税）",
        "process": "方案实施",
        "dividend_payable_date": "2020/04/22",
    }
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    connection.execute(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "dividend:test",
            "SZ000001",
            "dividend",
            "2020/03/17",
            "test feed",
            "",
            payload_hash,
            payload_text,
        ),
    )
    connection.commit()
    connection.close()
    expected = {
        "event_key": "dividend:test",
        "issuer_id": "SZ000001",
        "payload_hash": payload_hash,
        "expected_payload": payload,
    }
    assert validate_futu_event(database, expected)["payload"] == payload
    expected["payload_hash"] = "0" * 64
    with pytest.raises(DividendReconciliationError, match="payload_hash mismatch"):
        validate_futu_event(database, expected)


def test_manifest_verification_rejects_unlisted_file(tmp_path: Path) -> None:
    item = tmp_path / "report.json"
    item.write_text("{}\n", encoding="utf-8")
    manifest = {
        "file_count": 1,
        "files": [
            {
                "path": "report.json",
                "size_bytes": item.stat().st_size,
                "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
            }
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_file_manifest(tmp_path)["file_count"] == 1
    (tmp_path / "extra.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(DividendReconciliationError, match="file set mismatch"):
        verify_file_manifest(tmp_path)
