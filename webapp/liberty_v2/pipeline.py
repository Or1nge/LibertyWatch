from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .calculations import (
    CalculationError,
    InsufficientDataError,
    buyback_persistence_factor,
    buyback_quality_score,
    company_market_cap,
    conservative_growth_contribution,
    conservative_return_10y,
    coverage_score,
    effective_distribution,
    eligible_buyback,
    entry_risk_index,
    historical_conservative_distribution,
    history_stability_score,
    median_five_year_distribution,
    payout_quality_score,
    recent_trend_score,
    recent_two_year_distribution,
    recommendation_class,
    recommendation_index,
    return_score,
    return_type,
    robust_organic_growth,
    security_prices_at_four_percent,
    shareholder_yield,
    to_decimal,
    valuation_drag,
    winsorized_ten_year_distribution,
)
from .constants import CALCULATION_VERSION, METRIC_DEFINITION_VERSION, SCHEMA_VERSION
from .coverage import (
    BankCapitalAdapter,
    BankCapitalInput,
    DistributionCoverageAdapter,
    InsuranceSurplusAdapter,
    InsuranceSurplusInput,
    NonFinancialFCFAdapter,
    UnsupportedAdapter,
    ensure_non_financial_adapter,
)
from .models import (
    AnnualDistribution,
    CoverageResult,
    CoverageStatus,
    DataStatus,
    FCFYear,
    IndustryKind,
    MetricRecord,
    PublicationStatus,
    SecurityClassInput,
    StructuredConfigScore,
    VetoFlag,
    jsonable,
)
from .veto import evaluate_vetoes
from .validation import (
    merge_validation_results,
    required_provenance_field_ids,
    validate_accounting_reconciliations,
    validate_raw_provenance_records,
    validate_required_provenance_fields,
)
from .policy import decimal_value


ZERO = Decimal("0")
STATIC_VETO_CONFIG_KEYS = (
    "core_asset_expires_within_10y",
    "major_committed_capex_or_ma",
    "qualified_audit_opinion",
    "material_internal_control_weakness",
    "controlling_shareholder_fund_occupation",
    "major_related_party_alert",
    "major_regulatory_penalty",
)


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None or value == "" else to_decimal(value)


