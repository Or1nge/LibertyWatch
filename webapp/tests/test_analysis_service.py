from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from liberty_v2.analysis.job_store import AnalysisJobStore
from liberty_v2.analysis.publication import build_public_analysis_statuses
from liberty_v2.analysis.prompt_renderer import InputSnapshotBuilder, snapshot_hash
from liberty_v2.analysis.reviewed_overlay import ReviewedOverlayStore
from liberty_v2.analysis.storage import AnalysisStorage
from liberty_v2.analysis.triggers import evaluate_trigger
from liberty_v2.analysis.worker import CodexWorker, WorkerConfig
from liberty_v2.constants import CALCULATION_VERSION, PROMPT_VERSION


PROJECT = Path(__file__).resolve().parents[1]
FAKE_CODEX = PROJECT / "scripts" / "support" / "fake_codex.py"
SCHEMA = PROJECT / "analysis" / "schema" / "risk_analysis_output_v1.json"
NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


def metric(value: str | None) -> dict:
    return {
        "value": value,
        "status": "VALID" if value is not None else "INSUFFICIENT_DATA",
        "display": value or "数据不足",
        "reason": None,
        "unit": "ratio",
    }


def company_snapshot(
    *,
    company_id: str = "issuer-test",
    ssy: str = "0.041",
    raw_yield: str = "0.042",
    h: str = "100",
    sustainable: str = "90",
    cr10: str = "0.045",
    coverage: str = "1.2",
    ri: str = "75",
    eri: str = "35",
    status: str = "VALID",
    vetoes: list[dict] | None = None,
    analysis_eligibility: dict | None = None,
) -> dict:
    snapshot = {
        "schema_version": "shareholder-return-v2",
        "calculation_version": CALCULATION_VERSION,
        "metric_definition_version": CALCULATION_VERSION,
        "company_id": company_id,
        "company_name": "测试公司",
        "securities": [{"security_id": "security-test", "ticker": "600000", "market": "CN"}],
        "as_of_date": "2026-08-01",
        "price_timestamp": "2026-08-01T12:00:00+00:00",
        "data_status": status,
        "metrics": {
            "sustainable_shareholder_yield": metric(ssy),
            "raw_2y_shareholder_yield": metric(raw_yield),
            "historical_conservative_distribution": metric(h),
            "sustainable_distribution": metric(sustainable),
            "conservative_return_10y": metric(cr10),
            "coverage_ratio": metric(coverage),
        },
        "scores": {
            "recommendation_index": metric(ri),
            "entry_risk_index": metric(eri),
        },
        "veto_flags": vetoes or [],
        "source_summary": {"documents": []},
    }
    if analysis_eligibility is not None:
        snapshot["analysis_eligibility"] = analysis_eligibility
    return snapshot


def prepare_job(tmp_path: Path, *, scenario: str = "valid"):
    database = tmp_path / "jobs.sqlite3"
    jobs_root = tmp_path / "jobs"
    output_root = tmp_path / "output"
    store = AnalysisJobStore(database)
    snapshot = company_snapshot()
    digest = snapshot_hash(snapshot)
    job, created = store.enqueue(
        company_id=snapshot["company_id"],
        analysis_mode="FULL_ENTRY_REVIEW",
        trigger_type="FULL_ENTRY_REVIEW",
        trigger_payload={"type": "FULL_ENTRY_REVIEW", "summary": "test"},
        input_snapshot_hash=digest,
        calculation_version=CALCULATION_VERSION,
        prompt_version=PROMPT_VERSION,
    )
    assert created
    InputSnapshotBuilder(jobs_root).prepare(
        job,
        company_snapshot=snapshot,
        trigger={"type": "FULL_ENTRY_REVIEW", "summary": "test"},
    )
    worker = CodexWorker(
        store,
        WorkerConfig(
            project_root=PROJECT,
            jobs_root=jobs_root,
            output_root=output_root,
            schema_path=SCHEMA,
            codex_binary=FAKE_CODEX,
            timeout_seconds=1,
            extra_environment={"FAKE_CODEX_SCENARIO": scenario, "FAKE_CODEX_SLEEP": "2"},
        ),
    )
    return store, worker, job, jobs_root, output_root


