from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker

from ..constants import MODEL, OUTPUT_SCHEMA_VERSION, REASONING_EFFORT
from .job_store import AnalysisJob
from .reviewed_overlay import ReviewedOverlayError, validate_reviewed_candidates


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA = PROJECT_ROOT / "analysis" / "schema" / "risk_analysis_output_v1.json"


class OutputValidationError(ValueError):
    pass


def _reject_constant(value: str) -> None:
    raise OutputValidationError(f"non-finite JSON number is forbidden: {value}")


class AnalysisOutputValidator:
    def __init__(self, schema_path: Path = DEFAULT_SCHEMA) -> None:
        self.schema_path = schema_path
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(self.schema)
        self.validator = Draft202012Validator(self.schema, format_checker=FormatChecker())

    def load(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=_reject_constant,
            )
        except (OSError, json.JSONDecodeError) as error:
            raise OutputValidationError(f"final output is not valid JSON: {error}") from error
        if not isinstance(payload, dict):
            raise OutputValidationError("final output must be a JSON object")
        return payload

    def validate(
        self,
        payload: Mapping[str, Any],
        job: AnalysisJob,
        company_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        errors = sorted(self.validator.iter_errors(payload), key=lambda error: list(error.absolute_path))
        if errors:
            messages = [f"{'/'.join(map(str, error.absolute_path)) or '$'}: {error.message}" for error in errors[:20]]
            raise OutputValidationError("schema validation failed: " + " | ".join(messages))
        expected = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "prompt_version": job.prompt_version,
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "analysis_id": job.job_id,
            "analysis_mode": job.analysis_mode,
            "company_id": job.company_id,
            "input_snapshot_hash": job.input_snapshot_hash,
            "calculation_version": job.calculation_version,
        }
        mismatches = [
            f"{key}: expected {value!r}, got {payload.get(key)!r}"
            for key, value in expected.items()
            if payload.get(key) != value
        ]
        if mismatches:
            raise OutputValidationError("output identity mismatch: " + "; ".join(mismatches))
        if payload["trigger"]["type"] != job.trigger_type:
            raise OutputValidationError("output trigger type does not match the queued trigger")
        if company_snapshot is not None:
            securities = company_snapshot.get("securities")
            first = securities[0] if isinstance(securities, list) and securities else {}
            snapshot_expected = {
                "company_name": company_snapshot.get("company_name"),
                "ticker": first.get("ticker") if isinstance(first, Mapping) else None,
                "market": first.get("market") if isinstance(first, Mapping) else None,
                "as_of_date": company_snapshot.get("as_of_date"),
            }
            snapshot_mismatches = [
                f"{key}: expected {value!r}, got {payload.get(key)!r}"
                for key, value in snapshot_expected.items()
                if value is not None and payload.get(key) != value
            ]
            if snapshot_mismatches:
                raise OutputValidationError(
                    "output company identity mismatch: " + "; ".join(snapshot_mismatches)
                )
        report = str(payload["report_markdown"])
        if "<" in report or ">" in report:
            raise OutputValidationError("raw HTML is forbidden in report_markdown")
        for source in payload["sources"]:
            parsed = urlparse(source["url"])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise OutputValidationError("source URLs must use http or https")
        if payload["analysis_mode"] == "URGENT_VETO_REVIEW" and payload["verdict"] == "SCALE_IN":
            raise OutputValidationError("urgent veto reviews may not return SCALE_IN")
        if bool(payload["data_issue_detected"]) != bool(payload["data_issue_notes"]):
            raise OutputValidationError("data_issue_detected must agree with data_issue_notes")
        try:
            validate_reviewed_candidates(payload)
        except ReviewedOverlayError as error:
            raise OutputValidationError(f"reviewed overlay candidate rejected: {error}") from error
        return dict(payload)

    def validate_file(
        self,
        path: Path,
        job: AnalysisJob,
        company_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.validate(self.load(path), job, company_snapshot)