def _date_or_none(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _datetime_or_none(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _public_source_summary(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "[摘要层级已截断]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:100]:
            normalized = str(key).lower()
            if any(token in normalized for token in ("credential", "secret", "token", "private_key", "stderr", "jsonl")):
                continue
            result[str(key)] = _public_source_summary(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_public_source_summary(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        if value.startswith(("/", "~/")) or "/home/" in value or "/var/lib/" in value:
            return "[Linux本地证据已索引，公网不披露路径]"
        return value[:2000]
    return jsonable(value)


def _display(value: Decimal | None, *, kind: str = "number") -> str:
    if value is None:
        return "数据不足"
    if kind == "percent":
        return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"
    if kind == "score":
        return f"{value.quantize(Decimal('0.1'))}"
    if kind == "multiple":
        return f"{value.quantize(Decimal('0.01'))}×"
    return format(value, "f")


def metric(
    value: Decimal | None,
    *,
    status: str | None = None,
    kind: str = "number",
    reason: str | None = None,
    unit: str | None = None,
) -> MetricRecord:
    if status is None:
        status = "INSUFFICIENT_DATA" if value is None else ("KNOWN_ZERO" if value == ZERO else "VALID")
    display = _display(value, kind=kind) if value is not None else (reason or "数据不足")
    return MetricRecord(value=value, status=status, display=display, reason=reason, unit=unit)


@dataclass(frozen=True)
class SlowVariables:
    annual_effective_distributions: tuple[tuple[int, Decimal], ...]
    q_b: Decimal | None
    r2: Decimal | None
    m5: Decimal | None
    t10: Decimal | None
    historical_distribution: Decimal | None
    coverage: CoverageResult
    organic_growth: Decimal | None
    conservative_growth: Decimal | None
    payout_quality: Decimal | None
    eri: Decimal | None
    veto_flags: tuple[VetoFlag, ...]
    business_durability: Decimal | None
    governance: Decimal | None
    qualitative_overlay_pending: bool
    errors: tuple[str, ...]


def _annual_rows(
    raw: Sequence[Mapping[str, Any]],
    *,
    on_date: date | None = None,
) -> list[AnnualDistribution]:
    cutoff = on_date or date.today()
    rows: list[AnnualDistribution] = []
    allowed_dividend_statuses = {
        "PAID",
        "SHAREHOLDER_APPROVED",
        "LEGAL_COMMITMENT",
        "NO_DISTRIBUTION",
        "NOT_DISCLOSED",
    }
    for item in raw:
        if item.get("period_type") != "FULL_YEAR":
            raise InsufficientDataError("annual distributions require an explicit FULL_YEAR period")
        fiscal_year_end = _date_or_none(item.get("fiscal_year_end_date"))
        if fiscal_year_end is None or fiscal_year_end > cutoff:
            raise InsufficientDataError("fiscal year end date is missing or not yet complete")
        dividend_status = str(item.get("ordinary_dividend_status") or "")
        if dividend_status not in allowed_dividend_statuses:
            raise InsufficientDataError("ordinary dividend approval/payment status is missing or ineligible")
        ordinary = _decimal_or_none(item.get("ordinary_dividend"))
        if dividend_status in {"NO_DISTRIBUTION", "NOT_DISCLOSED"} and ordinary not in {None, ZERO}:
            raise CalculationError("ordinary dividend conflicts with its disclosure status")
        rows.append(
            AnnualDistribution(
                fiscal_year=int(item["fiscal_year"]),
                ordinary_dividend=ordinary,
                special_dividend=_decimal_or_none(item.get("special_dividend")),
                gross_cancelled_buyback=_decimal_or_none(item.get("gross_cancelled_buyback")),
                cancelled_shares=_decimal_or_none(item.get("cancelled_shares")),
                diluted_net_share_reduction=_decimal_or_none(item.get("diluted_net_share_reduction")),
            )
        )
    years = [row.fiscal_year for row in rows]
    if len(years) != len(set(years)):
        raise CalculationError("duplicate fiscal years are forbidden")
    return sorted(rows, key=lambda row: row.fiscal_year, reverse=True)[:10]


def _structured_score(value: Any, on_date: date) -> Decimal | None:
    if not isinstance(value, Mapping):
        return None
    score = StructuredConfigScore(
        value=_decimal_or_none(value.get("value")),
        source=str(value.get("source") or ""),
        as_of_date=_date_or_none(value.get("as_of_date")),
        expires_at=_date_or_none(value.get("expires_at")),
        reason=str(value.get("reason") or ""),
    )
    if not score.is_current(on_date):
        return None
    assert score.value is not None
    if not ZERO <= score.value <= Decimal("100"):
        raise CalculationError("structured scores must be within 0..100")
    return score.value


def _coverage_adapter(raw: Mapping[str, Any], industry: IndustryKind) -> DistributionCoverageAdapter:
    coverage = raw.get("coverage") if isinstance(raw.get("coverage"), Mapping) else {}
    if industry is IndustryKind.NON_FINANCIAL:
        years = [
            FCFYear(
                fiscal_year=int(item["fiscal_year"]),
                operating_cash_flow=_decimal_or_none(item.get("operating_cash_flow")),
                capital_expenditure=_decimal_or_none(item.get("capital_expenditure")),
                lease_principal_repayment=_decimal_or_none(item.get("lease_principal_repayment")),
            )
            for item in coverage.get("fcf_years", [])
        ]
        return NonFinancialFCFAdapter(tuple(sorted(years, key=lambda row: row.fiscal_year, reverse=True)))
    if industry is IndustryKind.BANK:
        return BankCapitalAdapter(
            BankCapitalInput(
                **{
                    name: _decimal_or_none(coverage.get(name))
                    for name in BankCapitalInput.__dataclass_fields__
                }
            )
        )
    if industry is IndustryKind.INSURANCE:
        return InsuranceSurplusAdapter(
            InsuranceSurplusInput(
                **{
                    name: _decimal_or_none(coverage.get(name))
                    for name in InsuranceSurplusInput.__dataclass_fields__
                }
            )
        )
    return UnsupportedAdapter(industry)


def _organic_growth_values(
    raw: Mapping[str, Any],
    *,
    industry: IndustryKind,
    on_date: date,
) -> list[Decimal]:
    expected_metric = {
        IndustryKind.NON_FINANCIAL: "normalized_fcf",
        IndustryKind.BANK: "adjusted_net_income_or_capital_generation",
        IndustryKind.INSURANCE: "free_surplus_generation",
    }.get(industry)
    if expected_metric is None or raw.get("organic_growth_metric") != expected_metric:
        raise InsufficientDataError("industry-appropriate organic growth metric is missing")
    items = raw.get("organic_growth_series")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise InsufficientDataError("organic growth history must be an annual series")
    annual: list[tuple[int, Decimal]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise InsufficientDataError("organic growth history requires fiscal-year metadata")
        if item.get("period_type") != "FULL_YEAR":
            raise InsufficientDataError("organic growth history may only use complete fiscal years")
        fiscal_end = _date_or_none(item.get("fiscal_year_end_date"))
        if fiscal_end is None or fiscal_end > on_date:
            raise InsufficientDataError("organic growth fiscal period is incomplete")
        value = _decimal_or_none(item.get("value"))
        if value is None:
            raise InsufficientDataError("organic growth value is missing")
        annual.append((int(item["fiscal_year"]), value))
    annual = sorted(annual, key=lambda item: item[0])[-5:]
    years = [year for year, _value in annual]
    if len(years) < 2 or len(years) != len(set(years)):
        raise InsufficientDataError("organic growth requires at least two unique fiscal years")
    if any(right - left != 1 for left, right in zip(years, years[1:])):
        raise InsufficientDataError("organic growth fiscal years must be continuous")
    return [value for _year, value in annual]


def _valuation_inputs(
    raw: Mapping[str, Any],
    *,
    industry: IndustryKind,
) -> tuple[Decimal | None, Decimal | None]:
    valuation = raw.get("valuation") if isinstance(raw.get("valuation"), Mapping) else {}
    metric_name = str(valuation.get("metric") or "")
    allowed = {
        IndustryKind.NON_FINANCIAL: {"P_FCF", "EV_EBIT"},
        IndustryKind.BANK: {"P_B_WITH_ROE"},
        IndustryKind.INSURANCE: {"P_EV", "P_B_WITH_ROE"},
    }.get(industry, set())
    if metric_name not in allowed or valuation.get("basis_consistent") is not True:
        raise InsufficientDataError("valuation metric is unsupported or not comparable")
    if metric_name == "P_B_WITH_ROE" and any(
        _decimal_or_none(valuation.get(field)) is None
        for field in ("current_roe", "historical_median_roe")
    ):
        raise InsufficientDataError("P/B valuation requires comparable current and historical ROE")
    return (
        _decimal_or_none(valuation.get("historical_median")),
        _decimal_or_none(valuation.get("current")),
    )


def _annual_total_distribution(item: Mapping[str, Any]) -> Decimal | None:
    """Return actual surface distribution without treating absent components as zero."""
    explicit = _decimal_or_none(item.get("total_distribution"))
    if explicit is not None:
        return explicit
    required = (
        "ordinary_dividend",
        "special_dividend",
        "gross_cancelled_buyback",
        "asset_sale_distribution",
    )
    if any(item.get(field) is None for field in required):
        return None
    return sum((to_decimal(item[field]) for field in required), ZERO)


def _derive_veto_inputs(
    raw: Mapping[str, Any],
    rows: Sequence[AnnualDistribution],
    *,
    industry: IndustryKind,
    on_date: date,
) -> tuple[dict[str, Any], list[str]]:
    """Derive threshold flags from immutable structured inputs when data is complete."""
    configured = raw.get("veto_inputs")
    configured = configured if isinstance(configured, Mapping) else {}
    values: dict[str, Any] = {}
    errors: list[str] = []
    for key in STATIC_VETO_CONFIG_KEYS:
        item = configured.get(key)
        if not isinstance(item, Mapping):
            errors.append(f"veto_config:{key}:missing versioned source")
            continue
        as_of = _date_or_none(item.get("as_of_date"))
        expires = _date_or_none(item.get("expires_at"))
        if (
            not str(item.get("source") or "").strip()
            or not str(item.get("reason") or "").strip()
            or as_of is None
            or expires is None
            or not as_of <= on_date <= expires
            or not isinstance(item.get("value"), bool)
        ):
            errors.append(f"veto_config:{key}:missing, expired or invalid metadata")
            continue
        values[key] = item["value"]
    annual_raw = {
        int(item["fiscal_year"]): item
        for item in raw.get("annual_distributions", [])
        if isinstance(item, Mapping) and item.get("fiscal_year") is not None
    }
    if rows:
        latest = rows[0]
        values["ordinary_dividend_latest"] = latest.ordinary_dividend
        values["claimed_buyback"] = latest.gross_cancelled_buyback
        values["diluted_net_share_reduction"] = latest.diluted_net_share_reduction
        if len(rows) > 1:
            values["ordinary_dividend_previous"] = rows[1].ordinary_dividend

        latest_raw = annual_raw.get(latest.fiscal_year, {})
        total = _annual_total_distribution(latest_raw)
        one_off_fields = (
            latest.special_dividend,
            _decimal_or_none(latest_raw.get("asset_sale_distribution")),
            _decimal_or_none(latest_raw.get("one_off_buyback")),
        )
        if total is not None and all(value is not None for value in one_off_fields):
            values["surface_distribution"] = total
            values["one_off_distribution"] = sum(
                (value for value in one_off_fields if value is not None),
                ZERO,
            )

    coverage = raw.get("coverage") if isinstance(raw.get("coverage"), Mapping) else {}
    if industry is IndustryKind.NON_FINANCIAL and len(rows) >= 2:
        fcf_by_year: dict[int, Decimal] = {}
        for item in coverage.get("fcf_years", []):
            if not isinstance(item, Mapping) or item.get("fiscal_year") is None:
                continue
            fields = (
                item.get("operating_cash_flow"),
                item.get("capital_expenditure"),
                item.get("lease_principal_repayment"),
            )
            if all(value is not None for value in fields):
                fcf_by_year[int(item["fiscal_year"])] = (
                    to_decimal(fields[0]) - to_decimal(fields[1]) - to_decimal(fields[2])
                )
        over_limit: list[bool] = []
        for row in rows[:2]:
            total = _annual_total_distribution(annual_raw.get(row.fiscal_year, {}))
            fcf = fcf_by_year.get(row.fiscal_year)
            if total is None or fcf is None:
                over_limit = []
                break
            over_limit.append(
                total > decimal_value("thresholds", "fcf_overdistribution_ratio") * fcf
            )
        if len(over_limit) == 2:
            values["two_year_distribution_over_fcf_125"] = all(over_limit)

    balance_history = raw.get("balance_sheet_history")
    if isinstance(balance_history, Sequence) and not isinstance(balance_history, (str, bytes)):
        ordered = sorted(
            (
                item
                for item in balance_history
                if isinstance(item, Mapping)
                and item.get("fiscal_year") is not None
                and item.get("net_debt") is not None
            ),
            key=lambda item: int(item["fiscal_year"]),
            reverse=True,
        )
        if len(ordered) >= 2:
            values["net_debt_increased"] = to_decimal(ordered[0]["net_debt"]) > to_decimal(
                ordered[1]["net_debt"]
            )

    if industry is IndustryKind.BANK:
        cet1 = _decimal_or_none(coverage.get("cet1_ratio"))
        minimum = _decimal_or_none(coverage.get("cet1_regulatory_minimum"))
        if cet1 is not None and minimum is not None:
            values["financial_capital_buffer_near_minimum"] = (
                cet1 - minimum
                <= decimal_value("industry_adapters", "bank", "minimum_capital_buffer")
            )
    elif industry is IndustryKind.INSURANCE:
        comprehensive = _decimal_or_none(coverage.get("comprehensive_solvency_ratio"))
        comprehensive_minimum = _decimal_or_none(coverage.get("comprehensive_solvency_minimum"))
        core = _decimal_or_none(coverage.get("core_solvency_ratio"))
        core_minimum = _decimal_or_none(coverage.get("core_solvency_minimum"))
        if all(
            value is not None
            for value in (comprehensive, comprehensive_minimum, core, core_minimum)
        ):
            values["financial_capital_buffer_near_minimum"] = min(
                comprehensive - comprehensive_minimum,  # type: ignore[operator]
                core - core_minimum,  # type: ignore[operator]
            ) <= decimal_value(
                "industry_adapters", "insurance", "minimum_solvency_buffer"
            )
    return values, errors


def compute_slow_variables(raw: Mapping[str, Any], *, on_date: date | None = None) -> SlowVariables:
    current_date = on_date or date.today()
    provenance = merge_validation_results(
        validate_raw_provenance_records(
            raw.get("raw_data_points"),
            expected_company_id=str(raw.get("company_id") or ""),
        ),
        validate_required_provenance_fields(
            raw.get("raw_data_points"),
            required_provenance_field_ids(raw),
        ),
    )
    errors: list[str] = [
        f"provenance:{issue.code}:{issue.field}"
        for issue in provenance.issues
        if issue.severity == "ERROR"
    ]
    q_b: Decimal | None = None
    rows: list[AnnualDistribution] = []
    effective: list[tuple[int, Decimal]] = []
    r2 = m5 = t10 = historical = None
    try:
        rows = _annual_rows(raw.get("annual_distributions", []), on_date=current_date)
        q_b = buyback_persistence_factor([row.diluted_net_share_reduction for row in rows[:5]])
        for row in rows:
            eligible = eligible_buyback(
                row.gross_cancelled_buyback,
                row.cancelled_shares,
                row.diluted_net_share_reduction,
            )
            effective.append((row.fiscal_year, effective_distribution(row.ordinary_dividend, q_b, eligible)))
        values = [value for _, value in effective]
        r2 = recent_two_year_distribution(values)
        m5 = median_five_year_distribution(values)
        t10 = winsorized_ten_year_distribution(values)
        historical = historical_conservative_distribution(values)
    except CalculationError as error:
        errors.append(f"distribution:{error}")

    try:
        industry = IndustryKind(str(raw.get("industry_kind", "UNSUPPORTED")))
    except ValueError:
        industry = IndustryKind.UNSUPPORTED
        errors.append("coverage:unknown industry kind")
    adapter = _coverage_adapter(raw, industry)
    ensure_non_financial_adapter(industry, adapter)
    if historical is None:
        coverage = CoverageResult(
            status=CoverageStatus.INSUFFICIENT_DATA,
            adapter=adapter.name,
            sustainable_distribution=None,
            coverage_ratio=None,
            capacity=None,
            required_missing_fields=("historical_conservative_distribution",),
        )
    else:
        coverage = adapter.calculate(historical)
        if coverage.status is CoverageStatus.INSUFFICIENT_DATA:
            errors.append("coverage:" + ",".join(coverage.required_missing_fields))

    organic = conservative_growth = None
    try:
        growth_values = _organic_growth_values(
            raw,
            industry=industry,
            on_date=current_date,
        )
        organic = robust_organic_growth(growth_values)
        conservative_growth = conservative_growth_contribution(organic)
    except CalculationError as error:
        errors.append(f"growth:{error}")

    score_config = raw.get("structured_scores") if isinstance(raw.get("structured_scores"), Mapping) else {}
    reviewed_score_config = (
        raw.get("reviewed_overlay_scores")
        if isinstance(raw.get("reviewed_overlay_scores"), Mapping)
        else {}
    )
    balance_sheet = _structured_score(score_config.get("balance_sheet"), current_date)
    data_completeness = _structured_score(score_config.get("data_completeness"), current_date)
    # A current manually maintained configuration always wins. Codex-reviewed
    # values are a separate, audited fallback and never overwrite raw inputs.
    durability = _structured_score(score_config.get("business_durability"), current_date)
    if durability is None:
        durability = _structured_score(reviewed_score_config.get("business_durability"), current_date)
    governance = _structured_score(score_config.get("governance_capital_allocation"), current_date)
    if governance is None:
        governance = _structured_score(
            reviewed_score_config.get("governance_capital_allocation"),
            current_date,
        )

    payout = None
    trend = stability = coverage_component = buyback_component = None
    if effective and r2 is not None and m5 is not None and len(effective) >= 2:
        try:
            trend = recent_trend_score(r2, m5, effective[0][1], effective[1][1])
            stability = history_stability_score([value for _, value in effective])
            if coverage.coverage_ratio is not None:
                coverage_component = coverage_score(coverage.coverage_ratio)
            has_buyback = any(
                _decimal_or_none(item.get("gross_cancelled_buyback")) not in {None, ZERO}
                for item in raw.get("annual_distributions", [])
            )
            latest_net = rows[0].diluted_net_share_reduction if rows else None
            if q_b is not None and latest_net is not None:
                buyback_component = buyback_quality_score(
                    q_b,
                    has_buyback=has_buyback,
                    has_material_dilution=bool(raw.get("has_material_dilution")),
                    net_reduction=latest_net,
                )
            components = {
                "coverage": coverage_component,
                "recent_trend": trend,
                "history_stability": stability,
                "balance_sheet": balance_sheet,
                "buyback_quality": buyback_component,
                "data_completeness": data_completeness,
            }
            if all(value is not None for value in components.values()):
                payout = payout_quality_score({key: value for key, value in components.items() if value is not None})
            else:
                errors.append("payout_quality:structured subscore missing or expired")
        except CalculationError as error:
            errors.append(f"payout_quality:{error}")

    risk_config = raw.get("risk_scores") if isinstance(raw.get("risk_scores"), Mapping) else {}
    eri = None
    risk_components = {
        "distribution_deterioration": Decimal("100") - trend if trend is not None else None,
        "coverage": Decimal("100") - coverage_component if coverage_component is not None else None,
        "balance_sheet": Decimal("100") - balance_sheet if balance_sheet is not None else None,
        "structural_cycle": _structured_score(risk_config.get("structural_cycle"), current_date),
        "policy_asset_life": _structured_score(risk_config.get("policy_asset_life"), current_date),
        "valuation_trap": _structured_score(risk_config.get("valuation_trap"), current_date),
        "governance": Decimal("100") - governance if governance is not None else None,
        "data_quality": Decimal("100") - data_completeness if data_completeness is not None else None,
    }
    if all(value is not None for value in risk_components.values()):
        eri = entry_risk_index({key: value for key, value in risk_components.items() if value is not None})
    else:
        errors.append("entry_risk:structured risk component missing or expired")

    qualitative_overlay_pending = bool(
        (durability is None or governance is None)
        and payout is not None
        and all(
            value is not None
            for key, value in risk_components.items()
            if key != "governance"
        )
    )

    veto_inputs, veto_config_errors = _derive_veto_inputs(
        raw,
        rows,
        industry=industry,
        on_date=current_date,
    )
    errors.extend(veto_config_errors)
    veto_inputs.setdefault(
        "key_source_validation_failed",
        any(issue.severity == "ERROR" for issue in provenance.issues),
    )
    veto_flags = tuple(evaluate_vetoes(veto_inputs))
    return SlowVariables(
        annual_effective_distributions=tuple(effective),
        q_b=q_b,
        r2=r2,
        m5=m5,
        t10=t10,
        historical_distribution=historical,
        coverage=coverage,
        organic_growth=organic,
        conservative_growth=conservative_growth,
        payout_quality=payout,
        eri=eri,
        veto_flags=veto_flags,
        business_durability=durability,
        governance=governance,
        qualitative_overlay_pending=qualitative_overlay_pending,
        errors=tuple(errors),
    )


def _security_classes(raw: Sequence[Mapping[str, Any]]) -> list[SecurityClassInput]:
    result: list[SecurityClassInput] = []
    for item in raw:
        try:
            quote_status = DataStatus(str(item.get("quote_status", "MISSING")))
        except ValueError:
            quote_status = DataStatus.MISSING
        result.append(
            SecurityClassInput(
                security_id=str(item.get("security_id") or ""),
                share_class=str(item.get("share_class") or ""),
                price=_decimal_or_none(item.get("price")),
                issued_shares=_decimal_or_none(item.get("issued_shares")),
                currency=str(item.get("currency") or ""),
                fx_to_base=_decimal_or_none(item.get("fx_to_base")),
                price_timestamp=_datetime_or_none(item.get("price_timestamp")),
                quote_status=quote_status,
                rights_verified=bool(item.get("rights_verified")),
                economic_rights_factor=_decimal_or_none(item.get("economic_rights_factor")),
                material=item.get("material") is not False,
            )
        )
    return result


def compute_fast_variables(
    raw: Mapping[str, Any],
    slow: SlowVariables,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    errors = list(slow.errors)
    market_cap = raw_yield = ssy = four_pct_value = drag = cr10 = return_score_value = ri = None
    ri_complete = False
    prices_4pct: dict[str, Decimal] = {}
    share_classes = _security_classes(raw.get("share_classes", []))
    expected = [str(value) for value in raw.get("expected_share_classes", [])]
    try:
        market_cap = company_market_cap(
            share_classes,
            expected_share_classes=expected,
            now=now,
            stale_hours=int(raw.get("stale_quote_hours", 24)),
        )
        if slow.r2 is not None:
            raw_yield = shareholder_yield(slow.r2, market_cap)
        if slow.coverage.sustainable_distribution is not None:
            ssy = shareholder_yield(slow.coverage.sustainable_distribution, market_cap)
            four_pct_value = slow.coverage.sustainable_distribution / Decimal("0.04")
            prices_4pct = security_prices_at_four_percent(slow.coverage.sustainable_distribution, share_classes)
    except CalculationError as error:
        errors.append(f"market_cap:{error}")

    reconciliation = raw.get("reconciliation_inputs")
    if not isinstance(reconciliation, Mapping):
        errors.append("reconciliation:inputs_missing")
    else:
        try:
            class_caps = [
                to_decimal(item.price)
                * to_decimal(item.issued_shares)
                * to_decimal(item.fx_to_base)
                for item in share_classes
                if item.material
                and item.price is not None
                and item.issued_shares is not None
                and item.fx_to_base is not None
            ]
            latest_rows = raw.get("annual_distributions", [])
            latest = latest_rows[0] if latest_rows else {}
            result = validate_accounting_reconciliations(
                total_market_cap=market_cap,
                share_class_market_caps=class_caps or None,
                total_dividend=_decimal_or_none(latest.get("ordinary_dividend")),
                dividend_per_share_times_entitled_shares=_decimal_or_none(
                    reconciliation.get("dividend_per_share_times_entitled_shares")
                ),
                buyback_cash=_decimal_or_none(latest.get("gross_cancelled_buyback")),
                repurchased_shares_times_average_price=_decimal_or_none(
                    reconciliation.get("repurchased_shares_times_average_price")
                ),
                opening_minus_closing_shares=_decimal_or_none(
                    reconciliation.get("opening_minus_closing_shares")
                ),
                cancelled_minus_issued_and_converted=_decimal_or_none(
                    reconciliation.get("cancelled_minus_issued_and_converted")
                ),
                relative_tolerance=decimal_value(
                    "reconciliation_tolerances", "relative"
                ),
                absolute_tolerance=decimal_value(
                    "reconciliation_tolerances",
                    "absolute_currency_minor_units",
                ),
                share_count_relative_tolerance=decimal_value(
                    "reconciliation_tolerances", "share_count_relative"
                ),
            )
            errors.extend(
                f"reconciliation:{issue.code}:{issue.field}"
                for issue in result.issues
                if issue.severity == "ERROR"
            )
        except CalculationError as error:
            errors.append(f"reconciliation:{error}")

    try:
        try:
            industry = IndustryKind(str(raw.get("industry_kind", "UNSUPPORTED")))
        except ValueError:
            industry = IndustryKind.UNSUPPORTED
        historical_valuation, current_valuation = _valuation_inputs(
            raw,
            industry=industry,
        )
        drag = valuation_drag(historical_valuation, current_valuation)
    except CalculationError as error:
        errors.append(f"valuation:{error}")
    if ssy is not None and slow.conservative_growth is not None and drag is not None:
        cr10 = conservative_return_10y(ssy, slow.conservative_growth, drag)
        return_score_value = return_score(cr10)
        if slow.payout_quality is not None:
            ri, ri_complete = recommendation_index(
                return_score_value=return_score_value,
                payout_quality_value=slow.payout_quality,
                business_durability=slow.business_durability,
                governance_capital_allocation=slow.governance,
                history_years=len(slow.annual_effective_distributions),
            )

    triggered_vetoes = [flag for flag in slow.veto_flags if flag.triggered]
    recommendation_blocked = any(
        error.startswith(("provenance:", "market_cap:", "reconciliation:"))
        for error in errors
    )
    if recommendation_blocked:
        ri = None
        ri_complete = False
    data_complete = bool(
        market_cap is not None
        and ssy is not None
        and cr10 is not None
        and ri is not None
        and slow.eri is not None
        and ri_complete
        and slow.coverage.status is CoverageStatus.VALID
        and not errors
    )
    permitted_bootstrap_errors = {
        "entry_risk:structured risk component missing or expired",
    }
    qualitative_bootstrap_eligible = bool(
        slow.qualitative_overlay_pending
        and market_cap is not None
        and ssy is not None
        and cr10 is not None
        and return_score_value is not None
        and slow.payout_quality is not None
        and slow.coverage.status is CoverageStatus.VALID
        and all(error in permitted_bootstrap_errors for error in errors)
    )
    if data_complete:
        data_status = PublicationStatus.VALID
    elif any("stale" in error.lower() for error in errors):
        data_status = PublicationStatus.STALE
    elif any(
        error.startswith(("provenance:", "reconciliation:"))
        or (error.startswith("market_cap:") and "incomplete" in error)
        for error in errors
    ):
        data_status = PublicationStatus.INVALID
    else:
        data_status = PublicationStatus.PARTIAL
    category = recommendation_class(
        ri,
        slow.eri,
        unresolved_veto=bool(triggered_vetoes) or recommendation_blocked,
        major_veto=(
            any(flag.severity == "MAJOR" for flag in triggered_vetoes)
            or recommendation_blocked
        ),
        data_complete=data_complete,
    )
    return {
        "market_cap": market_cap,
        "raw_yield": raw_yield,
        "ssy": ssy,
        "four_pct_value": four_pct_value,
        "security_prices_4pct": prices_4pct,
        "valuation_drag": drag,
        "cr10": cr10,
        "return_score": return_score_value,
        "recommendation_index": ri,
        "recommendation_complete": ri_complete,
        "entry_risk_index": slow.eri,
        "classification": category,
        "return_type": return_type(ssy, cr10),
        "data_status": data_status,
        "analysis_eligibility": {
            "eligible": data_complete or qualitative_bootstrap_eligible,
            "status": (
                "FULLY_VALID"
                if data_complete
                else "CORE_VALID_QUALITATIVE_OVERLAY_PENDING"
                if qualitative_bootstrap_eligible
                else "NOT_ELIGIBLE"
            ),
            "missing_qualitative_scores": [
                score_id
                for score_id, value in (
                    ("business_durability", slow.business_durability),
                    ("governance_capital_allocation", slow.governance),
                )
                if value is None
            ],
        },
        "errors": tuple(errors),
    }


def compute_company_snapshot(
    raw: Mapping[str, Any],
    *,
    now: datetime | None = None,
    slow_variables: SlowVariables | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    slow = slow_variables or compute_slow_variables(raw, on_date=current.date())
    fast = compute_fast_variables(raw, slow, now=current)
    coverage_reason = (
        "; ".join(slow.coverage.required_missing_fields)
        if slow.coverage.required_missing_fields
        else None
    )
    distribution_history: list[dict[str, Any]] = []
    latest_eligible: Decimal | None = None
    try:
        for row in _annual_rows(raw.get("annual_distributions", []), on_date=current.date()):
            eligible_value: Decimal | None = None
            effective_value: Decimal | None = None
            try:
                eligible_value = eligible_buyback(
                    row.gross_cancelled_buyback,
                    row.cancelled_shares,
                    row.diluted_net_share_reduction,
                )
                if slow.q_b is not None:
                    effective_value = effective_distribution(
                        row.ordinary_dividend,
                        slow.q_b,
                        eligible_value,
                    )
            except CalculationError:
                pass
            if latest_eligible is None:
                latest_eligible = eligible_value
            distribution_history.append(
                {
                    "fiscal_year": row.fiscal_year,
                    "ordinary_dividend": metric(
                        row.ordinary_dividend,
                        reason="等待披露",
                        unit="CNY",
                    ).public_dict(),
                    "special_dividend": metric(
                        row.special_dividend,
                        reason="等待披露",
                        unit="CNY",
                    ).public_dict(),
                    "eligible_buyback": metric(
                        eligible_value,
                        reason="回购注销或净股本数据不足",
                        unit="CNY",
                    ).public_dict(),
                    "effective_distribution": metric(
                        effective_value,
                        reason="暂不可计算",
                        unit="CNY",
                    ).public_dict(),
                }
            )
    except CalculationError:
        pass
    trend_ratio = (
        slow.r2 / slow.m5
        if slow.r2 is not None and slow.m5 is not None and slow.m5 > 0
        else None
    )
    trend_record = metric(
        trend_ratio,
        reason="数据不足",
        unit="ratio_to_median",
    )
    if trend_ratio is not None and len(slow.annual_effective_distributions) >= 2:
        try:
            trend_score = recent_trend_score(
                slow.r2,
                slow.m5,
                slow.annual_effective_distributions[0][1],
                slow.annual_effective_distributions[1][1],
            )
            trend_label = (
                "增强" if trend_score >= Decimal("100")
                else "稳定" if trend_score >= Decimal("85")
                else "走弱" if trend_score >= Decimal("60")
                else "明显下降" if trend_score > 0
                else "中断或低迷"
            )
            trend_record = MetricRecord(
                value=trend_ratio,
                status="VALID",
                display=f"{trend_label}（R2/M5={trend_ratio.quantize(Decimal('0.01'))}）",
                unit="ratio_to_median",
            )
        except CalculationError:
            pass
    metrics = {
        "eligible_buyback": metric(latest_eligible, reason="数据不足", unit="CNY"),
        "buyback_persistence_factor": metric(slow.q_b, reason="数据不足"),
        "recent_2y_distribution": metric(slow.r2, reason="暂不可计算", unit="CNY"),
        "median_5y_distribution": metric(slow.m5, reason="数据不足", unit="CNY"),
        "winsorized_10y_distribution": metric(slow.t10, reason="数据不足", unit="CNY"),
        "historical_conservative_distribution": metric(slow.historical_distribution, reason="暂不可计算", unit="CNY"),
        "distribution_trend": trend_record,
        "sustainable_distribution": metric(
            slow.coverage.sustainable_distribution,
            status=slow.coverage.status.value,
            reason=coverage_reason or "行业口径尚不完整",
            unit="CNY",
        ),
        "coverage_ratio": metric(
            slow.coverage.coverage_ratio,
            status=slow.coverage.status.value,
            kind="multiple",
            reason=coverage_reason or "暂不可计算",
            unit="multiple",
        ),
        "net_debt_ebitda": metric(
            _decimal_or_none(
                raw.get("balance_sheet", {}).get("net_debt_ebitda")
                if isinstance(raw.get("balance_sheet"), Mapping)
                else None
            ),
            kind="multiple",
            reason="口径不适用或数据不足",
            unit="multiple",
        ),
        "company_market_cap": metric(fast["market_cap"], reason="行情或股本口径不完整", unit="CNY"),
        "raw_2y_shareholder_yield": metric(fast["raw_yield"], kind="percent", reason="暂不可计算", unit="ratio"),
        "sustainable_shareholder_yield": metric(fast["ssy"], kind="percent", reason="暂不可计算", unit="ratio"),
        "company_value_at_4pct": metric(fast["four_pct_value"], reason="暂不可计算", unit="CNY"),
        "conservative_growth": metric(slow.conservative_growth, kind="percent", reason="数据不足", unit="ratio"),
        "valuation_drag": metric(fast["valuation_drag"], kind="percent", reason="估值口径不可比", unit="ratio"),
        "conservative_return_10y": metric(fast["cr10"], kind="percent", reason="暂不可计算", unit="ratio"),
    }
    scores = {
        "return_score": metric(fast["return_score"], kind="score", reason="评分不完整", unit="score_0_100"),
        "payout_quality": metric(slow.payout_quality, kind="score", reason="评分不完整", unit="score_0_100"),
        "business_durability": metric(slow.business_durability, kind="score", reason="等待结构化来源", unit="score_0_100"),
        "governance_capital_allocation": metric(slow.governance, kind="score", reason="等待结构化来源", unit="score_0_100"),
        "recommendation_index": metric(fast["recommendation_index"], kind="score", reason="评分不完整", unit="score_0_100"),
        "entry_risk_index": metric(slow.eri, kind="score", reason="风险分项不完整", unit="score_0_100"),
    }
    price_timestamp = max(
        (item.price_timestamp for item in _security_classes(raw.get("share_classes", [])) if item.price_timestamp),
        default=None,
    )
    public_vetoes = [jsonable(flag) for flag in slow.veto_flags if flag.triggered]
    if any(error.startswith("reconciliation:") for error in fast["errors"]):
        public_vetoes.append(
            {
                "code": "DATA_RECONCILIATION_FAILED",
                "severity": "MAJOR",
                "triggered": True,
                "evidence_fields": ["reconciliation_inputs"],
                "message_zh": "核心财务字段未完成发布前对账，自动推荐已关闭。",
                "source": None,
                "as_of_date": current.date().isoformat(),
            }
        )
    expected_classes = {str(value) for value in raw.get("expected_share_classes", [])}
    existing_veto_codes = {str(item.get("code")) for item in public_vetoes}
    if (
        {"A", "H"}.issubset(expected_classes)
        and any(error.startswith("market_cap:") for error in fast["errors"])
        and "INCOMPLETE_AH_MARKET_CAP" not in existing_veto_codes
    ):
        public_vetoes.append(
            {
                "code": "INCOMPLETE_AH_MARKET_CAP",
                "severity": "MAJOR",
                "triggered": True,
                "evidence_fields": ["expected_share_classes", "share_classes"],
                "message_zh": "A/H总市值、总股本或行情口径不完整，禁止公司级收益率更新。",
                "source": None,
                "as_of_date": current.date().isoformat(),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "calculation_version": CALCULATION_VERSION,
        "metric_definition_version": METRIC_DEFINITION_VERSION,
        "company_id": str(raw.get("company_id") or ""),
        "company_name": str(raw.get("company_name") or ""),
        "securities": jsonable(raw.get("securities", [])),
        "as_of_date": str(raw.get("as_of_date") or current.date().isoformat()),
        "price_timestamp": price_timestamp.isoformat() if price_timestamp else None,
        "data_status": fast["data_status"].value,
        "analysis_eligibility": fast["analysis_eligibility"],
        "update_status": "CURRENT" if fast["data_status"] in {PublicationStatus.VALID, PublicationStatus.PARTIAL} else "BLOCKED",
        "validation_errors": list(fast["errors"]),
        "distribution_history": distribution_history,
        "metrics": {key: value.public_dict() for key, value in metrics.items()},
        "security_metrics": {
            security_id: {
                "price_at_4pct": metric(value, unit="security_currency").public_dict()
            }
            for security_id, value in fast["security_prices_4pct"].items()
        },
        "scores": {key: value.public_dict() for key, value in scores.items()},
        "classification": fast["classification"],
        "return_type": fast["return_type"],
        "veto_flags": public_vetoes,
        "source_summary": _public_source_summary(raw.get("source_summary", {})),
        "coverage_adapter": {
            "name": slow.coverage.adapter,
            "status": slow.coverage.status.value,
            "caveats": list(slow.coverage.caveats),
            "missing_fields": list(slow.coverage.required_missing_fields),
        },
        "analysis_status": jsonable(raw.get("analysis_status", {"status": "NOT_REQUESTED", "latest_success_at": None})),
        "calculated_at": current.isoformat(),
    }