def test_trigger_full_entry_crossing() -> None:
    previous = company_snapshot(ssy="0.039")
    current = company_snapshot(ssy="0.041")
    result = evaluate_trigger(
        current,
        previous,
        current_prompt_version=PROMPT_VERSION,
        trade_date=date(2026, 8, 1),
        now=NOW,
    )
    assert result.should_trigger
    assert result.analysis_mode == "FULL_ENTRY_REVIEW"


def test_trigger_yield_trap_full_review() -> None:
    current = company_snapshot(ssy="0.039", raw_yield="0.045")
    result = evaluate_trigger(current, None, current_prompt_version=PROMPT_VERSION, now=NOW)
    assert result.should_trigger and "收益陷阱" in result.summary


def test_urgent_veto_ignores_price_cooldown() -> None:
    current = company_snapshot(
        vetoes=[{"code": "AUDIT_GOVERNANCE_ALERT", "severity": "MAJOR"}]
    )
    state = {"last_price_trigger_at": NOW.isoformat(), "in_observation_zone": True}
    result = evaluate_trigger(
        current,
        current,
        state=state,
        has_legal_report=True,
        last_success_at=NOW,
        events=("AUDIT_GOVERNANCE_ALERT",),
        current_prompt_version=PROMPT_VERSION,
        now=NOW,
    )
    assert result.analysis_mode == "URGENT_VETO_REVIEW"


def test_nonurgent_company_analysis_requires_30_days_since_success() -> None:
    result = evaluate_trigger(
        company_snapshot(ssy="0.041"),
        company_snapshot(ssy="0.039"),
        state={"in_observation_zone": False},
        has_legal_report=True,
        last_success_at=NOW - timedelta(days=29),
        current_prompt_version=PROMPT_VERSION,
        now=NOW,
    )
    assert not result.should_trigger
    assert "不足30天" in result.summary


def test_ordinary_four_percent_entry_runs_at_30_days() -> None:
    result = evaluate_trigger(
        company_snapshot(ssy="0.041"),
        company_snapshot(ssy="0.039"),
        state={"in_observation_zone": False},
        has_legal_report=True,
        last_success_at=NOW - timedelta(days=30),
        current_prompt_version=PROMPT_VERSION,
        now=NOW,
    )
    assert result.should_trigger
    assert result.analysis_mode == "FULL_ENTRY_REVIEW"


def test_qualitative_overlay_only_partial_can_bootstrap() -> None:
    current = company_snapshot(
        status="PARTIAL",
        analysis_eligibility={
            "eligible": True,
            "status": "CORE_VALID_QUALITATIVE_OVERLAY_PENDING",
            "missing_qualitative_scores": [
                "business_durability",
                "governance_capital_allocation",
            ],
        },
    )
    result = evaluate_trigger(
        current,
        None,
        state={"in_observation_zone": False},
        current_prompt_version=PROMPT_VERSION,
        now=NOW,
    )
    assert result.should_trigger
    assert result.analysis_mode == "FULL_ENTRY_REVIEW"


def test_arbitrary_partial_cannot_forge_bootstrap_eligibility() -> None:
    current = company_snapshot(
        status="PARTIAL",
        analysis_eligibility={
            "eligible": True,
            "status": "CORE_VALID_QUALITATIVE_OVERLAY_PENDING",
            "missing_qualitative_scores": ["cash_flow"],
        },
    )
    result = evaluate_trigger(current, None, current_prompt_version=PROMPT_VERSION, now=NOW)
    assert not result.should_trigger


def test_v21_blocked_tier_cannot_trigger_codex_even_with_forged_eligibility() -> None:
    current = company_snapshot(
        status="PARTIAL",
        analysis_eligibility={
            "eligible": True,
            "status": "CORE_VALID_QUALITATIVE_OVERLAY_PENDING",
            "missing_qualitative_scores": ["business_durability"],
        },
    )
    current["data_tier"] = "BLOCKED"
    result = evaluate_trigger(current, None, current_prompt_version=PROMPT_VERSION, now=NOW)
    assert not result.should_trigger


