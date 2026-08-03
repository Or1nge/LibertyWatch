from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .calculations import to_decimal
from .models import (
    DataStatus,
    PublicationStatus,
    RawDataPoint,
    ValidationIssue,
    ValidationResult,
)


def select_latest_restatements(points: Iterable[RawDataPoint]) -> list[RawDataPoint]:
    """Deduplicate exact announcements and keep the latest fetched restatement."""

    selected: dict[tuple[str, str, str | None, str, str], RawDataPoint] = {}
    for point in points:
        key = (
            point.company_id,
            point.field_id,
            point.security_id,
            point.fiscal_period,
            point.source_document,
        )
        previous = selected.get(key)
        if previous is None or point.source_fetch_time > previous.source_fetch_time:
            selected[key] = point
    return sorted(
        selected.values(),
        key=lambda point: (
            point.company_id,
            point.field_id,
            point.fiscal_period,
            point.source_document,
            point.unit,
        ),
    )


def _relative_difference(actual: Decimal, expected: Decimal) -> Decimal:
    denominator = max(abs(actual), abs(expected), Decimal("1"))
    return abs(actual - expected) / denominator


def reconcile_value(
    actual: Decimal | None,
    expected: Decimal | None,
    *,
    field: str,
    relative_tolerance: Decimal = Decimal("0.02"),
    absolute_tolerance: Decimal = Decimal("1"),
) -> ValidationIssue | None:
    if actual is None or expected is None:
        return ValidationIssue(
            code="RECONCILIATION_INPUT_MISSING",
            severity="ERROR",
            field=field,
            message_zh=f"{field} 对账所需字段缺失。",
        )
    left = to_decimal(actual)
    right = to_decimal(expected)
    if abs(left - right) <= to_decimal(absolute_tolerance):
        return None
    if _relative_difference(left, right) <= to_decimal(relative_tolerance):
        return None
    return ValidationIssue(
        code="RECONCILIATION_MISMATCH",
        severity="ERROR",
        field=field,
        message_zh=f"{field} 对账差异超过集中配置的容差。",
    )


def validate_raw_points(points: Iterable[RawDataPoint]) -> ValidationResult:
    issues: list[ValidationIssue] = []
    seen: dict[tuple[str, str, str | None, str, str], RawDataPoint] = {}
    for point in points:
        key = (
            point.company_id,
            point.field_id,
            point.security_id,
            point.fiscal_period,
            point.source_document,
        )
        previous = seen.get(key)
        if previous and previous.value == point.value and previous.restatement_status == point.restatement_status:
            issues.append(
                ValidationIssue(
                    code="DUPLICATE_SOURCE_RECORD",
                    severity="ERROR",
                    field=point.source_document,
                    message_zh="同一公告被重复抓取。",
                )
            )
        seen[key] = point
        if point.data_status in {DataStatus.CONFLICT, DataStatus.CALCULATION_FAILED}:
            issues.append(
                ValidationIssue(
                    code="RAW_SOURCE_CONFLICT",
                    severity="ERROR",
                    field=point.source_document,
                    message_zh="关键原始来源冲突或计算失败。",
                )
            )
        elif point.data_status in {DataStatus.MISSING, DataStatus.NOT_DISCLOSED, DataStatus.STALE}:
            issues.append(
                ValidationIssue(
                    code="RAW_SOURCE_INCOMPLETE",
                    severity="WARNING",
                    field=point.source_document,
                    message_zh="原始来源缺失、尚未披露或已经过期。",
                )
            )
    return validation_result(issues)


