"""Typed access to the versioned calculation policy.

Business thresholds live in ``config/metric_policy_v2.json``.  Domain modules
import these immutable values so routes, templates and workers cannot grow
independent copies of the policy.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Any, Mapping, Sequence

from .registry import load_policy


@lru_cache(maxsize=1)
def policy() -> dict[str, Any]:
    return load_policy()


def section(name: str) -> Mapping[str, Any]:
    value = policy().get(name)
    if not isinstance(value, Mapping):
        raise ValueError(f"policy section is missing: {name}")
    return value


def decimal_value(*path: str) -> Decimal:
    value: Any = policy()
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            raise ValueError(f"policy value is missing: {'.'.join(path)}")
        value = value[key]
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"policy value is not finite: {'.'.join(path)}")
    return result


def integer_value(*path: str) -> int:
    value: Any = policy()
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            raise ValueError(f"policy value is missing: {'.'.join(path)}")
        value = value[key]
    return int(value)


def decimal_mapping(*path: str) -> dict[str, Decimal]:
    value: Any = policy()
    for key in path:
        value = value[key]
    if not isinstance(value, Mapping):
        raise ValueError(f"policy mapping is missing: {'.'.join(path)}")
    return {str(key): Decimal(str(item)) for key, item in value.items()}


def decimal_sequence(*path: str) -> Sequence[Decimal]:
    value: Any = policy()
    for key in path:
        value = value[key]
    if not isinstance(value, list):
        raise ValueError(f"policy list is missing: {'.'.join(path)}")
    return tuple(Decimal(str(item)) for item in value)
