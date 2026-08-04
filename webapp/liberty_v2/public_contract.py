"""Public shareholder-screen-v2.2 release and global activation contract."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from .constants import CALCULATION_VERSION, METRIC_DEFINITION_VERSION, SCHEMA_VERSION


VALID_STATUSES = {"READY", "DATA_LIMITED", "STALE", "UNAVAILABLE"}
EXPECTED_COMPANY_COUNT = 67
PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = PROJECT_ROOT / "config" / "metric_policy_v2.json"
CONTRACT_PATH = Path(__file__).resolve()


class V2ContractError(ValueError):
    pass


@dataclass(frozen=True)
class V2CanarySummary:
    company_count: int
    opportunity_score_count: int
    financial_resilience_score_count: int
    trigger_candidate_count: int
    status_counts: Mapping[str, int]
    scored_company_ids: tuple[str, ...] = ()
    legacy_tier_counts: Mapping[str, int] | None = None

    @property
    def tier_counts(self) -> Mapping[str, int]:
        return self.legacy_tier_counts or self.status_counts

    def public_dict(self) -> dict[str, Any]:
        value = {
            "company_count": self.company_count,
            "opportunity_score_count": self.opportunity_score_count,
            "financial_resilience_score_count": self.financial_resilience_score_count,
            "trigger_candidate_count": self.trigger_candidate_count,
            "status_counts": dict(self.status_counts),
        }
        if self.legacy_tier_counts is not None:
            value.update(
                scored_company_count=len(self.scored_company_ids),
                scored_company_ids=list(self.scored_company_ids),
                tier_counts=dict(self.legacy_tier_counts),
            )
        return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _contains_non_finite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, str) and value.strip().lower() in {
        "nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"
    }:
        return True
    if isinstance(value, Mapping):
        return any(_contains_non_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite(item) for item in value)
    return False


def _bounded_decimal(value: Any, *, maximum: str, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise V2ContractError(f"invalid {label}") from error
    if not parsed.is_finite() or not Decimal("0") <= parsed <= Decimal(maximum):
        raise V2ContractError(f"{label} outside 0..{maximum}")
    return parsed


def _validate_score(record: Any, *, company_id: str, score_id: str) -> bool:
    if not isinstance(record, Mapping):
        raise V2ContractError(f"{score_id} must be an object: {company_id}")
    coverage = _bounded_decimal(record.get("coverage"), maximum="1", label=f"coverage {company_id}.{score_id}")
    value = record.get("value")
    if value is None:
        if coverage != 0 or record.get("status") != "UNAVAILABLE":
            raise V2ContractError(f"null score must be unavailable with zero coverage: {company_id}.{score_id}")
    else:
        _bounded_decimal(value, maximum="100", label=f"score {company_id}.{score_id}")
    if not record.get("basis") or not isinstance(record.get("warnings"), list):
        raise V2ContractError(f"score basis/warnings invalid: {company_id}.{score_id}")
    components = record.get("components")
    if not isinstance(components, Mapping) or not components:
        raise V2ContractError(f"score components missing: {company_id}.{score_id}")
    for component_id, component in components.items():
        if not isinstance(component, Mapping) or not component.get("basis"):
            raise V2ContractError(f"component basis missing: {company_id}.{score_id}.{component_id}")
        if not isinstance(component.get("source_summary"), Mapping):
            raise V2ContractError(f"component source summary missing: {company_id}.{score_id}.{component_id}")
        component_value = component.get("value")
        if component_value is not None:
            _bounded_decimal(component_value, maximum="100", label=f"component {company_id}.{score_id}.{component_id}")
    return value is not None


def _legacy_score_value(record: Any, *, company_id: str, score_id: str) -> Decimal | None:
    if not isinstance(record, Mapping) or record.get("value") is None:
        return None
    return _bounded_decimal(record["value"], maximum="100", label=f"score {company_id}.{score_id}")


def _validate_legacy_public_index(payload: Mapping[str, Any]) -> V2CanarySummary:
    if payload.get("calculation_version") != "shareholder-return-v2.1.0":
        raise V2ContractError("unsupported v2 calculation version")
    if payload.get("metric_definition_version") != "shareholder-return-v2.1.0":
        raise V2ContractError("unsupported v2 metric definition version")
    if payload.get("release_validity") != "VALID_RELEASE":
        raise V2ContractError("structured release is not VALID_RELEASE")
    companies = payload.get("companies")
    if not isinstance(companies, list) or not companies:
        raise V2ContractError("structured release has no companies")
    if payload.get("company_count") != len(companies):
        raise V2ContractError("company_count does not match release records")
    identifiers: set[str] = set()
    scored: list[str] = []
    tiers: dict[str, int] = {}
    valid_tiers = {"BLOCKED", "ESTIMATED", "CALCULABLE", "VERIFIED"}
    valid_freshness = {"CURRENT", "MARKET_CLOSED_CURRENT", "STALE_LAST_GOOD"}
    for row in companies:
        company_id = str(row.get("company_id") or "") if isinstance(row, Mapping) else ""
        if not company_id or company_id in identifiers:
            raise V2ContractError("company IDs must be present and unique")
        identifiers.add(company_id)
        tier = str(row.get("data_tier") or "")
        if tier not in valid_tiers or row.get("freshness") not in valid_freshness:
            raise V2ContractError(f"invalid legacy company status: {company_id}")
        tiers[tier] = tiers.get(tier, 0) + 1
        confidence = row.get("data_confidence")
        if not isinstance(confidence, Mapping) or not isinstance(confidence.get("value"), int) or isinstance(confidence.get("value"), bool) or not 0 <= confidence["value"] <= 100:
            raise V2ContractError(f"invalid data confidence: {company_id}")
        for key in ("metrics", "metric_bases", "selected_input_plan", "source_summary"):
            if not isinstance(row.get(key), Mapping):
                raise V2ContractError(f"{key} must be an object: {company_id}")
        scores = row.get("scores")
        if not isinstance(scores, Mapping):
            raise V2ContractError(f"scores must be an object: {company_id}")
        if tier == "BLOCKED" and scores:
            raise V2ContractError(f"BLOCKED company exposes scores: {company_id}")
        ri = _legacy_score_value(scores.get("recommendation_index"), company_id=company_id, score_id="recommendation_index")
        eri = _legacy_score_value(scores.get("entry_risk_index"), company_id=company_id, score_id="entry_risk_index")
        if (ri is None) != (eri is None):
            raise V2ContractError(f"RI and ERI must publish together: {company_id}")
        if ri is not None:
            scored.append(company_id)
        if _contains_non_finite(row):
            raise V2ContractError(f"non-finite value in company record: {company_id}")
    return V2CanarySummary(
        company_count=len(companies),
        opportunity_score_count=0,
        financial_resilience_score_count=0,
        trigger_candidate_count=0,
        status_counts={},
        scored_company_ids=tuple(sorted(scored)),
        legacy_tier_counts=dict(sorted(tiers.items())),
    )


def validate_public_index(payload: Mapping[str, Any]) -> V2CanarySummary:
    if payload.get("schema_version") == "shareholder-return-v2":
        return _validate_legacy_public_index(payload)
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise V2ContractError("unsupported screening schema")
    if payload.get("calculation_version") != CALCULATION_VERSION:
        raise V2ContractError("unsupported screening calculation version")
    if payload.get("metric_definition_version") != METRIC_DEFINITION_VERSION:
        raise V2ContractError("unsupported metric definition version")
    if payload.get("release_validity") != "VALID_RELEASE":
        raise V2ContractError("structured release is not VALID_RELEASE")
    companies = payload.get("companies")
    if not isinstance(companies, list) or len(companies) != EXPECTED_COMPANY_COUNT:
        raise V2ContractError(f"structured release must contain exactly {EXPECTED_COMPANY_COUNT} companies")
    if payload.get("company_count") != len(companies):
        raise V2ContractError("company_count does not match release records")
    identifiers: set[str] = set()
    status_counts: dict[str, int] = {}
    opportunity_count = resilience_count = trigger_count = 0
    for row in companies:
        if not isinstance(row, Mapping):
            raise V2ContractError("company record must be an object")
        company_id = str(row.get("company_id") or "")
        if not company_id or company_id in identifiers:
            raise V2ContractError("company IDs must be present and unique")
        identifiers.add(company_id)
        if row.get("schema_version") != SCHEMA_VERSION or row.get("calculation_version") != CALCULATION_VERSION:
            raise V2ContractError(f"company version mismatch: {company_id}")
        status = str(row.get("status") or "")
        if status not in VALID_STATUSES:
            raise V2ContractError(f"invalid company status: {company_id}")
        status_counts[status] = status_counts.get(status, 0) + 1
        for key in ("price", "source_summary", "analysis_status", "research_trigger"):
            if not isinstance(row.get(key), Mapping):
                raise V2ContractError(f"{key} must be an object: {company_id}")
        if not isinstance(row.get("warnings"), list):
            raise V2ContractError(f"warnings must be an array: {company_id}")
        opportunity_count += int(_validate_score(row.get("opportunity_score"), company_id=company_id, score_id="opportunity_score"))
        resilience_count += int(_validate_score(row.get("financial_resilience_score"), company_id=company_id, score_id="financial_resilience_score"))
        trigger_count += int(row["research_trigger"].get("eligible") is True)
        if _contains_non_finite(row):
            raise V2ContractError(f"non-finite value in company record: {company_id}")
    return V2CanarySummary(
        company_count=len(companies),
        opportunity_score_count=opportunity_count,
        financial_resilience_score_count=resilience_count,
        trigger_candidate_count=trigger_count,
        status_counts=dict(sorted(status_counts.items())),
    )


def validate_activation_canary(
    payload: Mapping[str, Any],
    *,
    approval: Mapping[str, Any] | None = None,
    expected_company_count: int = EXPECTED_COMPANY_COUNT,
    **_legacy_arguments: Any,
) -> V2CanarySummary:
    summary = validate_public_index(payload)
    if payload.get("schema_version") == "shareholder-return-v2":
        minimum = int(_legacy_arguments.get("minimum_scored_companies", 5))
        if summary.company_count != expected_company_count:
            raise V2ContractError(f"activation requires {expected_company_count} companies, got {summary.company_count}")
        if len(summary.scored_company_ids) < minimum:
            raise V2ContractError(f"activation requires {minimum} scored companies, got {len(summary.scored_company_ids)}")
        approved: Iterable[str] = _legacy_arguments.get("approved_company_ids", ())
        unreviewed = sorted(set(summary.scored_company_ids) - {str(item) for item in approved})
        if unreviewed:
            raise V2ContractError("scored companies lack manual activation approval: " + ",".join(unreviewed))
        return summary
    if summary.company_count != expected_company_count:
        raise V2ContractError(f"activation requires {expected_company_count} companies")
    value = approval or {}
    if value.get("calculation_version") != CALCULATION_VERSION:
        raise V2ContractError("global activation approval calculation version mismatch")
    if value.get("metric_policy_sha256") != _sha256(POLICY_PATH):
        raise V2ContractError("global activation approval policy SHA mismatch")
    if value.get("public_contract_sha256") != _sha256(CONTRACT_PATH):
        raise V2ContractError("global activation approval contract SHA mismatch")
    if not all(str(value.get(key) or "").strip() for key in ("approved_at", "reviewer")):
        raise V2ContractError("global activation approval is incomplete")
    return summary