def validate_raw_provenance_records(
    records: Any,
    *,
    expected_company_id: str,
) -> ValidationResult:
    """Validate the v2 raw-source envelope without exposing it publicly."""

    if not isinstance(records, list) or not records:
        return validation_result(
            [
                ValidationIssue(
                    code="RAW_PROVENANCE_MISSING",
                    severity="ERROR",
                    field="raw_data_points",
                    message_zh="关键原始数据缺少逐字段来源、币种、单位、财年和状态记录。",
                )
            ]
        )
    points: list[RawDataPoint] = []
    issues: list[ValidationIssue] = []
    field_ids: set[str] = set()
    for index, record in enumerate(records):
        field = f"raw_data_points[{index}]"
        if not isinstance(record, Mapping):
            issues.append(ValidationIssue("RAW_PROVENANCE_INVALID", "ERROR", field, "原始来源记录必须是对象。"))
            continue
        field_id = str(record.get("field_id") or "")
        if not field_id or field_id in field_ids:
            issues.append(ValidationIssue("RAW_FIELD_ID_INVALID", "ERROR", field, "原始字段ID缺失或重复。"))
            continue
        field_ids.add(field_id)
        try:
            company_id = str(record.get("company_id") or "")
            if company_id != expected_company_id:
                raise ValueError("company_id与公司快照不一致")
            publish_date = date.fromisoformat(str(record["source_publish_date"]))
            fetch_time = datetime.fromisoformat(str(record["source_fetch_time"]).replace("Z", "+00:00"))
            if fetch_time.tzinfo is None:
                fetch_time = fetch_time.replace(tzinfo=timezone.utc)
            status = DataStatus(str(record["data_status"]))
            raw_value = record.get("value")
            value = to_decimal(raw_value) if raw_value is not None else None
            points.append(
                RawDataPoint(
                    company_id=company_id,
                    field_id=field_id,
                    security_id=str(record["security_id"]) if record.get("security_id") else None,
                    share_class=str(record["share_class"]) if record.get("share_class") else None,
                    source_name=str(record.get("source_name") or ""),
                    source_document=str(record.get("source_document") or ""),
                    source_url_or_local_path=str(record.get("source_url_or_local_path") or ""),
                    source_publish_date=publish_date,
                    source_fetch_time=fetch_time,
                    fiscal_period=str(record.get("fiscal_period") or ""),
                    currency=str(record["currency"]) if record.get("currency") else None,
                    unit=str(record.get("unit") or ""),
                    value=value,
                    data_status=status,
                    restatement_status=str(record.get("restatement_status") or ""),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            issues.append(
                ValidationIssue(
                    code="RAW_PROVENANCE_INVALID",
                    severity="ERROR",
                    field=field_id or field,
                    message_zh=f"原始来源记录无效：{error}",
                )
            )
    return merge_validation_results(validation_result(issues), validate_raw_points(points))


def required_provenance_field_ids(snapshot: Mapping[str, Any]) -> set[str]:
    """Return the numeric source-ledger fields needed by the current calculation."""
    required: set[str] = set()
    annual_fields = (
        "ordinary_dividend",
        "special_dividend",
        "gross_cancelled_buyback",
        "cancelled_shares",
        "diluted_net_share_reduction",
        "asset_sale_distribution",
        "one_off_buyback",
    )
    for item in snapshot.get("annual_distributions", []):
        if isinstance(item, Mapping) and item.get("fiscal_year") is not None:
            prefix = f"FY{int(item['fiscal_year'])}"
            required.update(f"{prefix}.{field}" for field in annual_fields)

    for item in snapshot.get("share_classes", []):
        if not isinstance(item, Mapping) or item.get("material") is False:
            continue
        security_id = str(item.get("security_id") or "")
        if security_id:
            required.update(
                {
                    f"MARKET.{security_id}.price",
                    f"MARKET.{security_id}.fx_to_base",
                    f"SECURITY.{security_id}.issued_shares",
                    f"SECURITY.{security_id}.economic_rights_factor",
                }
            )

    coverage = snapshot.get("coverage") if isinstance(snapshot.get("coverage"), Mapping) else {}
    industry = str(snapshot.get("industry_kind") or "UNSUPPORTED")
    if industry == "NON_FINANCIAL":
        for item in coverage.get("fcf_years", []):
            if isinstance(item, Mapping) and item.get("fiscal_year") is not None:
                prefix = f"FY{int(item['fiscal_year'])}"
                required.update(
                    f"{prefix}.{field}"
                    for field in (
                        "operating_cash_flow",
                        "capital_expenditure",
                        "lease_principal_repayment",
                    )
                )
    elif industry == "BANK":
        required.update(
            f"CURRENT.{field}"
            for field in (
                "adjusted_net_income",
                "capital_generation_capacity",
                "cet1_ratio",
                "cet1_regulatory_minimum",
                "risk_weighted_asset_growth",
                "nonperforming_loan_ratio",
                "provision_coverage_ratio",
                "credit_cost",
                "net_interest_margin",
            )
        )
    elif industry == "INSURANCE":
        required.update(
            f"CURRENT.{field}"
            for field in (
                "free_surplus_generation",
                "distributable_surplus_capacity",
                "comprehensive_solvency_ratio",
                "comprehensive_solvency_minimum",
                "core_solvency_ratio",
                "core_solvency_minimum",
                "investment_asset_quality_score",
                "interest_rate_sensitivity_score",
                "new_business_value",
            )
        )

    for index, item in enumerate(snapshot.get("organic_growth_series", [])):
        if isinstance(item, Mapping) and item.get("fiscal_year") is not None:
            required.add(f"GROWTH.FY{int(item['fiscal_year'])}.value")
        else:
            required.add(f"GROWTH.{index}.value")
    required.update({"VALUATION.current", "VALUATION.historical_median"})
    required.update(
        f"RECONCILIATION.{field}"
        for field in (
            "dividend_per_share_times_entitled_shares",
            "repurchased_shares_times_average_price",
            "opening_minus_closing_shares",
            "cancelled_minus_issued_and_converted",
        )
    )
    for item in snapshot.get("balance_sheet_history", []):
        if isinstance(item, Mapping) and item.get("fiscal_year") is not None:
            required.add(f"FY{int(item['fiscal_year'])}.net_debt")
    return required


def validate_required_provenance_fields(
    records: Any,
    required_field_ids: Iterable[str],
) -> ValidationResult:
    present = {
        str(record.get("field_id"))
        for record in records
        if isinstance(records, list) and isinstance(record, Mapping) and record.get("field_id")
    } if isinstance(records, list) else set()
    issues = [
        ValidationIssue(
            code="RAW_PROVENANCE_FIELD_MISSING",
            severity="ERROR",
            field=field_id,
            message_zh="核心结构化字段缺少对应的逐字段来源记录。",
        )
        for field_id in sorted(set(required_field_ids) - present)
    ]
    return validation_result(issues)


def validation_result(issues: Sequence[ValidationIssue]) -> ValidationResult:
    if any(issue.severity == "ERROR" for issue in issues):
        status = PublicationStatus.INVALID
    elif issues:
        status = PublicationStatus.PARTIAL
    else:
        status = PublicationStatus.VALID
    return ValidationResult(status=status, issues=tuple(issues))


def validate_accounting_reconciliations(
    *,
    total_market_cap: Decimal | None,
    share_class_market_caps: Sequence[Decimal] | None,
    total_dividend: Decimal | None,
    dividend_per_share_times_entitled_shares: Decimal | None,
    buyback_cash: Decimal | None,
    repurchased_shares_times_average_price: Decimal | None,
    opening_minus_closing_shares: Decimal | None,
    cancelled_minus_issued_and_converted: Decimal | None,
    relative_tolerance: Decimal = Decimal("0.02"),
    absolute_tolerance: Decimal = Decimal("1"),
    share_count_relative_tolerance: Decimal = Decimal("0.005"),
) -> ValidationResult:
    issues: list[ValidationIssue] = []
    expected_market_cap = (
        sum((to_decimal(value) for value in share_class_market_caps), Decimal("0"))
        if share_class_market_caps is not None
        else None
    )
    checks = (
        (total_market_cap, expected_market_cap, "company_market_cap", relative_tolerance),
        (total_dividend, dividend_per_share_times_entitled_shares, "ordinary_dividend", relative_tolerance),
        (buyback_cash, repurchased_shares_times_average_price, "buyback_cash", relative_tolerance),
        (
            opening_minus_closing_shares,
            cancelled_minus_issued_and_converted,
            "share_count_bridge",
            share_count_relative_tolerance,
        ),
    )
    for actual, expected, field, tolerance in checks:
        issue = reconcile_value(
            actual,
            expected,
            field=field,
            relative_tolerance=tolerance,
            absolute_tolerance=absolute_tolerance,
        )
        if issue:
            issues.append(issue)
    return validation_result(issues)


def merge_validation_results(*results: ValidationResult) -> ValidationResult:
    return validation_result([issue for result in results for issue in result.issues])