def test_unchanged_urgent_veto_does_not_retrigger_after_legal_report() -> None:
    current = company_snapshot(
        vetoes=[{"code": "AUDIT_GOVERNANCE_ALERT", "severity": "MAJOR"}]
    )
    result = evaluate_trigger(
        current,
        current,
        state={"in_observation_zone": True},
        has_legal_report=True,
        last_success_at=NOW,
        current_prompt_version=PROMPT_VERSION,
        now=NOW,
    )
    assert not result.should_trigger


def test_small_price_update_does_not_trigger_again() -> None:
    previous = company_snapshot(ssy="0.0410", raw_yield="0.0420")
    current = company_snapshot(ssy="0.0411", raw_yield="0.0421")
    state = {"in_observation_zone": True, "last_price_trigger_at": NOW.isoformat()}
    result = evaluate_trigger(
        current,
        previous,
        state=state,
        has_legal_report=True,
        last_success_at=NOW,
        current_prompt_version=PROMPT_VERSION,
        now=NOW,
    )
    assert not result.should_trigger


def test_material_change_threshold_triggers() -> None:
    previous = company_snapshot(ssy="0.041", h="100")
    current = company_snapshot(ssy="0.047", h="120")
    state = {"in_observation_zone": True}
    result = evaluate_trigger(
        current,
        previous,
        state=state,
        has_legal_report=True,
        current_prompt_version=PROMPT_VERSION,
        now=NOW,
    )
    assert result.analysis_mode == "MATERIAL_CHANGE_REVIEW"


def test_price_only_material_change_respects_seven_day_cooldown() -> None:
    previous = company_snapshot(ssy="0.041", cr10="0.045", ri="75")
    current = company_snapshot(ssy="0.047", cr10="0.053", ri="88")
    result = evaluate_trigger(
        current,
        previous,
        state={"in_observation_zone": True, "last_price_trigger_at": NOW.isoformat()},
        has_legal_report=True,
        last_success_at=NOW,
        current_prompt_version=PROMPT_VERSION,
        now=NOW + timedelta(days=1),
    )
    assert not result.should_trigger
    assert "不足30天" in result.summary


def test_prompt_major_version_change_respects_company_cooldown() -> None:
    result = evaluate_trigger(
        company_snapshot(),
        company_snapshot(),
        state={"in_observation_zone": True},
        has_legal_report=True,
        last_success_at=NOW,
        last_prompt_version="risk-review-v0.9.0",
        current_prompt_version=PROMPT_VERSION,
        now=NOW,
    )
    assert not result.should_trigger
    assert "不足30天" in result.summary


def test_prompt_major_version_change_rebuilds_baseline_after_30_days() -> None:
    result = evaluate_trigger(
        company_snapshot(),
        company_snapshot(),
        state={"in_observation_zone": True},
        has_legal_report=True,
        last_success_at=NOW - timedelta(days=30),
        last_prompt_version="risk-review-v0.9.0",
        current_prompt_version=PROMPT_VERSION,
        now=NOW,
    )
    assert result.analysis_mode == "FULL_ENTRY_REVIEW"


def test_non_price_material_event_respects_company_cooldown() -> None:
    result = evaluate_trigger(
        company_snapshot(),
        company_snapshot(),
        state={"in_observation_zone": True},
        events=("ANNUAL_REPORT",),
        has_legal_report=True,
        last_success_at=NOW - timedelta(days=5),
        current_prompt_version=PROMPT_VERSION,
        now=NOW,
    )
    assert not result.should_trigger
    assert "不足30天" in result.summary


def test_periodic_high_risk_refresh_after_30_days() -> None:
    current = company_snapshot(eri="55")
    result = evaluate_trigger(
        current,
        current,
        state={"in_observation_zone": True},
        has_legal_report=True,
        last_success_at=NOW - timedelta(days=31),
        current_prompt_version=PROMPT_VERSION,
        now=NOW,
    )
    assert result.analysis_mode == "PERIODIC_REFRESH"


