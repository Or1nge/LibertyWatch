"""Deterministic LibertyWatch V2 dual-pillar screening calculations.

This module is deliberately independent from the legacy shareholder-return
v2.1 RI/ERI path.  Missing inputs remain missing and reduce evidence coverage;
they are never converted to zero or hidden by weight redistribution.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from statistics import median
from typing import Any, Mapping, Sequence


ZERO = Decimal("0")
ONE = Decimal("1")
FIFTY = Decimal("50")
HUNDRED = Decimal("100")
QUANT = Decimal("0.01")
COVERAGE_QUANT = Decimal("0.0001")


def finite_decimal(value: Any, *, positive: bool = False) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or (positive and parsed <= 0):
        return None
    return parsed


def _quantize(value: Decimal) -> Decimal:
    return min(HUNDRED, max(ZERO, value)).quantize(QUANT, rounding=ROUND_HALF_UP)


def _coverage(value: Decimal) -> Decimal:
    return min(ONE, max(ZERO, value)).quantize(COVERAGE_QUANT, rounding=ROUND_HALF_UP)


def linear_score(value: Any, bands: Sequence[Sequence[Any]]) -> Decimal | None:
    """Piecewise-linear interpolation over increasing ``(x, score)`` points."""

    number = finite_decimal(value)
    points = [(finite_decimal(item[0]), finite_decimal(item[1])) for item in bands]
    if number is None or any(x is None or y is None for x, y in points):
        return None
    normalized = [(x, y) for x, y in points if x is not None and y is not None]
    if len(normalized) < 2 or any(
        normalized[index][0] >= normalized[index + 1][0]
        for index in range(len(normalized) - 1)
    ):
        raise ValueError("score bands must contain at least two increasing x values")
    if number <= normalized[0][0]:
        return _quantize(normalized[0][1])
    if number >= normalized[-1][0]:
        return _quantize(normalized[-1][1])
    for (left_x, left_y), (right_x, right_y) in zip(normalized, normalized[1:]):
        if left_x <= number <= right_x:
            ratio = (number - left_x) / (right_x - left_x)
            return _quantize(left_y + ratio * (right_y - left_y))
    raise AssertionError("interpolation interval not found")


def coverage_shrunk_score(
    components: Mapping[str, Mapping[str, Any]],
    weights: Mapping[str, Any],
) -> dict[str, Any]:
    valid_weight = ZERO
    weighted_total = ZERO
    public_components: dict[str, dict[str, Any]] = {}
    for component_id, raw_weight in weights.items():
        weight = finite_decimal(raw_weight)
        if weight is None or weight < 0:
            raise ValueError(f"invalid component weight: {component_id}")
        source = dict(components.get(component_id) or {})
        value = finite_decimal(source.get("value"))
        source["value"] = format(_quantize(value), "f") if value is not None else None
        source.setdefault("status", "VALID" if value is not None else "UNAVAILABLE")
        source.setdefault("basis", "UNAVAILABLE" if value is None else "DERIVED")
        source.setdefault("warnings", [])
        source["nominal_weight"] = format(weight, "f")
        if value is not None:
            valid_weight += weight
            weighted_total += value * weight
        public_components[component_id] = source
    if valid_weight <= 0:
        return {
            "value": None,
            "coverage": "0.0000",
            "status": "UNAVAILABLE",
            "components": public_components,
            "basis": "DETERMINISTIC_COVERAGE_SHRINKAGE",
            "warnings": sorted(
                {warning for item in public_components.values() for warning in item.get("warnings", [])}
            ),
        }
    raw_score = weighted_total / valid_weight
    final_score = FIFTY + valid_weight * (raw_score - FIFTY)
    warnings = sorted(
        {warning for item in public_components.values() for warning in item.get("warnings", [])}
    )
    return {
        "value": format(_quantize(final_score), "f"),
        "coverage": format(_coverage(valid_weight), "f"),
        "status": "READY" if valid_weight >= ONE else "DATA_LIMITED",
        "raw_score": format(_quantize(raw_score), "f"),
        "components": public_components,
        "basis": "DETERMINISTIC_COVERAGE_SHRINKAGE",
        "warnings": warnings,
    }


def dividend_yield_component(value_pct: Any, policy: Mapping[str, Any]) -> dict[str, Any]:
    value = finite_decimal(value_pct)
    score = linear_score(value, policy["dividend_yield_bands_pct"]) if value is not None and value >= 0 else None
    return {
        "value": score,
        "input_value": format(value, "f") if value is not None else None,
        "input_unit": "percent",
        "status": "VALID" if score is not None else "UNAVAILABLE",
        "basis": "VENDOR",
        "source_summary": {"source": "Futu OpenD", "field_id": "dividend_ratio_ttm"},
        "warnings": [] if score is not None else ["DIVIDEND_YIELD_TTM_MISSING"],
    }


def valuation_component(
    *,
    pe_ttm: Any,
    pe: Any,
    pb: Any,
    profile: str,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    pe_ttm_value = finite_decimal(pe_ttm, positive=True)
    pe_value = finite_decimal(pe, positive=True)
    pb_value = finite_decimal(pb, positive=True)
    is_financial = profile == "FINANCIAL"
    if is_financial and pb_value is not None:
        return {
            "value": linear_score(pb_value, policy["financial_pb_bands"]),
            "metric": "PB",
            "input_value": format(pb_value, "f"),
            "status": "VALID",
            "basis": "VENDOR",
            "source_summary": {"source": "Futu OpenD", "field_id": "pb"},
            "warnings": [],
        }
    selected_pe = pe_ttm_value if pe_ttm_value is not None else pe_value
    selected_field = "pe_ttm" if pe_ttm_value is not None else "pe"
    if not is_financial and selected_pe is not None:
        earnings_yield = HUNDRED / selected_pe
        return {
            "value": linear_score(earnings_yield, policy["earnings_yield_bands_pct"]),
            "metric": "EARNINGS_YIELD_FROM_PE_TTM" if selected_field == "pe_ttm" else "EARNINGS_YIELD_FROM_PE",
            "input_value": format(selected_pe, "f"),
            "derived_earnings_yield_pct": format(earnings_yield.quantize(Decimal("0.0001")), "f"),
            "status": "VALID",
            "basis": "VENDOR_DERIVED",
            "source_summary": {"source": "Futu OpenD", "field_id": selected_field},
            "warnings": [],
        }
    if pb_value is not None:
        bands = policy["financial_pb_bands"] if is_financial else policy["non_financial_pb_fallback_bands"]
        return {
            "value": linear_score(pb_value, bands),
            "metric": "PB",
            "input_value": format(pb_value, "f"),
            "status": "VALID",
            "basis": "VENDOR_FALLBACK" if not is_financial else "VENDOR",
            "source_summary": {"source": "Futu OpenD", "field_id": "pb"},
            "warnings": ["PB_FALLBACK"] if not is_financial else [],
        }
    return {
        "value": None,
        "metric": None,
        "input_value": None,
        "status": "UNAVAILABLE",
        "basis": "UNAVAILABLE",
        "source_summary": {"source": "Futu OpenD", "field_ids": ["pe_ttm", "pe", "pb"]},
        "warnings": ["VALUATION_ANCHOR_MISSING"],
    }


def five_year_price_position_component(
    current_price: Any,
    weekly_prices: Sequence[Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    price = finite_decimal(current_price, positive=True)
    values = [item for raw in weekly_prices if (item := finite_decimal(raw, positive=True)) is not None]
    minimum = int(policy["minimum_weekly_points"])
    if price is None or len(values) < minimum:
        return {
            "value": None,
            "percentile_rank": None,
            "valid_weekly_points": len(values),
            "status": "UNAVAILABLE",
            "basis": "VENDOR_HISTORY",
            "source_summary": {"source": "Futu OpenD", "field_id": "qfq_weekly_close"},
            "warnings": ["PRICE_HISTORY_INSUFFICIENT"],
        }
    rank = Decimal(sum(item <= price for item in values)) / Decimal(len(values))
    score = HUNDRED * (ONE - rank)
    return {
        "value": _quantize(score),
        "percentile_rank": format(rank.quantize(Decimal("0.0001")), "f"),
        "valid_weekly_points": len(values),
        "status": "VALID",
        "basis": "VENDOR_HISTORY",
        "source_summary": {"source": "Futu OpenD", "field_id": "qfq_weekly_close"},
        "warnings": [],
    }


def opportunity_score(
    *,
    dividend_yield_ttm_pct: Any,
    pe_ttm: Any,
    pe: Any,
    pb: Any,
    profile: str,
    current_price: Any,
    weekly_prices: Sequence[Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    components = {
        "dividend_yield": dividend_yield_component(dividend_yield_ttm_pct, policy),
        "valuation": valuation_component(pe_ttm=pe_ttm, pe=pe, pb=pb, profile=profile, policy=policy),
        "five_year_price_position": five_year_price_position_component(current_price, weekly_prices, policy),
    }
    return coverage_shrunk_score(components, policy["weights"])


def _trend(values: Sequence[Decimal]) -> Decimal:
    if len(values) < 2:
        return ZERO
    if values[0] > 0 and values[-1] > 0:
        try:
            result = (float(values[-1] / values[0]) ** (1 / (len(values) - 1))) - 1
            trend = Decimal(str(result))
        except (OverflowError, ValueError):
            trend = ZERO
    else:
        changes = [
            (current - previous) / abs(previous)
            for previous, current in zip(values, values[1:])
            if previous != 0
        ]
        trend = Decimal(str(median(changes))) if changes else ZERO
    return min(Decimal("0.20"), max(Decimal("-0.20"), trend))


def series_quality(values: Sequence[Any], policy: Mapping[str, Any]) -> tuple[Decimal | None, Decimal, dict[str, Any]]:
    series = [value for raw in values if (value := finite_decimal(raw)) is not None]
    if not series:
        return None, ZERO, {"valid_years": 0, "positive_ratio": None, "trend": None}
    positive_ratio = Decimal(sum(value > 0 for value in series)) / Decimal(len(series))
    trend = _trend(series)
    trend_score = linear_score(trend, policy["trend_bands"])
    assert trend_score is not None
    score = Decimal("0.70") * positive_ratio * HUNDRED + Decimal("0.30") * trend_score
    evidence_factor = min(ONE, Decimal(len(series)) / Decimal(str(policy["full_evidence_years"])))
    return _quantize(score), evidence_factor, {
        "valid_years": len(series),
        "positive_ratio": format(positive_ratio.quantize(Decimal("0.0001")), "f"),
        "trend": format(trend.quantize(Decimal("0.0001")), "f"),
        "trend_score": format(trend_score, "f"),
    }


def _latest_contiguous_rows(rows: Sequence[Mapping[str, Any]], maximum: int) -> list[Mapping[str, Any]]:
    indexed: dict[int, Mapping[str, Any]] = {}
    for row in rows:
        try:
            year = int(row["fiscal_year"])
        except (KeyError, TypeError, ValueError):
            continue
        indexed[year] = row
    if not indexed:
        return []
    selected: list[Mapping[str, Any]] = []
    year = max(indexed)
    while year in indexed and len(selected) < maximum:
        selected.append(indexed[year])
        year -= 1
    return list(reversed(selected))


def _series_component(
    rows: Sequence[Mapping[str, Any]],
    field: str,
    nominal_weight: Decimal,
    policy: Mapping[str, Any],
    *,
    warnings: Sequence[str] = (),
) -> tuple[dict[str, Any], Decimal]:
    usable = [row for row in rows if finite_decimal(row.get(field)) is not None]
    values = [row[field] for row in usable]
    score, factor, details = series_quality(values, policy)
    effective = nominal_weight * factor if score is not None else ZERO
    return {
        "value": score,
        "status": "VALID" if score is not None else "UNAVAILABLE",
        "basis": "DETERMINISTIC_FROM_ANNUAL_FINANCIALS" if score is not None else "UNAVAILABLE",
        "evidence_factor": format(_coverage(factor), "f"),
        "valid_fiscal_years": [int(row["fiscal_year"]) for row in usable],
        "details": details,
        "source_summary": {"field_id": field, "sources": sorted({str(row.get("source") or "") for row in usable if row.get("source")})},
        "warnings": list(warnings),
    }, effective


def _balance_sheet_score(row: Mapping[str, Any], policy: Mapping[str, Any]) -> tuple[Decimal | None, str, list[str]]:
    cash = finite_decimal(row.get("cash"))
    debt = finite_decimal(row.get("interest_bearing_debt"))
    equity = finite_decimal(row.get("total_equity"), positive=True)
    if cash is not None and debt is not None and equity is not None:
        net_debt = debt - cash
        if net_debt <= 0:
            return HUNDRED, "NET_CASH", []
        ratio = net_debt / equity
        for maximum, score in policy["net_debt_to_equity_bands"]:
            limit = finite_decimal(maximum)
            if limit is not None and ratio <= limit:
                return finite_decimal(score), "NET_DEBT_TO_EQUITY", []
        return finite_decimal(policy["net_debt_to_equity_above_score"]), "NET_DEBT_TO_EQUITY", []
    liabilities = finite_decimal(row.get("total_liabilities"))
    assets = finite_decimal(row.get("total_assets"), positive=True)
    if liabilities is not None and assets is not None:
        ratio = liabilities / assets
        for maximum, score in policy["liabilities_to_assets_bands"]:
            limit = finite_decimal(maximum)
            if limit is not None and ratio <= limit:
                return finite_decimal(score), "LIABILITIES_TO_ASSETS_PROXY", ["BALANCE_SHEET_PROXY"]
        return finite_decimal(policy["liabilities_to_assets_above_score"]), "LIABILITIES_TO_ASSETS_PROXY", ["BALANCE_SHEET_PROXY"]
    return None, "UNAVAILABLE", ["BALANCE_SHEET_MINIMUM_MISSING"]


def _roe_components(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for previous, current in zip(rows, rows[1:]):
        profit = finite_decimal(current.get("net_profit"))
        previous_equity = finite_decimal(previous.get("total_equity"), positive=True)
        current_equity = finite_decimal(current.get("total_equity"), positive=True)
        if profit is None or previous_equity is None or current_equity is None:
            continue
        average = (previous_equity + current_equity) / Decimal("2")
        result.append({"fiscal_year": current["fiscal_year"], "roe_pct": profit / average * HUNDRED, "source": current.get("source")})
    return result


def financial_resilience_score(
    annual_rows: Sequence[Mapping[str, Any]],
    *,
    profile: str,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    rows = _latest_contiguous_rows(annual_rows, int(policy["maximum_fiscal_years"]))
    nominal = {key: finite_decimal(value) or ZERO for key, value in policy["weights"][profile].items()}
    components: dict[str, dict[str, Any]] = {}
    effective_weights: dict[str, Decimal] = {}

    net_profit, effective = _series_component(rows, "net_profit", nominal["net_profit_quality"], policy)
    components["net_profit_quality"] = net_profit
    effective_weights["net_profit_quality"] = effective

    if profile == "NON_FINANCIAL":
        ocf, effective = _series_component(rows, "operating_cash_flow", nominal["operating_cash_flow_quality"], policy)
        components["operating_cash_flow_quality"] = ocf
        effective_weights["operating_cash_flow_quality"] = effective
        fcf_rows: list[dict[str, Any]] = []
        for row in rows:
            ocf_value = finite_decimal(row.get("operating_cash_flow"))
            capex_value = finite_decimal(row.get("capital_expenditure"))
            if ocf_value is None or capex_value is None:
                continue
            fcf_rows.append({**row, "simplified_fcf": ocf_value - capex_value})
        fcf, effective = _series_component(
            fcf_rows,
            "simplified_fcf",
            nominal["simplified_fcf_quality"],
            policy,
            warnings=("SIMPLIFIED_FCF",),
        )
        components["simplified_fcf_quality"] = fcf
        effective_weights["simplified_fcf_quality"] = effective
        latest = rows[-1] if rows else {}
        balance_value, balance_basis, balance_warnings = _balance_sheet_score(latest, policy)
        components["balance_sheet_resilience"] = {
            "value": balance_value,
            "status": "VALID" if balance_value is not None else "UNAVAILABLE",
            "basis": balance_basis,
            "evidence_factor": "1.0000" if balance_value is not None else "0.0000",
            "valid_fiscal_years": [int(latest["fiscal_year"])] if balance_value is not None else [],
            "source_summary": {"field_ids": ["cash", "interest_bearing_debt", "total_equity", "total_liabilities", "total_assets"], "source": latest.get("source")},
            "warnings": balance_warnings,
        }
        effective_weights["balance_sheet_resilience"] = nominal["balance_sheet_resilience"] if balance_value is not None else ZERO
    else:
        roe_rows = _roe_components(rows)
        roe_values = [row["roe_pct"] for row in roe_rows]
        if roe_values:
            latest_level = linear_score(roe_values[-1], policy["roe_level_bands_pct"])
            trend_value = _trend(roe_values)
            trend_score = linear_score(trend_value, policy["trend_bands"])
            assert latest_level is not None and trend_score is not None
            roe_score = Decimal("0.70") * latest_level + Decimal("0.30") * trend_score
            roe_factor = min(ONE, Decimal(len(roe_values)) / Decimal(str(policy["full_evidence_years"])))
        else:
            roe_score, roe_factor, latest_level, trend_value, trend_score = None, ZERO, None, None, None
        components["roe_quality"] = {
            "value": _quantize(roe_score) if roe_score is not None else None,
            "status": "VALID" if roe_score is not None else "UNAVAILABLE",
            "basis": "DERIVED_NET_PROFIT_OVER_AVERAGE_EQUITY" if roe_score is not None else "UNAVAILABLE",
            "evidence_factor": format(_coverage(roe_factor), "f"),
            "valid_fiscal_years": [int(row["fiscal_year"]) for row in roe_rows],
            "details": {"latest_roe_pct": format(roe_values[-1].quantize(QUANT), "f") if roe_values else None, "latest_level_score": format(latest_level, "f") if latest_level is not None else None, "trend": format(trend_value, "f") if trend_value is not None else None, "trend_score": format(trend_score, "f") if trend_score is not None else None},
            "source_summary": {"field_ids": ["net_profit", "total_equity"]},
            "warnings": [],
        }
        effective_weights["roe_quality"] = nominal["roe_quality"] * roe_factor if roe_score is not None else ZERO

        equity_rows = [row for row in rows if finite_decimal(row.get("total_equity"), positive=True) is not None]
        equity_values = [finite_decimal(row["total_equity"], positive=True) for row in equity_rows]
        growth = _trend([value for value in equity_values if value is not None]) if len(equity_values) >= 2 else None
        growth_score = linear_score(growth, policy["equity_growth_bands"]) if growth is not None else None
        growth_years = max(0, len(equity_values) - 1)
        growth_factor = min(ONE, Decimal(growth_years) / Decimal(str(policy["full_evidence_years"])))
        components["equity_growth"] = {
            "value": growth_score,
            "status": "VALID" if growth_score is not None else "UNAVAILABLE",
            "basis": "DERIVED_FROM_TOTAL_EQUITY" if growth_score is not None else "UNAVAILABLE",
            "evidence_factor": format(_coverage(growth_factor), "f"),
            "valid_fiscal_years": [int(row["fiscal_year"]) for row in equity_rows],
            "details": {"annualized_growth": format(growth.quantize(Decimal("0.0001")), "f") if growth is not None else None},
            "source_summary": {"field_id": "total_equity"},
            "warnings": [],
        }
        effective_weights["equity_growth"] = nominal["equity_growth"] * growth_factor if growth_score is not None else ZERO

        capital_rows = [row for row in rows if finite_decimal(row.get("capital_quality_score")) is not None]
        capital_value = finite_decimal(capital_rows[-1].get("capital_quality_score")) if capital_rows else None
        components["capital_or_asset_quality"] = {
            "value": _quantize(capital_value) if capital_value is not None else None,
            "status": "VALID" if capital_value is not None else "UNAVAILABLE",
            "basis": "DIRECT_REGULATORY_METRIC" if capital_value is not None else "UNAVAILABLE",
            "evidence_factor": "1.0000" if capital_value is not None else "0.0000",
            "valid_fiscal_years": [int(capital_rows[-1]["fiscal_year"])] if capital_rows else [],
            "source_summary": {"field_id": "capital_quality_score"},
            "warnings": [] if capital_value is not None else ["FINANCIAL_CAPITAL_QUALITY_MISSING"],
        }
        effective_weights["capital_or_asset_quality"] = nominal["capital_or_asset_quality"] if capital_value is not None else ZERO

    weighted_components: dict[str, dict[str, Any]] = {}
    for component_id, component in components.items():
        item = dict(component)
        component_value = finite_decimal(item.get("value"))
        item["value"] = format(_quantize(component_value), "f") if component_value is not None else None
        item["nominal_weight"] = format(nominal[component_id], "f")
        item["effective_weight"] = format(effective_weights[component_id], "f")
        weighted_components[component_id] = item
    valid_weight = sum(effective_weights.values(), ZERO)
    weighted_total = sum(
        (finite_decimal(weighted_components[key].get("value")) or ZERO) * effective_weights[key]
        for key in weighted_components
    )
    if valid_weight <= 0:
        final = None
        raw = None
        status = "UNAVAILABLE"
    else:
        raw = weighted_total / valid_weight
        final = FIFTY + valid_weight * (raw - FIFTY)
        status = "READY" if valid_weight >= ONE else "DATA_LIMITED"
    warnings = sorted({warning for item in weighted_components.values() for warning in item.get("warnings", [])})
    return {
        "value": format(_quantize(final), "f") if final is not None else None,
        "coverage": format(_coverage(valid_weight), "f"),
        "status": status,
        "profile": profile,
        "raw_score": format(_quantize(raw), "f") if raw is not None else None,
        "components": weighted_components,
        "basis": "DETERMINISTIC_COVERAGE_SHRINKAGE",
        "warnings": warnings,
        "fiscal_years": [int(row["fiscal_year"]) for row in rows],
    }


def research_trigger(
    *,
    opportunity: Mapping[str, Any],
    resilience: Mapping[str, Any],
    dividend_yield_ttm_pct: Any,
    price_is_fresh: bool,
    v1_target_reached: bool = False,
    events: Sequence[str] = (),
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    opportunity_value = finite_decimal(opportunity.get("value"))
    resilience_value = finite_decimal(resilience.get("value"))
    dividend_value = finite_decimal(dividend_yield_ttm_pct)
    urgent_events = sorted(set(map(str, events)) & set(map(str, policy["material_event_codes"])))
    reasons: list[str] = []
    trigger_type: str | None = None
    if urgent_events:
        trigger_type = urgent_events[0]
        reasons.append("重大事件：" + "、".join(urgent_events))
    if v1_target_reached:
        trigger_type = trigger_type or "V1_TARGET_PRICE_REACHED"
        reasons.append("达到已有且可追溯的v1理想目标价")
    if dividend_value is not None and dividend_value >= finite_decimal(policy["dividend_yield_pct_min"]):
        trigger_type = trigger_type or "DIVIDEND_YIELD_TTM"
        reasons.append("Futu TTM股息率达到4%")
    if opportunity_value is not None and opportunity_value >= finite_decimal(policy["opportunity_high_min"]):
        trigger_type = trigger_type or "OPPORTUNITY_SCORE_HIGH"
        reasons.append("价格机会分达到高阈值")
    elif (
        opportunity_value is not None
        and resilience_value is not None
        and opportunity_value >= finite_decimal(policy["opportunity_combined_min"])
        and resilience_value >= finite_decimal(policy["resilience_combined_min"])
    ):
        trigger_type = trigger_type or "COMBINED_SCREEN"
        reasons.append("价格机会分与财务韧性分同时达到复合阈值")
    eligible = bool(trigger_type and (price_is_fresh or urgent_events))
    return {
        "eligible": eligible,
        "trigger_type": trigger_type,
        "reason": "；".join(reasons) if reasons else "当前未达到确定性研究触发条件",
        "in_observation_zone": bool(trigger_type),
        "price_fresh": bool(price_is_fresh),
        "event_codes": urgent_events,
    }


def filter_recent_weekly_prices(
    points: Sequence[Mapping[str, Any]],
    *,
    as_of: date,
    years: int,
) -> list[Any]:
    try:
        start = as_of.replace(year=as_of.year - years)
    except ValueError:
        start = as_of.replace(year=as_of.year - years, day=28)
    values: list[Any] = []
    for point in points:
        try:
            timestamp = date.fromisoformat(str(point.get("timestamp") or "")[:10])
        except ValueError:
            continue
        if start <= timestamp <= as_of:
            values.append(point.get("price"))
    return values
