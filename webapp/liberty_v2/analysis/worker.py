from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Mapping

from ..constants import MODEL, PROMPT_VERSION, REASONING_EFFORT
from .job_store import AnalysisJob, AnalysisJobStore, utc_now
from .output_validator import AnalysisOutputValidator, OutputValidationError
from .prompt_renderer import PromptRenderer, verify_input_snapshot
from .reviewed_overlay import ReviewedOverlayError, ReviewedOverlayStore
from .storage import AnalysisStorage
from .triggers import is_analysis_eligible


@dataclass(frozen=True)
class WorkerConfig:
    project_root: Path
    jobs_root: Path
    output_root: Path
    schema_path: Path
    codex_binary: Path = Path("codex")
    timeout_seconds: int = 1800
    global_concurrency: int = 1
    max_automatic_retries: int = 2
    schema_retries: int = 1
    extra_environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 1 <= self.global_concurrency <= 8:
            raise ValueError("global Codex concurrency must be within 1..8")
        if self.timeout_seconds <= 0:
            raise ValueError("Codex timeout must be positive")
        if self.max_automatic_retries < 0 or self.schema_retries < 0:
            raise ValueError("Codex retry limits cannot be negative")


@dataclass(frozen=True)
class RunResult:
    status: str
    error_code: str | None = None
    message: str | None = None
    result_path: Path | None = None


class CodexStartupError(RuntimeError):
    pass


