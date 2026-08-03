from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from liberty_v2.source_ledger import (
    SourceLedgerConflict,
    apply_ledger_to_staging_record,
    build_futu_financial_ledger,
    extract_official_pdf_text_candidates,
    load_sqlite_buyback_evidence,
    merge_raw_points,
    normalize_futu_statement_payload,
    official_candidates_to_raw_points,
    validate_official_pdf_candidates,
)
from liberty_v2.validation import validate_raw_provenance_records


ROOT = Path(__file__).resolve().parents[1]
LIBERTY_ROOT = ROOT.parent


def statement(
    *,
    fiscal_year: int,
    fiscal_end: str,
    period: str,
    currency: str,
    structures: list[tuple[int, str]],
    values: dict[int, str],
) -> dict:
    return {
        "next_key": "-1",
        "structure_list": [
            {"field_id": field_id, "display_name": name}
            for field_id, name in structures
        ],
        "report_list": [
            {
                "date_time_str": fiscal_end,
                "fiscal_year": fiscal_year,
                "financial_type": 7,
                "period_text": period,
                "currency_info": currency,
                "currency_code": currency,
                "accounting_standards": "test standards",
                "auditor_report": "无保留意见",
                "item_list": [
                    {
                        "field_id": field_id,
                        "display_name": dict(structures)[field_id],
                        "data": value,
                    }
                    for field_id, value in values.items()
                ],
            }
        ],
    }


def evidence(cash_flow: dict, balance_sheet: dict, *, issuer: str = "SH600000") -> dict:
    return {
        "schema_version": "futu-financial-evidence-v1",
        "fetched_at": "2026-08-02T01:00:00+00:00",
        "company": {
            "issuer_id": issuer,
            "security_id": issuer,
            "share_class": "A" if issuer.startswith(("SH", "SZ")) else "H",
        },
        "statements": {"cash_flow": cash_flow, "balance_sheet": balance_sheet},
        "errors": {},
        "sha256": "a" * 64,
    }


def test_normalize_provider_amounts_as_decimal_strings() -> None:
    raw = {
        "next_key": -1,
        "structure_list": [{"field_id": 3001, "display_name": "经营活动产生的现金流量净额"}],
        "report_list": [
            {
                "date_time_str": "2025-12-31",
                "fiscal_year": 2025,
                "financial_type": 7,
                "period_text": "2025/FY",
                "currency_code": "CNY",
                "item_list": [{"field_id": 3001, "data": 528305915.49}],
            }
        ],
    }
    normalized = normalize_futu_statement_payload(raw)
    assert normalized["report_list"][0]["item_list"][0]["data"] == "528305915.49"
    assert not isinstance(normalized["report_list"][0]["item_list"][0]["data"], float)


def test_a_share_futu_ledger_extracts_cfo_capex_but_not_share_count(tmp_path: Path) -> None:
    cash = statement(
        fiscal_year=2025,
        fiscal_end="2025-12-31",
        period="2025/FY",
        currency="CNY",
        structures=[
            (3001, "经营活动产生的现金流量净额"),
            (3043, "购建固定资产、无形资产和其他长期资产支付的现金"),
        ],
        values={3001: "2645781309.68", 3043: "200572338.00"},
    )
    balance = statement(
        fiscal_year=2025,
        fiscal_end="2025-12-31",
        period="2025/FY",
        currency="CNY",
        structures=[(3099, "实收资本(或股本)")],
        values={3099: "411967000"},
    )
    path = tmp_path / "evidence.json"
    ledger = build_futu_financial_ledger(evidence(cash, balance), evidence_path=path)
    row = ledger["annual_source_ledger"][0]
    assert row["fiscal_period"] == "2025/FY"
    assert row["fiscal_year_end_date"] == "2025-12-31"
    assert row["values"]["operating_cash_flow"]["value"] == "2645781309.68"
    assert row["values"]["capital_expenditure"]["value"] == "200572338.00"
    assert row["values"]["reported_share_capital_amount"]["value"] == "411967000"
    assert row["values"]["diluted_total_shares"]["value"] is None
    assert row["values"]["diluted_total_shares"]["data_status"] == "MISSING"
    assert row["values"]["lease_principal_repayment"]["data_status"] == "MISSING"
    assert all(isinstance(point["value"], (str, type(None))) for point in ledger["raw_data_points"])


