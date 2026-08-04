from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..constants import CALCULATION_VERSION, PROMPT_VERSION
from ..snapshot_store import atomic_write_json
from .job_store import AnalysisJobStore, SAFE_IDENTIFIER
from .prompt_renderer import InputSnapshotBuilder, snapshot_hash
from .storage import AnalysisStorage
from .triggers import evaluate_trigger


class AnalysisDispatcher:
    def __init__(
        self,
        *,
        store: AnalysisJobStore,
        jobs_root: Path,
        observation_root: Path,
        analysis_storage: AnalysisStorage,
    ) -> None:
        self.store = store
        self.snapshot_builder = InputSnapshotBuilder(jobs_root)
        self.observation_root = observation_root
        self.analysis_storage = analysis_storage

    def _previous_snapshot(self, company_id: str) -> dict[str, Any] | None:
        path = self.observation_root / f"{company_id}.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None

    def dispatch_company(
        self,
        company: Mapping[str, Any],
        *,
        events: Sequence[str] = (),
        prompt_major_upgrade: bool = False,
        prior_baseline_invalid: bool = False,
        initial_backlog: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        company_id = str(company.get("company_id") or "")
        if not SAFE_IDENTIFIER.fullmatch(company_id):
            raise ValueError("company_id is missing or unsafe")
        current_time = now or datetime.now(timezone.utc)
        previous = self._previous_snapshot(company_id)
        latest_success = self.store.latest_success(company_id)
        try:
            previous_analysis_entry = self.analysis_storage.latest_public_payload(company_id)
        except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError):
            # A damaged previous-output pointer is itself a reason to rebuild the
            # baseline, but must not prevent other companies from dispatching.
            previous_analysis_entry = None
            prior_baseline_invalid = True
        previous_analysis = previous_analysis_entry[0] if previous_analysis_entry else None
        state = self.store.observation_state(company_id)
        decision = evaluate_trigger(
            company,
            previous,
            state=state,
            events=events,
            has_legal_report=latest_success is not None,
            last_success_at=(
                datetime.fromisoformat(latest_success.finished_at)
                if latest_success and latest_success.finished_at
                else None
            ),
            last_prompt_version=latest_success.prompt_version if latest_success else None,
            current_prompt_version=PROMPT_VERSION,
            prompt_major_upgrade=prompt_major_upgrade,
            prior_baseline_invalid=prior_baseline_invalid,
            trade_date=current_time.date(),
            now=current_time,
            initial_backlog=initial_backlog,
        )
        self.store.save_observation_state(company_id, decision.state)
        atomic_write_json(self.observation_root / f"{company_id}.json", dict(company))
        if not decision.should_trigger:
            return {"company_id": company_id, "created": False, "reason": decision.summary}
        assert decision.analysis_mode and decision.trigger_type
        if self.store.has_running_mode(company_id, decision.analysis_mode):
            return {"company_id": company_id, "created": False, "reason": "同类任务正在运行。"}
        digest = snapshot_hash(company)
        trigger_payload = {
            "type": decision.trigger_type,
            "summary": decision.summary,
            "events": list(events),
            "created_at": current_time.isoformat(),
        }
        job, created = self.store.enqueue(
            company_id=company_id,
            analysis_mode=decision.analysis_mode,
            trigger_type=decision.trigger_type,
            trigger_payload=trigger_payload,
            input_snapshot_hash=digest,
            calculation_version=str(company.get("calculation_version") or CALCULATION_VERSION),
            prompt_version=PROMPT_VERSION,
        )
        if created:
            self.snapshot_builder.prepare(
                job,
                company_snapshot=company,
                trigger=trigger_payload,
                previous_analysis=previous_analysis,
                source_index=company.get("source_summary") if isinstance(company.get("source_summary"), Mapping) else {},
            )
        return {
            "company_id": company_id,
            "created": created,
            "job_id": job.job_id,
            "analysis_mode": job.analysis_mode,
            "reason": decision.summary,
        }

    def dispatch_release(
        self,
        release_path: Path,
        *,
        events_by_company: Mapping[str, Sequence[str]] | None = None,
        initial_backlog: bool = False,
    ) -> list[dict[str, Any]]:
        index = json.loads((release_path / "companies.json").read_text(encoding="utf-8"))
        records: list[dict[str, Any]] = []
        for summary in index.get("companies", []):
            company_id = str(summary.get("company_id") or "")
            company_path = release_path / "companies" / f"{company_id}.json"
            records.append(json.loads(company_path.read_text(encoding="utf-8")))

        def number(record: Mapping[str, Any], key: str) -> float:
            try:
                return float(record.get(key, {}).get("value"))
            except (AttributeError, TypeError, ValueError):
                return float("-inf")

        def priority(record: Mapping[str, Any]) -> tuple[Any, ...]:
            company_id = str(record.get("company_id") or "")
            trigger = record.get("research_trigger") if isinstance(record.get("research_trigger"), Mapping) else {}
            events = set(map(str, (events_by_company or {}).get(company_id, ())))
            trigger_type = str(trigger.get("trigger_type") or "")
            urgent = bool(events or trigger.get("event_codes"))
            order = 0 if urgent else 1 if trigger_type == "DIVIDEND_YIELD_TTM" else 2
            return (order, -number(record, "opportunity_score"), -number(record, "financial_resilience_score"), company_id)

        results: list[dict[str, Any]] = []
        for company in sorted(records, key=priority):
            company_id = str(company.get("company_id") or "")
            results.append(
                self.dispatch_company(
                    company,
                    events=(events_by_company or {}).get(company_id, ()),
                    initial_backlog=initial_backlog,
                )
            )
        return results
