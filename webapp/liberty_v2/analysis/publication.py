"""Build the non-sensitive analysis status contract published to FastAPI."""

from __future__ import annotations

from typing import Any, Mapping

from ..constants import MODEL, REASONING_EFFORT
from .job_store import AnalysisJob, AnalysisJobStore


def _public_status(job: AnalysisJob) -> str:
    # Public readers only need to know that the service is waiting. Authentication
    # and model-catalog details remain on the Linux data server.
    if job.status in {"WAITING_AUTH", "WAITING_MODEL"}:
        return "WAITING_RETRY"
    return job.status


def build_public_analysis_statuses(
    store: AnalysisJobStore,
    analyses: Mapping[str, tuple[Mapping[str, Any], str]],
) -> dict[str, dict[str, Any]]:
    """Return one safe current status per company, preserving last success data."""
    latest_jobs = {job.company_id: job for job in store.latest_jobs()}
    company_ids = sorted(set(latest_jobs) | set(analyses))
    result: dict[str, dict[str, Any]] = {}
    for company_id in company_ids:
        job = latest_jobs.get(company_id)
        report = analyses.get(company_id)
        report_payload = report[0] if report else None
        report_id = str(report_payload.get("analysis_id") or "") if report_payload else None
        latest_success = store.latest_success(company_id)

        if job is None:
            status = "SUCCEEDED" if report_payload else "NOT_REQUESTED"
        else:
            status = _public_status(job)
            # A database success is not public until its immutable output has been
            # verified and included in the release. Keep the previous report live.
            if status == "SUCCEEDED" and report_id != job.job_id:
                status = "WAITING_RETRY"

        value: dict[str, Any] = {
            "status": status,
            "job_id": job.job_id if job else None,
            "analysis_mode": job.analysis_mode if job else None,
            "trigger_type": job.trigger_type if job else None,
            "created_at": job.created_at if job else None,
            "started_at": job.started_at if job else None,
            "finished_at": job.finished_at if job else None,
            "next_retry_at": job.next_retry_at if job else None,
            "attempt_count": job.attempt_count if job else 0,
            "prompt_version": job.prompt_version if job else None,
            "model": job.model if job else MODEL,
            "reasoning_effort": job.reasoning_effort if job else REASONING_EFFORT,
            "latest_success_at": (
                latest_success.finished_at
                if latest_success and latest_success.job_id == report_id
                else None
            ),
            "latest_analysis_id": report_id,
            "latest_analysis_as_of_date": (
                report_payload.get("as_of_date") if report_payload else None
            ),
        }
        result[company_id] = value
    return result