def test_march_year_end_keeps_provider_fy2026_and_sums_complete_ifrs_capex(tmp_path: Path) -> None:
    cash = statement(
        fiscal_year=2026,
        fiscal_end="2026-03-31",
        period="2026/FY",
        currency="USD",
        structures=[
            (5001, "经营活动现金流量净额"),
            (5071, "购买固定资产"),
            (5073, "购买无形资产"),
        ],
        values={5001: "100.25", 5071: "-10", 5073: "-2.5"},
    )
    balance = statement(
        fiscal_year=2026,
        fiscal_end="2026-03-31",
        period="2026/FY",
        currency="USD",
        structures=[(5111, "股本")],
        values={5111: "50"},
    )
    ledger = build_futu_financial_ledger(
        evidence(cash, balance, issuer="HK0179"),
        evidence_path=tmp_path / "hk.json",
    )
    row = ledger["annual_source_ledger"][0]
    assert row["fiscal_year"] == 2026
    assert row["fiscal_year_end_date"] == "2026-03-31"
    assert row["values"]["capital_expenditure"]["value"] == "12.5"


def test_ifrs_capex_missing_component_is_not_zero(tmp_path: Path) -> None:
    cash = statement(
        fiscal_year=2026,
        fiscal_end="2026-03-31",
        period="2026/FY",
        currency="USD",
        structures=[
            (5001, "经营活动现金流量净额"),
            (5071, "购买固定资产"),
            (5073, "购买无形资产"),
        ],
        values={5001: "100", 5071: "10"},
    )
    balance = statement(
        fiscal_year=2026,
        fiscal_end="2026-03-31",
        period="2026/FY",
        currency="USD",
        structures=[(5111, "股本")],
        values={5111: "50"},
    )
    ledger = build_futu_financial_ledger(
        evidence(cash, balance, issuer="HK0179"),
        evidence_path=tmp_path / "hk.json",
    )
    capex = ledger["annual_source_ledger"][0]["values"]["capital_expenditure"]
    assert capex["value"] is None
    assert capex["data_status"] == "MISSING"


def test_patch_fills_fcf_only_and_keeps_rights_unverified(tmp_path: Path) -> None:
    cash = statement(
        fiscal_year=2025,
        fiscal_end="2025-12-31",
        period="2025/FY",
        currency="CNY",
        structures=[
            (3001, "经营活动产生的现金流量净额"),
            (3043, "购建固定资产、无形资产和其他长期资产支付的现金"),
        ],
        values={3001: "100", 3043: "20"},
    )
    balance = statement(
        fiscal_year=2025,
        fiscal_end="2025-12-31",
        period="2025/FY",
        currency="CNY",
        structures=[(3099, "实收资本(或股本)")],
        values={3099: "50"},
    )
    ledger = build_futu_financial_ledger(
        evidence(cash, balance, issuer="SH600660"),
        evidence_path=tmp_path / "futu.json",
    )
    staging = {
        "company_id": "SH600660",
        "company_name": "福耀玻璃",
        "industry_kind": "UNSUPPORTED",
        "securities": [{"security_id": "SH600660"}],
        "share_classes": [
            {
                "security_id": "SH600660",
                "share_class": "A",
                "issued_shares": None,
                "rights_verified": False,
            }
        ],
        "raw_data_points": [],
        "coverage": {},
        "source_summary": {},
    }
    patched = apply_ledger_to_staging_record(staging, ledger)
    assert patched["industry_kind"] == "NON_FINANCIAL"
    assert patched["coverage"]["fcf_years"][0]["operating_cash_flow"] == "100"
    assert patched["coverage"]["fcf_years"][0]["capital_expenditure"] == "20"
    assert patched["coverage"]["fcf_years"][0]["lease_principal_repayment"] is None
    assert patched["share_classes"][0]["issued_shares"] is None
    assert patched["share_classes"][0]["rights_verified"] is False
    scope = patched["source_summary"]["share_class_coverage"]
    assert scope["status"] == "CROSS_LISTING_REVIEW_REQUIRED"
    assert scope["known_market_classes"] == ["A", "H"]

    validation = validate_raw_provenance_records(
        patched["raw_data_points"],
        expected_company_id="SH600660",
    )
    assert validation.status.value == "PARTIAL"
    assert not any(issue.severity == "ERROR" for issue in validation.issues)


def test_existing_accepted_amount_is_never_silently_overwritten() -> None:
    common = {
        "field_id": "FY2025.operating_cash_flow",
        "data_status": "VALID",
        "value": "100",
    }
    with pytest.raises(SourceLedgerConflict):
        merge_raw_points([common], [{**common, "value": "101"}])
    merged = merge_raw_points([common], [{**common, "value": None, "data_status": "MISSING"}])
    assert merged[0]["value"] == "100"

    provider = {**common, "source_name": "Futu OpenD financial statement database"}
    filing = {
        **common,
        "source_name": "巨潮资讯网（法定信息披露平台）",
        "source_level": "OFFICIAL_FILING",
    }
    promoted = merge_raw_points([provider], [filing])
    assert promoted[0]["source_name"] == "巨潮资讯网（法定信息披露平台）"
    assert promoted[0]["source_level"] == "OFFICIAL_FILING"


