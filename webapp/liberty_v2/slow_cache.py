from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .analysis.prompt_renderer import canonical_json_bytes
from .constants import CALCULATION_VERSION
from .assessment import CompanyAssessment
from .models import CoverageResult, CoverageStatus, VetoFlag, jsonable
from .pipeline import (
    SlowVariables,
    V21SlowVariables,
    compute_slow_variables,
    compute_slow_variables_v21,
)
from .snapshot_store import atomic_write_json


SLOW_INPUT_KEYS = (
    "industry_kind",
    "annual_distributions",
    "coverage",
    "organic_growth_metric",
    "organic_growth_series",
    "structured_scores",
    "reviewed_overlay_scores",
    "risk_scores",
    "veto_inputs",
    "balance_sheet",
    "balance_sheet_history",
    "has_material_dilution",
)
V21_SLOW_CACHE_SCHEMA_VERSION = 2


def slow_input_hash(raw: Mapping[str, Any], on_date: date) -> str:
    raw_points = [
        item
        for item in raw.get("raw_data_points", [])
        if isinstance(item, Mapping)
    ]
    non_market_points = sorted(
        [
        item
        for item in raw_points
        if not str(item.get("field_id") or "").startswith("MARKET.")
        ],
        key=lambda item: (
            str(item.get("field_id") or ""),
            str(item.get("source_document") or ""),
        ),
    )
    market_contract = sorted(
        [
        {
            key: item.get(key)
            for key in (
                "field_id",
                "company_id",
                "security_id",
                "share_class",
                "source_name",
                "currency",
                "unit",
                "data_status",
                "restatement_status",
            )
        }
        for item in raw_points
        if str(item.get("field_id") or "").startswith("MARKET.")
        ],
        key=lambda item: str(item.get("field_id") or ""),
    )
    payload = {
        "calculation_version": CALCULATION_VERSION,
        "on_date": on_date.isoformat(),
        **{key: raw.get(key) for key in SLOW_INPUT_KEYS},
        "raw_data_points": non_market_points,
        "market_provenance_contract": market_contract,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _decimal(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _serialize(slow: SlowVariables, input_hash: str) -> dict[str, Any]:
    return {
        "calculation_version": CALCULATION_VERSION,
        "slow_input_hash": input_hash,
        "annual_effective_distributions": [
            [year, format(value, "f")]
            for year, value in slow.annual_effective_distributions
        ],
        "q_b": format(slow.q_b, "f") if slow.q_b is not None else None,
        "r2": format(slow.r2, "f") if slow.r2 is not None else None,
        "m5": format(slow.m5, "f") if slow.m5 is not None else None,
        "t10": format(slow.t10, "f") if slow.t10 is not None else None,
        "historical_distribution": (
            format(slow.historical_distribution, "f")
            if slow.historical_distribution is not None
            else None
        ),
        "coverage": {
            "status": slow.coverage.status.value,
            "adapter": slow.coverage.adapter,
            "sustainable_distribution": (
                format(slow.coverage.sustainable_distribution, "f")
                if slow.coverage.sustainable_distribution is not None
                else None
            ),
            "coverage_ratio": (
                format(slow.coverage.coverage_ratio, "f")
                if slow.coverage.coverage_ratio is not None
                else None
            ),
            "capacity": (
                format(slow.coverage.capacity, "f")
                if slow.coverage.capacity is not None
                else None
            ),
            "caveats": list(slow.coverage.caveats),
            "required_missing_fields": list(slow.coverage.required_missing_fields),
        },
        "organic_growth": format(slow.organic_growth, "f") if slow.organic_growth is not None else None,
        "conservative_growth": format(slow.conservative_growth, "f") if slow.conservative_growth is not None else None,
        "payout_quality": format(slow.payout_quality, "f") if slow.payout_quality is not None else None,
        "eri": format(slow.eri, "f") if slow.eri is not None else None,
        "veto_flags": [
            {
                "code": flag.code,
                "severity": flag.severity,
                "triggered": flag.triggered,
                "evidence_fields": list(flag.evidence_fields),
                "message_zh": flag.message_zh,
                "source": flag.source,
                "as_of_date": flag.as_of_date.isoformat() if flag.as_of_date else None,
            }
            for flag in slow.veto_flags
        ],
        "business_durability": (
            format(slow.business_durability, "f")
            if slow.business_durability is not None
            else None
        ),
        "governance": format(slow.governance, "f") if slow.governance is not None else None,
        "qualitative_overlay_pending": slow.qualitative_overlay_pending,
        "errors": list(slow.errors),
    }


def _deserialize(value: Mapping[str, Any]) -> SlowVariables:
    coverage = value["coverage"]
    return SlowVariables(
        annual_effective_distributions=tuple(
            (int(year), Decimal(amount))
            for year, amount in value["annual_effective_distributions"]
        ),
        q_b=_decimal(value.get("q_b")),
        r2=_decimal(value.get("r2")),
        m5=_decimal(value.get("m5")),
        t10=_decimal(value.get("t10")),
        historical_distribution=_decimal(value.get("historical_distribution")),
        coverage=CoverageResult(
            status=CoverageStatus(coverage["status"]),
            adapter=str(coverage["adapter"]),
            sustainable_distribution=_decimal(coverage.get("sustainable_distribution")),
            coverage_ratio=_decimal(coverage.get("coverage_ratio")),
            capacity=_decimal(coverage.get("capacity")),
            caveats=tuple(coverage.get("caveats", [])),
            required_missing_fields=tuple(coverage.get("required_missing_fields", [])),
        ),
        organic_growth=_decimal(value.get("organic_growth")),
        conservative_growth=_decimal(value.get("conservative_growth")),
        payout_quality=_decimal(value.get("payout_quality")),
        eri=_decimal(value.get("eri")),
        veto_flags=tuple(
            VetoFlag(
                code=str(item["code"]),
                severity=str(item["severity"]),
                triggered=bool(item["triggered"]),
                evidence_fields=tuple(item.get("evidence_fields", [])),
                message_zh=str(item["message_zh"]),
                source=item.get("source"),
                as_of_date=date.fromisoformat(item["as_of_date"])
                if item.get("as_of_date")
                else None,
            )
            for item in value.get("veto_flags", [])
        ),
        business_durability=_decimal(value.get("business_durability")),
        governance=_decimal(value.get("governance")),
        qualitative_overlay_pending=bool(value.get("qualitative_overlay_pending")),
        errors=tuple(value.get("errors", [])),
    )


def load_or_compute_slow(
    raw: Mapping[str, Any],
    cache_path: Path,
    *,
    on_date: date,
    force: bool = False,
) -> tuple[SlowVariables, bool]:
    digest = slow_input_hash(raw, on_date)
    if not force and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                cached.get("calculation_version") == CALCULATION_VERSION
                and cached.get("slow_input_hash") == digest
            ):
                return _deserialize(cached), True
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass
    slow = compute_slow_variables(raw, on_date=on_date)
    atomic_write_json(cache_path, _serialize(slow, digest))
    return slow, False


def _serialize_v21(slow: V21SlowVariables, input_hash: str) -> dict[str, Any]:
    return {
        "calculation_version": CALCULATION_VERSION,
        "slow_input_hash": input_hash,
        "v21": True,
        "v21_slow_cache_schema_version": V21_SLOW_CACHE_SCHEMA_VERSION,
        "distribution_history": jsonable(slow.distribution_history),
        "annual_effective_distributions": jsonable(slow.annual_effective_distributions),
        "r2": jsonable(slow.r2),
        "m5": jsonable(slow.m5),
        "t10": jsonable(slow.t10),
        "historical_distribution": jsonable(slow.historical_distribution),
        "fcf_history": jsonable(slow.fcf_history),
        "simplified_fcf": slow.simplified_fcf,
        "fcf_capacity": jsonable(slow.fcf_capacity),
        "sustainable_distribution": jsonable(slow.sustainable_distribution),
        "coverage_ratio": jsonable(slow.coverage_ratio),
        "organic_growth": jsonable(slow.organic_growth),
        "conservative_growth": jsonable(slow.conservative_growth),
        "coverage_component": jsonable(slow.coverage_component),
        "trend_component": jsonable(slow.trend_component),
        "stability_component": jsonable(slow.stability_component),
        "balance_component": jsonable(slow.balance_component),
        "payout_quality": jsonable(slow.payout_quality),
        "business_durability": jsonable(slow.business_durability),
        "governance": jsonable(slow.governance),
        "entry_risk_index": jsonable(slow.entry_risk_index),
        "risk_components": jsonable(slow.risk_components),
        "veto_flags": jsonable(slow.veto_flags),
        "unknown_veto_uplift": jsonable(slow.unknown_veto_uplift),
        "warnings": list(slow.warnings),
        "errors": list(slow.errors),
    }


def _deserialize_v21(value: Mapping[str, Any]) -> V21SlowVariables:
    def d(key: str) -> Decimal | None:
        return _decimal(value.get(key))

    return V21SlowVariables(
        distribution_history=tuple(value.get("distribution_history", [])),
        annual_effective_distributions=tuple(
            (int(year), Decimal(amount))
            for year, amount in value.get("annual_effective_distributions", [])
        ),
        r2=d("r2"),
        m5=d("m5"),
        t10=d("t10"),
        historical_distribution=d("historical_distribution"),
        fcf_history=tuple(
            (int(year), Decimal(amount))
            for year, amount in value.get("fcf_history", [])
        ),
        simplified_fcf=bool(value.get("simplified_fcf")),
        fcf_capacity=d("fcf_capacity"),
        sustainable_distribution=d("sustainable_distribution"),
        coverage_ratio=d("coverage_ratio"),
        organic_growth=d("organic_growth"),
        conservative_growth=d("conservative_growth") or Decimal("0"),
        coverage_component=d("coverage_component"),
        trend_component=d("trend_component"),
        stability_component=d("stability_component"),
        balance_component=d("balance_component"),
        payout_quality=d("payout_quality"),
        business_durability=d("business_durability"),
        governance=d("governance"),
        entry_risk_index=d("entry_risk_index"),
        risk_components={
            str(key): Decimal(str(item))
            for key, item in value.get("risk_components", {}).items()
        },
        veto_flags=tuple(
            VetoFlag(
                code=str(item["code"]),
                severity=str(item["severity"]),
                triggered=bool(item["triggered"]),
                evidence_fields=tuple(item.get("evidence_fields", [])),
                message_zh=str(item["message_zh"]),
                source=item.get("source"),
                as_of_date=date.fromisoformat(item["as_of_date"])
                if item.get("as_of_date")
                else None,
            )
            for item in value.get("veto_flags", [])
        ),
        unknown_veto_uplift=d("unknown_veto_uplift") or Decimal("0"),
        warnings=tuple(value.get("warnings", [])),
        errors=tuple(value.get("errors", [])),
    )


def load_or_compute_slow_v21(
    raw: Mapping[str, Any],
    assessment: CompanyAssessment,
    cache_path: Path,
    *,
    on_date: date,
    force: bool = False,
) -> tuple[V21SlowVariables, bool]:
    digest = slow_input_hash(raw, on_date)
    if not force and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                cached.get("v21") is True
                and cached.get("v21_slow_cache_schema_version")
                == V21_SLOW_CACHE_SCHEMA_VERSION
                and cached.get("calculation_version") == CALCULATION_VERSION
                and cached.get("slow_input_hash") == digest
            ):
                return _deserialize_v21(cached), True
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass
    slow = compute_slow_variables_v21(raw, assessment, on_date=on_date)
    atomic_write_json(cache_path, _serialize_v21(slow, digest))
    return slow, False
