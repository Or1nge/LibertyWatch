from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .official_cashflow_candidates import (
    _CFO_LABELS,
    _DIRECT_CAPEX_LABELS,
    _FIXED_ASSET_LABELS,
    _INTANGIBLE_LABELS,
    _NUMBER_RE,
    _compact,
    _decimal_text,
    _ends_consolidated_cashflow,
    _labels_in_line,
    _parse_number,
    _select_unique_rows,
    _statement_title_line,
    _sum_components,
    _unit_context,
)


SCHEMA_VERSION = "cashflow-coverage-reconciliation-v2.0"
FIELD_NAMES = ("operating_cash_flow", "capital_expenditure")
ACCEPTED_FUTU_STATUSES = {"VALID", "KNOWN_ZERO"}
MAX_WRAPPED_ROW_LINES = 3


class CashflowCoverageReconciliationError(RuntimeError):
    pass


def exact_decimal_equal(left: Any, right: Any) -> bool:
    try:
        first = Decimal(str(left))
        second = Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return first.is_finite() and second.is_finite() and first == second


def _label_starts_on_line(line: str, label: str) -> bool:
    compact_line = _compact(line)
    compact_label = _compact(label)
    if compact_label in compact_line:
        return True
    common = 0
    for left, right in zip(compact_line, compact_label):
        if left != right:
            break
        common += 1
    # Four characters (for example ``经营活动``) is too broad and can make the
    # preceding cash-inflow/outflow subtotal consume the target row below it.
    return common >= 6


def wrapped_statement_rows(
    text: str,
    metadata: Mapping[str, Any],
    labels: Sequence[str],
    *,
    absolute_value: bool,
) -> list[dict[str, Any]]:
    """Extract a row whose label and cells may wrap over at most three lines."""

    rows: list[dict[str, Any]] = []
    active_statement = False
    active_unit: dict[str, Any] | None = None
    global_offset = 0
    for page_number, page in enumerate(text.split("\f"), start=1):
        lines = page.splitlines()
        page_unit = _unit_context(
            page,
            str(metadata.get("currency") or "") or None,
            str(metadata.get("market") or ""),
        )
        for index, line in enumerate(lines):
            if _statement_title_line(line):
                active_statement = True
                active_unit = page_unit
            elif active_statement and _ends_consolidated_cashflow(line):
                active_statement = False
                active_unit = None
            if not active_statement:
                continue
            started_labels = [label for label in labels if _label_starts_on_line(line, label)]
            if not started_labels:
                continue
            chosen_lines: list[str] | None = None
            matched_labels: list[str] = []
            tokens: list[str] = []
            for length in range(1, MAX_WRAPPED_ROW_LINES + 1):
                excerpt = lines[index : index + length]
                combined = " ".join(excerpt)
                label_text = _NUMBER_RE.sub("", combined)
                matched = _labels_in_line(label_text, started_labels)
                amounts = _NUMBER_RE.findall(combined)
                if matched and len(amounts) >= 2:
                    chosen_lines = excerpt
                    matched_labels = matched
                    tokens = amounts
                    break
            if chosen_lines is None:
                continue
            amounts = [_parse_number(token) for token in tokens]
            current = amounts[-2]
            comparative = amounts[-1]
            if absolute_value:
                current = abs(current)
                comparative = abs(comparative)
            multiplier = active_unit.get("multiplier") if active_unit else None
            rows.append(
                {
                    "matched_labels": matched_labels,
                    "raw_current_value": _decimal_text(current),
                    "raw_comparative_value": _decimal_text(comparative),
                    "normalized_current_value": (
                        _decimal_text(current * multiplier) if multiplier is not None else None
                    ),
                    "normalized_comparative_value": (
                        _decimal_text(comparative * multiplier) if multiplier is not None else None
                    ),
                    "currency": active_unit.get("currency") if active_unit else None,
                    "unit": "currency" if multiplier is not None else None,
                    "source_unit_label": active_unit.get("unit_label") if active_unit else None,
                    "unit_multiplier": _decimal_text(multiplier),
                    "unit_context_basis": active_unit.get("basis") if active_unit else None,
                    "unit_context_status": (
                        active_unit.get("status") if active_unit else "REVIEW"
                    ),
                    "page": page_number,
                    "page_line": index + 1,
                    "text_line": global_offset + index + 1,
                    "line_excerpt": " | ".join(
                        item.strip() for item in chosen_lines
                    )[:750],
                    "wrapped_line_count": len(chosen_lines),
                }
            )
        global_offset += len(lines) + 1
    return rows


