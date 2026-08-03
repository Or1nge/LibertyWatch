#!/usr/bin/env python3
"""Linux-side shareholder-return v2 pipeline and Codex service entrypoint."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBERTY_ROOT = PROJECT_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT))

SAFE_COMPANY_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
SENSITIVE_ABSOLUTE_PATH = re.compile(r"/(?:home|var|etc|opt|root)/[^\s,;]+")

from liberty_v2.analysis.dispatcher import AnalysisDispatcher  # noqa: E402
from liberty_v2.analysis.job_store import AnalysisJobStore, utc_now  # noqa: E402
from liberty_v2.analysis.publication import build_public_analysis_statuses  # noqa: E402
from liberty_v2.analysis.prompt_renderer import InputSnapshotBuilder, snapshot_hash  # noqa: E402
from liberty_v2.analysis.reviewed_overlay import ReviewedOverlayStore  # noqa: E402
from liberty_v2.analysis.storage import AnalysisStorage, AnalysisStorageError  # noqa: E402
from liberty_v2.analysis.worker import (  # noqa: E402
    CodexStartupError,
    CodexWorker,
    RunResult,
    WorkerConfig,
)
from liberty_v2.assessment import assess_company  # noqa: E402
from liberty_v2.balance_sheet_adapter import (  # noqa: E402
    load_balance_sheet_assessment,
    overlay_balance_sheet,
)
from liberty_v2.capital_structure import load_capital_structure_registry  # noqa: E402
from liberty_v2.constants import CALCULATION_VERSION, MODEL, PROMPT_VERSION, REASONING_EFFORT  # noqa: E402
from liberty_v2.market_observation import (  # noqa: E402
    load_market_observations,
    overlay_market_observation,
)
from liberty_v2.migration import migrate_v1  # noqa: E402
from liberty_v2.pipeline import compute_company_snapshot  # noqa: E402
from liberty_v2.registry import load_metric_definitions, load_policy  # noqa: E402
from liberty_v2.release import (  # noqa: E402
    AtomicReleaseBuilder,
    PUBLIC_ANALYSIS_STATUS_FIELDS,
    build_analysis_release,
    build_structured_release,
    verify_release,
)
from liberty_v2.slow_cache import load_or_compute_slow  # noqa: E402
from liberty_v2.snapshot_store import LastValidSnapshotStore, atomic_write_json  # noqa: E402
from liberty_v2.sync import AliReleaseSynchronizer, AliSyncConfig  # noqa: E402


def runtime_root() -> Path:
    return Path(
        os.getenv(
            "SHAREHOLDER_V2_LOCAL_ROOT",
            str(LIBERTY_ROOT / "data" / "shareholder-v2"),
        )
    ).resolve()


def paths() -> dict[str, Path]:
    root = runtime_root()
    return {
        "root": root,
        "staging": Path(os.getenv("SHAREHOLDER_V2_STAGING_DIR", root / "staging")),
        "slow_cache": root / "cache" / "slow",
        "valid": root / "snapshots" / "last-valid",
        "structured": root / "published" / "structured",
        "analysis_release": root / "published" / "analysis",
        "jobs": root / "analysis" / "jobs",
        "analysis_output": root / "analysis" / "output",
        "observations": root / "analysis" / "observations",
        "job_db": Path(os.getenv("ANALYSIS_JOB_DB", root / "analysis" / "jobs.sqlite3")),
        "worker_lock": root / "analysis" / "worker.lock",
        "runtime_status": root / "status" / "runtime.json",
        "sync_status_structured": root / "status" / "sync-structured.json",
        "sync_status_analysis": root / "status" / "sync-analysis.json",
        "market_snapshot": Path(
            os.getenv(
                "SHAREHOLDER_V2_QUOTE_SNAPSHOT",
                PROJECT_ROOT / "runtime" / "latest_snapshot.json",
            )
        ),
        "capital_structure": Path(
            os.getenv(
                "SHAREHOLDER_V2_CAPITAL_STRUCTURE",
                PROJECT_ROOT / "config" / "issuer_capital_structure_v1.json",
            )
        ),
        "financial_evidence": Path(
            os.getenv(
                "SHAREHOLDER_V2_FUTU_FINANCIAL_EVIDENCE",
                LIBERTY_ROOT / "data" / "shareholder-v2" / "source-evidence" / "futu-financials",
            )
        ),
    }


def dump(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def current_release(channel_root: Path) -> Path:
    current = channel_root / "current"
    if not current.exists():
        raise RuntimeError(f"no active release for {channel_root.name}")
    release = current.resolve()
    verify_release(release)
    return release


def job_store() -> AnalysisJobStore:
    return AnalysisJobStore(paths()["job_db"])


def worker_config(*, codex_binary: Path | None = None, timeout: int | None = None) -> WorkerConfig:
    policy = load_policy()["codex"]
    selected_binary = codex_binary or Path(os.getenv("CODEX_BINARY", "codex"))
    return WorkerConfig(
        project_root=PROJECT_ROOT,
        jobs_root=paths()["jobs"],
        output_root=paths()["analysis_output"],
        schema_path=PROJECT_ROOT / "analysis" / "schema" / "risk_analysis_output_v1.json",
        codex_binary=selected_binary,
        timeout_seconds=timeout or int(os.getenv("CODEX_TIMEOUT_SECONDS", policy["timeout_seconds"])),
        global_concurrency=int(os.getenv("CODEX_GLOBAL_CONCURRENCY", policy["global_concurrency"])),
        max_automatic_retries=int(os.getenv("CODEX_MAX_AUTOMATIC_RETRIES", policy["max_automatic_retries"])),
        schema_retries=int(os.getenv("CODEX_SCHEMA_RETRIES", policy["schema_retries"])),
    )


def runtime_status(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    local = paths()
    store = job_store()
    structured = None
    analysis = None
    try:
        structured = current_release(local["structured"]).name
    except (RuntimeError, OSError):
        pass
    try:
        analysis = current_release(local["analysis_release"]).name
    except (RuntimeError, OSError):
        pass
    sync_status: dict[str, Any] = {}
    for channel in ("structured", "analysis"):
        status_path = local[f"sync_status_{channel}"]
        if status_path.is_file():
            item = json.loads(status_path.read_text(encoding="utf-8"))
            item.pop("error", None)
            sync_status[channel] = item
    value = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "current_calculation_version": CALCULATION_VERSION,
        "current_prompt_version": PROMPT_VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "structured_release": structured,
        "analysis_release": analysis,
        "jobs": store.counts(),
        "last_sync": sync_status or None,
    }
    value.update(extra or {})
    atomic_write_json(local["runtime_status"], value)
    return value


def command_migrate(args: argparse.Namespace) -> int:
    result = migrate_v1(
        Path(args.companies),
        Path(args.watchlist),
        paths()["staging"],
        apply=args.apply,
        backup_root=Path(args.backup_root) if args.backup_root else None,
    )
    dump(result)
    return 0


def _staging_inputs() -> list[Path]:
    company_root = paths()["staging"] / "companies"
    return sorted(company_root.glob("*.json")) if company_root.is_dir() else []


def _fast_context(now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    local = paths()
    observations = (
        load_market_observations(local["market_snapshot"], now=now)
        if local["market_snapshot"].is_file()
        else {}
    )
    registry = load_capital_structure_registry(local["capital_structure"])
    return observations, registry


def _prepare_company_input(
    raw: Mapping[str, Any],
    *,
    now: datetime,
    observations: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> tuple[dict[str, Any], Any]:
    company_id = str(raw.get("company_id") or "")
    authorization = registry.get(company_id)
    if authorization is None:
        raise ValueError(f"capital-structure authorization missing: {company_id}")
    observation = observations.get(company_id)
    prepared = overlay_market_observation(raw, observation)
    balance = load_balance_sheet_assessment(paths()["financial_evidence"], company_id)
    prepared = overlay_balance_sheet(
        prepared,
        balance,
        evidence_root=paths()["financial_evidence"],
    )
    assessment = assess_company(
        prepared,
        authorization=authorization,
        market_observation=observation,
        balance_sheet=balance,
        now=now,
    )
    prepared["selected_security_equivalent_value"] = assessment.market_value.public_dict()
    prepared["selected_input_plan"] = assessment.input_plan.public_dict()
    prepared["company_assessment"] = assessment.public_dict()
    return prepared, assessment


def command_compute(args: argparse.Namespace) -> int:
    inputs = _staging_inputs()
    if not inputs:
        raise RuntimeError("no staged company records; run migrate --apply or provide backfilled inputs")
    now = datetime.now(timezone.utc)
    valid_store = LastValidSnapshotStore(paths()["valid"])
    companies: list[dict[str, Any]] = []
    company_ids: set[str] = set()
    cache_hits = 0
    failures: list[dict[str, str]] = []
    reviewed_overlays = ReviewedOverlayStore(paths()["analysis_output"])
    observations, registry = _fast_context(now)
    for position, source in enumerate(inputs):
        raw: dict[str, Any] | None = None
        fallback_id = f"invalid-input-{position:03d}"
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
            company_id = str(raw.get("company_id") or "")
            if not SAFE_COMPANY_ID.fullmatch(company_id):
                raise ValueError("company_id is missing or unsafe")
            if company_id in company_ids:
                raise ValueError("duplicate company_id in staging inputs")
            raw = reviewed_overlays.apply_to_raw(
                raw,
                company_id=company_id,
                on_date=now.date(),
            )
            raw, assessment = _prepare_company_input(
                raw,
                now=now,
                observations=observations,
                registry=registry,
            )
            slow, cache_hit = load_or_compute_slow(
                raw,
                paths()["slow_cache"] / f"{company_id}.json",
                on_date=now.date(),
                force=args.force_slow,
            )
            cache_hits += int(cache_hit)
            candidate = compute_company_snapshot(raw, now=now, slow_variables=slow)
            candidate["readiness_assessment"] = assessment.public_dict()
            companies.append(valid_store.select_publishable(candidate))
        except Exception as error:
            raw_id = str((raw or {}).get("company_id") or "")
            company_id = (
                raw_id
                if SAFE_COMPANY_ID.fullmatch(raw_id) and raw_id not in company_ids
                else fallback_id
            )
            public_error = SENSITIVE_ABSOLUTE_PATH.sub(
                "[internal path]",
                f"{type(error).__name__}: {error}",
            )[:1000]
            candidate = {
                "schema_version": "shareholder-return-v2",
                "calculation_version": CALCULATION_VERSION,
                "company_id": company_id,
                "company_name": str((raw or {}).get("company_name") or company_id),
                "data_status": "INVALID",
                "update_status": "BLOCKED",
                "validation_errors": [public_error],
                "calculated_at": now.isoformat(),
            }
            companies.append(valid_store.select_publishable(candidate))
            failures.append({"company_id": company_id, "error": str(error)[:500]})
        company_ids.add(company_id)
    status_counts: dict[str, int] = {}
    for company in companies:
        status = str(company.get("data_status") or "INVALID")
        status_counts[status] = status_counts.get(status, 0) + 1
    pipeline = runtime_status(
        {
            "last_structured_calculation_at": now.isoformat(),
            "company_status_counts": status_counts,
            "slow_cache_hits": cache_hits,
            "slow_cache_misses": len(inputs) - cache_hits,
            "failed_company_count": len(failures),
        }
    )
    release = build_structured_release(
        paths()["structured"],
        companies=companies,
        metric_definitions=load_metric_definitions(),
        pipeline_status=pipeline,
    )
    dump(
        {
            "release": release.name,
            "company_count": len(companies),
            "status_counts": status_counts,
            "slow_cache_hits": cache_hits,
            "company_failures": failures,
        }
    )
    return 0


def command_refresh_prices(args: argparse.Namespace) -> int:
    """Validate the fast market overlay without rewriting slow staging."""

    snapshot_path = Path(args.snapshot)
    now = datetime.now(timezone.utc)
    observations = load_market_observations(snapshot_path, now=now)
    staged_ids = {
        str(json.loads(path.read_text(encoding="utf-8")).get("company_id") or "")
        for path in _staging_inputs()
    }
    freshness_counts: dict[str, int] = {}
    for observation in observations.values():
        key = observation.freshness.value
        freshness_counts[key] = freshness_counts.get(key, 0) + 1
    dump(
        {
            "mode": "NON_MUTATING_MARKET_OVERLAY",
            "snapshot": snapshot_path.name,
            "observation_count": len(observations),
            "matched_staging_companies": len(staged_ids & set(observations)),
            "unmatched_staging_companies": sorted(staged_ids - set(observations)),
            "freshness_counts": freshness_counts,
        }
    )
    return 0


def command_readiness(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    observations, registry = _fast_context(now)
    rows: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []
    for source in _staging_inputs():
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
            prepared, assessment = _prepare_company_input(
                raw,
                now=now,
                observations=observations,
                registry=registry,
            )
            rows.append(
                {
                    "company_id": assessment.company_id,
                    "company_name": str(prepared.get("company_name") or ""),
                    **assessment.public_dict(),
                }
            )
        except Exception as error:
            parse_errors.append(
                {
                    "source": source.name,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    tier_counts: dict[str, int] = {}
    blocker_counts: dict[str, int] = {}
    for row in rows:
        tier = str(row["data_tier"])
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        for blocker in row["blockers"]:
            blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
    report = {
        "report_version": "shareholder-return-v2.1-unified-assessment-v1",
        "generated_at": now.isoformat(),
        "staging_company_count": len(_staging_inputs()),
        "capital_structure_company_count": len(registry),
        "market_observation_count": len(observations),
        "assessed_company_count": len(rows),
        "parse_error_count": len(parse_errors),
        "tier_counts": dict(sorted(tier_counts.items())),
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "companies": rows,
        "parse_errors": parse_errors,
    }
    if args.output:
        atomic_write_json(Path(args.output), report)
    dump(report if not args.compact else {key: report[key] for key in (
        "report_version",
        "generated_at",
        "staging_company_count",
        "capital_structure_company_count",
        "market_observation_count",
        "assessed_company_count",
        "parse_error_count",
        "tier_counts",
        "blocker_counts",
    )})
    return 2 if parse_errors else 0


def command_dispatch(args: argparse.Namespace) -> int:
    local = paths()
    events: dict[str, list[str]] = {}
    if args.events:
        loaded = json.loads(Path(args.events).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("events file must be an object keyed by company_id")
        events = {str(key): list(value) for key, value in loaded.items()}
    store = job_store()
    storage = AnalysisStorage(local["analysis_output"], local["jobs"])
    dispatcher = AnalysisDispatcher(
        store=store,
        jobs_root=local["jobs"],
        observation_root=local["observations"],
        analysis_storage=storage,
    )
    results = dispatcher.dispatch_release(
        current_release(local["structured"]),
        events_by_company=events,
    )
    status = runtime_status({"last_dispatch_at": datetime.now(timezone.utc).isoformat()})
    dump({"created": sum(bool(item.get("created")) for item in results), "results": results, "jobs": status["jobs"]})
    return 0


def _defer_for_startup_failure(store: AnalysisJobStore, error: Exception) -> str:
    message = str(error)
    if "MODEL_UNAVAILABLE" in message:
        status, code = "WAITING_MODEL", "MODEL_UNAVAILABLE"
    elif "AUTH" in message:
        status, code = "WAITING_AUTH", "AUTH_REQUIRED"
    else:
        status, code = "WAITING_RETRY", "STARTUP_CHECK_FAILED"
    store.defer_available(
        status=status,
        error_code=code,
        error_message=message,
        next_retry_at=utc_now() + timedelta(minutes=5),
    )
    runtime_status({"worker_startup_status": status, "worker_error_code": code})
    return status


def _run_worker(args: argparse.Namespace, store: AnalysisJobStore) -> int:
    worker = CodexWorker(
        store,
        worker_config(
            codex_binary=Path(args.codex_binary) if args.codex_binary else None,
            timeout=args.timeout,
        ),
    )
    try:
        check = worker.startup_check()
    except CodexStartupError as error:
        status = _defer_for_startup_failure(store, error)
        print(f"worker startup deferred tasks as {status}: {error}", file=sys.stderr)
        return 75
    if args.startup_check_only:
        dump(check)
        return 0
    resumed_external = store.resume_paused(external_only=True)
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    processed = 0

    def record_result(result: RunResult) -> None:
        nonlocal processed
        processed += 1
        runtime_status(
            {
                "last_worker_run_at": datetime.now(timezone.utc).isoformat(),
                "last_worker_result": result.status,
                "last_worker_error_code": result.error_code,
            }
        )

    if args.once:
        result = worker.run_once()
        if result is not None:
            record_result(result)
    else:
        concurrency = worker.config.global_concurrency
        in_flight: dict[Any, Any] = {}
        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="codex-worker",
        ) as executor:
            while not stopping or in_flight:
                while not stopping and len(in_flight) < concurrency:
                    job = store.claim_next(global_concurrency=concurrency)
                    if job is None:
                        break
                    in_flight[executor.submit(worker.run_job, job)] = job
                if not in_flight:
                    for _ in range(max(1, args.poll_seconds * 2)):
                        if stopping:
                            break
                        time.sleep(0.5)
                    continue
                completed, _pending = wait(
                    tuple(in_flight),
                    timeout=1,
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    job = in_flight.pop(future)
                    try:
                        result = future.result()
                    except Exception as error:
                        store.mark_error(
                            job.job_id,
                            status="WAITING_RETRY",
                            error_code="WORKER_INTERNAL_ERROR",
                            error_message=str(error),
                            next_retry_at=utc_now() + timedelta(minutes=1),
                        )
                        result = RunResult(
                            "WAITING_RETRY",
                            "WORKER_INTERNAL_ERROR",
                            str(error),
                        )
                    record_result(result)
    dump(
        {
            "processed": processed,
            "resumed_external_waits": resumed_external,
            "stopped": stopping,
            "startup": check,
        }
    )
    return 0


def command_worker(args: argparse.Namespace) -> int:
    store = job_store()
    if args.startup_check_only:
        return _run_worker(args, store)

    lock_path = paths()["worker_lock"]
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as worker_lock:
        try:
            fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another Codex worker process already owns the worker lock", file=sys.stderr)
            return 75
        recovered = store.recover_running_jobs()
        if recovered:
            runtime_status({"recovered_running_jobs": recovered})
        return _run_worker(args, store)


def command_resume_waiting(args: argparse.Namespace) -> int:
    resumed = job_store().resume_paused(external_only=args.external_only)
    dump({"resumed": resumed, "external_only": args.external_only})
    return 0


def _current_public_analyses() -> dict[str, tuple[dict[str, Any], str]]:
    local = paths()
    try:
        release = current_release(local["analysis_release"])
    except RuntimeError:
        return {}
    index = json.loads((release / "analyses.json").read_text(encoding="utf-8"))
    analyses: dict[str, tuple[dict[str, Any], str]] = {}
    for item in index.get("analyses", []):
        company_id = str(item.get("company_id") or "")
        if not SAFE_COMPANY_ID.fullmatch(company_id):
            continue
        payload_path = release / "companies" / company_id / "latest.json"
        report_path = release / "companies" / company_id / "report.md"
        if payload_path.is_file() and report_path.is_file():
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                analyses[company_id] = (payload, report_path.read_text(encoding="utf-8"))
    return analyses


def _collect_local_analyses() -> tuple[dict[str, tuple[dict[str, Any], str]], list[str]]:
    local = paths()
    storage = AnalysisStorage(local["analysis_output"], local["jobs"])
    # Carry the last manifest-verified public report forward if a newer local
    # pointer is corrupt. One failed company must not remove old legal output or
    # block status updates for other companies.
    analyses = _current_public_analyses()
    failures: list[str] = []
    if local["analysis_output"].is_dir():
        for company_dir in sorted(local["analysis_output"].iterdir()):
            if not company_dir.is_dir():
                continue
            try:
                value = storage.latest_public_payload(company_dir.name)
            except (AnalysisStorageError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
                failures.append(company_dir.name if SAFE_COMPANY_ID.fullmatch(company_dir.name) else "invalid-company-id")
                print(f"analysis output skipped for {company_dir.name}: {error}", file=sys.stderr)
                continue
            if value is not None:
                analyses[company_dir.name] = value
    return analyses, failures


def _analysis_public_state(
    analyses: Mapping[str, tuple[Mapping[str, Any], str]],
    statuses: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    analysis_index = [
        {
            "company_id": company_id,
            "analysis_id": payload["analysis_id"],
            "as_of_date": payload["as_of_date"],
            "input_snapshot_hash": payload["input_snapshot_hash"],
            "verdict": payload["verdict"],
            "risk_overlay": payload["risk_overlay"],
        }
        for company_id, (payload, _report) in sorted(analyses.items())
    ]
    status_index = []
    for company_id, raw in sorted(statuses.items()):
        public = {
            key: raw.get(key)
            for key in sorted(PUBLIC_ANALYSIS_STATUS_FIELDS)
            if key in raw
        }
        public["company_id"] = company_id
        status_index.append(public)
    return {"schema_version": "1.0", "analyses": analysis_index, "statuses": status_index}


def command_publish_analysis(_args: argparse.Namespace) -> int:
    local = paths()
    analyses, failures = _collect_local_analyses()
    statuses = build_public_analysis_statuses(job_store(), analyses)
    release = build_analysis_release(
        local["analysis_release"],
        analyses=analyses,
        statuses=statuses,
    )
    runtime_status(
        {
            "last_analysis_publish_at": datetime.now(timezone.utc).isoformat(),
            "published_analysis_count": len(analyses),
            "published_analysis_status_count": len(statuses),
            "analysis_publish_validation_failures": len(failures),
        }
    )
    dump(
        {
            "release": release.name,
            "analysis_count": len(analyses),
            "status_count": len(statuses),
            "validation_failure_companies": sorted(failures),
        }
    )
    return 0


def synchronizer(channel: str) -> AliReleaseSynchronizer:
    return AliReleaseSynchronizer(
        AliSyncConfig.from_environment(),
        activation_script=PROJECT_ROOT / "scripts" / "support" / "activate_remote_release.py",
        rollback_script=PROJECT_ROOT / "scripts" / "support" / "rollback_remote_release.py",
        status_path=paths()[f"sync_status_{channel}"],
    )


def command_sync(args: argparse.Namespace) -> int:
    root = paths()["structured" if args.channel == "structured" else "analysis_release"]
    release = current_release(root)
    if args.dry_run:
        config = AliSyncConfig.from_environment()
        config.validate()
        manifest = verify_release(release)
        dump(
            {
                "status": "dry-run-ok",
                "channel": args.channel,
                "release": release.name,
                "file_count": len(manifest.get("files", [])),
                "ssh_configuration_valid": True,
                "remote_changed": False,
            }
        )
        return 0
    synchronizer(args.channel).sync(release, channel=args.channel)
    dump(runtime_status({"last_successful_sync_channel": args.channel, "last_successful_sync_release": release.name}))
    return 0


def _published_analysis_state() -> dict[str, Any] | None:
    local = paths()
    try:
        release = current_release(local["analysis_release"])
    except RuntimeError:
        return None
    payload = json.loads((release / "analyses.json").read_text(encoding="utf-8"))
    return {
        "schema_version": payload.get("schema_version"),
        "analyses": sorted(payload.get("analyses", []), key=lambda item: str(item.get("company_id"))),
        "statuses": sorted(payload.get("statuses", []), key=lambda item: str(item.get("company_id"))),
    }


def _local_analysis_state() -> dict[str, Any]:
    analyses, _failures = _collect_local_analyses()
    statuses = build_public_analysis_statuses(job_store(), analyses)
    return _analysis_public_state(analyses, statuses)


def command_publisher(_args: argparse.Namespace) -> int:
    local = paths()
    if _local_analysis_state() != _published_analysis_state():
        command_publish_analysis(argparse.Namespace())
    results: list[dict[str, str]] = []
    failures = 0
    for channel, key in (("structured", "structured"), ("analysis", "analysis_release")):
        try:
            release = current_release(local[key])
        except RuntimeError:
            continue
        status_path = local[f"sync_status_{channel}"]
        status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {}
        if status.get("status") == "SUCCEEDED" and status.get("release_id") == release.name:
            results.append({"channel": channel, "release": release.name, "status": "UNCHANGED"})
            continue
        try:
            synchronizer(channel).sync(release, channel=channel)
            results.append({"channel": channel, "release": release.name, "status": "SUCCEEDED"})
        except Exception as error:
            failures += 1
            results.append({"channel": channel, "release": release.name, "status": f"WAITING_RETRY:{error}"})
    runtime_status({"last_publisher_run_at": datetime.now(timezone.utc).isoformat()})
    dump({"channels": results})
    return 1 if failures else 0


def command_rollback(args: argparse.Namespace) -> int:
    if args.remote:
        synchronizer(args.channel).rollback(channel=args.channel, release_id=args.release_id)
        dump({"channel": args.channel, "active_release": args.release_id, "scope": "Ali"})
        return 0
    root = paths()["structured" if args.channel == "structured" else "analysis_release"]
    release = AtomicReleaseBuilder(root).activate(args.release_id)
    dump({"channel": args.channel, "active_release": release.name, "scope": "local"})
    return 0


def command_status(_args: argparse.Namespace) -> int:
    dump(runtime_status())
    return 0


def command_health(args: argparse.Namespace) -> int:
    load_metric_definitions()
    load_policy()
    local = paths()
    watchlist = json.loads((PROJECT_ROOT / "config" / "watchlist.json").read_text(encoding="utf-8"))
    expected_company_ids = {
        str(item.get("issuerId") or "")
        for item in watchlist.get("securities", [])
        if isinstance(item, Mapping)
    }
    capital_registry = load_capital_structure_registry(
        local["capital_structure"],
        expected_company_ids=expected_company_ids,
    )
    probe_keys = ("jobs", "analysis_output") if args.worker_scope else (
        "root",
        "jobs",
        "analysis_output",
    )
    for key in probe_keys:
        local[key].mkdir(parents=True, exist_ok=True)
        probe = local[key] / ".health-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    result: dict[str, Any] = {
        "status": "ok",
        "calculation_version": CALCULATION_VERSION,
        "prompt_version": PROMPT_VERSION,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "jobs": job_store().counts(),
        "capital_structure_company_count": len(capital_registry),
    }
    if args.codex:
        result["codex"] = CodexWorker(job_store(), worker_config()).startup_check()
    dump(result)
    return 0


def command_smoke(args: argparse.Namespace) -> int:
    if not args.confirm_real_codex:
        raise RuntimeError("real Codex smoke test requires --confirm-real-codex")
    company = json.loads(Path(args.company_snapshot).read_text(encoding="utf-8"))
    if company.get("data_status") != "VALID":
        raise RuntimeError("smoke-test input must be a VALID structured snapshot")
    digest = snapshot_hash(company)
    store = job_store()
    job, created = store.enqueue(
        company_id=str(company["company_id"]),
        analysis_mode="FULL_ENTRY_REVIEW",
        trigger_type="MANUAL_REAL_SMOKE_TEST",
        trigger_payload={"type": "MANUAL_REAL_SMOKE_TEST", "summary": "显式真实Codex冒烟测试"},
        input_snapshot_hash=digest,
        calculation_version=str(company["calculation_version"]),
        prompt_version=PROMPT_VERSION,
    )
    if created:
        InputSnapshotBuilder(paths()["jobs"]).prepare(
            job,
            company_snapshot=company,
            trigger={"type": "MANUAL_REAL_SMOKE_TEST", "summary": "显式真实Codex冒烟测试"},
            source_index=company.get("source_summary", {}),
        )
    worker = CodexWorker(store, worker_config())
    worker.startup_check()
    claimed = store.claim_next(global_concurrency=1)
    if claimed is None or claimed.job_id != job.job_id:
        raise RuntimeError("smoke-test job was not claimable; clear older queued work first")
    result = worker.run_job(claimed)
    dump({"job_id": job.job_id, "result": result.status, "result_path": result.result_path})
    return 0 if result.status == "SUCCEEDED" else 1


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    sub = value.add_subparsers(dest="command", required=True)
    migrate = sub.add_parser("migrate", help="stage v1 company IDs without rewriting production data")
    migrate.add_argument("--companies", default=str(LIBERTY_ROOT / "data" / "source" / "companies.json"))
    migrate.add_argument("--watchlist", default=str(PROJECT_ROOT / "config" / "watchlist.json"))
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--backup-root")
    migrate.set_defaults(function=command_migrate)
    compute = sub.add_parser("compute", help="compute and atomically publish structured v2 data")
    compute.add_argument("--force-slow", action="store_true")
    compute.set_defaults(function=command_compute)
    prices = sub.add_parser(
        "refresh-prices",
        help="validate the non-mutating latest-snapshot market overlay",
    )
    prices.add_argument(
        "--snapshot",
        default=os.getenv(
            "SHAREHOLDER_V2_QUOTE_SNAPSHOT",
            str(PROJECT_ROOT / "runtime" / "latest_snapshot.json"),
        ),
    )
    prices.set_defaults(function=command_refresh_prices)
    readiness = sub.add_parser(
        "readiness",
        help="run the same company assessment used by the production pipeline",
    )
    readiness.add_argument("--output")
    readiness.add_argument("--compact", action="store_true")
    readiness.set_defaults(function=command_readiness)
    dispatch = sub.add_parser("dispatch", help="evaluate deterministic triggers and enqueue analyses")
    dispatch.add_argument("--events", help="JSON mapping company IDs to versioned event codes")
    dispatch.set_defaults(function=command_dispatch)
    worker = sub.add_parser("worker", help="run queued Codex jobs outside FastAPI")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--startup-check-only", action="store_true")
    worker.add_argument("--poll-seconds", type=int, default=5)
    worker.add_argument("--timeout", type=int)
    worker.add_argument("--codex-binary")
    worker.set_defaults(function=command_worker)
    resume = sub.add_parser(
        "resume-waiting",
        help="explicitly resume retry tasks paused after the automatic retry budget",
    )
    resume.add_argument("--external-only", action="store_true")
    resume.set_defaults(function=command_resume_waiting)
    analysis_publish = sub.add_parser("publish-analysis", help="build public-only analysis release")
    analysis_publish.set_defaults(function=command_publish_analysis)
    sync = sub.add_parser("sync", help="push one manifest release to Ali and activate atomically")
    sync.add_argument("--channel", choices=("structured", "analysis"), required=True)
    sync.add_argument("--dry-run", action="store_true", help="validate release and SSH config without connecting")
    sync.set_defaults(function=command_sync)
    publish = sub.add_parser("publisher", help="publish changed public analyses and sync pending channels")
    publish.set_defaults(function=command_publisher)
    rollback = sub.add_parser("rollback", help="activate a previous verified local release")
    rollback.add_argument("--channel", choices=("structured", "analysis"), required=True)
    rollback.add_argument("--release-id", required=True)
    rollback.add_argument("--remote", action="store_true")
    rollback.set_defaults(function=command_rollback)
    status = sub.add_parser("status")
    status.set_defaults(function=command_status)
    health = sub.add_parser("health-check")
    health.add_argument("--codex", action="store_true")
    health.add_argument(
        "--worker-scope",
        action="store_true",
        help="probe only the Codex worker's writable analysis directories",
    )
    health.set_defaults(function=command_health)
    smoke = sub.add_parser("codex-smoke-test")
    smoke.add_argument("--company-snapshot", required=True)
    smoke.add_argument("--confirm-real-codex", action="store_true")
    smoke.set_defaults(function=command_smoke)
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.function(args))
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
