"""Pure watchlist normalization and derived-value logic.

The module deliberately treats missing market or research inputs as ``None``.
It never substitutes a numeric zero for data that was not supplied.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from math import isfinite
from statistics import median
from typing import Any, Mapping


DEFAULT_REFRESH_INTERVAL_MS = 60_000

FUTU_SNAPSHOT_METRIC_KEYS = (
    "pe",
    "peTtm",
    "pb",
    "dividendYieldTtmPct",
    "totalMarketValue",
    "earningsPerShare",
    "bookValuePerShare",
)

TARGET_KEYS = (
    ("watch", "关注价"),
    ("preferred", "理想价"),
    ("deep", "深度价值价"),
)

SECTOR_SIGNAL_KEYS = (
    "return1dPct",
    "return5dPct",
    "return20dPct",
    "advancers5dPct",
    "aboveMa20Pct",
)

SECTOR_HEAT_WEIGHTS = {
    "return1dPct": 0.20,
    "return5dPct": 0.30,
    "return20dPct": 0.30,
    "advancers5dPct": 0.10,
    "aboveMa20Pct": 0.10,
}

VALUATION_SCORES = {
    "deeply_attractive": 10.0,
    "deeply-undervalued": 10.0,
    "深度低估": 10.0,
    "attractive": 8.0,
    "undervalued": 8.0,
    "低估": 8.0,
    "fair": 5.0,
    "合理": 5.0,
    "expensive": 1.0,
    "overvalued": 1.0,
    "高估": 1.0,
}


OPPORTUNITY_METHODOLOGY = {
    "priceAttractivenessWeight": 55,
    "technicalWeight": 30,
    "sectorHeatWeight": 15,
    "targetDistance": (
        "distanceToPreferredPct = "
        "(currentPrice - preferredPrice) / preferredPrice × 100。"
    ),
    "targetBuckets": {
        "reached": "d ≤ 0",
        "within3": "0 < d ≤ 3",
        "within10": "3 < d ≤ 10",
        "far": "d > 10",
    },
    "targetAttractiveness": (
        "TargetAttractiveness = clamp(100 - max(d, 0) × 5, 0, 100)。"
        "它只描述现价与 4% 股息回购率理想目标价的距离。"
    ),
    "generalPriceOpportunity": (
        "通用价格机会只要求有效现价和理想价；股息率与估值标签只作旁注，"
        "不改变价格接近程度。"
    ),
    "sectorHeat": {
        "name": "观察池行业热度",
        "eligibility": "至少 3 个独立发行人且历史覆盖率 ≥ 80%，五项输入均完整。",
        "formula": (
            "Heat = 100 × (0.20×P(R1) + 0.30×P(R5) + "
            "0.30×P(R20) + 0.10×P(Breadth5) + 0.10×P(AboveMA20))。"
        ),
        "note": (
            "P 为合格行业之间的百分位；单日涨跌和当日上涨宽度仅作展示，"
            "不替代历史行业热度。"
        ),
    },
    "technical": {
        "requiredInputs": (
            "RSI14、60 日回撤、相对行业 5 日收益和至少 60 个有效交易日。"
        ),
        "formula": (
            "Technical = 100 × [0.40×clamp((50-RSI14)/20) + "
            "0.35×clamp((-Drawdown60-5%)/20%) + "
            "0.25×clamp((-RelativeReturn5-2%)/8%)]。"
        ),
        "hotSectorDislocation": (
            "Heat ≥ 60、d ≤ 10%、相对行业 5 日收益 ≤ -3 个百分点，"
            "且 RSI14 ≤ 40 或 60 日回撤 ≤ -10%；人工暂停时不入选。"
        ),
        "opportunityFormula": (
            "OpportunityTechnical = 0.55×TargetAttractiveness + "
            "0.30×Technical + 0.15×SectorHeat。"
        ),
    },
    "note": (
        "所有分数只用于观察清单内的机械筛选和排序，不构成投资建议；"
        "所需输入缺失时返回 null。"
    ),
}


class ConfigError(ValueError):
    """Raised when a watchlist or runtime snapshot violates the contract."""


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return min(maximum, max(minimum, value))


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _positive_number(value: Any) -> float | None:
    number = _finite_number(value)
    return number if number is not None and number > 0 else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None or not isfinite(value):
        return None
    return round(value, digits)


def _text(value: Any, fallback: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _text(item))]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _sanitize_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, float) and not isfinite(value):
        return None
    return deepcopy(value)


def _normalize_metrics(value: Any) -> dict[str, Any]:
    raw = _mapping(value)
    result = _sanitize_json(dict(raw))
    for key in FUTU_SNAPSHOT_METRIC_KEYS:
        if key in raw:
            result[key] = _finite_number(raw.get(key))
    return result


def _normalize_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for index, point in enumerate(value):
        raw = _mapping(point)
        price = _positive_number(raw.get("price"))
        if price is None:
            continue
        result.append(
            {
                "label": _text(raw.get("label"), f"T-{len(value) - index - 1}"),
                "timestamp": _text(raw.get("timestamp")) or None,
                "price": price,
            }
        )
    return result


def _normalize_target_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for index, entry in enumerate(value):
        raw = _mapping(entry)
        result.append(
            {
                "label": _text(raw.get("label"), f"修订 {index + 1}"),
                "preferredPrice": _positive_number(raw.get("preferredPrice")),
                "watchPrice": _positive_number(raw.get("watchPrice")),
                "deepPrice": _positive_number(raw.get("deepPrice")),
                "reason": _text(raw.get("reason")),
                "changedAt": _text(raw.get("changedAt")) or None,
            }
        )
    return result


def _normalize_technical(raw_security: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = _mapping(
        raw_security.get("technicalIndicators", raw_security.get("technical"))
    )
    rsi14 = _finite_number(raw.get("rsi14"))
    drawdown = _finite_number(
        raw.get("drawdown60dPct", raw.get("drawdown60Pct"))
    )
    relative_return = _finite_number(
        raw.get("relativeSector5dPct", raw.get("relativeReturn5dPct"))
    )
    history_days = _integer(
        raw.get("historyTradingDays", raw_security.get("historyTradingDays"))
    )
    if (
        rsi14 is None
        or not 0 <= rsi14 <= 100
        or drawdown is None
        or relative_return is None
        or history_days is None
        or history_days < 0
    ):
        return None
    return {
        "rsi14": rsi14,
        "drawdown60dPct": drawdown,
        "relativeSector5dPct": relative_return,
        "historyTradingDays": history_days,
        "asOf": _text(raw.get("asOf")) or None,
    }


def _normalize_security(raw_value: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw_value, Mapping):
        raise ConfigError(f"securities[{index}] 必须是对象")
    raw = raw_value
    security_id = _text(raw.get("id"))
    if not security_id:
        raise ConfigError(f"securities[{index}].id 不能为空")

    quote = _mapping(raw.get("quote"))
    targets = _mapping(raw.get("targetPrices"))
    yield_basis = _mapping(raw.get("yieldBasis"))
    annual_average_cny = _positive_number(
        yield_basis.get("annualAveragePerShareCny")
    )
    window_years = _integer(yield_basis.get("windowYears"))
    start_year = _integer(yield_basis.get("startYear"))
    end_year = _integer(yield_basis.get("endYear"))

    return {
        "id": security_id,
        "issuerId": _text(raw.get("issuerId"), security_id),
        "quoteCode": _text(raw.get("quoteCode")) or None,
        "name": _text(raw.get("name"), security_id),
        "ticker": _text(raw.get("ticker")),
        "market": _text(raw.get("market"), "UNKNOWN").upper(),
        "currency": _text(raw.get("currency")),
        "sector": _text(raw.get("sector"), "未分类"),
        "sectorId": _text(
            raw.get("sectorId"), _text(raw.get("sector"), "未分类")
        ),
        "industry": _text(raw.get("industry"), "未分类"),
        "quote": {
            "currentPrice": _positive_number(
                quote.get("currentPrice", raw.get("currentPrice"))
            ),
            "currentPriceCny": _positive_number(
                quote.get("currentPriceCny", raw.get("currentPriceCny"))
            ),
            "dailyChangePct": _finite_number(
                quote.get("dailyChangePct", raw.get("dailyChangePct"))
            ),
            "marketState": _text(
                quote.get("marketState"), "unknown"
            ).lower(),
            "lastUpdatedAt": _text(quote.get("lastUpdatedAt")) or None,
            "status": _text(quote.get("status"), "unavailable").lower(),
        },
        "targetPrices": {
            key: _positive_number(targets.get(key)) for key, _ in TARGET_KEYS
        },
        "targetPricesCny": {
            key: None for key, _ in TARGET_KEYS
        },
        "yieldBasis": {
            "annualAveragePerShareCny": annual_average_cny,
            "windowYears": (
                window_years
                if window_years is not None and window_years > 0
                else None
            ),
            "startYear": start_year,
            "endYear": end_year,
            "method": _text(yield_basis.get("method")),
        },
        "currentShareholderYieldPct": None,
        "expectedDividendYieldPct": _finite_number(
            raw.get("expectedDividendYieldPct")
        ),
        "valuationStatus": _text(raw.get("valuationStatus"), "unconfigured"),
        "metrics": _normalize_metrics(raw.get("metrics")),
        "investmentThesis": _text_list(raw.get("investmentThesis")),
        "risks": _text_list(raw.get("risks")),
        "notes": _text(raw.get("notes")),
        "targetRevisionHistory": _normalize_target_history(
            raw.get("targetRevisionHistory")
        ),
        "history": _normalize_history(raw.get("history")),
        "technicalIndicators": _normalize_technical(raw),
        "recommendationPaused": raw.get("recommendationPaused") is True,
    }


def _normalize_sector_signals(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    records: dict[str, Mapping[str, Any]] = {}
    if isinstance(value, Mapping):
        records = {
            str(sector): _mapping(signal) for sector, signal in value.items()
        }
    elif isinstance(value, list):
        for item in value:
            raw = _mapping(item)
            sector = _text(raw.get("sector"))
            if sector:
                records[sector] = raw
    else:
        raise ConfigError("sectorSignals 必须是对象或数组")

    result: dict[str, dict[str, Any]] = {}
    for sector, raw in records.items():
        result[_text(sector, "未分类")] = {
            key: _finite_number(raw.get(key)) for key in SECTOR_SIGNAL_KEYS
        } | {
            "issuerCount": _integer(
                raw.get("issuerCount", raw.get("independentIssuerCount"))
            ),
            "coveredIssuerCount": _integer(raw.get("coveredIssuerCount")),
            "coveragePct": _finite_number(raw.get("coveragePct")),
            "asOf": _text(raw.get("asOf")) or None,
        }
    return result


def normalize_config(raw_value: Any, source: str = "watchlist") -> dict[str, Any]:
    """Validate and normalize a complete config/snapshot document."""

    if not isinstance(raw_value, Mapping):
        raise ConfigError(f"{source}: 根节点必须是对象")
    raw = raw_value
    if not isinstance(raw.get("securities"), list):
        raise ConfigError(f"{source}: securities 必须是数组")

    mode = raw.get("mode")
    if mode not in {"live", "demo"}:
        raise ConfigError(f"{source}: mode 必须是 live 或 demo")
    is_demo = raw.get("isDemo")
    if not isinstance(is_demo, bool) or ((mode == "demo") != is_demo):
        raise ConfigError(f"{source}: mode 与 isDemo 不一致")

    disclaimer = _text(raw.get("disclaimer"))
    market_data = _mapping(raw.get("marketData"))
    if is_demo and not any(token in disclaimer.lower() for token in ("虚构", "fictional")):
        raise ConfigError(f"{source}: 演示配置必须明确标注为虚构数据")
    if is_demo and market_data.get("realtime") is not False:
        raise ConfigError(f"{source}: 演示配置不得声明为实时数据")

    securities = [
        _normalize_security(value, index)
        for index, value in enumerate(raw["securities"])
    ]
    seen: set[str] = set()
    for security in securities:
        if security["id"] in seen:
            raise ConfigError(f"{source}: 重复证券 id: {security['id']}")
        seen.add(security["id"])

    configured_interval = _finite_number(raw.get("refreshIntervalMs"))
    refresh_interval_ms = (
        int(min(configured_interval, 3_600_000))
        if configured_interval is not None and configured_interval >= 30_000
        else DEFAULT_REFRESH_INTERVAL_MS
    )

    market_realtime = market_data.get("realtime") is True and not is_demo
    raw_fx_rates = _mapping(market_data.get("fxRates"))
    raw_hkd_cny = _mapping(raw_fx_rates.get("HKD_CNY"))
    hkd_cny_rate = _positive_number(raw_hkd_cny.get("rate"))
    return {
        "schemaVersion": (
            raw["schemaVersion"]
            if isinstance(raw.get("schemaVersion"), int)
            and not isinstance(raw.get("schemaVersion"), bool)
            else 1
        ),
        "mode": mode,
        "isDemo": is_demo,
        "title": _text(raw.get("title"), "Liberty 长期投资观察清单"),
        "description": _text(raw.get("description")),
        "disclaimer": disclaimer,
        "refreshIntervalMs": refresh_interval_ms,
        "snapshotGeneratedAt": _text(raw.get("snapshotGeneratedAt")) or None,
        "marketData": {
            "provider": _text(market_data.get("provider")) or None,
            "realtime": market_realtime,
            "status": _text(
                market_data.get("status"),
                "fictional_demo" if is_demo else "not_configured",
            ),
            "asOfLabel": _text(market_data.get("asOfLabel")) or None,
            "collectionStartedAt": (
                _text(market_data.get("collectionStartedAt")) or None
            ),
            "collectedAt": _text(market_data.get("collectedAt")) or None,
            "fxRates": {
                "HKD_CNY": (
                    {
                        "rate": hkd_cny_rate,
                        "asOf": _text(raw_hkd_cny.get("asOf")) or None,
                        "fetchedAt": (
                            _text(raw_hkd_cny.get("fetchedAt")) or None
                        ),
                        "source": _text(raw_hkd_cny.get("source")),
                        "status": _text(
                            raw_hkd_cny.get("status"), "available"
                        ),
                    }
                    if hkd_cny_rate is not None
                    else None
                )
            },
        },
        "sectorSignals": _normalize_sector_signals(raw.get("sectorSignals")),
        "securities": securities,
    }


def _valuation_from_shareholder_yield(value: float | None) -> str:
    if value is None:
        return "unconfigured"
    if value >= 5:
        return "deeply_attractive"
    if value >= 4:
        return "attractive"
    if value >= 3:
        return "fair"
    return "expensive"


def _apply_yield_model(
    security: dict[str, Any], market_data: Mapping[str, Any]
) -> None:
    """Derive 3%/4%/5% targets from the audited annual cash-return basis."""

    basis = security["yieldBasis"]["annualAveragePerShareCny"]
    if basis is None:
        return

    raw_fx = _mapping(_mapping(market_data.get("fxRates")).get("HKD_CNY"))
    hkd_cny = _positive_number(raw_fx.get("rate"))
    current_local = security["quote"]["currentPrice"]
    if security["currency"] == "CNY":
        current_cny = current_local
        local_per_cny = 1.0
    elif security["currency"] == "HKD" and hkd_cny is not None:
        current_cny = (
            current_local * hkd_cny if current_local is not None else None
        )
        local_per_cny = 1 / hkd_cny
    else:
        current_cny = None
        local_per_cny = None

    targets_cny = {
        "watch": basis / 0.03,
        "preferred": basis / 0.04,
        "deep": basis / 0.05,
    }
    security["targetPricesCny"] = {
        key: _round(value, 4) for key, value in targets_cny.items()
    }
    security["targetPrices"] = {
        key: (
            _round(value * local_per_cny, 4)
            if local_per_cny is not None
            else None
        )
        for key, value in targets_cny.items()
    }
    security["quote"]["currentPriceCny"] = _round(current_cny, 4)
    current_yield = (
        basis / current_cny * 100
        if current_cny is not None and current_cny > 0
        else None
    )
    security["currentShareholderYieldPct"] = _round(current_yield, 3)
    security["valuationStatus"] = _valuation_from_shareholder_yield(
        current_yield
    )
    security["valuationStatusSource"] = "shareholder_yield"
    security["metrics"] = dict(security["metrics"]) | {
        "annualAverageShareholderReturnPerShareCny": _round(basis, 6),
        "shareholderYieldWindowYears": security["yieldBasis"]["windowYears"],
        "currentShareholderYieldPct": _round(current_yield, 3),
    }


def _target_state(security: Mapping[str, Any]) -> dict[str, Any]:
    current_price = security["quote"]["currentPrice"]
    preferred_price = security["targetPrices"]["preferred"]
    if preferred_price is None:
        return {
            "distanceToPreferredPct": None,
            "targetStatus": "unconfigured",
            "alertStatus": "not_configured",
            "targetAttractiveness": None,
        }
    if current_price is None:
        return {
            "distanceToPreferredPct": None,
            "targetStatus": "price_unavailable",
            "alertStatus": "unavailable",
            "targetAttractiveness": None,
        }

    distance = _round((current_price - preferred_price) / preferred_price * 100)
    assert distance is not None
    if distance <= 0:
        target_status, alert_status = "reached", "buy_zone"
    elif distance <= 3:
        target_status, alert_status = "within_3", "approaching"
    elif distance <= 10:
        target_status, alert_status = "within_10", "watch"
    else:
        target_status, alert_status = "far", "none"
    return {
        "distanceToPreferredPct": distance,
        "targetStatus": target_status,
        "alertStatus": alert_status,
        "targetAttractiveness": _round(
            _clamp(100 - max(distance, 0) * 5, 0, 100), 1
        ),
    }


def _issuer_daily_changes(
    members: list[Mapping[str, Any]],
) -> list[float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for member in members:
        value = member["quote"]["dailyChangePct"]
        if value is not None:
            grouped[member["issuerId"]].append(value)
    return [float(median(values)) for values in grouped.values() if values]


def _average(values: list[float], digits: int = 2) -> float | None:
    return _round(sum(values) / len(values), digits) if values else None


def _sector_bases(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for security in config["securities"]:
        groups[security["sector"]].append(security)

    bases: dict[str, dict[str, Any]] = {}
    for sector, members in groups.items():
        issuers = {member["issuerId"] for member in members}
        daily_changes = _issuer_daily_changes(members)
        advancers = sum(value > 0 for value in daily_changes)
        decliners = sum(value < 0 for value in daily_changes)
        unchanged = len(daily_changes) - advancers - decliners
        daily_breadth = (
            _round(advancers / len(daily_changes) * 100)
            if daily_changes
            else None
        )

        signal = deepcopy(config["sectorSignals"].get(sector, {}))
        declared_issuer_count = signal.get("issuerCount")
        actual_issuer_count = len(issuers)
        eligible_issuer_count = (
            min(actual_issuer_count, declared_issuer_count)
            if isinstance(declared_issuer_count, int)
            and declared_issuer_count >= 0
            else actual_issuer_count
        )
        covered_count = signal.get("coveredIssuerCount")
        if (
            isinstance(covered_count, int)
            and covered_count >= 0
            and actual_issuer_count
        ):
            heat_coverage = _round(
                min(covered_count, actual_issuer_count)
                / actual_issuer_count
                * 100
            )
        else:
            heat_coverage = (
                signal.get("coveragePct")
                if _finite_number(signal.get("coveragePct")) is not None
                else None
            )
        if heat_coverage is not None and not 0 <= heat_coverage <= 100:
            heat_coverage = None

        history_complete = all(
            signal.get(key) is not None for key in SECTOR_SIGNAL_KEYS
        )
        if not signal:
            heat_status = "missing_history"
        elif eligible_issuer_count < 3:
            heat_status = "insufficient_issuers"
        elif heat_coverage is None or heat_coverage < 80:
            heat_status = "insufficient_coverage"
        elif not history_complete:
            heat_status = "incomplete_history"
        elif not (
            0 <= signal["advancers5dPct"] <= 100
            and 0 <= signal["aboveMa20Pct"] <= 100
        ):
            heat_status = "invalid_history"
        else:
            heat_status = "eligible"

        bases[sector] = {
            "sector": sector,
            "securityCount": len(members),
            "issuerCount": actual_issuer_count,
            "quotedIssuerCount": len(daily_changes),
            "quoteCoveragePct": (
                _round(len(daily_changes) / actual_issuer_count * 100)
                if actual_issuer_count
                else None
            ),
            "averageDailyChangePct": _average(daily_changes),
            "advancers": advancers,
            "decliners": decliners,
            "unchanged": unchanged,
            "breadthPct": daily_breadth,
            "return1dPct": signal.get("return1dPct"),
            "return5dPct": signal.get("return5dPct"),
            "return20dPct": signal.get("return20dPct"),
            "advancers5dPct": signal.get("advancers5dPct"),
            "aboveMa20Pct": signal.get("aboveMa20Pct"),
            "heatIssuerCount": eligible_issuer_count,
            "heatCoveragePct": heat_coverage,
            "heatAsOf": signal.get("asOf"),
            "heatStatus": heat_status,
            "heatScore": None,
            "heatLabel": "数据不足",
            "heatPercentiles": None,
        }

    _apply_sector_heat_percentiles(bases)
    return bases


def _percentile_ranks(
    sectors: list[str], bases: Mapping[str, Mapping[str, Any]], key: str
) -> dict[str, float]:
    """Return average-rank percentiles; a single eligible sector maps to 50."""

    if len(sectors) == 1:
        return {sectors[0]: 50.0}
    sorted_items = sorted((bases[sector][key], sector) for sector in sectors)
    result: dict[str, float] = {}
    index = 0
    while index < len(sorted_items):
        end = index + 1
        while end < len(sorted_items) and sorted_items[end][0] == sorted_items[index][0]:
            end += 1
        average_rank = (index + end - 1) / 2
        percentile = average_rank / (len(sorted_items) - 1) * 100
        for _, sector in sorted_items[index:end]:
            result[sector] = percentile
        index = end
    return result


def _heat_label(score: float) -> str:
    if score >= 80:
        return "偏热"
    if score >= 60:
        return "升温"
    if score >= 40:
        return "中性"
    if score >= 20:
        return "降温"
    return "偏冷"


def _apply_sector_heat_percentiles(bases: dict[str, dict[str, Any]]) -> None:
    eligible = [
        sector
        for sector, base in bases.items()
        if base["heatStatus"] == "eligible"
    ]
    if not eligible:
        return
    percentiles = {
        key: _percentile_ranks(eligible, bases, key)
        for key in SECTOR_SIGNAL_KEYS
    }
    for sector in eligible:
        sector_percentiles = {
            key: _round(percentiles[key][sector], 1) for key in SECTOR_SIGNAL_KEYS
        }
        score = sum(
            percentiles[key][sector] * weight
            for key, weight in SECTOR_HEAT_WEIGHTS.items()
        )
        bases[sector]["heatScore"] = _round(score, 1)
        bases[sector]["heatLabel"] = _heat_label(score)
        bases[sector]["heatStatus"] = "computed"
        bases[sector]["heatPercentiles"] = sector_percentiles


def _technical_score(
    technical: Mapping[str, Any] | None,
) -> tuple[float | None, dict[str, float | None] | None]:
    if technical is None or technical["historyTradingDays"] < 60:
        return None, None
    rsi_component = _clamp((50 - technical["rsi14"]) / 20)
    drawdown_component = _clamp(
        ((-technical["drawdown60dPct"] / 100) - 0.05) / 0.20
    )
    lag_component = _clamp(
        ((-technical["relativeSector5dPct"] / 100) - 0.02) / 0.08
    )
    score = 100 * (
        0.40 * rsi_component
        + 0.35 * drawdown_component
        + 0.25 * lag_component
    )
    return _round(score, 1), {
        "rsi": _round(rsi_component, 3),
        "drawdown": _round(drawdown_component, 3),
        "relativeLag": _round(lag_component, 3),
    }


def _valuation_score(status: str) -> float | None:
    return VALUATION_SCORES.get(status.lower())


def _general_opportunity(
    security: Mapping[str, Any], target_attractiveness: float | None
) -> tuple[float | None, dict[str, float | None]]:
    """Keep the general price opportunity independent from technical signals."""

    dividend = security["currentShareholderYieldPct"]
    valuation = _valuation_score(security["valuationStatus"])
    return target_attractiveness, {
        "targetAttractiveness": target_attractiveness,
        # These are displayed as context only and do not change the price
        # proximity score.
        "expectedDividend": (
            _round(_clamp(dividend / 8, 0, 1) * 10, 1)
            if dividend is not None
            else None
        ),
        "valuation": _round(valuation, 1) if valuation is not None else None,
    }


def _target_lines(security: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "label": label,
            "price": security["targetPrices"][key],
            "priceCny": security["targetPricesCny"][key],
            "currency": security["currency"],
        }
        for key, label in TARGET_KEYS
    ]


def _build_market(
    config: Mapping[str, Any], securities: list[Mapping[str, Any]]
) -> dict[str, Any]:
    if config["isDemo"]:
        return {
            "status": "demo",
            "label": "虚构演示",
            "isRealtime": False,
            "fxRates": deepcopy(config["marketData"]["fxRates"]),
            "markets": [
                {"market": market, "status": "demo", "label": "虚构演示"}
                for market in sorted({security["market"] for security in securities})
            ],
        }
    if not securities:
        return {
            "status": "unconfigured",
            "label": "等待配置标的",
            "isRealtime": False,
            "fxRates": deepcopy(config["marketData"]["fxRates"]),
            "markets": [],
        }

    markets: list[dict[str, Any]] = []
    for market in sorted({security["market"] for security in securities}):
        states = [
            security["quote"]["marketState"]
            for security in securities
            if security["market"] == market
        ]
        if "open" in states:
            status = "open"
        elif states and all(state == "closed" for state in states):
            status = "closed"
        else:
            status = "unknown"
        markets.append(
            {
                "market": market,
                "status": status,
                "label": (
                    "交易中"
                    if status == "open"
                    else "已收盘"
                    if status == "closed"
                    else "未知"
                ),
            }
        )

    if any(item["status"] == "open" for item in markets):
        status = "open"
    elif markets and all(item["status"] == "closed" for item in markets):
        status = "closed"
    else:
        status = "unknown"
    return {
        "status": status,
        "label": (
            "交易中"
            if status == "open"
            else "已收盘"
            if status == "closed"
            else "未知"
        ),
        "isRealtime": config["marketData"]["realtime"],
        "fxRates": deepcopy(config["marketData"]["fxRates"]),
        "markets": markets,
    }


def _summarize(securities: list[Mapping[str, Any]]) -> dict[str, Any]:
    distances = [
        security["derived"]["distanceToPreferredPct"]
        for security in securities
        if security["derived"]["distanceToPreferredPct"] is not None
    ]
    scored = sorted(
        (
            security
            for security in securities
            if security["derived"]["opportunityScore"] is not None
        ),
        key=lambda security: security["derived"]["opportunityScore"],
        reverse=True,
    )
    reached = sum(
        security["derived"]["targetStatus"] == "reached"
        for security in securities
    )
    within3 = sum(
        security["derived"]["targetStatus"] == "within_3"
        for security in securities
    )
    within10 = sum(
        security["derived"]["targetStatus"] == "within_10"
        for security in securities
    )
    return {
        "totalSecurities": len(securities),
        "targetConfiguredCount": sum(
            security["targetPrices"]["preferred"] is not None
            for security in securities
        ),
        "priceAvailableCount": sum(
            security["quote"]["currentPrice"] is not None
            for security in securities
        ),
        "peAvailableCount": sum(
            (
                security["metrics"].get("peTtm")
                if security["metrics"].get("peTtm") is not None
                else security["metrics"].get("pe")
            )
            is not None
            for security in securities
        ),
        "pbAvailableCount": sum(
            security["metrics"].get("pb") is not None
            for security in securities
        ),
        "futuMetricCompleteCount": sum(
            all(
                security["metrics"].get(key) is not None
                for key in FUTU_SNAPSHOT_METRIC_KEYS
            )
            for security in securities
        ),
        "reachedTargetCount": reached,
        "within3PctCount": within3,
        "within10PctCount": within10,
        "atOrWithin3PctCount": reached + within3,
        "atOrWithin10PctCount": reached + within3 + within10,
        "unconfiguredTargetCount": sum(
            security["derived"]["targetStatus"] == "unconfigured"
            for security in securities
        ),
        "averageDistanceToPreferredPct": _average(distances),
        "hotSectorDislocationCount": sum(
            security["derived"]["hotSectorDislocation"] is True
            for security in securities
        ),
        "topOpportunity": (
            {
                "id": scored[0]["id"],
                "name": scored[0]["name"],
                "score": scored[0]["derived"]["opportunityScore"],
            }
            if scored
            else None
        ),
    }


def _rank_sectors(
    securities: list[Mapping[str, Any]],
    bases: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    sectors: list[dict[str, Any]] = []
    for sector, base in bases.items():
        members = [
            security for security in securities if security["sector"] == sector
        ]
        distances = [
            security["derived"]["distanceToPreferredPct"]
            for security in members
            if security["derived"]["distanceToPreferredPct"] is not None
        ]
        scored = sorted(
            (
                security
                for security in members
                if security["derived"]["opportunityScore"] is not None
            ),
            key=lambda security: security["derived"]["opportunityScore"],
            reverse=True,
        )
        average_opportunity = _average(
            [security["derived"]["opportunityScore"] for security in scored], 1
        )
        sectors.append(
            deepcopy(dict(base))
            | {
                "reachedTargetCount": sum(
                    security["derived"]["targetStatus"] == "reached"
                    for security in members
                ),
                "within3PctCount": sum(
                    security["derived"]["targetStatus"] == "within_3"
                    for security in members
                ),
                "within10PctCount": sum(
                    security["derived"]["targetStatus"] == "within_10"
                    for security in members
                ),
                "averageDistanceToPreferredPct": _average(distances),
                "averageOpportunityScore": average_opportunity,
                "rankingScore": base["heatScore"],
                "topOpportunity": (
                    {
                        "id": scored[0]["id"],
                        "name": scored[0]["name"],
                        "score": scored[0]["derived"]["opportunityScore"],
                    }
                    if scored
                    else None
                ),
            }
        )

    sectors.sort(
        key=lambda item: (
            item["rankingScore"] is None,
            -(item["rankingScore"] or 0),
            item["sector"],
        )
    )
    rank = 0
    for item in sectors:
        if item["rankingScore"] is None:
            item["rank"] = None
        else:
            rank += 1
            item["rank"] = rank
    return sectors


def build_watchlist_data(config: Mapping[str, Any]) -> dict[str, Any]:
    """Build the stable API body from a normalized configuration."""

    bases = _sector_bases(config)
    securities: list[dict[str, Any]] = []
    for source in config["securities"]:
        security = deepcopy(source)
        _apply_yield_model(security, config["marketData"])
        state = _target_state(security)
        base = bases[security["sector"]]
        technical, technical_components = _technical_score(
            security["technicalIndicators"]
        )
        technical_oversold = (
            (
                security["technicalIndicators"]["rsi14"] <= 40
                or security["technicalIndicators"]["drawdown60dPct"] <= -10
            )
            if technical is not None
            else None
        )
        general_opportunity, general_components = _general_opportunity(
            security, state["targetAttractiveness"]
        )
        if (
            technical is not None
            and base["heatScore"] is not None
            and state["targetAttractiveness"] is not None
        ):
            opportunity_technical = _round(
                state["targetAttractiveness"] * 0.55
                + technical * 0.30
                + base["heatScore"] * 0.15,
                1,
            )
            inputs_complete = True
        else:
            opportunity_technical = None
            inputs_complete = False

        indicators = security["technicalIndicators"]
        hot_sector_dislocation = (
            base["heatScore"] >= 60
            and state["distanceToPreferredPct"] is not None
            and state["distanceToPreferredPct"] <= 10
            and indicators is not None
            and indicators["relativeSector5dPct"] <= -3
            and technical_oversold is True
            and not security["recommendationPaused"]
            if inputs_complete
            else None
        )
        contrarian_low_price = (
            base["heatScore"] < 40 and state["targetStatus"] == "reached"
            if base["heatScore"] is not None
            else None
        )

        security.update(
            {
                "currentPrice": security["quote"]["currentPrice"],
                "currentPriceCny": security["quote"]["currentPriceCny"],
                "dailyChangePct": security["quote"]["dailyChangePct"],
                "lastUpdate": security["quote"]["lastUpdatedAt"],
                "preferredPrice": security["targetPrices"]["preferred"],
                "targetLines": _target_lines(security),
                "derived": {
                    **state,
                    "sectorHeatScore": base["heatScore"],
                    "sectorHeatStatus": base["heatStatus"],
                    "opportunityScore": general_opportunity,
                    "opportunityComponents": general_components,
                    "technical": technical,
                    "technicalOversold": technical_oversold,
                    "technicalComponents": technical_components,
                    "opportunityTechnical": opportunity_technical,
                    "hotSectorDislocation": hot_sector_dislocation,
                    "contrarianLowPrice": contrarian_low_price,
                },
            }
        )
        securities.append(security)

    securities.sort(
        key=lambda security: (
            security["derived"]["distanceToPreferredPct"] is None,
            security["derived"]["distanceToPreferredPct"]
            if security["derived"]["distanceToPreferredPct"] is not None
            else 0,
            security["name"],
        )
    )
    summary = _summarize(securities)
    return {
        "market": _build_market(config, securities),
        "summary": summary,
        "sectors": _rank_sectors(securities, bases),
        "securities": securities,
    }
