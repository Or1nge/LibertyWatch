from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..constants import MODEL, REASONING_EFFORT


SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
SNAPSHOT_HASH = re.compile(r"^[a-f0-9]{64}$")
ANALYSIS_MODES = {
    "FULL_ENTRY_REVIEW",
    "URGENT_VETO_REVIEW",
    "MATERIAL_CHANGE_REVIEW",
    "PERIODIC_REFRESH",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


@dataclass(frozen=True)
class AnalysisJob:
    job_id: str
    company_id: str
    analysis_mode: str
    trigger_type: str
    trigger_payload: dict[str, Any]
    input_snapshot_hash: str
    calculation_version: str
    prompt_version: str
    model: str
    reasoning_effort: str
    status: str
    attempt_count: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    next_retry_at: str | None
    result_path: str | None
    error_code: str | None
    error_message: str | None


class AnalysisJobStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS analysis_jobs (
                    job_id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    analysis_mode TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    trigger_payload TEXT NOT NULL,
                    input_snapshot_hash TEXT NOT NULL,
                    calculation_version TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    model TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    next_retry_at TEXT,
                    result_path TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    UNIQUE(company_id, analysis_mode, input_snapshot_hash, prompt_version, model)
                );
                CREATE INDEX IF NOT EXISTS analysis_jobs_status_retry
                    ON analysis_jobs(status, next_retry_at, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS analysis_one_running_per_company
                    ON analysis_jobs(company_id) WHERE status='RUNNING';
                CREATE TABLE IF NOT EXISTS analysis_observation_state (
                    company_id TEXT PRIMARY KEY,
                    in_observation_zone INTEGER NOT NULL DEFAULT 0,
                    below_exit_days INTEGER NOT NULL DEFAULT 0,
                    last_below_trade_date TEXT,
                    last_price_trigger_at TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def recover_running_jobs(self) -> int:
        """Requeue jobs left RUNNING by the previous single worker process.

        Recovery is explicit because API, dispatcher and publisher processes also
        open this database; a read-only process must never interrupt a live worker.
        """
        now_text = iso(utc_now())
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_jobs
                SET status='WAITING_RETRY', next_retry_at=?, finished_at=?,
                    error_code='WORKER_RESTARTED',
                    error_message='worker restarted while the task was RUNNING'
                WHERE status='RUNNING'
                """,
                (now_text, now_text),
            )
        return int(cursor.rowcount)

    @staticmethod
    def _job(row: sqlite3.Row) -> AnalysisJob:
        value = dict(row)
        value["trigger_payload"] = json.loads(value["trigger_payload"])
        return AnalysisJob(**value)

    def enqueue(
        self,
        *,
        company_id: str,
        analysis_mode: str,
        trigger_type: str,
        trigger_payload: Mapping[str, Any],
        input_snapshot_hash: str,
        calculation_version: str,
        prompt_version: str,
        model: str = MODEL,
        reasoning_effort: str = REASONING_EFFORT,
        job_id: str | None = None,
    ) -> tuple[AnalysisJob, bool]:
        if model != MODEL or reasoning_effort != REASONING_EFFORT:
            raise ValueError("analysis jobs may not silently downgrade model or reasoning")
        if not SAFE_IDENTIFIER.fullmatch(company_id):
            raise ValueError("unsafe company_id")
        if analysis_mode not in ANALYSIS_MODES or not SNAPSHOT_HASH.fullmatch(input_snapshot_hash):
            raise ValueError("invalid analysis mode or input snapshot hash")
        if not calculation_version or not prompt_version:
            raise ValueError("calculation and prompt versions are required")
        identifier = job_id or uuid.uuid4().hex
        if not SAFE_IDENTIFIER.fullmatch(identifier):
            raise ValueError("unsafe job_id")
        created = iso(utc_now())
        payload = json.dumps(trigger_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO analysis_jobs(
                        job_id, company_id, analysis_mode, trigger_type, trigger_payload,
                        input_snapshot_hash, calculation_version, prompt_version, model,
                        reasoning_effort, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                    """,
                    (
                        identifier,
                        company_id,
                        analysis_mode,
                        trigger_type,
                        payload,
                        input_snapshot_hash,
                        calculation_version,
                        prompt_version,
                        model,
                        reasoning_effort,
                        created,
                    ),
                )
                created_new = True
                connection.execute(
                    """
                    UPDATE analysis_jobs
                    SET status='SUPERSEDED', finished_at=?, next_retry_at=NULL,
                        error_code='SUPERSEDED_BY_NEWER_SNAPSHOT',
                        error_message='a newer immutable snapshot replaced this queued retry'
                    WHERE company_id=? AND analysis_mode=? AND job_id<>?
                      AND status IN ('PENDING','WAITING_RETRY','WAITING_MODEL','WAITING_AUTH')
                    """,
                    (created, company_id, analysis_mode, identifier),
                )
            except sqlite3.IntegrityError:
                created_new = False
            row = connection.execute(
                """
                SELECT * FROM analysis_jobs
                WHERE company_id=? AND analysis_mode=? AND input_snapshot_hash=?
                    AND prompt_version=? AND model=?
                """,
                (company_id, analysis_mode, input_snapshot_hash, prompt_version, model),
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._job(row), created_new

    def get(self, job_id: str) -> AnalysisJob | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def claim_next(self, *, global_concurrency: int = 1, now: datetime | None = None) -> AnalysisJob | None:
        current = iso(now or utc_now())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            running = int(connection.execute("SELECT COUNT(*) FROM analysis_jobs WHERE status='RUNNING'").fetchone()[0])
            if running >= max(1, global_concurrency):
                connection.commit()
                return None
            row = connection.execute(
                """
                SELECT j.* FROM analysis_jobs j
                WHERE (
                    j.status='PENDING'
                    OR (j.status='WAITING_RETRY' AND j.next_retry_at IS NOT NULL AND j.next_retry_at<=?)
                    OR (j.status='WAITING_MODEL' AND j.next_retry_at<=?)
                    OR (j.status='WAITING_AUTH' AND j.next_retry_at<=?)
                )
                AND NOT EXISTS (
                    SELECT 1 FROM analysis_jobs r
                    WHERE r.company_id=j.company_id AND r.status='RUNNING'
                )
                ORDER BY
                    CASE j.analysis_mode
                        WHEN 'URGENT_VETO_REVIEW' THEN 0
                        WHEN 'FULL_ENTRY_REVIEW' THEN 1
                        WHEN 'MATERIAL_CHANGE_REVIEW' THEN 2
                        ELSE 3
                    END,
                    j.created_at
                LIMIT 1
                """,
                (current, current, current),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            started = iso(now or utc_now())
            connection.execute(
                """
                UPDATE analysis_jobs
                SET status='RUNNING', started_at=?, finished_at=NULL,
                    attempt_count=attempt_count+1, error_code=NULL, error_message=NULL
                WHERE job_id=?
                """,
                (started, row["job_id"]),
            )
            updated = connection.execute("SELECT * FROM analysis_jobs WHERE job_id=?", (row["job_id"],)).fetchone()
            connection.commit()
        assert updated is not None
        return self._job(updated)

    def mark_succeeded(self, job_id: str, result_path: Path) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE analysis_jobs SET status='SUCCEEDED', finished_at=?, result_path=?,
                    next_retry_at=NULL, error_code=NULL, error_message=NULL WHERE job_id=?
                """,
                (iso(utc_now()), str(result_path), job_id),
            )

    def mark_error(
        self,
        job_id: str,
        *,
        status: str,
        error_code: str,
        error_message: str,
        next_retry_at: datetime | None = None,
    ) -> None:
        allowed = {"WAITING_RETRY", "WAITING_MODEL", "WAITING_AUTH", "FAILED", "INVALID_INPUT"}
        if status not in allowed:
            raise ValueError(f"unsupported error status: {status}")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE analysis_jobs SET status=?, finished_at=?, next_retry_at=?,
                    error_code=?, error_message=? WHERE job_id=?
                """,
                (
                    status,
                    iso(utc_now()),
                    iso(next_retry_at),
                    error_code,
                    error_message[:2000],
                    job_id,
                ),
            )

    def latest_success(self, company_id: str) -> AnalysisJob | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM analysis_jobs WHERE company_id=? AND status='SUCCEEDED'
                ORDER BY finished_at DESC LIMIT 1
                """,
                (company_id,),
            ).fetchone()
        return self._job(row) if row else None

    def latest_jobs(self) -> list[AnalysisJob]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM analysis_jobs
                ORDER BY company_id, created_at DESC, job_id DESC
                """
            ).fetchall()
        result: list[AnalysisJob] = []
        seen: set[str] = set()
        for row in rows:
            company_id = str(row["company_id"])
            if company_id in seen:
                continue
            seen.add(company_id)
            result.append(self._job(row))
        return result

    def has_running_mode(self, company_id: str, analysis_mode: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM analysis_jobs WHERE company_id=? AND analysis_mode=?
                    AND status='RUNNING'
                LIMIT 1
                """,
                (company_id, analysis_mode),
            ).fetchone()
        return row is not None

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS count FROM analysis_jobs GROUP BY status").fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        return {
            "queued": counts.get("PENDING", 0),
            "running": counts.get("RUNNING", 0),
            "failed": counts.get("FAILED", 0),
            "waiting_retry": sum(counts.get(name, 0) for name in ("WAITING_RETRY", "WAITING_MODEL", "WAITING_AUTH")),
            "succeeded": counts.get("SUCCEEDED", 0),
            "superseded": counts.get("SUPERSEDED", 0),
        }

    def defer_available(
        self,
        *,
        status: str,
        error_code: str,
        error_message: str,
        next_retry_at: datetime,
    ) -> int:
        if status not in {"WAITING_MODEL", "WAITING_AUTH", "WAITING_RETRY"}:
            raise ValueError("only retryable waiting statuses may defer queued jobs")
        now_text = iso(utc_now())
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE analysis_jobs
                SET status=?, error_code=?, error_message=?, next_retry_at=?, finished_at=?
                WHERE status='PENDING'
                   OR (status IN ('WAITING_RETRY','WAITING_MODEL','WAITING_AUTH')
                       AND next_retry_at IS NOT NULL AND next_retry_at<=?)
                """,
                (
                    status,
                    error_code,
                    error_message[:2000],
                    iso(next_retry_at),
                    now_text,
                    now_text,
                ),
            )
        return int(cursor.rowcount)

    def resume_paused(self, *, external_only: bool = False) -> int:
        statuses = ("WAITING_MODEL", "WAITING_AUTH") if external_only else (
            "WAITING_RETRY",
            "WAITING_MODEL",
            "WAITING_AUTH",
        )
        placeholders = ",".join("?" for _ in statuses)
        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE analysis_jobs
                SET status='PENDING', next_retry_at=NULL, finished_at=NULL,
                    error_code=NULL, error_message=NULL
                WHERE status IN ({placeholders}) AND next_retry_at IS NULL
                """,
                statuses,
            )
        return int(cursor.rowcount)

    def observation_state(self, company_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_observation_state WHERE company_id=?", (company_id,)
            ).fetchone()
        if row is None:
            return {
                "company_id": company_id,
                "in_observation_zone": False,
                "below_exit_days": 0,
                "last_below_trade_date": None,
                "last_price_trigger_at": None,
            }
        result = dict(row)
        result["in_observation_zone"] = bool(result["in_observation_zone"])
        return result

    def save_observation_state(self, company_id: str, state: Mapping[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO analysis_observation_state(
                    company_id, in_observation_zone, below_exit_days,
                    last_below_trade_date, last_price_trigger_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_id) DO UPDATE SET
                    in_observation_zone=excluded.in_observation_zone,
                    below_exit_days=excluded.below_exit_days,
                    last_below_trade_date=excluded.last_below_trade_date,
                    last_price_trigger_at=excluded.last_price_trigger_at,
                    updated_at=excluded.updated_at
                """,
                (
                    company_id,
                    int(bool(state.get("in_observation_zone"))),
                    int(state.get("below_exit_days") or 0),
                    state.get("last_below_trade_date"),
                    state.get("last_price_trigger_at"),
                    iso(utc_now()),
                ),
            )
