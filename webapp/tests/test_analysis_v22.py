from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from liberty_v2.analysis.job_store import AnalysisJobStore
from liberty_v2.analysis.output_validator import AnalysisOutputValidator, OutputValidationError
from liberty_v2.analysis.prompt_renderer import InputSnapshotBuilder, snapshot_hash
from liberty_v2.analysis.storage import AnalysisStorage
from liberty_v2.analysis.worker import CodexWorker, WorkerConfig
from liberty_v2.constants import CALCULATION_VERSION, PROMPT_VERSION


PROJECT = Path(__file__).resolve().parents[1]
FAKE_CODEX = PROJECT / "scripts" / "support" / "fake_codex.py"
SCHEMA_V2 = PROJECT / "analysis" / "schema" / "risk_analysis_output_v2.json"
WORKER_UNIT = PROJECT / "systemd" / "shareholder-codex-worker.service"


def screening_company() -> dict:
    component = {"value": "75", "basis": "TEST", "source_summary": {"source": "test"}, "warnings": []}
    score = {"value": "75", "coverage": "1.0000", "status": "READY", "basis": "DETERMINISTIC_COVERAGE_SHRINKAGE", "warnings": [], "components": {"test": component}}
    return {
        "schema_version": "shareholder-screen-v2", "calculation_version": CALCULATION_VERSION,
        "metric_definition_version": CALCULATION_VERSION, "company_id": "issuer-v22", "company_name": "V22测试公司",
        "securities": [{"security_id": "security-v22", "ticker": "600001", "market": "CN"}],
        "as_of_date": "2026-08-04", "price_timestamp": "2026-08-04T07:00:00+00:00", "status": "READY",
        "price": {"value": "10", "basis": "VENDOR"}, "market_metrics": {},
        "opportunity_score": score, "financial_resilience_score": {**score, "profile": "NON_FINANCIAL"},
        "research_trigger": {"eligible": True, "trigger_type": "OPPORTUNITY_SCORE_HIGH", "reason": "达到阈值", "in_observation_zone": True, "event_codes": []},
        "warnings": [], "source_summary": {"documents": []}, "financial_history": [], "research_inputs": {},
        "analysis_status": {"status": "NOT_REQUESTED"},
    }


def build_worker(tmp_path: Path, scenario: str = "valid") -> tuple[AnalysisJobStore, CodexWorker, str]:
    store = AnalysisJobStore(tmp_path / "jobs.sqlite3")
    company = screening_company()
    digest = snapshot_hash(company)
    job, _ = store.enqueue(company_id=company["company_id"], analysis_mode="PRICE_RISK_ANALYSIS", trigger_type="OPPORTUNITY_SCORE_HIGH", trigger_payload={"summary": "test"}, input_snapshot_hash=digest, calculation_version=CALCULATION_VERSION, prompt_version=PROMPT_VERSION)
    InputSnapshotBuilder(tmp_path / "jobs").prepare(job, company_snapshot=company, trigger={"type": "OPPORTUNITY_SCORE_HIGH", "summary": "test"})
    worker = CodexWorker(store, WorkerConfig(project_root=PROJECT, jobs_root=tmp_path / "jobs", output_root=tmp_path / "output", schema_path=SCHEMA_V2, codex_binary=FAKE_CODEX, timeout_seconds=2, extra_environment={"FAKE_CODEX_SCENARIO": scenario}))
    return store, worker, job.job_id


def test_worker_command_supports_immutable_non_git_release(tmp_path: Path) -> None:
    _, worker, _ = build_worker(tmp_path)
    command = worker.command(tmp_path / "final.json")
    assert command[command.index("exec") + 1] == "--skip-git-repo-check"
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--ask-for-approval") + 1] == "never"


def test_worker_unit_can_publish_only_analysis_channel() -> None:
    unit = WORKER_UNIT.read_text(encoding="utf-8")
    read_write = next(line for line in unit.splitlines() if line.startswith("ReadWritePaths="))
    inaccessible = next(line for line in unit.splitlines() if line.startswith("InaccessiblePaths="))
    assert "/var/lib/liberty/shareholder-v2/published/analysis" in read_write.split()
    assert "-/var/lib/liberty/shareholder-v2/published/structured" in inaccessible.split()
    assert "-/var/lib/liberty/shareholder-v2/published" not in inaccessible.split()


