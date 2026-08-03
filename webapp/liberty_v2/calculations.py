from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Iterable, Mapping, Sequence

from .models import DataStatus, SecurityClassInput
from .policy import decimal_mapping, decimal_sequence, decimal_value, integer_value, policy


ZERO = Decimal("0")
ONE = Decimal("1")


class CalculationError(ValueError):
    pass


class InsufficientDataError(CalculationError):
    pass


def to_decimal(value: Decimal | str | int) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, float):
        raise CalculationError("binary float is not accepted for financial calculations")
    else:
        try:
            result = Decimal(value)
        except (InvalidOperation, TypeError) as error:
            raise CalculationError(f"invalid decimal value: {value!r}") from error
    if not result.is_finite():
        raise CalculationError("NaN and Infinity are forbidden")
    return result


def clip(value: Decimal, minimum: Decimal = ZERO, maximum: Decimal = Decimal("100")) -> Decimal:
    value = to_decimal(value)
    return min(maximum, max(minimum, value))


def eligible_buyback(
    gross_cancelled_buyback: Decimal | None,
    cancelled_shares: Decimal | None,
    diluted_net_share_reduction: Decimal | None,
) -> Decimal:
    if gross_cancelled_buyback is None or cancelled_shares is None or diluted_net_share_reduction is None:
        raise InsufficientDataError("buyback cash, cancelled shares and diluted net reduction are required")
    gross = to_decimal(gross_cancelled_buyback)
    cancelled = to_decimal(cancelled_shares)
    net_reduction = to_decimal(diluted_net_share_reduction)
    if gross < 0 or cancelled < 0:
        raise CalculationError("buyback cash and cancelled shares cannot be negative")
    if cancelled == 0:
        return ZERO
    factor = min(ONE, max(ZERO, net_reduction) / cancelled)
    return gross * factor


def buyback_persistence_factor(net_reductions: Sequence[Decimal | None]) -> Decimal:
    latest_five = list(net_reductions[:5])
    if not latest_five or any(value is None for value in latest_five):
        raise InsufficientDataError("complete diluted-share changes are required for qB")
    count = sum(to_decimal(value) > 0 for value in latest_five if value is not None)
    for band in policy()["buyback_persistence"]:
        if count >= int(band["minimum_reduction_years"]):
            return to_decimal(band["factor"])
    raise CalculationError("buyback persistence policy has no matching band")


def effective_distribution(ordinary_dividend: Decimal | None, q_b: Decimal, eligible: Decimal) -> Decimal:
    if ordinary_dividend is None:
        raise InsufficientDataError("ordinary dividend is missing")
    dividend = to_decimal(ordinary_dividend)
    if dividend < 0:
        raise CalculationError("ordinary dividend cannot be negative")
    return dividend + to_decimal(q_b) * to_decimal(eligible)


def effective_distribution_v21(
    ordinary_dividend: Decimal | None,
    verified_eligible_buyback: Decimal | None = None,
) -> Decimal:
    """v2.1 distribution: confirmed cash dividend plus verified buyback only.

    Missing buyback evidence is deliberately worth zero contribution.  The
    caller keeps the original buyback metric null and exposes the conservative
    basis/warning; there is no qB persistence multiplier in this definition.
    """

    if ordinary_dividend is None:
        raise InsufficientDataError("ordinary dividend is missing")
    dividend = to_decimal(ordinary_dividend)
    if dividend < 0:
        raise CalculationError("ordinary dividend cannot be negative")
    buyback = ZERO if verified_eligible_buyback is None else to_decimal(verified_eligible_buyback)
    if buyback < 0:
        raise CalculationError("verified eligible buyback cannot be negative")
    return dividend + buyback


