from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ..constants import MODEL, OUTPUT_SCHEMA_VERSION, PROMPT_VERSION, REASONING_EFFORT
from ..registry import load_metric_definitions
from ..policy import policy
from .job_store import AnalysisJob


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMPT_ROOT = PROJECT_ROOT / "analysis" / "prompts" / "v2"
MODE_FILES = {
    "FULL_ENTRY_REVIEW": "full_entry_review.md",
    "URGENT_VETO_REVIEW": "urgent_veto_review.md",
    "MATERIAL_CHANGE_REVIEW": "material_change_review.md",
    "PERIODIC_REFRESH": "periodic_refresh.md",
    "PRICE_RISK_ANALYSIS": "price_risk_analysis.md",
    "URGENT_RISK_REVIEW": "urgent_risk_review.md",
}
INPUT_DOCUMENT_NAMES = {
    "company_snapshot.json",
    "metric_definitions.json",
    "trigger.json",
    "previous_analysis.json",
    "source_index.json",
    "prompt_metadata.json",
    "research_bundle.json",
}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(snapshot))


def verify_input_snapshot(input_dir: Path, expected_snapshot_hash: str) -> dict[str, Any]:
    """Verify the exact immutable input file set before any model process starts."""
    if not input_dir.is_dir():
        raise ValueError("immutable input directory is missing")
    entries = list(input_dir.iterdir())
    if any(not item.is_file() for item in entries):
        raise ValueError("immutable input directory may only contain regular files")
    actual_files = {item.name for item in entries}
    expected_files = INPUT_DOCUMENT_NAMES | {"sha256sums.json"}
    if actual_files != expected_files:
        raise ValueError("immutable input file set is incomplete or contains extras")
    checksums = json.loads((input_dir / "sha256sums.json").read_text(encoding="utf-8"))
    if not isinstance(checksums, dict) or set(checksums) != INPUT_DOCUMENT_NAMES:
        raise ValueError("immutable input checksum index is invalid")
    for filename in sorted(INPUT_DOCUMENT_NAMES):
        expected = checksums.get(filename)
        if not isinstance(expected, str) or sha256_bytes((input_dir / filename).read_bytes()) != expected:
            raise ValueError(f"immutable input hash mismatch: {filename}")
    company = json.loads((input_dir / "company_snapshot.json").read_text(encoding="utf-8"))
    if not isinstance(company, dict) or snapshot_hash(company) != expected_snapshot_hash:
        raise ValueError("immutable company snapshot hash mismatch")
    research = json.loads((input_dir / "research_bundle.json").read_text(encoding="utf-8"))
    if not isinstance(research, dict) or research.get("input_snapshot_hash") != expected_snapshot_hash:
        raise ValueError("immutable research bundle identity mismatch")
    return company


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class PromptRenderer:
    def __init__(self, prompt_root: Path = DEFAULT_PROMPT_ROOT) -> None:
        self.prompt_root = prompt_root

    def render(self, job: AnalysisJob, input_dir: Path) -> str:
        if job.prompt_version != PROMPT_VERSION:
            raise ValueError("job prompt version does not match the installed prompt")
        mode_file = MODE_FILES.get(job.analysis_mode)
        if mode_file is None:
            raise ValueError(f"unsupported analysis mode: {job.analysis_mode}")
        base = (self.prompt_root / "base_risk_review.md").read_text(encoding="utf-8").strip()
        mode = (self.prompt_root / mode_file).read_text(encoding="utf-8").strip()
        metadata = {
            "analysis_id": job.job_id,
            "analysis_mode": job.analysis_mode,
            "trigger_type": job.trigger_type,
            "company_id": job.company_id,
            "input_snapshot_hash": job.input_snapshot_hash,
            "calculation_version": job.calculation_version,
            "prompt_version": job.prompt_version,
            "output_schema_version": OUTPUT_SCHEMA_VERSION,
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "input_directory": str(input_dir),
        }
        return "\n\n".join(
            [
                base,
                mode,
                "## 本次固定输入",
                "只读取下列不可变任务目录。不得读取或推断不断变化的当前数据文件：",
                f"`{input_dir}`",
                "必须核对 `prompt_metadata.json`、`sha256sums.json` 和全部输入文件。",
                "输出字段必须逐项使用以下元数据，不得改写：",
                "```json\n" + json.dumps(metadata, ensure_ascii=False, indent=2) + "\n```",
            ]
        ) + "\n"


