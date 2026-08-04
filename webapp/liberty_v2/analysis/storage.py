from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .job_store import AnalysisJob


SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")


class AnalysisStorageError(RuntimeError):
    pass


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _copy_input_files_without_metadata(source: Path, destination: Path) -> None:
    """Archive verified inputs without copying setgid metadata into the sandbox."""
    destination.mkdir(parents=False, exist_ok=False)
    for item in sorted(source.iterdir(), key=lambda path: path.name):
        if item.is_symlink() or not item.is_file():
            raise AnalysisStorageError("analysis input archive may only contain regular files")
        shutil.copyfile(item, destination / item.name)


class AnalysisStorage:
    def __init__(self, output_root: Path, jobs_root: Path) -> None:
        self.output_root = output_root
        self.jobs_root = jobs_root

    def finalize_success(
        self,
        job: AnalysisJob,
        payload: Mapping[str, Any],
        *,
        reviewed_overlay: Mapping[str, Any] | None = None,
        events_path: Path,
        stderr_path: Path,
        command: list[str],
        cli_version: str | None,
    ) -> Path:
        company_root = self.output_root / job.company_id
        run_root = company_root / "runs" / job.job_id
        if run_root.exists():
            raise FileExistsError(f"successful run already exists: {run_root}")
        staging = company_root / "runs" / f".{job.job_id}.tmp"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=False)
        input_dir = self.jobs_root / job.job_id / "input"
        _copy_input_files_without_metadata(input_dir, staging / "input")
        final_bytes = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"
        report_bytes = str(payload["report_markdown"]).encode("utf-8")
        _atomic_write(staging / "final.json", final_bytes)
        _atomic_write(staging / "report.md", report_bytes)
        overlay_bytes = None
        if reviewed_overlay is not None:
            overlay_bytes = (
                json.dumps(
                    reviewed_overlay,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            _atomic_write(staging / "reviewed_overlay.json", overlay_bytes)
        if events_path.is_file():
            shutil.copy2(events_path, staging / "run.events.jsonl")
        if stderr_path.is_file():
            shutil.copy2(stderr_path, staging / "stderr.log")
        metadata = {
            "analysis_id": job.job_id,
            "company_id": job.company_id,
            "input_snapshot_hash": job.input_snapshot_hash,
            "prompt_version": job.prompt_version,
            "calculation_version": job.calculation_version,
            "model": job.model,
            "reasoning_effort": job.reasoning_effort,
            "cli_version": cli_version,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "final_sha256": hashlib.sha256(final_bytes).hexdigest(),
            "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            "reviewed_overlay_sha256": (
                hashlib.sha256(overlay_bytes).hexdigest() if overlay_bytes is not None else None
            ),
        }
        _atomic_write(
            staging / "metadata.json",
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        os.replace(staging, run_root)
        pointer = {
            "analysis_id": job.job_id,
            "relative_path": f"runs/{job.job_id}/final.json",
            "sha256": metadata["final_sha256"],
            "completed_at": metadata["completed_at"],
        }
        if metadata["reviewed_overlay_sha256"] is not None:
            pointer["reviewed_overlay_sha256"] = metadata["reviewed_overlay_sha256"]
        _atomic_write(
            company_root / "latest.json",
            json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        return run_root

    def latest_public_payload(self, company_id: str) -> tuple[dict[str, Any], str] | None:
        if not SAFE_IDENTIFIER.fullmatch(company_id):
            raise AnalysisStorageError("unsafe company_id in analysis storage lookup")
        company_root = self.output_root / company_id
        pointer_path = company_root / "latest.json"
        if not pointer_path.is_file():
            return None
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        if not isinstance(pointer, dict):
            raise AnalysisStorageError("latest analysis pointer must be an object")
        analysis_id = str(pointer.get("analysis_id") or "")
        relative_text = str(pointer.get("relative_path") or "")
        expected_hash = str(pointer.get("sha256") or "")
        if not SAFE_IDENTIFIER.fullmatch(analysis_id) or not SHA256.fullmatch(expected_hash):
            raise AnalysisStorageError("latest analysis pointer identity or hash is invalid")
        relative = PurePosixPath(relative_text)
        expected_relative = PurePosixPath("runs") / analysis_id / "final.json"
        if relative.is_absolute() or ".." in relative.parts or relative != expected_relative:
            raise AnalysisStorageError("latest analysis pointer path is invalid")
        final_path = company_root.joinpath(*relative.parts)
        if not final_path.is_file():
            raise AnalysisStorageError("latest analysis result is missing")
        final_bytes = final_path.read_bytes()
        actual_hash = hashlib.sha256(final_bytes).hexdigest()
        if actual_hash != expected_hash:
            raise AnalysisStorageError("latest analysis result hash mismatch")
        payload = json.loads(final_bytes)
        if not isinstance(payload, dict):
            raise AnalysisStorageError("latest analysis result must be an object")
        if payload.get("analysis_id") != analysis_id or payload.get("company_id") != company_id:
            raise AnalysisStorageError("latest analysis result identity mismatch")
        if not isinstance(payload.get("report_markdown"), str):
            raise AnalysisStorageError("latest analysis report_markdown is missing")
        return payload, str(payload["report_markdown"])
