from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .constants import (
    CALCULATION_VERSION,
    METRIC_DEFINITION_VERSION,
    OUTPUT_SCHEMA_VERSION,
    PROMPT_VERSION,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEFINITIONS = PROJECT_ROOT / "config" / "metric_definitions_v2.json"
DEFAULT_POLICY = PROJECT_ROOT / "config" / "metric_policy_v2.json"
REQUIRED_DEFINITION_FIELDS = {
    "id",
    "label_zh",
    "short_label_zh",
    "category",
    "version",
    "unit",
    "direction",
    "formula_symbolic",
    "formula_plain_zh",
    "simple_interpretation_zh",
    "good_range_zh",
    "warning_range_zh",
    "data_window_zh",
    "applicability",
    "caveats_zh",
    "required_fields",
    "source_level",
}


class RegistryError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RegistryError(f"cannot load registry {path}: {error}") from error
    if not isinstance(value, dict):
        raise RegistryError(f"registry root must be an object: {path}")
    return value


def load_metric_definitions(path: Path = DEFAULT_DEFINITIONS) -> dict[str, Any]:
    payload = _read_json(path)
    if payload.get("definition_version") != METRIC_DEFINITION_VERSION:
        raise RegistryError("metric definition version does not match code")
    if payload.get("calculation_version") != CALCULATION_VERSION:
        raise RegistryError("metric calculation version does not match code")
    metrics = payload.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise RegistryError("metric definitions must contain a non-empty list")
    seen: set[str] = set()
    for index, metric in enumerate(metrics):
        if not isinstance(metric, dict):
            raise RegistryError(f"metric #{index} must be an object")
        missing = REQUIRED_DEFINITION_FIELDS - set(metric)
        extra = set(metric) - REQUIRED_DEFINITION_FIELDS
        if missing or extra:
            raise RegistryError(
                f"metric #{index} fields mismatch: missing={sorted(missing)} extra={sorted(extra)}"
            )
        metric_id = metric["id"]
        if not isinstance(metric_id, str) or not metric_id or metric_id in seen:
            raise RegistryError(f"invalid or duplicate metric id: {metric_id!r}")
        seen.add(metric_id)
        for list_key in ("applicability", "caveats_zh", "required_fields"):
            if not isinstance(metric[list_key], list):
                raise RegistryError(f"{metric_id}.{list_key} must be an array")
    return payload


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    payload = _read_json(path)
    if payload.get("calculation_version") != CALCULATION_VERSION:
        raise RegistryError("policy calculation version does not match code")
    if payload.get("metric_definition_version") != METRIC_DEFINITION_VERSION:
        raise RegistryError("policy definition version does not match code")
    if payload.get("prompt_version") != PROMPT_VERSION:
        raise RegistryError("policy prompt version does not match code")
    if payload.get("output_schema_version") != OUTPUT_SCHEMA_VERSION:
        raise RegistryError("policy output schema version does not match code")
    codex = payload.get("codex")
    if not isinstance(codex, dict):
        raise RegistryError("policy.codex must be an object")
    if codex.get("model") != "gpt-5.6-sol" or codex.get("reasoning_effort") != "xhigh":
        raise RegistryError("runtime model policy may not silently downgrade")
    return payload


def definitions_by_id(payload: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    source = payload or load_metric_definitions()
    return {item["id"]: item for item in source["metrics"]}
