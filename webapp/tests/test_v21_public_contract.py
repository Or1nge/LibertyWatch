from __future__ import annotations

from copy import deepcopy

import pytest

from app.v2_contract import (
    V2ContractError,
    validate_activation_canary,
    validate_public_index,
)


def score(value: str) -> dict[str, str | None]:
    return {
        "value": value,
        "status": "VALID",
        "display": value,
        "reason": None,
        "unit": "score_0_100",
        "basis": "DERIVED",
        "warning": None,
    }


def index(*, scored: int = 0) -> dict:
    companies = []
    for position in range(67):
        is_scored = position < scored
        companies.append(
            {
                "company_id": f"issuer-{position:02d}",
                "data_tier": "ESTIMATED" if is_scored else "BLOCKED",
                "data_confidence": {"value": 50, "domains": {}, "caveats": []},
                "freshness": "CURRENT",
                "warnings": [],
                "blockers": [] if is_scored else ["DATA_GAP"],
                "metrics": {},
                "metric_bases": {},
                "selected_input_plan": {},
                "source_summary": {},
                "scores": (
                    {
                        "recommendation_index": score("70"),
                        "entry_risk_index": score("35"),
                    }
                    if is_scored
                    else {}
                ),
            }
        )
    return {
        "schema_version": "shareholder-return-v2",
        "calculation_version": "shareholder-return-v2.1.0",
        "metric_definition_version": "shareholder-return-v2.1.0",
        "release_validity": "VALID_RELEASE",
        "company_count": len(companies),
        "companies": companies,
    }


def test_public_contract_accepts_mixed_tiers_but_keeps_blocked_scores_empty() -> None:
    summary = validate_public_index(index(scored=5))
    assert summary.company_count == 67
    assert len(summary.scored_company_ids) == 5
    assert summary.tier_counts == {"BLOCKED": 62, "ESTIMATED": 5}

    invalid = deepcopy(index(scored=0))
    invalid["companies"][0]["scores"] = {"recommendation_index": score("70")}
    with pytest.raises(V2ContractError, match="BLOCKED company exposes scores"):
        validate_public_index(invalid)


def test_activation_requires_five_scored_and_manual_approval_for_each() -> None:
    with pytest.raises(V2ContractError, match="requires 5 scored companies"):
        validate_activation_canary(index(scored=0), approved_company_ids=[])

    payload = index(scored=5)
    with pytest.raises(V2ContractError, match="lack manual activation approval"):
        validate_activation_canary(payload, approved_company_ids=[])
    approved = [f"issuer-{position:02d}" for position in range(5)]
    summary = validate_activation_canary(payload, approved_company_ids=approved)
    assert summary.scored_company_ids == tuple(approved)


def test_public_contract_rejects_out_of_range_or_non_finite_scores() -> None:
    for bad in ("101", "NaN", "Infinity"):
        payload = index(scored=1)
        payload["companies"][0]["scores"]["recommendation_index"]["value"] = bad
        with pytest.raises(V2ContractError):
            validate_public_index(payload)