def test_hysteresis_requires_five_distinct_trading_days() -> None:
    state = {"in_observation_zone": True, "below_exit_days": 0}
    low = company_snapshot(ssy="0.034", raw_yield="0.034")
    for day in range(4):
        result = evaluate_trigger(
            low,
            low,
            state=state,
            has_legal_report=True,
            last_success_at=NOW,
            current_prompt_version=PROMPT_VERSION,
            trade_date=date(2026, 8, 1) + timedelta(days=day),
            now=NOW + timedelta(days=day),
        )
        state = result.state
        assert state["in_observation_zone"]
    result = evaluate_trigger(
        low,
        low,
        state=state,
        has_legal_report=True,
        last_success_at=NOW,
        current_prompt_version=PROMPT_VERSION,
        trade_date=date(2026, 8, 5),
        now=NOW + timedelta(days=4),
    )
    assert not result.state["in_observation_zone"]


def test_invalid_structured_input_never_triggers() -> None:
    result = evaluate_trigger(
        company_snapshot(status="PARTIAL"),
        None,
        current_prompt_version=PROMPT_VERSION,
        now=NOW,
    )
    assert not result.should_trigger


def test_job_store_deduplicates_and_recovers_running_jobs(tmp_path: Path) -> None:
    store = AnalysisJobStore(tmp_path / "jobs.sqlite3")
    values = dict(
        company_id="issuer",
        analysis_mode="FULL_ENTRY_REVIEW",
        trigger_type="FULL_ENTRY_REVIEW",
        trigger_payload={"summary": "test"},
        input_snapshot_hash="a" * 64,
        calculation_version=CALCULATION_VERSION,
        prompt_version=PROMPT_VERSION,
    )
    first, created = store.enqueue(**values)
    duplicate, duplicate_created = store.enqueue(**values)
    assert created and not duplicate_created and first.job_id == duplicate.job_id
    claimed = store.claim_next()
    assert claimed and claimed.status == "RUNNING"
    restarted = AnalysisJobStore(tmp_path / "jobs.sqlite3")
    assert restarted.get(first.job_id).status == "RUNNING"
    assert restarted.recover_running_jobs() == 1
    recovered = restarted.get(first.job_id)
    assert recovered and recovered.status == "WAITING_RETRY"


def test_job_store_rejects_path_like_company_id(tmp_path: Path) -> None:
    store = AnalysisJobStore(tmp_path / "jobs.sqlite3")
    with pytest.raises(ValueError, match="company_id"):
        store.enqueue(
            company_id="../../escape",
            analysis_mode="FULL_ENTRY_REVIEW",
            trigger_type="FULL_ENTRY_REVIEW",
            trigger_payload={"summary": "test"},
            input_snapshot_hash="a" * 64,
            calculation_version=CALCULATION_VERSION,
            prompt_version=PROMPT_VERSION,
        )


def test_new_snapshot_supersedes_waiting_same_mode_but_not_running_job(tmp_path: Path) -> None:
    store = AnalysisJobStore(tmp_path / "jobs.sqlite3")
    common = {
        "company_id": "issuer",
        "analysis_mode": "MATERIAL_CHANGE_REVIEW",
        "trigger_type": "MATERIAL_METRIC_CHANGE",
        "trigger_payload": {"summary": "test"},
        "calculation_version": CALCULATION_VERSION,
        "prompt_version": PROMPT_VERSION,
    }
    old, _ = store.enqueue(input_snapshot_hash="a" * 64, **common)
    store.mark_error(
        old.job_id,
        status="WAITING_AUTH",
        error_code="AUTH_FAILED",
        error_message="private",
        next_retry_at=NOW + timedelta(minutes=5),
    )
    newer, created = store.enqueue(input_snapshot_hash="b" * 64, **common)
    assert created and newer.status == "PENDING"
    assert store.get(old.job_id).status == "SUPERSEDED"
    claimed = store.claim_next()
    assert claimed and claimed.job_id == newer.job_id
    assert store.has_running_mode("issuer", "MATERIAL_CHANGE_REVIEW")