class CodexWorker:
    def __init__(self, store: AnalysisJobStore, config: WorkerConfig) -> None:
        self.store = store
        self.config = config
        self.validator = AnalysisOutputValidator(config.schema_path)
        self.storage = AnalysisStorage(config.output_root, config.jobs_root)
        self.reviewed_overlays = ReviewedOverlayStore(config.output_root)
        self.cli_version: str | None = None

    def _base_environment(self) -> dict[str, str]:
        allowed = {
            "PATH",
            "HOME",
            "CODEX_HOME",
            "CODEX_API_KEY",
            "OPENAI_API_KEY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "LANG",
            "LC_ALL",
            "TZ",
            "TMPDIR",
        }
        environment = {
            key: value for key, value in os.environ.items() if key in allowed
        }
        environment.setdefault("PATH", os.defpath)
        environment.update({str(key): str(value) for key, value in self.config.extra_environment.items()})
        return environment

    def startup_check(self) -> dict[str, str]:
        binary = str(self.config.codex_binary)
        environment = self._base_environment()
        try:
            version = subprocess.run(
                [binary, "--version"],
                text=True,
                capture_output=True,
                timeout=15,
                check=True,
                env=environment,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise CodexStartupError(f"CODEX_CLI_UNAVAILABLE: {error}") from error
        self.cli_version = version
        required_help = (
            (("--help",), ("--ask-for-approval", "--search")),
            (
                ("exec", "--help"),
                (
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--model",
                    "--sandbox",
                    "--json",
                    "--output-schema",
                    "--output-last-message",
                ),
            ),
        )
        for arguments, required_flags in required_help:
            try:
                help_run = subprocess.run(
                    [binary, *arguments],
                    text=True,
                    capture_output=True,
                    timeout=15,
                    check=True,
                    env=environment,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise CodexStartupError(f"CLI_VERSION_UNSUPPORTED: {error}") from error
            help_text = help_run.stdout + help_run.stderr
            missing_flags = [flag for flag in required_flags if flag not in help_text]
            if missing_flags:
                raise CodexStartupError(
                    "CLI_VERSION_UNSUPPORTED: missing " + ", ".join(missing_flags)
                )
        try:
            models_run = subprocess.run(
                [binary, "debug", "models"],
                text=True,
                capture_output=True,
                timeout=30,
                check=True,
                env=environment,
            )
            catalog = json.loads(models_run.stdout)
            models = catalog if isinstance(catalog, list) else catalog.get("models", [])
            slugs = {
                item.get("slug") or item.get("id") or item.get("model")
                for item in models
                if isinstance(item, dict)
            }
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise CodexStartupError(f"MODEL_CATALOG_UNAVAILABLE: {error}") from error
        if MODEL not in slugs:
            raise CodexStartupError(f"MODEL_UNAVAILABLE: {MODEL}")
        if "CODEX_API_KEY" not in environment:
            try:
                auth = subprocess.run(
                    [binary, "login", "status"],
                    text=True,
                    capture_output=True,
                    timeout=15,
                    env=environment,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise CodexStartupError(f"AUTH_CHECK_FAILED: {error}") from error
            if auth.returncode != 0 or "logged in" not in (auth.stdout + auth.stderr).lower():
                raise CodexStartupError("AUTH_REQUIRED: Codex CLI is not authenticated")
        self.config.output_root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(dir=self.config.output_root, prefix=".write-check-", delete=True):
                pass
        except OSError as error:
            raise CodexStartupError(f"OUTPUT_NOT_WRITABLE: {error}") from error
        if self.validator.schema.get("properties", {}).get("model", {}).get("const") != MODEL:
            raise CodexStartupError("OUTPUT_SCHEMA_MODEL_MISMATCH")
        return {
            "cli_version": version,
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "prompt_version": PROMPT_VERSION,
            "schema_version": str(self.validator.schema.get("properties", {}).get("schema_version", {}).get("const")),
        }

    def command(self, output_path: Path) -> list[str]:
        return [
            str(self.config.codex_binary),
            "--ask-for-approval",
            "never",
            "--search",
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--cd",
            str(self.config.project_root),
            "--model",
            MODEL,
            "-c",
            f'model_reasoning_effort="{REASONING_EFFORT}"',
            "--sandbox",
            "read-only",
            "--json",
            "--output-schema",
            str(self.config.schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ]

    @staticmethod
    def _stderr_tail(path: Path, limit: int = 4000) -> str:
        if not path.is_file():
            return ""
        data = path.read_bytes()
        return data[-limit:].decode("utf-8", errors="replace")

    @staticmethod
    def _classify_failure(returncode: int, stderr: str) -> tuple[str, str]:
        lowered = stderr.lower()
        if "not logged in" in lowered or "authentication" in lowered or "unauthorized" in lowered or "401" in lowered:
            return "WAITING_AUTH", "AUTH_FAILED"
        if "model" in lowered and ("not available" in lowered or "unsupported" in lowered or "not found" in lowered):
            return "WAITING_MODEL", "MODEL_UNAVAILABLE"
        if "quota" in lowered or "rate limit" in lowered or "429" in lowered:
            return "WAITING_RETRY", "QUOTA_OR_RATE_LIMIT"
        if returncode < 0:
            return "WAITING_RETRY", "PROCESS_TERMINATED"
        return "WAITING_RETRY", "CLI_NONZERO_EXIT"

    def _schedule_error(self, job: AnalysisJob, status: str, code: str, message: str) -> RunResult:
        model_or_auth = status in {"WAITING_MODEL", "WAITING_AUTH"}
        retry_allowed = job.attempt_count <= self.config.max_automatic_retries
        if model_or_auth:
            next_retry = utc_now() + timedelta(minutes=5 * (2 ** max(0, job.attempt_count - 1))) if retry_allowed else None
            self.store.mark_error(
                job.job_id,
                status=status,
                error_code=code,
                error_message=message,
                next_retry_at=next_retry,
            )
            return RunResult(status, code, message)
        if retry_allowed:
            next_retry = utc_now() + timedelta(minutes=2 ** max(0, job.attempt_count - 1))
            self.store.mark_error(
                job.job_id,
                status="WAITING_RETRY",
                error_code=code,
                error_message=message,
                next_retry_at=next_retry,
            )
            return RunResult("WAITING_RETRY", code, message)
        if code == "QUOTA_OR_RATE_LIMIT":
            self.store.mark_error(
                job.job_id,
                status="WAITING_RETRY",
                error_code=code,
                error_message=message,
                next_retry_at=None,
            )
            return RunResult("WAITING_RETRY", code, message)
        self.store.mark_error(
            job.job_id,
            status="FAILED",
            error_code=code,
            error_message=message,
        )
        return RunResult("FAILED", code, message)

    def run_job(self, job: AnalysisJob) -> RunResult:
        job_root = self.config.jobs_root / job.job_id
        input_dir = job_root / "input"
        prompt_path = job_root / "rendered_prompt.md"
        if not input_dir.is_dir() or not prompt_path.is_file():
            self.store.mark_error(
                job.job_id,
                status="INVALID_INPUT",
                error_code="INPUT_SNAPSHOT_MISSING",
                error_message="immutable job input or rendered prompt is missing",
            )
            return RunResult("INVALID_INPUT", "INPUT_SNAPSHOT_MISSING")
        try:
            company_snapshot = verify_input_snapshot(input_dir, job.input_snapshot_hash)
            if prompt_path.read_text(encoding="utf-8") != PromptRenderer().render(job, input_dir):
                raise ValueError("rendered prompt does not match the installed version")
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            self.store.mark_error(
                job.job_id,
                status="INVALID_INPUT",
                error_code="INPUT_SNAPSHOT_INVALID",
                error_message=str(error),
            )
            return RunResult("INVALID_INPUT", "INPUT_SNAPSHOT_INVALID")
        if not is_analysis_eligible(company_snapshot):
            self.store.mark_error(
                job.job_id,
                status="INVALID_INPUT",
                error_code="STRUCTURED_INPUT_NOT_VALID",
                error_message="Codex analysis requires VALID core inputs; only qualitative-overlay-only PARTIAL is allowed",
            )
            return RunResult("INVALID_INPUT", "STRUCTURED_INPUT_NOT_VALID")

        output_path = job_root / "final.output.tmp.json"
        output_path.unlink(missing_ok=True)
        events_path = job_root / "run.events.jsonl"
        stderr_path = job_root / "stderr.log"
        command = self.command(output_path)
        environment = self._base_environment()
        environment["CODEX_JOB_INPUT_DIR"] = str(input_dir)
        environment["CODEX_EXPECTED_OUTPUT"] = str(output_path)
        environment["CODEX_EXPECTED_MODEL"] = MODEL
        environment["CODEX_EXPECTED_REASONING"] = REASONING_EFFORT
        prompt = prompt_path.read_text(encoding="utf-8")
        try:
            with events_path.open("wb") as events, stderr_path.open("wb") as stderr:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=events,
                    stderr=stderr,
                    text=True,
                    start_new_session=True,
                    env=environment,
                    cwd=self.config.project_root,
                )
                try:
                    process.communicate(prompt, timeout=self.config.timeout_seconds)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=10)
                    return self._schedule_error(job, "WAITING_RETRY", "TIMEOUT", "Codex process exceeded the configured timeout")
        except OSError as error:
            return self._schedule_error(job, "WAITING_RETRY", "CLI_START_FAILED", str(error))

        if process.returncode != 0:
            stderr_tail = self._stderr_tail(stderr_path)
            status, code = self._classify_failure(process.returncode, stderr_tail)
            return self._schedule_error(job, status, code, f"exit={process.returncode}; {stderr_tail}")
        if not output_path.is_file():
            return self._schedule_error(job, "WAITING_RETRY", "OUTPUT_MISSING", "Codex did not write the final output file")
        try:
            payload = self.validator.validate_file(output_path, job, company_snapshot)
        except OutputValidationError as error:
            if job.attempt_count <= self.config.schema_retries:
                next_retry = utc_now() + timedelta(minutes=1)
                self.store.mark_error(
                    job.job_id,
                    status="WAITING_RETRY",
                    error_code="SCHEMA_INVALID",
                    error_message=str(error),
                    next_retry_at=next_retry,
                )
                return RunResult("WAITING_RETRY", "SCHEMA_INVALID", str(error))
            self.store.mark_error(
                job.job_id,
                status="FAILED",
                error_code="SCHEMA_INVALID",
                error_message=str(error),
            )
            return RunResult("FAILED", "SCHEMA_INVALID", str(error))
        reviewed_overlay = None
        if "reviewed_overlay_candidates" in payload:
            try:
                reviewed_overlay = self.reviewed_overlays.build(payload, job)
            except ReviewedOverlayError as error:
                if job.attempt_count <= self.config.schema_retries:
                    self.store.mark_error(
                        job.job_id,
                        status="WAITING_RETRY",
                        error_code="SCHEMA_INVALID",
                        error_message=str(error),
                        next_retry_at=utc_now() + timedelta(minutes=1),
                    )
                    return RunResult("WAITING_RETRY", "SCHEMA_INVALID", str(error))
                self.store.mark_error(
                    job.job_id,
                    status="FAILED",
                    error_code="SCHEMA_INVALID",
                    error_message=str(error),
                )
                return RunResult("FAILED", "SCHEMA_INVALID", str(error))
        result_path = self.storage.finalize_success(
            job,
            payload,
            reviewed_overlay=reviewed_overlay,
            events_path=events_path,
            stderr_path=stderr_path,
            command=command,
            cli_version=self.cli_version,
        )
        self.store.mark_succeeded(job.job_id, result_path)
        return RunResult("SUCCEEDED", result_path=result_path)

    def run_once(self) -> RunResult | None:
        job = self.store.claim_next(global_concurrency=self.config.global_concurrency)
        if job is None:
            return None
        return self.run_job(job)
