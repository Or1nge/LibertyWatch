from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from liberty_v2.analysis.job_store import AnalysisJobStore
from liberty_v2.analysis.prompt_renderer import (
    INPUT_DOCUMENT_NAMES,
    InputSnapshotBuilder,
    snapshot_hash,
    verify_input_snapshot,
)
from liberty_v2.analysis.storage import AnalysisStorage
from liberty_v2.analysis.triggers import evaluate_trigger
from liberty_v2.constants import CALCULATION_VERSION, PROMPT_VERSION
from liberty_v2.input_resolution import load_screening_financial_rows, screening_profile
from liberty_v2.policy import policy
from liberty_v2.public_contract import validate_public_index
from liberty_v2.screening import (
    coverage_shrunk_score,
    dividend_yield_component,
    financial_resilience_score,
    five_year_price_position_component,
    opportunity_score,
    valuation_component,
)


PROJECT = Path(__file__).resolve().parents[1]
SCREENING = policy()["screening"]
NOW = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(("yield_pct", "expected"), [(0, 0), (1, 20), (2.5, 50), (4, 80), (5, 100), (8, 100)])
def test_dividend_piecewise_bands(yield_pct: float, expected: int) -> None:
    assert dividend_yield_component(yield_pct, SCREENING["price_opportunity"])["value"] == expected


def test_null_dividend_is_not_zero() -> None:
    record = dividend_yield_component(None, SCREENING["price_opportunity"])
    assert record["value"] is None and record["input_value"] is None


@pytest.mark.parametrize("pe", [0, -1, "NaN", "Infinity"])
def test_nonpositive_or_nonfinite_pe_is_unavailable_without_pb(pe: object) -> None:
    record = valuation_component(pe_ttm=pe, pe=None, pb=None, profile="NON_FINANCIAL", policy=SCREENING["price_opportunity"])
    assert record["value"] is None


def test_pb_fallback_and_financial_pb_first() -> None:
    fallback = valuation_component(pe_ttm=-3, pe=0, pb=1, profile="NON_FINANCIAL", policy=SCREENING["price_opportunity"])
    financial = valuation_component(pe_ttm=5, pe=5, pb=0.8, profile="FINANCIAL", policy=SCREENING["price_opportunity"])
    assert fallback["value"] == 80 and "PB_FALLBACK" in fallback["warnings"]
    assert financial["value"] == 85 and financial["metric"] == "PB"


def test_five_year_price_percentile_and_minimum_points() -> None:
    price_policy = SCREENING["price_opportunity"]
    values = list(range(1, 101))
    result = five_year_price_position_component(25, values, price_policy)
    assert result["percentile_rank"] == "0.2500" and result["value"] == 75
    assert five_year_price_position_component(25, values[:51], price_policy)["value"] is None


def test_coverage_shrink_prevents_one_field_extreme() -> None:
    result = coverage_shrunk_score(
        {"a": {"value": 100, "basis": "TEST", "source_summary": {}}, "b": {"value": None}, "c": {"value": None}},
        {"a": "0.40", "b": "0.35", "c": "0.25"},
    )
    assert result["coverage"] == "0.4000" and result["value"] == "70.00"


def _nonfinancial_rows() -> list[dict]:
    return [
        {"fiscal_year": year, "net_profit": profit, "operating_cash_flow": profit + 20, "capital_expenditure": 10, "cash": 100, "interest_bearing_debt": 20, "total_equity": 200, "total_assets": 400, "total_liabilities": 200, "source": "test"}
        for year, profit in ((2023, 80), (2024, 90), (2025, 100))
    ]


def test_nonfinancial_resilience_and_simplified_fcf_warning() -> None:
    result = financial_resilience_score(_nonfinancial_rows(), profile="NON_FINANCIAL", policy=SCREENING["financial_resilience"])
    assert result["value"] is not None and result["coverage"] == "1.0000"
    assert "SIMPLIFIED_FCF" in result["components"]["simplified_fcf_quality"]["warnings"]


def test_financial_profile_does_not_use_fcf_and_missing_capital_reduces_coverage() -> None:
    result = financial_resilience_score(_nonfinancial_rows(), profile="FINANCIAL", policy=SCREENING["financial_resilience"])
    assert "simplified_fcf_quality" not in result["components"]
    assert result["components"]["capital_or_asset_quality"]["value"] is None
    assert float(result["coverage"]) < 1