def decimal_median(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise InsufficientDataError("median requires at least one value")
    ordered = sorted(to_decimal(value) for value in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def recent_two_year_distribution(newest_first: Sequence[Decimal]) -> Decimal:
    if len(newest_first) < 2:
        raise InsufficientDataError("R2 requires two complete fiscal years")
    weights = decimal_sequence("formula_parameters", "recent_two_year_weights")
    return weights[0] * to_decimal(newest_first[0]) + weights[1] * to_decimal(newest_first[1])


def median_five_year_distribution(newest_first: Sequence[Decimal]) -> Decimal:
    if not newest_first:
        raise InsufficientDataError("M5 requires at least one complete fiscal year")
    return decimal_median([to_decimal(value) for value in newest_first[:5]])


def winsorized_ten_year_distribution(newest_first: Sequence[Decimal]) -> Decimal:
    values = [to_decimal(value) for value in newest_first[:10]]
    if not values:
        raise InsufficientDataError("T10 requires at least one complete fiscal year")
    if len(values) >= integer_value("formula_parameters", "winsorize_minimum_years"):
        ordered = sorted(values)
        adjusted = [ordered[1], *ordered[1:-1], ordered[-2]]
        return sum(adjusted, ZERO) / Decimal(len(adjusted))
    return sum(values, ZERO) / Decimal(len(values))


def historical_conservative_distribution(newest_first: Sequence[Decimal]) -> Decimal:
    values = [to_decimal(value) for value in newest_first[:10]]
    count = len(values)
    if count < 2:
        raise InsufficientDataError("H cannot be calculated with fewer than two fiscal years")
    r2 = recent_two_year_distribution(values)
    m5 = median_five_year_distribution(values)
    if count >= integer_value("formula_parameters", "winsorize_minimum_years"):
        t10 = winsorized_ten_year_distribution(values)
        weights = decimal_sequence("formula_parameters", "history_8_plus_weights")
        weighted = weights[0] * r2 + weights[1] * m5 + weights[2] * t10
        return min(
            weighted,
            decimal_value("formula_parameters", "history_8_plus_median_cap") * m5,
            decimal_value("formula_parameters", "history_8_plus_ten_year_cap") * t10,
        )
    if count >= 5:
        weights = decimal_sequence("formula_parameters", "history_5_7_weights")
        weighted = weights[0] * r2 + weights[1] * m5
        return min(
            weighted,
            decimal_value("formula_parameters", "history_5_7_median_cap") * m5,
        )
    if count >= 3:
        return min(r2, decimal_median(values))
    return min(values[0], r2)


def history_recommendation_cap(year_count: int) -> tuple[Decimal, bool]:
    for band in policy()["history_caps"]:
        if int(band["minimum_years"]) <= year_count <= int(band["maximum_years"]):
            return to_decimal(band["recommendation_cap"]), bool(band["recommendable"])
    raise CalculationError("history cap policy has no matching band")


def company_market_cap(
    share_classes: Sequence[SecurityClassInput],
    *,
    expected_share_classes: Iterable[str],
    now: datetime | None = None,
    stale_hours: int = 24,
) -> Decimal:
    expected = set(expected_share_classes)
    actual = {item.share_class for item in share_classes if item.material}
    if not expected or actual != expected:
        raise InsufficientDataError(f"material share classes incomplete: expected={sorted(expected)} actual={sorted(actual)}")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise CalculationError("current time must be timezone-aware")
    total = ZERO
    for item in share_classes:
        if not item.material:
            continue
        if item.quote_status not in {DataStatus.VALID, DataStatus.KNOWN_ZERO}:
            raise InsufficientDataError(f"quote is not valid for {item.share_class}: {item.quote_status.value}")
        if item.price is None or item.issued_shares is None or item.fx_to_base is None or item.price_timestamp is None:
            raise InsufficientDataError(f"price, shares, FX and timestamp are required for {item.share_class}")
        timestamp = item.price_timestamp
        if timestamp.tzinfo is None:
            raise CalculationError("price timestamp must be timezone-aware")
        age_seconds = (current.astimezone(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
        if age_seconds < 0 or age_seconds > stale_hours * 3600:
            raise InsufficientDataError(f"quote is stale for {item.share_class}")
        price = to_decimal(item.price)
        shares = to_decimal(item.issued_shares)
        fx = to_decimal(item.fx_to_base)
        if price <= 0 or shares <= 0 or fx <= 0:
            raise CalculationError("price, shares and FX must be positive")
        total += price * shares * fx
    return total


def security_prices_at_four_percent(
    sustainable_distribution: Decimal,
    share_classes: Sequence[SecurityClassInput],
) -> dict[str, Decimal]:
    distribution = to_decimal(sustainable_distribution)
    if distribution < 0:
        raise CalculationError("sustainable distribution cannot be negative")
    if not share_classes or any(not item.rights_verified for item in share_classes if item.material):
        raise InsufficientDataError("economic rights must be verified for every material share class")
    if any(
        item.material
        and (
            item.economic_rights_factor is None
            or to_decimal(item.economic_rights_factor) <= 0
        )
        for item in share_classes
    ):
        raise InsufficientDataError("positive economic rights factors are required for every material share class")
    equivalent_shares = sum(
        (
            to_decimal(item.issued_shares) * to_decimal(item.economic_rights_factor)
            for item in share_classes
            if item.material and item.issued_shares is not None
        ),
        ZERO,
    )
    material_count = sum(item.material for item in share_classes)
    available_count = sum(item.material and item.issued_shares is not None and item.fx_to_base is not None for item in share_classes)
    if material_count != available_count or equivalent_shares <= 0:
        raise InsufficientDataError("shares and FX are required for security-level 4% prices")
    company_value = distribution / Decimal("0.04")
    base_price_per_right = company_value / equivalent_shares
    result: dict[str, Decimal] = {}
    for item in share_classes:
        if not item.material:
            continue
        fx = to_decimal(item.fx_to_base)
        if fx <= 0:
            raise CalculationError("FX must be positive")
        result[item.security_id] = base_price_per_right * to_decimal(item.economic_rights_factor) / fx
    return result


def shareholder_yield(distribution: Decimal, market_cap: Decimal) -> Decimal:
    numerator = to_decimal(distribution)
    denominator = to_decimal(market_cap)
    if numerator < 0 or denominator <= 0:
        raise CalculationError("yield requires non-negative distribution and positive market cap")
    return numerator / denominator


def sustainable_distribution_non_financial(historical: Decimal, fcf5: Decimal) -> Decimal:
    historical_value = to_decimal(historical)
    fcf_value = to_decimal(fcf5)
    if historical_value < 0:
        raise CalculationError("H cannot be negative")
    haircut = decimal_value("formula_parameters", "fcf_capacity_haircut")
    return max(ZERO, min(historical_value, haircut * max(fcf_value, ZERO)))


def sustainable_distribution_non_financial_v21(
    historical: Decimal,
    fcf_values: Sequence[Decimal],
    *,
    simplified_fcf: bool,
) -> tuple[Decimal, Decimal]:
    """Return ``(S, median_capacity)`` for the selected 2--5 year FCF window."""

    historical_value = to_decimal(historical)
    if historical_value < 0:
        raise CalculationError("H cannot be negative")
    values = [to_decimal(value) for value in fcf_values[:5]]
    if len(values) < 2:
        raise InsufficientDataError("v2.1 sustainable distribution requires two FCF years")
    capacity = decimal_median(values)
    haircut = decimal_value(
        "formula_parameters",
        "simplified_fcf_capacity_haircut" if simplified_fcf else "fcf_capacity_haircut",
    )
    sustainable = max(ZERO, min(historical_value, haircut * max(capacity, ZERO)))
    return sustainable, capacity


def robust_organic_growth(values_oldest_first: Sequence[Decimal]) -> Decimal:
    values = [to_decimal(value) for value in values_oldest_first]
    if len(values) < 2:
        raise InsufficientDataError("organic growth requires at least two values")
    if all(value > 0 for value in values):
        periods = Decimal(len(values) - 1)
        with localcontext() as context:
            context.prec = 36
            growth = context.power(values[-1] / values[0], ONE / periods) - ONE
    else:
        changes: list[Decimal] = []
        for previous, current in zip(values, values[1:]):
            if previous == 0:
                continue
            changes.append((current - previous) / abs(previous))
        if not changes:
            raise InsufficientDataError("organic growth has no comparable year pairs")
        growth = decimal_median(changes)
    return min(
        decimal_value("formula_parameters", "organic_growth_ceiling"),
        max(decimal_value("formula_parameters", "organic_growth_floor"), growth),
    )


def conservative_growth_contribution(organic_growth: Decimal) -> Decimal:
    growth = to_decimal(organic_growth)
    positive = min(
        decimal_value("formula_parameters", "positive_growth_contribution_cap"),
        decimal_value("formula_parameters", "positive_growth_confirmation") * max(growth, ZERO),
    )
    negative = min(growth, ZERO)
    return positive + negative


def conservative_growth_contribution_v21(
    organic_growth: Decimal,
    *,
    year_count: int,
) -> Decimal:
    """Apply the v2.1 evidence-length cap while retaining negative growth."""

    growth = to_decimal(organic_growth)
    if growth <= 0:
        return growth
    if year_count >= 5:
        return min(decimal_value("formula_parameters", "growth_positive_cap_5y"), growth)
    if year_count >= 3:
        return min(decimal_value("formula_parameters", "growth_positive_cap_3_4y"), growth)
    return min(decimal_value("formula_parameters", "growth_positive_cap_2y"), growth)


def valuation_drag(historical_median: Decimal | None, current: Decimal | None) -> Decimal:
    if historical_median is None or current is None:
        raise InsufficientDataError("comparable current and historical valuations are required")
    historical = to_decimal(historical_median)
    present = to_decimal(current)
    if historical <= 0 or present <= 0:
        raise InsufficientDataError("non-positive valuation metrics are not comparable")
    with localcontext() as context:
        context.prec = 36
        years = Decimal(integer_value("formula_parameters", "valuation_compression_years"))
        compression = context.power(historical / present, ONE / years) - ONE
    return min(ZERO, compression)


def conservative_return_10y(ssy: Decimal, g_cons: Decimal, drag: Decimal) -> Decimal:
    result = to_decimal(ssy) + to_decimal(g_cons) + to_decimal(drag)
    if to_decimal(drag) > 0:
        raise CalculationError("valuation drag may not be positive")
    return result


def return_score(cr10: Decimal) -> Decimal:
    value = to_decimal(cr10)
    return clip(
        decimal_value("formula_parameters", "return_score_center")
        + decimal_value("formula_parameters", "return_score_points_per_1pct")
        * ((value - decimal_value("formula_parameters", "return_score_cr10_center")) / Decimal("0.01"))
    )


def return_score_v21(cr10: Decimal) -> Decimal:
    return clip(
        piecewise_score(
            cr10,
            [
                (to_decimal(point), to_decimal(score))
                for point, score in policy()["return_score_bands_v21"]
            ],
        )
    )


def piecewise_score(value: Decimal, bands: Sequence[tuple[Decimal, Decimal]]) -> Decimal:
    point = to_decimal(value)
    ordered = sorted((to_decimal(x), to_decimal(score)) for x, score in bands)
    if point <= ordered[0][0]:
        return ordered[0][1]
    if point >= ordered[-1][0]:
        return ordered[-1][1]
    for (left_x, left_score), (right_x, right_score) in zip(ordered, ordered[1:]):
        if left_x <= point <= right_x:
            fraction = (point - left_x) / (right_x - left_x)
            return left_score + fraction * (right_score - left_score)
    raise CalculationError("piecewise interpolation failed")


def coverage_score(coverage_ratio: Decimal) -> Decimal:
    return piecewise_score(
        coverage_ratio,
        [(to_decimal(point), to_decimal(score)) for point, score in policy()["coverage_score_bands"]],
    )


def recent_trend_score(r2: Decimal, m5: Decimal, latest: Decimal, previous: Decimal) -> Decimal:
    recent = to_decimal(r2)
    median = to_decimal(m5)
    latest_value = to_decimal(latest)
    previous_value = to_decimal(previous)
    if latest_value == 0:
        return ZERO
    if median <= 0:
        raise InsufficientDataError("trend ratio requires positive M5")
    ratio = recent / median
    obvious_decline = (
        previous_value > 0
        and latest_value / previous_value
        < decimal_value("recent_trend_obvious_decline_ratio")
    )
    if obvious_decline:
        if ratio >= Decimal("0.95"):
            return decimal_value("recent_trend_obvious_decline_score")
    for band in policy()["recent_trend_score_bands"]:
        if obvious_decline and band.get("requires_no_obvious_decline"):
            continue
        if ratio >= to_decimal(band["ratio_min"]):
            return to_decimal(band["score"])
    return ZERO


def coefficient_of_variation(values: Sequence[Decimal]) -> Decimal:
    items = [to_decimal(value) for value in values]
    if not items:
        raise InsufficientDataError("CV requires values")
    mean = sum(items, ZERO) / Decimal(len(items))
    if mean == 0:
        if all(value == 0 for value in items):
            return ZERO
        raise CalculationError("CV is undefined when non-zero values have zero mean")
    variance = sum(((value - mean) ** 2 for value in items), ZERO) / Decimal(len(items))
    with localcontext() as context:
        context.prec = 36
        return context.sqrt(variance) / abs(mean)


def history_stability_score(newest_first: Sequence[Decimal]) -> Decimal:
    values = [to_decimal(value) for value in newest_first[:5]]
    if not values:
        raise InsufficientDataError("stability requires distribution history")
    if values[0] == 0:
        return ZERO
    interruptions = sum(value == 0 for value in values)
    cv = coefficient_of_variation(values)
    scores = decimal_mapping("history_stability_bands")
    thresholds = policy()["history_stability_thresholds"]
    if interruptions == 0 and cv <= to_decimal(thresholds["excellent_cv_max"]):
        return scores["no_interruption_cv_015"]
    if interruptions == 0 and cv <= to_decimal(thresholds["good_cv_max"]):
        return scores["no_interruption_cv_030"]
    if interruptions <= int(thresholds["acceptable_interruptions_max"]) or cv <= to_decimal(thresholds["acceptable_cv_max"]):
        return scores["one_interruption_or_cv_050"]
    return scores["otherwise"]


def buyback_quality_score(
    q_b: Decimal,
    *,
    has_buyback: bool,
    has_material_dilution: bool,
    net_reduction: Decimal,
) -> Decimal:
    factor = to_decimal(q_b)
    net = to_decimal(net_reduction)
    if has_material_dilution or (has_buyback and net <= 0):
        return ZERO
    scores = decimal_mapping("buyback_quality_scores")
    if not has_buyback:
        return scores["no_buyback_no_dilution"]
    return scores.get(format(factor, "f"), scores["failed_net_reduction"])


def weighted_score(values: Mapping[str, Decimal], weights: Mapping[str, Decimal]) -> Decimal:
    if set(values) != set(weights):
        raise InsufficientDataError(f"score components mismatch: values={sorted(values)} weights={sorted(weights)}")
    total_weight = sum((to_decimal(value) for value in weights.values()), ZERO)
    if total_weight <= 0:
        raise CalculationError("score weights must be positive")
    result = sum((clip(values[key]) * to_decimal(weights[key]) for key in values), ZERO) / total_weight
    return clip(result)


def payout_quality_score(components: Mapping[str, Decimal]) -> Decimal:
    weights = decimal_mapping("score_weights", "payout_quality_legacy")
    return weighted_score(components, weights)


def payout_quality_score_v21(components: Mapping[str, Decimal]) -> Decimal:
    return weighted_score(components, decimal_mapping("score_weights", "payout_quality"))


def recommendation_index_v21(
    *,
    return_score_value: Decimal,
    payout_quality_value: Decimal,
    business_durability: Decimal | None,
    governance_capital_allocation: Decimal | None,
) -> tuple[Decimal, bool]:
    complete = business_durability is not None and governance_capital_allocation is not None
    return (
        weighted_score(
            {
                "return_score": to_decimal(return_score_value),
                "payout_quality": to_decimal(payout_quality_value),
                "business_durability": (
                    decimal_value("recommendation_defaults_v21", "missing_business_durability")
                    if business_durability is None
                    else to_decimal(business_durability)
                ),
                "governance_capital_allocation": (
                    decimal_value(
                        "recommendation_defaults_v21",
                        "missing_governance_capital_allocation",
                    )
                    if governance_capital_allocation is None
                    else to_decimal(governance_capital_allocation)
                ),
            },
            decimal_mapping("score_weights", "recommendation"),
        ),
        complete,
    )


def entry_risk_index_v21(
    components: Mapping[str, Decimal],
    *,
    unknown_veto_uplift: Decimal = ZERO,
    triggered_warning_uplift: Decimal = ZERO,
) -> Decimal:
    base = weighted_score(components, decimal_mapping("score_weights", "entry_risk"))
    return clip(base + to_decimal(unknown_veto_uplift) + to_decimal(triggered_warning_uplift))


def recommendation_index(
    *,
    return_score_value: Decimal,
    payout_quality_value: Decimal,
    business_durability: Decimal | None,
    governance_capital_allocation: Decimal | None,
    history_years: int,
) -> tuple[Decimal, bool]:
    components = {
        "return_score": to_decimal(return_score_value),
        "payout_quality": to_decimal(payout_quality_value),
    }
    policy_weights = decimal_mapping("score_weights", "recommendation")
    weights = {key: policy_weights[key] for key in components}
    complete = business_durability is not None and governance_capital_allocation is not None
    if business_durability is not None:
        components["business_durability"] = to_decimal(business_durability)
        weights["business_durability"] = policy_weights["business_durability"]
    if governance_capital_allocation is not None:
        components["governance"] = to_decimal(governance_capital_allocation)
        weights["governance"] = policy_weights["governance_capital_allocation"]
    score = weighted_score(components, weights)
    if not complete:
        score = min(score, decimal_value("formula_parameters", "incomplete_recommendation_cap"))
    history_cap, _ = history_recommendation_cap(history_years)
    return min(score, history_cap), complete


def entry_risk_index(components: Mapping[str, Decimal]) -> Decimal:
    weights = decimal_mapping("score_weights", "entry_risk_legacy")
    return weighted_score(components, weights)


def recommendation_class(
    ri: Decimal | None,
    eri: Decimal | None,
    *,
    unresolved_veto: bool,
    major_veto: bool,
    data_complete: bool,
) -> str:
    if major_veto:
        return "D"
    if ri is None or eri is None:
        return "C"
    recommendation = to_decimal(ri)
    risk = to_decimal(eri)
    bands = policy()["classification_bands"]
    if recommendation < to_decimal(bands["D"]["ri_below"]) or risk > to_decimal(bands["D"]["eri_above"]):
        return "D"
    if not data_complete:
        return "C"
    if recommendation >= to_decimal(bands["A"]["ri_min"]) and risk <= to_decimal(bands["A"]["eri_max"]) and not unresolved_veto and data_complete:
        return "A"
    if recommendation >= to_decimal(bands["B"]["ri_min"]) and risk <= to_decimal(bands["B"]["eri_max"]) and not major_veto:
        return "B"
    return "C"


def return_type(ssy: Decimal | None, cr10: Decimal | None) -> str | None:
    if ssy is None or cr10 is None:
        return None
    threshold = decimal_value("thresholds", "cash_anchor_yield")
    if to_decimal(ssy) >= threshold:
        return "CASH_ANCHORED"
    if to_decimal(cr10) >= threshold:
        return "GROWTH_SUPPLEMENTED"
    return "BELOW_THRESHOLD"
