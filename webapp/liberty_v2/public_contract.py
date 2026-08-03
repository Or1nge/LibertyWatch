"""Public v2.1 release validation and activation canary policy."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping


CALCULATION_VERSION = "shareholder-return-v2.1.0"
VALID_TIERS = {"BLOCKED", "ESTIMATED", "CALCULABLE", "VERIFIED"}
VALID_FRESHNESS = {"CURRENT", "MARKET_CLOSED_CURRENT", "STALE_LAST_GOOD"}


class V2ContractError(ValueError):
    pass


@dataclass(frozen=True)
class V2CanarySummary:
    company_count: int
    scored_company_ids: tuple[str, ...]
    tier_counts: Mapping[str, int]

    def public_dict(self) -> dict[str, Any]:
        return {
            "company_count": self.company_count,
            "scored_company_count": len(self.scored_company_ids),
            "scored_company_ids": list(self.scored_company_ids),
            "tier_counts": dict(self.tier_counts),
        }


def _contains_non_finite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, str) and value.strip().lower() in {
        "nan",
        "+nan",
        "-nan",
        "inf",
        "+inf",
        "-inf",
        "infinity",
        "+infinity",
        "-infinity",
    }:
        return True
    if isinstance(value, Mapping):
        return any(_contains_non_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite(item) for item in value)
    return False


def _score_value(record: Any, *, company_id: str, score_id: str) -> Decimal | None:
    if not isinstance(record, Mapping) or record.get("value") is None:
        return None
    try:
        value = Decimal(str(record["value"]))
    except (InvalidOperation, ValueError) as error:
        raise V2ContractError(f"invalid score {company_id}.{score_id}") from error
    if not value.is_finite() or not Decimal("0") <= value <= Decimal("100"):
        raise V2ContractError(f"score outside 0..100: {company_id}.{score_id}")
    return value


def validate_public_index(payload: Mapping[str, Any]) -> V2CanarySummary:
    if payload.get("schema_version") != "shareholder-return-v2":
        raise V2ContractError("unsupported v2 schema")
    if payload.get("calculation_version") != CALCULATION_VERSION:
        raise V2ContractError("unsupported v2 calculation version")
    if payload.get("metric_definition_version") != CALCULATION_VERSION:
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
    tier_counts: dict[str, int] = {}
    for row in companies:
        if not isinstance(row, Mapping):
            raise V2ContractError("company record must be an object")
        company_id = str(row.get("company_id") or "")
        if not company_id or company_id in identifiers:
            raise V2ContractError("company IDs must be present and unique")
        identifiers.add(company_id)
        tier = str(row.get("data_tier") or "")
        if tier not in VALID_TIERS:
            raise V2ContractError(f"invalid data tier: {company_id}")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        if row.get("freshness") not in VALID_FRESHNESS:
            raise V2ContractError(f"invalid freshness: {company_id}")
        confidence = row.get("data_confidence")
        if not isinstance(confidence, Mapping):
            raise V2ContractError(f"data_confidence must be an object: {company_id}")
        confidence_value = confidence.get("value")
        if (
            not isinstance(confidence_value, int)
            or isinstance(confidence_value, bool)
            or not 0 <= confidence_value <= 100
        ):
            raise V2ContractError(f"invalid data confidence: {company_id}")
        for object_key in (
            "metrics",
            "metric_bases",
            "selected_input_plan",
            "source_summary",
        ):
            if not isinstance(row.get(object_key), Mapping):
                raise V2ContractError(f"{object_key} must be an object: {company_id}")
        for list_key in ("warnings", "blockers"):
            if not isinstance(row.get(list_key), list):
                raise V2ContractError(f"{list_key} must be an array: {company_id}")
        scores = row.get("scores")
        if not isinstance(scores, Mapping):
            raise V2ContractError(f"scores must be an object: {company_id}")
        if tier == "BLOCKED" and scores:
            raise V2ContractError(f"BLOCKED company exposes scores: {company_id}")
        for score_id, record in scores.items():
            _score_value(record, company_id=company_id, score_id=str(score_id))
        ri = _score_value(
            scores.get("recommendation_index"),
            company_id=company_id,
            score_id="recommendation_index",
        )
        eri = _score_value(
            scores.get("entry_risk_index"),
            company_id=company_id,
            score_id="entry_risk_index",
        )
        if (ri is None) != (eri is None):
            raise V2ContractError(f"RI and ERI must publish together: {company_id}")
        if ri is not None:
            if tier == "BLOCKED":
                raise V2ContractError(f"BLOCKED company is scored: {company_id}")
            scored.append(company_id)
        if _contains_non_finite(row):
            raise V2ContractError(f"non-finite value in company record: {company_id}")
    return V2CanarySummary(
        company_count=len(companies),
        scored_company_ids=tuple(sorted(scored)),
        tier_counts=dict(sorted(tier_counts.items())),
    )


def validate_activation_canary(
    payload: Mapping[str, Any],
    *,
    approved_company_ids: Iterable[str],
    expected_company_count: int = 67,
    minimum_scored_companies: int = 5,
) -> V2CanarySummary:
    summary = validate_public_index(payload)
    if summary.company_count != expected_company_count:
        raise V2ContractError(
            f"activation requires {expected_company_count} companies, got {summary.company_count}"
        )
    if len(summary.scored_company_ids) < minimum_scored_companies:
        raise V2ContractError(
            f"activation requires {minimum_scored_companies} scored companies, got {len(summary.scored_company_ids)}"
        )
    approved = {str(item) for item in approved_company_ids}
    unreviewed = sorted(set(summary.scored_company_ids) - approved)
    if unreviewed:
        raise V2ContractError(
            "scored companies lack manual activation approval: " + ",".join(unreviewed)
        )
    return summary