def test_bank_and_insurance_profile_detection() -> None:
    profile_policy = SCREENING["profile"]
    assert screening_profile({"company_id": "bank"}, {"industry": "商业银行"}, profile_policy) == "FINANCIAL"
    assert screening_profile({"company_id": "insurer"}, {"sector": "保险"}, profile_policy) == "FINANCIAL"


def test_source_conflict_nulls_only_one_field(tmp_path: Path) -> None:
    raw = {
        "company_id": "issuer-conflict",
        "raw_data_points": [{"field_id": "FY2025.net_profit", "fiscal_period": "FY2025", "data_status": "CONFLICT", "value": "10", "source_name": "official"}],
    }
    rows, summary = load_screening_financial_rows(raw, evidence_root=tmp_path)
    assert rows[0]["net_profit"] is None and rows[0]["total_assets"] is None
    assert rows[0]["field_sources"]["net_profit"]["basis"] == "SOURCE_CONFLICT"
    assert summary["source_conflicts"] == [{"fiscal_year": 2025, "field_id": "net_profit"}]


def _component(value: str | None, weight: str) -> dict:
    return {"value": value, "nominal_weight": weight, "status": "VALID" if value is not None else "UNAVAILABLE", "basis": "TEST", "source_summary": {"source": "test"}, "warnings": []}


def screening_record(position: int) -> dict:
    opportunity = {"value": "70", "coverage": "0.7500", "status": "DATA_LIMITED", "basis": "DETERMINISTIC_COVERAGE_SHRINKAGE", "warnings": [], "components": {"dividend_yield": _component("80", "0.40"), "valuation": _component("60", "0.35"), "five_year_price_position": _component(None, "0.25")}}
    resilience = {"value": "65", "coverage": "1.0000", "status": "READY", "basis": "DETERMINISTIC_COVERAGE_SHRINKAGE", "warnings": [], "profile": "NON_FINANCIAL", "components": {"net_profit_quality": _component("65", "1.0")}}
    return {
        "schema_version": "shareholder-screen-v2", "calculation_version": CALCULATION_VERSION,
        "metric_definition_version": CALCULATION_VERSION, "company_id": f"issuer-{position:02d}", "company_name": f"公司{position}",
        "securities": [{"security_id": f"security-{position:02d}", "ticker": f"{position:06d}", "market": "CN"}],
        "as_of_date": "2026-08-04", "price_timestamp": NOW.isoformat(), "status": "DATA_LIMITED",
        "price": {"value": "10", "basis": "VENDOR"}, "opportunity_score": opportunity,
        "financial_resilience_score": resilience, "research_trigger": {"eligible": True, "trigger_type": "COMBINED_SCREEN", "reason": "test", "in_observation_zone": True, "event_codes": []},
        "warnings": [], "source_summary": {}, "analysis_status": {"status": "NOT_REQUESTED"},
        "market_metrics": {}, "financial_history": [], "research_inputs": {},
    }


def test_exact_67_company_public_release_and_no_nonfinite() -> None:
    rows = [screening_record(index) for index in range(67)]
    index = {"schema_version": "shareholder-screen-v2", "calculation_version": CALCULATION_VERSION, "metric_definition_version": CALCULATION_VERSION, "release_validity": "VALID_RELEASE", "company_count": 67, "companies": rows}
    summary = validate_public_index(index)
    assert summary.company_count == 67 and summary.opportunity_score_count == 67
    json.dumps(index, allow_nan=False)


def test_single_company_initial_backlog_and_cooldowns() -> None:
    company = screening_record(0)
    initial = evaluate_trigger(company, None, current_prompt_version=PROMPT_VERSION, now=NOW, initial_backlog=True)
    assert initial.should_trigger and initial.trigger_type == "INITIAL_TRIGGER_BACKLOG"
    cooled = evaluate_trigger(company, company, state=initial.state, has_legal_report=True, last_success_at=NOW, current_prompt_version=PROMPT_VERSION, now=NOW + timedelta(days=6))
    assert not cooled.should_trigger and "7日" in cooled.summary
    company["research_trigger"]["trigger_type"] = "ANNUAL_REPORT"
    ordinary = evaluate_trigger(company, company, state={"in_observation_zone": True, "initial_backlog_completed": True}, has_legal_report=True, last_success_at=NOW, current_prompt_version=PROMPT_VERSION, now=NOW + timedelta(days=29))
    assert not ordinary.should_trigger and "30日" in ordinary.summary