def test_sqlite_buyback_is_review_evidence_not_cancelled_buyback(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE events (
            event_key TEXT, issuer_id TEXT, event_type TEXT, event_date TEXT,
            source TEXT, source_url TEXT, payload_json TEXT,
            first_seen_at TEXT, last_seen_at TEXT
        )"""
    )
    connection.execute(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "buyback:1",
            "SH600000",
            "buyback",
            "2025-12-31",
            "Futu OpenD corporate actions",
            "",
            json.dumps(
                {
                    "buy_back_money": 100.25,
                    "buy_back_sum": 10,
                    "event_proce_desc": "实施完成",
                    "change_reg_date_str": "2025-12-31",
                    "record_market": "A",
                    "share_type": "流通A股",
                }
            ),
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()
    rows = load_sqlite_buyback_evidence(database, "SH600000")
    assert rows[0]["reported_buyback_cash"] == "100.25"
    assert rows[0]["reported_buyback_shares"] == "10"
    assert rows[0]["cancellation_verification_status"] == "REVIEW_REQUIRED"
    assert rows[0]["eligible_for_core_cancelled_buyback"] is False
    assert rows[0]["fiscal_period"] == "UNALLOCATED_REQUIRES_FISCAL_MAPPING"


def pdf_metadata() -> dict:
    return {
        "company_id": "SZ002032",
        "security_id": "SZ002032",
        "share_class": "A",
        "source_document": "2025年度报告.pdf",
        "source_url_or_local_path": "/evidence/2025年度报告.pdf",
        "source_publish_date": "2026-03-27",
        "source_fetch_time": "2026-08-02T01:00:00+00:00",
        "fiscal_period": "2025/FY",
        "fiscal_year": 2025,
        "fiscal_year_end_date": "2025-12-31",
    }


def test_official_pdf_candidate_requires_unique_match_and_prior_reconciliation() -> None:
    text = """合并现金流量表
单位：元 币种：人民币
经营活动产生的现金流量净额        2,645,781,309.68   2,500,000,000.00
购建固定资产、无形资产和其他长期资产支付的现金  200,572,338.00  190,000,000.00
\f合并资产负债表
单位：元 币种：人民币
实收资本（或股本）                 411,967,000.00    411,967,000.00
"""
    candidates = extract_official_pdf_text_candidates(text, pdf_metadata())
    cfo = next(item for item in candidates if item["field_name"] == "operating_cash_flow")
    assert cfo["match_count"] == 1
    assert cfo["value"] == "2645781309.68"
    assert cfo["comparative_value"] == "2500000000.00"
    assert cfo["page"] == 1
    assert cfo["status"] == "REVIEW_REQUIRED"

    prior = {
        "operating_cash_flow": {"value": "2500000000", "currency": "CNY", "unit": "currency"},
        "capital_expenditure": {"value": "190000000", "currency": "CNY", "unit": "currency"},
        "reported_share_capital_amount": {
            "value": "411967000",
            "currency": "CNY",
            "unit": "currency",
        },
    }
    validated = validate_official_pdf_candidates(candidates, prior_period_values=prior)
    assert {item["status"] for item in validated} == {"VALID"}
    points = official_candidates_to_raw_points(validated)
    assert all(point["data_status"] == "VALID" for point in points)
    assert all(isinstance(point["value"], str) for point in points)


def test_official_pdf_duplicate_or_bad_comparative_stays_conflict() -> None:
    text = """合并现金流量表
单位：万元 币种：人民币
经营活动产生的现金流量净额  100 90
经营活动产生的现金流量净额  100 90
"""
    candidates = extract_official_pdf_text_candidates(text, pdf_metadata())
    cfo_rows = [item for item in candidates if item["field_name"] == "operating_cash_flow"]
    assert len(cfo_rows) == 2
    assert all(item["status"] == "CONFLICT" for item in cfo_rows)

    unique_text = """合并现金流量表
单位：万元 币种：人民币
经营活动产生的现金流量净额  100 90
"""
    unique = extract_official_pdf_text_candidates(unique_text, pdf_metadata())
    checked = validate_official_pdf_candidates(
        unique,
        prior_period_values={
            "operating_cash_flow": {"value": "800000", "currency": "CNY", "unit": "currency"}
        },
    )
    cfo = next(item for item in checked if item["field_name"] == "operating_cash_flow")
    assert cfo["status"] == "CONFLICT"


def test_cli_identifies_exactly_56_remaining_companies() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "backfill_source_ledger.py"),
            "targets",
            "--companies",
            str(LIBERTY_ROOT / "data" / "source" / "companies.json"),
            "--annual-reports",
            str(LIBERTY_ROOT / "data" / "raw" / "annual_reports"),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["formal_company_count"] == 67
    assert payload["annual_report_covered_count"] == 11
    assert payload["target_count"] == 56
    flagged = {item["security_id"] for item in payload["known_cross_listing_review_required"]}
    assert {"SH600660", "SZ002352", "SH688235"} <= flagged