def test_job_store_enforces_global_and_per_company_concurrency(tmp_path: Path) -> None:
    store = AnalysisJobStore(tmp_path / "jobs.sqlite3")
    jobs = []
    for index in range(3):
        job, _ = store.enqueue(
            company_id=f"issuer-{index}",
            analysis_mode="FULL_ENTRY_REVIEW",
            trigger_type="FULL_ENTRY_REVIEW",
            trigger_payload={"summary": "test"},
            input_snapshot_hash=str(index + 1) * 64,
            calculation_version=CALCULATION_VERSION,
            prompt_version=PROMPT_VERSION,
        )
        jobs.append(job)
    first = store.claim_next(global_concurrency=2)
    assert first is not None
    same_company, _ = store.enqueue(
        company_id=first.company_id,
        analysis_mode="MATERIAL_CHANGE_REVIEW",
        trigger_type="MATERIAL_METRIC_CHANGE",
        trigger_payload={"summary": "same company"},
        input_snapshot_hash="d" * 64,
        calculation_version=CALCULATION_VERSION,
        prompt_version=PROMPT_VERSION,
    )
    second = store.claim_next(global_concurrency=2)
    assert second and first.company_id != second.company_id
    assert store.get(same_company.job_id).status == "PENDING"
    assert store.claim_next(global_concurrency=2) is None
    store.mark_succeeded(first.job_id, tmp_path / "first")
    third = store.claim_next(global_concurrency=2)
    remaining_full = ({job.job_id for job in jobs} - {first.job_id, second.job_id}).pop()
    assert third and third.job_id == remaining_full


def test_worker_startup_checks_fake_catalog_and_auth(tmp_path: Path) -> None:
    _, worker, _, _, _ = prepare_job(tmp_path)
    status = worker.startup_check()
    assert status["model"] == "gpt-5.6-sol"
    assert status["reasoning_effort"] == "xhigh"


def test_worker_environment_does_not_inherit_publisher_or_database_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALI_SSH_KEY_PATH", "/private/publisher-key")
    monkeypatch.setenv("ANALYSIS_JOB_DB", "/private/jobs.sqlite3")
    monkeypatch.setenv("OPENAI_API_KEY", "model-credential")
    _, worker, _, _, _ = prepare_job(tmp_path)
    environment = worker._base_environment()
    assert environment["OPENAI_API_KEY"] == "model-credential"
    assert environment["FAKE_CODEX_SCENARIO"] == "valid"
    assert "ALI_SSH_KEY_PATH" not in environment
    assert "ANALYSIS_JOB_DB" not in environment


def test_worker_valid_output_is_atomic_and_latest_points_to_success(tmp_path: Path) -> None:
    store, worker, job, _, output_root = prepare_job(tmp_path)
    worker.startup_check()
    result = worker.run_once()
    assert result and result.status == "SUCCEEDED"
    latest = json.loads((output_root / job.company_id / "latest.json").read_text())
    assert latest["analysis_id"] == job.job_id
    run = output_root / job.company_id / "runs" / job.job_id
    assert (run / "final.json").is_file()
    assert (run / "report.md").is_file()
    assert (run / "reviewed_overlay.json").is_file()
    assert (run / "input" / "sha256sums.json").is_file()
    assert store.get(job.job_id).status == "SUCCEEDED"
    active = ReviewedOverlayStore(output_root).active_scores(
        job.company_id,
        on_date=date(2026, 8, 1),
    )
    assert active["business_durability"]["value"] == 72
    assert active["governance_capital_allocation"]["value"] == 68
    private_overlay = json.loads((run / "reviewed_overlay.json").read_text())
    assert private_overlay["scores"]["business_durability"]["produced_by_codex"] is True
    assert private_overlay["scores"]["business_durability"]["review_status"] == "DETERMINISTICALLY_ACCEPTED"


def test_latest_analysis_pointer_and_payload_hash_are_verified(tmp_path: Path) -> None:
    _, worker, job, jobs_root, output_root = prepare_job(tmp_path)
    assert worker.run_once().status == "SUCCEEDED"
    storage = AnalysisStorage(output_root, jobs_root)
    payload, report = storage.latest_public_payload(job.company_id)
    assert payload["analysis_id"] == job.job_id and report
    final_path = output_root / job.company_id / "runs" / job.job_id / "final.json"
    final_path.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        storage.latest_public_payload(job.company_id)