def extract_official_first_cashflow_report(
    text: str, metadata: Mapping[str, Any]
) -> dict[str, Any]:
    required = (
        "company_id",
        "company_name",
        "security_id",
        "share_class",
        "market",
        "currency",
        "fiscal_year",
        "fiscal_year_end_date",
        "source_document",
        "source_url",
        "source_publish_date",
        "source_fetch_time",
        "local_path",
        "sha256",
    )
    missing = [key for key in required if metadata.get(key) in (None, "")]
    if missing:
        raise CashflowCoverageReconciliationError(
            f"annual-report metadata is incomplete: {missing}"
        )
    cfo = _select_unique_rows(
        wrapped_statement_rows(text, metadata, _CFO_LABELS, absolute_value=False),
        component_name="operating_cash_flow",
    )
    direct = _select_unique_rows(
        wrapped_statement_rows(
            text, metadata, _DIRECT_CAPEX_LABELS, absolute_value=True
        ),
        component_name="direct",
    )
    fixed = _select_unique_rows(
        wrapped_statement_rows(text, metadata, _FIXED_ASSET_LABELS, absolute_value=True),
        component_name="fixed_assets",
    )
    intangible = _select_unique_rows(
        wrapped_statement_rows(text, metadata, _INTANGIBLE_LABELS, absolute_value=True),
        component_name="intangible_assets",
    )
    if direct.get("status") == "CONFLICT" or direct.get("current_value") is not None:
        capex = _sum_components([direct], ("direct",))
        capex["definition_basis"] = "DIRECT_COMPREHENSIVE_CASHFLOW_ROW"
    else:
        capex = _sum_components(
            [fixed, intangible], ("fixed_assets", "intangible_assets")
        )
        capex["definition_basis"] = "PPE_PLUS_INTANGIBLES_COMPLETE_SET"
    return {
        "schema_version": SCHEMA_VERSION,
        "company_id": str(metadata["company_id"]),
        "company_name": str(metadata["company_name"]),
        "security_id": str(metadata["security_id"]),
        "share_class": str(metadata["share_class"]),
        "market": str(metadata["market"]),
        "fiscal_year": int(metadata["fiscal_year"]),
        "fiscal_year_end_date": str(metadata["fiscal_year_end_date"]),
        "source_document": str(metadata["source_document"]),
        "source_url": str(metadata["source_url"]),
        "source_publish_date": str(metadata["source_publish_date"]),
        "source_fetch_time": str(metadata["source_fetch_time"]),
        "source_local_path": str(metadata["local_path"]),
        "source_sha256": str(metadata["sha256"]),
        "fields": {
            "operating_cash_flow": cfo,
            "capital_expenditure": capex,
        },
    }


def adjacent_reconciliations(
    reports: Mapping[int, Mapping[str, Any]],
    fiscal_year: int,
    field_name: str,
) -> dict[str, str]:
    current = reports.get(fiscal_year, {}).get("fields", {}).get(field_name, {})
    value = current.get("current_value")
    currency = current.get("currency")
    result = {"backward": "UNAVAILABLE", "forward": "UNAVAILABLE"}
    prior = reports.get(fiscal_year - 1, {}).get("fields", {}).get(field_name, {})
    if (
        current.get("comparative_value") is not None
        and prior.get("current_value") is not None
        and current.get("currency") == prior.get("currency")
    ):
        result["backward"] = (
            "MATCH"
            if exact_decimal_equal(current["comparative_value"], prior["current_value"])
            else "MISMATCH"
        )
    following = reports.get(fiscal_year + 1, {}).get("fields", {}).get(field_name, {})
    if (
        value is not None
        and following.get("comparative_value") is not None
        and currency == following.get("currency")
    ):
        result["forward"] = (
            "MATCH"
            if exact_decimal_equal(value, following["comparative_value"])
            else "MISMATCH"
        )
    return result


def classify_coverage_decision(
    official_field: Mapping[str, Any] | None,
    futu_field: Mapping[str, Any],
    adjacent: Mapping[str, str],
    *,
    accepted_by_v1: bool,
) -> tuple[str, str | None, list[str]]:
    futu_numeric = (
        futu_field.get("data_status") in ACCEPTED_FUTU_STATUSES
        and futu_field.get("value") is not None
    )
    if not isinstance(official_field, Mapping) or official_field.get("current_value") is None:
        return (
            "FUTU_ONLY" if futu_numeric else "INSUFFICIENT_DATA",
            None,
            ["OFFICIAL_UNIQUE_VALUE_UNAVAILABLE"],
        )
    official_value = official_field.get("current_value")
    official_currency = str(official_field.get("currency") or "")
    if not official_currency or official_field.get("unit") != "currency":
        return "REVIEW", None, ["OFFICIAL_CURRENCY_OR_UNIT_UNCLEAR"]
    futu_relation = "UNAVAILABLE"
    if futu_numeric:
        if str(futu_field.get("currency") or "") != official_currency:
            futu_relation = "CURRENCY_MISMATCH"
        else:
            futu_relation = (
                "MATCH"
                if exact_decimal_equal(official_value, futu_field.get("value"))
                else "AMOUNT_MISMATCH"
            )
    relations = {str(adjacent.get("backward")), str(adjacent.get("forward"))}
    if futu_relation in {"CURRENCY_MISMATCH", "AMOUNT_MISMATCH"} or "MISMATCH" in relations:
        return "CONFLICT", None, ["OFFICIAL_FUTU_OR_ADJACENT_CONFLICT"]
    if accepted_by_v1:
        if futu_relation != "MATCH":
            return "CONFLICT", None, ["V1_ACCEPT_NO_LONGER_MATCHES_FUTU"]
        return "ACCEPT_V1", str(official_value), ["CASHFLOW_V1_ACCEPT_RECONFIRMED"]
    if "MATCH" in relations:
        return (
            "ACCEPT_OFFICIAL_ADJACENT",
            str(official_value),
            ["UNIQUE_OFFICIAL_ROW_AND_EXACT_ADJACENT_REPORT"],
        )
    if futu_relation == "MATCH":
        return (
            "ACCEPT_OFFICIAL_PLUS_FUTU",
            str(official_value),
            ["UNIQUE_OFFICIAL_ROW_AND_EXACT_FUTU"],
        )
    return "REVIEW", None, ["OFFICIAL_SINGLE_REPORT_WITHOUT_CORROBORATION"]


def accepted_status(status: str) -> bool:
    return status in {
        "ACCEPT_V1",
        "ACCEPT_OFFICIAL_ADJACENT",
        "ACCEPT_OFFICIAL_PLUS_FUTU",
    }
