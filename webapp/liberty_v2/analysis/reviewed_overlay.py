from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from ..policy import integer_value, policy
from .job_store import AnalysisJob, SAFE_IDENTIFIER


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA = PROJECT_ROOT / "analysis" / "schema" / "reviewed_overlay_v1.json"
SCORE_IDS = ("business_durability", "governance_capital_allocation")
REVIEWER_ID = "deterministic-overlay-validator-v1"


class ReviewedOverlayError(ValueError):
    pass


def _parse_date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ReviewedOverlayError(f"invalid reviewed overlay date: {field}") from error


def validate_reviewed_candidates(
    payload: Mapping[str, Any],
    *,
    max_validity_days: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Deterministically audit model-proposed score configuration.

    The model may only nominate the two explicitly allowed score inputs. A
    candidate is accepted only when its URL is one of the already schema-checked
    report sources and its validity interval is bounded. This function never
    touches financial inputs, calculated metrics or veto flags.
    """

    maximum = (
        max_validity_days
        if max_validity_days is not None
        else integer_value("codex", "reviewed_overlay_max_validity_days")
    )
    if maximum < 1:
        raise ReviewedOverlayError("reviewed overlay maximum validity must be positive")
    raw_candidates = payload.get("reviewed_overlay_candidates")
    if not isinstance(raw_candidates, Mapping):
        raise ReviewedOverlayError("reviewed_overlay_candidates must be an object")
    if set(raw_candidates) != set(SCORE_IDS):
        raise ReviewedOverlayError("reviewed overlay candidate fields are not allowed")

    source_records: dict[str, dict[str, Any]] = {}
    duplicate_source_urls: set[str] = set()
    for source in payload.get("sources", []):
        if not isinstance(source, Mapping):
            continue
        url = str(source.get("url") or "")
        if url in source_records:
            duplicate_source_urls.add(url)
        else:
            source_records[url] = dict(source)

    report_as_of = _parse_date(payload.get("as_of_date"), "as_of_date")
    codex_policy = policy().get("codex", {})
    rubric = codex_policy.get("reviewed_overlay_score_rubric", {}) if isinstance(codex_policy, Mapping) else {}
    if not isinstance(rubric, Mapping) or not str(rubric.get("version") or ""):
        raise ReviewedOverlayError("versioned reviewed overlay rubric is missing")
    accepted: dict[str, dict[str, Any]] = {}
    for score_id in SCORE_IDS:
        candidate = raw_candidates[score_id]
        if candidate is None:
            continue
        if not isinstance(candidate, Mapping):
            raise ReviewedOverlayError(f"{score_id} candidate must be an object or null")
        source_url = str(candidate.get("source") or "")
        if source_url in duplicate_source_urls:
            raise ReviewedOverlayError(f"{score_id} source URL is duplicated and ambiguous")
        source = source_records.get(source_url)
        if source is None:
            raise ReviewedOverlayError(f"{score_id} source must exactly match sources[].url")
        candidate_as_of = _parse_date(candidate.get("as_of_date"), f"{score_id}.as_of_date")
        expires_at = _parse_date(candidate.get("expires_at"), f"{score_id}.expires_at")
        publish_date = _parse_date(source.get("publish_date"), f"{score_id}.source.publish_date")
        if candidate_as_of != report_as_of:
            raise ReviewedOverlayError(f"{score_id} as_of_date must match the immutable snapshot")
        if publish_date > candidate_as_of:
            raise ReviewedOverlayError(f"{score_id} source was published after as_of_date")
        if expires_at < candidate_as_of:
            raise ReviewedOverlayError(f"{score_id} expires_at precedes as_of_date")
        if expires_at - candidate_as_of > timedelta(days=maximum):
            raise ReviewedOverlayError(f"{score_id} validity exceeds {maximum} days")
        score_rubric = rubric.get(score_id)
        if not isinstance(score_rubric, Mapping):
            raise ReviewedOverlayError(f"{score_id} rubric is missing")
        if candidate.get("rubric_version") != rubric.get("version"):
            raise ReviewedOverlayError(f"{score_id} rubric_version mismatch")
        dimensions = candidate.get("dimension_scores")
        expected_dimensions = score_rubric.get("dimensions")
        if (
            not isinstance(dimensions, Mapping)
            or not isinstance(expected_dimensions, list)
            or set(dimensions) != set(map(str, expected_dimensions))
        ):
            raise ReviewedOverlayError(f"{score_id} dimension set does not match policy")
        dimension_values = [int(dimensions[name]) for name in expected_dimensions]
        expected_value = (sum(dimension_values) + len(dimension_values) // 2) // len(dimension_values)
        if int(candidate.get("value")) != expected_value:
            raise ReviewedOverlayError(f"{score_id} value does not equal the policy mean")
        red_flags = candidate.get("red_flags")
        allowed_red_flags = score_rubric.get("red_flags")
        if (
            not isinstance(red_flags, list)
            or not isinstance(allowed_red_flags, list)
            or not set(map(str, red_flags)).issubset(set(map(str, allowed_red_flags)))
        ):
            raise ReviewedOverlayError(f"{score_id} red_flags do not match policy")
        if red_flags and expected_value > int(score_rubric.get("red_flag_cap")):
            raise ReviewedOverlayError(f"{score_id} exceeds the policy red-flag cap")
        accepted[score_id] = {**dict(candidate), "source_record": source}
    return accepted


class ReviewedOverlayStore:
    """Read/write the private reviewed overlay bound to a successful analysis run."""

    def __init__(
        self,
        output_root: Path,
        *,
        schema_path: Path = DEFAULT_SCHEMA,
        max_validity_days: int | None = None,
    ) -> None:
        self.output_root = output_root
        self.schema_path = schema_path
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(self.schema)
        self.validator = Draft202012Validator(self.schema, format_checker=FormatChecker())
        self.max_validity_days = max_validity_days

    def _validate_overlay(self, overlay: Mapping[str, Any]) -> dict[str, Any]:
        errors = sorted(self.validator.iter_errors(overlay), key=lambda error: list(error.absolute_path))
        if errors:
            messages = [
                f"{'/'.join(map(str, error.absolute_path)) or '$'}: {error.message}"
                for error in errors[:20]
            ]
            raise ReviewedOverlayError("reviewed overlay schema failed: " + " | ".join(messages))
        return dict(overlay)

    def latest(self, company_id: str) -> dict[str, Any] | None:
        if not SAFE_IDENTIFIER.fullmatch(company_id):
            raise ReviewedOverlayError("unsafe company_id in reviewed overlay lookup")
        company_root = self.output_root / company_id
        pointer_path = company_root / "latest.json"
        if not pointer_path.is_file():
            return None
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        analysis_id = str(pointer.get("analysis_id") or "") if isinstance(pointer, Mapping) else ""
        expected_hash = str(pointer.get("reviewed_overlay_sha256") or "") if isinstance(pointer, Mapping) else ""
        if not SAFE_IDENTIFIER.fullmatch(analysis_id) or len(expected_hash) != 64:
            return None
        relative = PurePosixPath("runs") / analysis_id / "reviewed_overlay.json"
        path = company_root.joinpath(*relative.parts)
        if not path.is_file():
            raise ReviewedOverlayError("reviewed overlay pointer target is missing")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise ReviewedOverlayError("reviewed overlay hash mismatch")
        value = json.loads(content)
        if not isinstance(value, Mapping):
            raise ReviewedOverlayError("reviewed overlay must be an object")
        overlay = self._validate_overlay(value)
        if overlay["company_id"] != company_id or overlay["latest_analysis_id"] != analysis_id:
            raise ReviewedOverlayError("reviewed overlay identity mismatch")
        return overlay

    def build(
        self,
        payload: Mapping[str, Any],
        job: AnalysisJob,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        accepted = validate_reviewed_candidates(
            payload,
            max_validity_days=self.max_validity_days,
        )
        previous = self.latest(job.company_id)
        scores = copy.deepcopy(previous.get("scores", {})) if previous else {}
        for score_id, candidate in accepted.items():
            scores[score_id] = {
                **candidate,
                "analysis_id": job.job_id,
                "analysis_mode": job.analysis_mode,
                "input_snapshot_hash": job.input_snapshot_hash,
                "prompt_version": job.prompt_version,
                "model": job.model,
                "reasoning_effort": job.reasoning_effort,
                "produced_by_codex": True,
                "review_status": "DETERMINISTICALLY_ACCEPTED",
                "reviewed_by": REVIEWER_ID,
            }
        if not scores:
            return None
        overlay = {
            "schema_version": "1.0",
            "company_id": job.company_id,
            "latest_analysis_id": job.job_id,
            "updated_at": (now or datetime.now(timezone.utc)).isoformat(),
            "scores": scores,
        }
        return self._validate_overlay(overlay)

    def active_scores(self, company_id: str, *, on_date: date) -> dict[str, dict[str, Any]]:
        overlay = self.latest(company_id)
        if overlay is None:
            return {}
        active: dict[str, dict[str, Any]] = {}
        for score_id, score in overlay["scores"].items():
            as_of = _parse_date(score["as_of_date"], f"{score_id}.as_of_date")
            expires = _parse_date(score["expires_at"], f"{score_id}.expires_at")
            if as_of <= on_date <= expires:
                active[score_id] = {
                    key: score[key]
                    for key in ("value", "source", "as_of_date", "expires_at", "reason")
                }
        return active

    def apply_to_raw(
        self,
        raw: Mapping[str, Any],
        *,
        company_id: str,
        on_date: date,
    ) -> dict[str, Any]:
        result = copy.deepcopy(dict(raw))
        result["reviewed_overlay_scores"] = self.active_scores(company_id, on_date=on_date)
        return result
