from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "shareholder_v2_readiness.py"
SPEC = importlib.util.spec_from_file_location("shareholder_v2_readiness", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
readiness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(readiness)

NOW = datetime(2026, 8, 3, 6, tzinfo=timezone.utc)


def practical_company(company_id: str = "issuer-ready") -> dict:
    return {
        "company_id": company_id,
        "company_name": "可计算样本",
        "industry_kind": "NON_FINANCIAL",
        "expected_share_classes": ["A"],
        "share_classes": [
            {
                "security_id": "a",
                "share_class": "A",
                "price": "10",
                "issued_shares": "100",
                "currency": "CNY",
                "fx_to_base": "1",
                "price_timestamp": NOW.isoformat(),
                "quote_status": "VALID",
                "material": True,
            }
        ],
        "annual_distributions": [
            {
                "fiscal_year": year,
                "fiscal_year_end_date": f"{year}-12-31",
                "period_type": "FULL_YEAR",
                "ordinary_dividend_status": "PAID",
                "ordinary_dividend": "100",
            }
            for year in (2025, 2024)
        ],
        "coverage": {
            "fcf_years": [
                {
                    "fiscal_year": year,
                    "operating_cash_flow": "160",
                    "capital_expenditure": "20",
                    "lease_principal_repayment": None,
                }
                for year in (2025, 2024)
            ]
        },
        "valuation": {
            "metric": "P_FCF",
            "current": "12",
        },
        "balance_sheet": {"net_debt_ebitda": "1.2"},
        "raw_data_points": [
            {
                "field_id": "FY2025.ordinary_dividend",
                "source_name": "exchange",
                "source_document": "FY2025 annual report",
                "source_url_or_local_path": "https://example.com/report.pdf",
                "fiscal_period": "FY2025",
                "unit": "currency",
                "data_status": "VALID",
            }
        ],
    }


def test_readiness_identifies_estimated_but_calculable_company() -> None:
    result = readiness.inspect_company(practical_company(), now=NOW)
    assert result["minimum_calculable"] is True
    assert result["estimated_tier"] == "ESTIMATED"
    assert result["eligible_dividend_year_count"] == 2
    assert result["coverage_year_count"] == 2
    assert "SIMPLIFIED_FCF" in result["proxy_or_warning_codes"]
    assert "VALUATION_CURRENT_ONLY" in result["proxy_or_warning_codes"]
    assert result["reconciliations_missing"] == 4


def test_readiness_blocks_missing_market_cap_basis() -> None:
    raw = practical_company("issuer-blocked")
    raw["share_classes"] = []
    result = readiness.inspect_company(raw, now=NOW)
    assert result["minimum_calculable"] is False
    assert result["estimated_tier"] == "BLOCKED"
    assert "MARKET_CAP_BASIS_MISSING" in result["blockers"]


def test_readiness_report_returns_required_intersection_counts(tmp_path: Path) -> None:
    company_root = tmp_path / "companies"
    company_root.mkdir()
    (company_root / "ready.json").write_text(
        json.dumps(practical_company()),
        encoding="utf-8",
    )
    blocked = practical_company("issuer-blocked")
    blocked["valuation"] = {}
    (company_root / "blocked.json").write_text(
        json.dumps(blocked),
        encoding="utf-8",
    )

    report = readiness.build_report(tmp_path, now=NOW)
    assert report["parsed_company_count"] == 2
    assert report["condition_counts"]["companies_with_2plus_eligible_dividend_years"] == 2
    assert report["condition_counts"]["minimum_calculable_candidates"] == 1
    assert report["estimated_tier_counts"] == {"BLOCKED": 1, "ESTIMATED": 1}
