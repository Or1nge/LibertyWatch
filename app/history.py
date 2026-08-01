"""Strict validation for the separately refreshed weekly history document."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from math import isfinite
from typing import Any, Mapping


class HistoryError(ValueError):
    """Raised when the weekly history file violates its contract."""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) and number > 0 else None


def normalize_history_document(
    raw_value: Any,
    expected: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """Validate an atomic history file against the live watchlist universe."""

    if not isinstance(raw_value, Mapping):
        raise HistoryError("周线历史根节点必须是对象")
    if raw_value.get("schemaVersion") != 1:
        raise HistoryError("周线历史 schemaVersion 必须是 1")
    if raw_value.get("frequency") != "weekly":
        raise HistoryError("周线历史 frequency 必须是 weekly")
    if raw_value.get("adjustment") != "qfq":
        raise HistoryError("周线历史 adjustment 必须是 qfq")
    if raw_value.get("windowYears") != 10:
        raise HistoryError("周线历史 windowYears 必须是 10")

    security_ids = raw_value.get("securityIds")
    if not isinstance(security_ids, list) or not security_ids:
        raise HistoryError("周线历史 securityIds 必须是非空数组")
    if len(set(security_ids)) != len(security_ids):
        raise HistoryError("周线历史 securityIds 不能重复")
    expected_ids = list(expected)
    selected_ids = set(security_ids)
    if not selected_ids <= set(expected_ids):
        raise HistoryError("周线历史包含正式观察清单以外的证券")
    if security_ids != [
        security_id
        for security_id in expected_ids
        if security_id in selected_ids
    ]:
        raise HistoryError("周线历史证券顺序与正式观察清单不匹配")
    raw_securities = raw_value.get("securities")
    if not isinstance(raw_securities, Mapping):
        raise HistoryError("周线历史 securities 必须是对象")
    if set(raw_securities) != selected_ids:
        raise HistoryError("周线历史证券集合与 securityIds 不匹配")

    securities: dict[str, dict[str, Any]] = {}
    for security_id in security_ids:
        expected_security = expected[security_id]
        raw = raw_securities.get(security_id)
        if not isinstance(raw, Mapping):
            raise HistoryError(f"{security_id} 周线记录必须是对象")
        if raw.get("quoteCode") != expected_security["quoteCode"]:
            raise HistoryError(f"{security_id} quoteCode 不匹配")
        if raw.get("currency") != expected_security["currency"]:
            raise HistoryError(f"{security_id} currency 不匹配")
        if (
            raw.get("frequency") != "weekly"
            or raw.get("adjustment") != "qfq"
            or raw.get("windowYears") != 10
        ):
            raise HistoryError(f"{security_id} 周线口径不匹配")

        raw_points = raw.get("points")
        if not isinstance(raw_points, list) or not 2 <= len(raw_points) <= 600:
            raise HistoryError(f"{security_id} 周线点数必须在 2..600")
        points: list[dict[str, Any]] = []
        previous: date | None = None
        for index, raw_point in enumerate(raw_points):
            if not isinstance(raw_point, Mapping):
                raise HistoryError(f"{security_id} points[{index}] 必须是对象")
            timestamp = _text(raw_point.get("timestamp"))
            try:
                parsed = date.fromisoformat(timestamp)
            except ValueError as error:
                raise HistoryError(
                    f"{security_id} points[{index}] 日期无效"
                ) from error
            if previous is not None and parsed <= previous:
                raise HistoryError(f"{security_id} 周线日期必须严格递增")
            previous = parsed
            price = _positive(raw_point.get("price"))
            if price is None:
                raise HistoryError(f"{security_id} points[{index}] 价格无效")
            points.append(
                {
                    "timestamp": timestamp,
                    "label": _text(raw_point.get("label")) or timestamp[:7],
                    "price": round(price, 4),
                }
            )

        if raw.get("pointCount") != len(points):
            raise HistoryError(f"{security_id} pointCount 不匹配")
        if raw.get("asOf") != points[-1]["timestamp"]:
            raise HistoryError(f"{security_id} asOf 不匹配")
        securities[security_id] = {
            "quoteCode": expected_security["quoteCode"],
            "currency": expected_security["currency"],
            "frequency": "weekly",
            "adjustment": "qfq",
            "windowYears": 10,
            "asOf": points[-1]["timestamp"],
            "pointCount": len(points),
            "points": points,
        }

    return {
        "schemaVersion": 1,
        "generatedAt": _text(raw_value.get("generatedAt")) or None,
        "provider": _text(raw_value.get("provider")),
        "frequency": "weekly",
        "adjustment": "qfq",
        "windowYears": 10,
        "windowStart": _text(raw_value.get("windowStart")) or None,
        "windowEnd": _text(raw_value.get("windowEnd")) or None,
        "securityIds": list(security_ids),
        "securities": deepcopy(securities),
    }