def test_emergency_event_bypasses_cooldown() -> None:
    company = screening_record(0)
    decision = evaluate_trigger(company, company, events=("PROFIT_WARNING",), has_legal_report=True, last_success_at=NOW, current_prompt_version=PROMPT_VERSION, now=NOW)
    assert decision.should_trigger and decision.analysis_mode == "URGENT_RISK_REVIEW"


def test_research_bundle_exact_file_set_and_sha(tmp_path: Path) -> None:
    company = screening_record(0)
    store = AnalysisJobStore(tmp_path / "jobs.sqlite3")
    digest = snapshot_hash(company)
    job, _ = store.enqueue(company_id=company["company_id"], analysis_mode="PRICE_RISK_ANALYSIS", trigger_type="COMBINED_SCREEN", trigger_payload={"summary": "test"}, input_snapshot_hash=digest, calculation_version=CALCULATION_VERSION, prompt_version=PROMPT_VERSION)
    input_dir = InputSnapshotBuilder(tmp_path / "jobs").prepare(job, company_snapshot=company, trigger={"type": "COMBINED_SCREEN", "summary": "test"})
    verify_input_snapshot(input_dir, digest)
    assert {path.name for path in input_dir.iterdir()} == INPUT_DOCUMENT_NAMES | {"sha256sums.json"}
    bundle = json.loads((input_dir / "research_bundle.json").read_text())
    assert bundle["input_snapshot_hash"] == digest and bundle["opportunity_score"]["value"] == "70"


def test_installer_copies_required_config_and_smokes_before_switch() -> None:
    text = (PROJECT / "scripts" / "install_shareholder_v2_services.sh").read_text(encoding="utf-8")
    assert "issuer_capital_structure_v1.json" in text
    assert "health-check" in text and "67-company configuration coverage" in text
    assert '"${RELEASE_DIR}/liberty_v2" "${RELEASE_DIR}/analysis" -type f -exec chmod 0644' in text
    assert text.index("health-check") < text.index('mv -Tf "${RELEASE_ROOT}/.current-${RELEASE_ID}"')


def test_web_image_contains_shareholder_v2_runtime() -> None:
    dockerfile = (PROJECT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY --chown=10001:10001 liberty_v2 /app/liberty_v2" in dockerfile


def test_remote_canary_runs_inside_the_python_311_web_container() -> None:
    deploy = (PROJECT / "scripts" / "deploy_ali.sh").read_text(encoding="utf-8")
    assert "docker exec -i liberty-watch-liberty-watch-1 python -" in deploy
    assert "sys.path.insert(0, release)" not in deploy


def test_feature_modes_default_closed_and_old_flag_not_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.shareholder_v2 import codex_analysis_mode, shareholder_screen_enabled, validate_sync_mode

    monkeypatch.delenv("SHAREHOLDER_SCREEN_ENABLED", raising=False)
    monkeypatch.delenv("CODEX_ANALYSIS_MODE", raising=False)
    monkeypatch.setenv("SHAREHOLDER_RETURN_V2_ENABLED", "true")
    assert shareholder_screen_enabled() is False and codex_analysis_mode() == "OFF"
    for mode in ("OFF", "INTERNAL", "PUBLIC"):
        monkeypatch.setenv("CODEX_ANALYSIS_MODE", mode)
        assert codex_analysis_mode() == mode

    monkeypatch.setenv("SHAREHOLDER_SCREEN_ENABLED", "false")
    monkeypatch.setenv("CODEX_ANALYSIS_MODE", "OFF")
    validate_sync_mode("structured")
    with pytest.raises(RuntimeError, match="CODEX_ANALYSIS_MODE=PUBLIC"):
        validate_sync_mode("analysis")
    monkeypatch.setenv("CODEX_ANALYSIS_MODE", "PUBLIC")
    validate_sync_mode("analysis")