def test_latest_analysis_pointer_cannot_escape_company_directory(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    company_root = output_root / "issuer"
    company_root.mkdir(parents=True)
    (company_root / "latest.json").write_text(
        json.dumps(
            {
                "analysis_id": "analysis-1",
                "relative_path": "../other/final.json",
                "sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="path is invalid"):
        AnalysisStorage(output_root, tmp_path / "jobs").latest_public_payload("issuer")


@pytest.mark.parametrize("target", ("input/trigger.json", "rendered_prompt.md"))
def test_worker_rejects_tampered_immutable_input_before_codex(
    tmp_path: Path, target: str
) -> None:
    store, worker, job, jobs_root, output_root = prepare_job(tmp_path)
    (jobs_root / job.job_id / target).write_text("tampered", encoding="utf-8")
    result = worker.run_once()
    assert result and result.status == "INVALID_INPUT"
    assert result.error_code == "INPUT_SNAPSHOT_INVALID"
    assert not (output_root / job.company_id / "latest.json").exists()
    assert store.get(job.job_id).attempt_count == 1


@pytest.mark.parametrize(
    ("scenario", "expected_status", "expected_code"),
    [
        ("schema_error", "WAITING_RETRY", "SCHEMA_INVALID"),
        ("nonzero", "WAITING_RETRY", "CLI_NONZERO_EXIT"),
        ("auth_failure", "WAITING_AUTH", "AUTH_FAILED"),
        ("model_unavailable", "WAITING_MODEL", "MODEL_UNAVAILABLE"),
        ("wrong_company", "WAITING_RETRY", "SCHEMA_INVALID"),
        ("wrong_hash", "WAITING_RETRY", "SCHEMA_INVALID"),
        ("wrong_ticker", "WAITING_RETRY", "SCHEMA_INVALID"),
        ("overlay_unknown_source", "WAITING_RETRY", "SCHEMA_INVALID"),
        ("overlay_expiry_too_long", "WAITING_RETRY", "SCHEMA_INVALID"),
        ("overlay_rubric_mismatch", "WAITING_RETRY", "SCHEMA_INVALID"),
        ("timeout", "WAITING_RETRY", "TIMEOUT"),
    ],
)
def test_worker_failure_modes_do_not_create_latest(
    tmp_path: Path, scenario: str, expected_status: str, expected_code: str
) -> None:
    store, worker, job, _, output_root = prepare_job(tmp_path, scenario=scenario)
    result = worker.run_once()
    assert result and result.status == expected_status and result.error_code == expected_code
    assert not (output_root / job.company_id / "latest.json").exists()
    assert store.get(job.job_id).status == expected_status


def test_quota_exhaustion_pauses_without_dropping_job_and_can_resume(tmp_path: Path) -> None:
    store, worker, job, _, _ = prepare_job(tmp_path, scenario="quota")
    worker = CodexWorker(
        store,
        replace(worker.config, max_automatic_retries=0),
    )
    result = worker.run_once()
    paused = store.get(job.job_id)
    assert result and result.status == "WAITING_RETRY"
    assert paused and paused.error_code == "QUOTA_OR_RATE_LIMIT"
    assert paused.next_retry_at is None
    assert store.claim_next() is None
    assert store.resume_paused(external_only=True) == 0
    assert store.resume_paused() == 1
    assert store.claim_next() is not None


def test_worker_rejects_nonvalid_snapshot_before_model(tmp_path: Path) -> None:
    store = AnalysisJobStore(tmp_path / "jobs.sqlite3")
    jobs_root = tmp_path / "jobs"
    snapshot = company_snapshot(status="PARTIAL")
    job, _ = store.enqueue(
        company_id=snapshot["company_id"],
        analysis_mode="FULL_ENTRY_REVIEW",
        trigger_type="FULL_ENTRY_REVIEW",
        trigger_payload={"summary": "test"},
        input_snapshot_hash=snapshot_hash(snapshot),
        calculation_version=CALCULATION_VERSION,
        prompt_version=PROMPT_VERSION,
    )
    InputSnapshotBuilder(jobs_root).prepare(
        job,
        company_snapshot=snapshot,
        trigger={"type": "FULL_ENTRY_REVIEW", "summary": "test"},
    )
    worker = CodexWorker(
        store,
        WorkerConfig(PROJECT, jobs_root, tmp_path / "output", SCHEMA, FAKE_CODEX),
    )
    result = worker.run_once()
    assert result and result.status == "INVALID_INPUT"


def test_worker_accepts_narrow_qualitative_overlay_bootstrap(tmp_path: Path) -> None:
    store = AnalysisJobStore(tmp_path / "jobs.sqlite3")
    jobs_root = tmp_path / "jobs"
    snapshot = company_snapshot(
        status="PARTIAL",
        analysis_eligibility={
            "eligible": True,
            "status": "CORE_VALID_QUALITATIVE_OVERLAY_PENDING",
            "missing_qualitative_scores": [
                "business_durability",
                "governance_capital_allocation",
            ],
        },
    )
    job, _ = store.enqueue(
        company_id=snapshot["company_id"],
        analysis_mode="FULL_ENTRY_REVIEW",
        trigger_type="FULL_ENTRY_REVIEW",
        trigger_payload={"summary": "bootstrap"},
        input_snapshot_hash=snapshot_hash(snapshot),
        calculation_version=CALCULATION_VERSION,
        prompt_version=PROMPT_VERSION,
    )
    InputSnapshotBuilder(jobs_root).prepare(
        job,
        company_snapshot=snapshot,
        trigger={"type": "FULL_ENTRY_REVIEW", "summary": "bootstrap"},
    )
    worker = CodexWorker(
        store,
        WorkerConfig(PROJECT, jobs_root, tmp_path / "output", SCHEMA, FAKE_CODEX),
    )
    result = worker.run_once()
    assert result and result.status == "SUCCEEDED"


def test_successful_output_survives_later_failure(tmp_path: Path) -> None:
    store, worker, first_job, jobs_root, output_root = prepare_job(tmp_path)
    assert worker.run_once().status == "SUCCEEDED"
    latest_before = (output_root / first_job.company_id / "latest.json").read_bytes()
    snapshot = company_snapshot(ssy="0.05")
    second, _ = store.enqueue(
        company_id=snapshot["company_id"],
        analysis_mode="MATERIAL_CHANGE_REVIEW",
        trigger_type="MATERIAL_METRIC_CHANGE",
        trigger_payload={"summary": "changed"},
        input_snapshot_hash=snapshot_hash(snapshot),
        calculation_version=CALCULATION_VERSION,
        prompt_version=PROMPT_VERSION,
    )
    InputSnapshotBuilder(jobs_root).prepare(
        second,
        company_snapshot=snapshot,
        trigger={"type": "MATERIAL_METRIC_CHANGE", "summary": "changed"},
    )
    failing = CodexWorker(
        store,
        WorkerConfig(
            PROJECT,
            jobs_root,
            output_root,
            SCHEMA,
            FAKE_CODEX,
            extra_environment={"FAKE_CODEX_SCENARIO": "nonzero"},
        ),
    )
    assert failing.run_once().status == "WAITING_RETRY"
    assert (output_root / first_job.company_id / "latest.json").read_bytes() == latest_before


def test_public_status_keeps_last_success_and_hides_auth_detail(tmp_path: Path) -> None:
    store, worker, first_job, jobs_root, output_root = prepare_job(tmp_path)
    assert worker.run_once().status == "SUCCEEDED"
    latest = AnalysisStorage(output_root, jobs_root).latest_public_payload(first_job.company_id)
    snapshot = company_snapshot(ssy="0.05")
    second, _ = store.enqueue(
        company_id=snapshot["company_id"],
        analysis_mode="MATERIAL_CHANGE_REVIEW",
        trigger_type="MATERIAL_METRIC_CHANGE",
        trigger_payload={"summary": "changed"},
        input_snapshot_hash=snapshot_hash(snapshot),
        calculation_version=CALCULATION_VERSION,
        prompt_version=PROMPT_VERSION,
    )
    store.mark_error(
        second.job_id,
        status="WAITING_AUTH",
        error_code="AUTH_FAILED",
        error_message="private authentication detail",
        next_retry_at=NOW + timedelta(minutes=5),
    )
    statuses = build_public_analysis_statuses(store, {first_job.company_id: latest})
    public = statuses[first_job.company_id]
    assert public["status"] == "WAITING_RETRY"
    assert public["latest_analysis_id"] == first_job.job_id
    assert "error_code" not in public and "error_message" not in public