def test_success_archive_does_not_copy_setgid_input_metadata(tmp_path: Path) -> None:
    store, worker, job_id = build_worker(tmp_path)
    input_dir = tmp_path / "jobs" / job_id / "input"
    input_dir.chmod(0o2700)
    result = worker.run_once()
    assert result and result.status == "SUCCEEDED"
    archived_input = tmp_path / "output" / "issuer-v22" / "runs" / job_id / "input"
    assert archived_input.stat().st_mode & stat.S_ISGID == 0
    assert {path.name: path.read_bytes() for path in archived_input.iterdir()} == {
        path.name: path.read_bytes() for path in input_dir.iterdir()
    }


def test_fake_codex_v2_schema_and_local_latest(tmp_path: Path) -> None:
    store, worker, job_id = build_worker(tmp_path)
    worker.startup_check()
    result = worker.run_once()
    assert result and result.status == "SUCCEEDED"
    job = store.get(job_id)
    payload, report = AnalysisStorage(tmp_path / "output", tmp_path / "jobs").latest_public_payload("issuer-v22")
    assert job and job.status == "SUCCEEDED"
    assert payload["schema_version"] == "2.0" and payload["opportunity_or_trap"] == "MIXED"
    assert payload["sources"] and report.startswith("#")
    assert not (result.result_path / "reviewed_overlay.json").exists()


def test_failed_new_v2_job_does_not_remove_last_good(tmp_path: Path) -> None:
    store, worker, _ = build_worker(tmp_path)
    assert worker.run_once().status == "SUCCEEDED"
    before = (tmp_path / "output" / "issuer-v22" / "latest.json").read_bytes()
    company = screening_company()
    company["as_of_date"] = "2026-08-05"
    digest = snapshot_hash(company)
    job, _ = store.enqueue(company_id=company["company_id"], analysis_mode="PRICE_RISK_ANALYSIS", trigger_type="OPPORTUNITY_SCORE_HIGH", trigger_payload={"summary": "retry"}, input_snapshot_hash=digest, calculation_version=CALCULATION_VERSION, prompt_version=PROMPT_VERSION)
    InputSnapshotBuilder(tmp_path / "jobs").prepare(job, company_snapshot=company, trigger={"type": "OPPORTUNITY_SCORE_HIGH", "summary": "retry"})
    failing = CodexWorker(store, WorkerConfig(project_root=PROJECT, jobs_root=tmp_path / "jobs", output_root=tmp_path / "output", schema_path=SCHEMA_V2, codex_binary=FAKE_CODEX, timeout_seconds=2, extra_environment={"FAKE_CODEX_SCENARIO": "schema_error"}))
    assert failing.run_once().status == "WAITING_RETRY"
    assert (tmp_path / "output" / "issuer-v22" / "latest.json").read_bytes() == before
    payload = json.loads((tmp_path / "output" / "issuer-v22" / "runs" / store.latest_success("issuer-v22").job_id / "final.json").read_text())
    assert payload["schema_version"] == "2.0"


def test_v2_schema_const_and_enum_nodes_have_explicit_types() -> None:
    schema = json.loads(SCHEMA_V2.read_text(encoding="utf-8"))

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "const" in node or "enum" in node:
                assert "type" in node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    assert schema["properties"]["sources"]["items"]["properties"]["url"] == {
        "type": "string",
        "pattern": "^https?://",
    }


def test_v2_validator_rejects_local_or_placeholder_sources(tmp_path: Path) -> None:
    store, worker, job_id = build_worker(tmp_path)
    assert worker.run_once().status == "SUCCEEDED"
    job = store.get(job_id)
    payload = json.loads(
        (tmp_path / "output" / "issuer-v22" / "runs" / job_id / "final.json").read_text()
    )
    payload["sources"][0]["url"] = "https://invalid.local/research_bundle.json"
    assert job is not None
    with pytest.raises(OutputValidationError, match="public evidence"):
        AnalysisOutputValidator(SCHEMA_V2).validate(payload, job, screening_company())