class InputSnapshotBuilder:
    def __init__(self, jobs_root: Path, renderer: PromptRenderer | None = None) -> None:
        self.jobs_root = jobs_root
        self.renderer = renderer or PromptRenderer()

    def prepare(
        self,
        job: AnalysisJob,
        *,
        company_snapshot: Mapping[str, Any],
        trigger: Mapping[str, Any],
        previous_analysis: Mapping[str, Any] | None = None,
        source_index: Mapping[str, Any] | None = None,
        metric_definitions: Mapping[str, Any] | None = None,
    ) -> Path:
        if snapshot_hash(company_snapshot) != job.input_snapshot_hash:
            raise ValueError("company snapshot hash does not match the queued job")
        job_root = self.jobs_root / job.job_id
        input_dir = job_root / "input"
        if input_dir.exists():
            verify_input_snapshot(input_dir, job.input_snapshot_hash)
            prompt_path = job_root / "rendered_prompt.md"
            if not prompt_path.is_file():
                _atomic_write(
                    prompt_path,
                    self.renderer.render(job, input_dir).encode("utf-8"),
                )
            return input_dir
        job_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=job_root, prefix=".input.", suffix=".tmp"))
        metadata = {
            "analysis_id": job.job_id,
            "analysis_mode": job.analysis_mode,
            "trigger_type": job.trigger_type,
            "company_id": job.company_id,
            "input_snapshot_hash": job.input_snapshot_hash,
            "calculation_version": job.calculation_version,
            "prompt_version": job.prompt_version,
            "output_schema_version": OUTPUT_SCHEMA_VERSION,
            "model": job.model,
            "reasoning_effort": job.reasoning_effort,
            "policy_version": policy().get("policy_version"),
        }
        price_position = (
            company_snapshot.get("opportunity_score", {})
            .get("components", {})
            .get("five_year_price_position", {})
            if isinstance(company_snapshot.get("opportunity_score"), Mapping)
            else {}
        )
        research_bundle = {
            "schema_version": "research-bundle-v2.0",
            "company_id": company_snapshot.get("company_id"),
            "company_name": company_snapshot.get("company_name"),
            "securities": company_snapshot.get("securities", []),
            "as_of_date": company_snapshot.get("as_of_date"),
            "price": company_snapshot.get("price", {}),
            "market_metrics": company_snapshot.get("market_metrics", {}),
            "five_year_price_percentile": price_position.get("percentile_rank"),
            "opportunity_score": company_snapshot.get("opportunity_score", {}),
            "financial_resilience_score": company_snapshot.get("financial_resilience_score", {}),
            "financial_history": company_snapshot.get("financial_history", []),
            "corporate_events": company_snapshot.get("research_inputs", {}).get("futu_events", {}),
            "official_disclosure_index": company_snapshot.get("research_inputs", {}).get("official_disclosure_index", []),
            "controlled_facts": company_snapshot.get("research_inputs", {}).get("controlled_facts", []),
            "source_summary": source_index or company_snapshot.get("source_summary", {}),
            "warnings": company_snapshot.get("warnings", []),
            "trigger": trigger,
            "previous_successful_analysis": previous_analysis or {},
            "calculation_version": job.calculation_version,
            "policy_version": policy().get("policy_version"),
            "input_snapshot_hash": job.input_snapshot_hash,
        }
        documents = {
            "company_snapshot.json": company_snapshot,
            "metric_definitions.json": metric_definitions or load_metric_definitions(),
            "trigger.json": trigger,
            "previous_analysis.json": previous_analysis or {},
            "source_index.json": source_index or company_snapshot.get("source_summary", {}),
            "prompt_metadata.json": metadata,
            "research_bundle.json": research_bundle,
        }
        try:
            checksums: dict[str, str] = {}
            for filename, value in documents.items():
                content = canonical_json_bytes(value) + b"\n"
                _atomic_write(staging / filename, content)
                checksums[filename] = sha256_bytes(content)
            _atomic_write(
                staging / "sha256sums.json",
                json.dumps(checksums, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
            )
            verify_input_snapshot(staging, job.input_snapshot_hash)
            os.replace(staging, input_dir)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        rendered = self.renderer.render(job, input_dir)
        _atomic_write(job_root / "rendered_prompt.md", rendered.encode("utf-8"))
        return input_dir
