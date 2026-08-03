from __future__ import annotations

import copy
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


LEDGER_VERSION = "source-ledger-v1.0"
FULL_YEAR_TYPE = 7

# This is deliberately a minimum list, not a claim that every other issuer is
# single-listed.  The source-ledger report still marks every issuer's complete
# share-class scope unverified until official capital records are supplied.
KNOWN_MULTI_MARKET_SECURITIES: dict[str, tuple[str, ...]] = {
    "SH600660": ("A", "H"),       # 福耀玻璃
    "SZ002352": ("A", "H"),       # 顺丰控股
    "SH688235": ("A", "H", "US"), # 百济神州
    "HK1179": ("H", "US"),        # 华住集团
    "HK2057": ("H", "US"),        # 中通快递
    "HK9987": ("H", "US"),        # 百胜中国
}

# Futu uses different statement dictionaries for PRC GAAP and IFRS reports.
# Match exact provider labels and retain the provider field ids as evidence.
OPERATING_CASH_FLOW_NAMES = {
    "经营活动产生的现金流量净额",
    "经营活动现金流量净额",
}
DIRECT_CAPEX_NAMES = {
    "购建固定资产、无形资产和其他长期资产支付的现金",
}
CAPEX_COMPONENT_GROUPS = (
    ("购买固定资产", "购买无形资产"),
)
SHARE_CAPITAL_AMOUNT_NAMES = {
    "实收资本(或股本)",
    "实收资本（或股本）",
    "股本",
}

OFFICIAL_FIELD_LABELS = {
    "operating_cash_flow": ("经营活动产生的现金流量净额", "经营活动现金流量净额"),
    "capital_expenditure": ("购建固定资产、无形资产和其他长期资产支付的现金",),
    "reported_share_capital_amount": ("实收资本（或股本）", "实收资本(或股本)", "股本"),
}
OFFICIAL_SECTION_MARKERS = {
    "operating_cash_flow": ("合并现金流量表",),
    "capital_expenditure": ("合并现金流量表",),
    "reported_share_capital_amount": ("合并资产负债表",),
}

CURRENCY_ALIASES = {
    "人民币": "CNY",
    "人民币元": "CNY",
    "港元": "HKD",
    "港币": "HKD",
    "美元": "USD",
    "美元元": "USD",
}
UNIT_MULTIPLIERS = {
    "元": Decimal("1"),
    "千元": Decimal("1000"),
    "万元": Decimal("10000"),
    "百万元": Decimal("1000000"),
    "亿元": Decimal("100000000"),
}


class SourceLedgerError(RuntimeError):
    pass


class SourceLedgerConflict(SourceLedgerError):
    pass


@dataclass(frozen=True)
class ExtractedValue:
    value: Decimal | None
    status: str
    provider_fields: tuple[dict[str, Any], ...] = ()
    reason: str | None = None


def decimal_value(value: Any) -> Decimal | None:
    """Convert a provider scalar through its text form; never Decimal(float)."""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise SourceLedgerError("boolean is not a financial amount")
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as error:
        raise SourceLedgerError(f"invalid decimal amount: {value!r}") from error
    if not parsed.is_finite():
        raise SourceLedgerError("NaN and Infinity are forbidden")
    return parsed


def decimal_text(value: Decimal | Any | None) -> str | None:
    parsed = decimal_value(value)
    return format(parsed, "f") if parsed is not None else None


def _parse_datetime(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise SourceLedgerError(f"invalid evidence fetch time: {value!r}") from error
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _parse_date(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise SourceLedgerError(f"invalid fiscal year end: {value!r}") from error


def _normalized_name(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("：", ":")


def normalize_futu_statement_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only auditable statement fields and stringify every numeric amount."""

    structures = [
        {
            "field_id": int(item["field_id"]),
            "display_name": str(item.get("display_name") or ""),
        }
        for item in payload.get("structure_list", [])
        if isinstance(item, Mapping) and item.get("field_id") is not None
    ]
    names = {item["field_id"]: item["display_name"] for item in structures}
    reports: list[dict[str, Any]] = []
    for report in payload.get("report_list", []):
        if not isinstance(report, Mapping):
            continue
        items: list[dict[str, Any]] = []
        for item in report.get("item_list", []):
            if not isinstance(item, Mapping) or item.get("field_id") is None:
                continue
            field_id = int(item["field_id"])
            normalized = {
                "field_id": field_id,
                "display_name": str(item.get("display_name") or names.get(field_id) or ""),
                "data": decimal_text(item.get("data")),
            }
            items.append(normalized)
        reports.append(
            {
                "date_time_str": str(report.get("date_time_str") or ""),
                "fiscal_year": int(report.get("fiscal_year") or 0),
                "financial_type": int(report.get("financial_type") or 0),
                "period_text": str(report.get("period_text") or ""),
                "currency_info": str(report.get("currency_info") or ""),
                "currency_code": str(report.get("currency_code") or ""),
                "accounting_standards": str(report.get("accounting_standards") or ""),
                "auditor_report": str(report.get("auditor_report") or ""),
                "item_list": items,
            }
        )
    return {
        "next_key": str(payload.get("next_key", "-1")),
        "structure_list": structures,
        "report_list": reports,
    }


def _structure_names(statement: Mapping[str, Any]) -> dict[int, str]:
    return {
        int(item["field_id"]): str(item.get("display_name") or "")
        for item in statement.get("structure_list", [])
        if isinstance(item, Mapping) and item.get("field_id") is not None
    }


def _extract_exact(
    report: Mapping[str, Any],
    structure: Mapping[int, str],
    names: Iterable[str],
) -> ExtractedValue:
    wanted = {_normalized_name(value) for value in names}
    matches: list[dict[str, Any]] = []
    for item in report.get("item_list", []):
        if not isinstance(item, Mapping) or item.get("field_id") is None:
            continue
        field_id = int(item["field_id"])
        display_name = str(item.get("display_name") or structure.get(field_id) or "")
        if _normalized_name(display_name) in wanted:
            matches.append(
                {
                    "field_id": field_id,
                    "display_name": display_name,
                    "value": decimal_text(item.get("data")),
                }
            )
    if not matches:
        return ExtractedValue(None, "MISSING", reason="provider line item is absent")
    if len(matches) != 1:
        return ExtractedValue(
            None,
            "CONFLICT",
            tuple(matches),
            "more than one provider field matched the canonical label",
        )
    value = decimal_value(matches[0]["value"])
    if value is None:
        return ExtractedValue(None, "MISSING", tuple(matches), "provider value is null")
    return ExtractedValue(
        value,
        "KNOWN_ZERO" if value == 0 else "VALID",
        tuple(matches),
    )


def _extract_capex(
    report: Mapping[str, Any],
    structure: Mapping[int, str],
) -> ExtractedValue:
    direct = _extract_exact(report, structure, DIRECT_CAPEX_NAMES)
    if direct.status != "MISSING":
        if direct.value is None:
            return direct
        normalized = abs(direct.value)
        return ExtractedValue(
            normalized,
            "KNOWN_ZERO" if normalized == 0 else "VALID",
            direct.provider_fields,
            "cash outflow normalized to a positive capital-expenditure amount",
        )

    available_names = {_normalized_name(value) for value in structure.values()}
    for group in CAPEX_COMPONENT_GROUPS:
        if not all(_normalized_name(name) in available_names for name in group):
            continue
        components = [_extract_exact(report, structure, (name,)) for name in group]
        if any(item.status == "CONFLICT" for item in components):
            fields = tuple(field for item in components for field in item.provider_fields)
            return ExtractedValue(None, "CONFLICT", fields, "capex component conflict")
        if any(item.value is None for item in components):
            fields = tuple(field for item in components for field in item.provider_fields)
            return ExtractedValue(
                None,
                "MISSING",
                fields,
                "capex component is absent; missing is not treated as zero",
            )
        value = sum((abs(item.value) for item in components if item.value is not None), Decimal("0"))
        fields = tuple(field for item in components for field in item.provider_fields)
        return ExtractedValue(
            value,
            "KNOWN_ZERO" if value == 0 else "VALID",
            fields,
            "cash outflow components normalized to a positive capital-expenditure amount",
        )
    return direct


def _report_key(report: Mapping[str, Any]) -> tuple[int, str, str]:
    return (
        int(report.get("fiscal_year") or 0),
        str(report.get("period_text") or ""),
        str(report.get("date_time_str") or ""),
    )


def _full_year_reports(statement: Mapping[str, Any]) -> dict[tuple[int, str, str], Mapping[str, Any]]:
    selected: dict[tuple[int, str, str], Mapping[str, Any]] = {}
    for report in statement.get("report_list", []):
        if not isinstance(report, Mapping):
            continue
        financial_type = int(report.get("financial_type") or 0)
        period_text = str(report.get("period_text") or "")
        if financial_type != FULL_YEAR_TYPE and not period_text.upper().endswith("/FY"):
            continue
        key = _report_key(report)
        if not key[0] or not key[1] or not key[2]:
            raise SourceLedgerError("full-year provider report lacks fiscal metadata")
        if key in selected:
            raise SourceLedgerConflict(f"duplicate Futu full-year report: {key}")
        selected[key] = report
    return selected


def _ledger_value(value: ExtractedValue) -> dict[str, Any]:
    return {
        "value": decimal_text(value.value),
        "data_status": value.status,
        "provider_fields": list(value.provider_fields),
        "reason": value.reason,
    }


def _raw_point(
    *,
    company_id: str,
    security_id: str,
    share_class: str,
    fiscal_year: int,
    field_name: str,
    value: ExtractedValue,
    currency: str | None,
    unit: str,
    fiscal_period: str,
    fetched_at: datetime,
    evidence_path: Path,
    source_document_suffix: str,
) -> dict[str, Any]:
    numeric = value.value if value.status in {"VALID", "KNOWN_ZERO"} else None
    return {
        "company_id": company_id,
        "field_id": f"FY{fiscal_year}.{field_name}",
        "security_id": security_id,
        "share_class": share_class,
        "source_name": "Futu OpenD financial statement database",
        "source_document": f"{evidence_path.name}#{source_document_suffix}",
        "source_url_or_local_path": str(evidence_path.resolve()),
        # Futu's statement response has a fiscal end date but no filing date.
        # For the API snapshot itself the only honest publication timestamp is
        # its retrieval date; the annual ledger keeps filing date as null.
        "source_publish_date": fetched_at.date().isoformat(),
        "source_publish_date_basis": "PROVIDER_SNAPSHOT_RETRIEVAL_DATE",
        "source_fetch_time": fetched_at.isoformat(),
        "fiscal_period": fiscal_period,
        "currency": currency,
        "unit": unit,
        "value": decimal_text(numeric),
        "data_status": value.status,
        "restatement_status": "UNKNOWN_RESTATEMENT_STATUS",
        "provider_fields": list(value.provider_fields),
        "reason": value.reason,
    }


def build_futu_financial_ledger(
    evidence: Mapping[str, Any],
    *,
    evidence_path: Path,
    max_years: int = 10,
) -> dict[str, Any]:
    """Convert one immutable Futu detail snapshot into a non-lossy annual ledger."""

    if str(evidence.get("schema_version")) != "futu-financial-evidence-v1":
        raise SourceLedgerError("unsupported Futu financial evidence schema")
    company = evidence.get("company")
    if not isinstance(company, Mapping):
        raise SourceLedgerError("Futu evidence has no company envelope")
    company_id = str(company.get("issuer_id") or "")
    security_id = str(company.get("security_id") or "")
    share_class = str(company.get("share_class") or "")
    if not company_id or not security_id or not share_class:
        raise SourceLedgerError("company/security/share class identifiers are required")
    fetched_at = _parse_datetime(evidence.get("fetched_at"))
    statements = evidence.get("statements")
    if not isinstance(statements, Mapping):
        raise SourceLedgerError("Futu evidence has no statements")
    cash_flow = statements.get("cash_flow")
    balance_sheet = statements.get("balance_sheet")
    if not isinstance(cash_flow, Mapping) or not isinstance(balance_sheet, Mapping):
        raise SourceLedgerError("cash-flow and balance-sheet payloads are required")
    cash_structure = _structure_names(cash_flow)
    balance_structure = _structure_names(balance_sheet)
    cash_reports = _full_year_reports(cash_flow)
    balance_reports = _full_year_reports(balance_sheet)

    keys = sorted(set(cash_reports) | set(balance_reports), reverse=True)[:max_years]
    annual: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    for fiscal_year, fiscal_period, fiscal_end_text in keys:
        fiscal_end = _parse_date(fiscal_end_text)
        cash_report = cash_reports.get((fiscal_year, fiscal_period, fiscal_end_text))
        balance_report = balance_reports.get((fiscal_year, fiscal_period, fiscal_end_text))
        cash_currency = str((cash_report or {}).get("currency_code") or "")
        balance_currency = str((balance_report or {}).get("currency_code") or "")
        currency = cash_currency or balance_currency or None
        currency_conflict = bool(cash_currency and balance_currency and cash_currency != balance_currency)

        if cash_report is None:
            operating = ExtractedValue(None, "MISSING", reason="cash-flow statement is absent")
            capex = ExtractedValue(None, "MISSING", reason="cash-flow statement is absent")
        else:
            operating = _extract_exact(cash_report, cash_structure, OPERATING_CASH_FLOW_NAMES)
            capex = _extract_capex(cash_report, cash_structure)
        if balance_report is None:
            share_capital = ExtractedValue(None, "MISSING", reason="balance sheet is absent")
        else:
            share_capital = _extract_exact(
                balance_report,
                balance_structure,
                SHARE_CAPITAL_AMOUNT_NAMES,
            )
        if currency_conflict:
            operating = ExtractedValue(None, "CONFLICT", operating.provider_fields, "statement currency conflict")
            capex = ExtractedValue(None, "CONFLICT", capex.provider_fields, "statement currency conflict")
            share_capital = ExtractedValue(
                None,
                "CONFLICT",
                share_capital.provider_fields,
                "statement currency conflict",
            )
        elif not currency:
            operating = ExtractedValue(None, "CONFLICT", operating.provider_fields, "statement currency missing")
            capex = ExtractedValue(None, "CONFLICT", capex.provider_fields, "statement currency missing")
            share_capital = ExtractedValue(
                None,
                "CONFLICT",
                share_capital.provider_fields,
                "statement currency missing",
            )

        lease = ExtractedValue(
            None,
            "MISSING",
            reason="Futu statement endpoint does not isolate lease principal repayment",
        )
        issued_shares = ExtractedValue(
            None,
            "MISSING",
            reason="reported share-capital amount is not an issued-share count",
        )
        cancelled_shares = ExtractedValue(
            None,
            "MISSING",
            reason="cancellation requires an exchange filing and share-count bridge",
        )
        net_reduction = ExtractedValue(
            None,
            "MISSING",
            reason="diluted net share reduction requires opening/closing diluted shares and issuance bridge",
        )
        values = {
            "operating_cash_flow": operating,
            "capital_expenditure": capex,
            "lease_principal_repayment": lease,
            "reported_share_capital_amount": share_capital,
            "diluted_total_shares": issued_shares,
            "cancelled_shares": cancelled_shares,
            "diluted_net_share_reduction": net_reduction,
        }
        annual.append(
            {
                "fiscal_year": fiscal_year,
                "fiscal_period": fiscal_period,
                "fiscal_year_end_date": fiscal_end.isoformat(),
                "period_type": "FULL_YEAR",
                "currency": currency,
                "unit": "currency",
                "source_publish_date": None,
                "source_fetch_time": fetched_at.isoformat(),
                "accounting_standards": str(
                    (cash_report or balance_report or {}).get("accounting_standards") or ""
                ),
                "auditor_report": str(
                    (cash_report or balance_report or {}).get("auditor_report") or ""
                ),
                "values": {name: _ledger_value(value) for name, value in values.items()},
            }
        )
        for name, value in values.items():
            unit = "shares" if name in {
                "diluted_total_shares",
                "cancelled_shares",
                "diluted_net_share_reduction",
            } else "currency"
            points.append(
                _raw_point(
                    company_id=company_id,
                    security_id=security_id,
                    share_class=share_class,
                    fiscal_year=fiscal_year,
                    field_name=name,
                    value=value,
                    currency=None if unit == "shares" else currency,
                    unit=unit,
                    fiscal_period=fiscal_period,
                    fetched_at=fetched_at,
                    evidence_path=evidence_path,
                    source_document_suffix=f"{fiscal_period}:{name}",
                )
            )

    return {
        "ledger_version": LEDGER_VERSION,
        "company_id": company_id,
        "security_id": security_id,
        "share_class": share_class,
        "source_evidence_sha256": str(evidence.get("sha256") or ""),
        "annual_source_ledger": annual,
        "raw_data_points": points,
    }


def _number_from_text(token: str) -> Decimal:
    stripped = token.strip().replace(",", "").replace("（", "(").replace("）", ")")
    negative = stripped.startswith("(") and stripped.endswith(")")
    stripped = stripped.strip("()")
    value = decimal_value(stripped)
    if value is None:
        raise SourceLedgerError("empty PDF amount")
    return -value if negative else value


PDF_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])(?:[-−]?\d[\d,]*(?:\.\d+)?|[（(]\d[\d,]*(?:\.\d+)?[）)])")
PDF_UNIT_RE = re.compile(
    r"单位\s*[:：]\s*(?:(人民币|港元|港币|美元)\s*)?(百万元|亿元|万元|千元|元)"
)
PDF_CURRENCY_RE = re.compile(r"币种\s*[:：]\s*(人民币|港元|港币|美元)")


def extract_official_pdf_text_candidates(
    text: str,
    metadata: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Extract page/line candidates from ``pdftotext -layout`` output.

    Extraction deliberately stops at REVIEW_REQUIRED. Validation against the
    prior year's comparative column is a separate deterministic step.
    """

    required = (
        "company_id",
        "security_id",
        "share_class",
        "source_document",
        "source_url_or_local_path",
        "source_publish_date",
        "source_fetch_time",
        "fiscal_period",
        "fiscal_year",
        "fiscal_year_end_date",
    )
    if any(not str(metadata.get(key) or "").strip() for key in required):
        raise SourceLedgerError("official PDF metadata is incomplete")
    _parse_date(metadata["source_publish_date"])
    _parse_date(metadata["fiscal_year_end_date"])
    _parse_datetime(metadata["source_fetch_time"])

    pages = text.split("\f")
    matches_by_field: dict[str, list[dict[str, Any]]] = {
        field: [] for field in OFFICIAL_FIELD_LABELS
    }
    global_line = 0
    for page_number, page in enumerate(pages, start=1):
        unit_match = PDF_UNIT_RE.search(page)
        currency_match = PDF_CURRENCY_RE.search(page)
        unit_label = unit_match.group(2) if unit_match else None
        currency_label = (
            (unit_match.group(1) if unit_match else None)
            or (currency_match.group(1) if currency_match else None)
        )
        currency = CURRENCY_ALIASES.get(str(currency_label)) if currency_label else None
        multiplier = UNIT_MULTIPLIERS.get(str(unit_label)) if unit_label else None
        lines = page.splitlines()
        for local_line, line in enumerate(lines, start=1):
            global_line += 1
            compact = _normalized_name(line)
            for field_name, labels in OFFICIAL_FIELD_LABELS.items():
                if not any(_normalized_name(marker) in _normalized_name(page) for marker in OFFICIAL_SECTION_MARKERS[field_name]):
                    continue
                matched_label = next(
                    (label for label in labels if _normalized_name(label) in compact),
                    None,
                )
                if matched_label is None:
                    continue
                suffix = line[line.find(matched_label) + len(matched_label) :] if matched_label in line else line
                tokens = PDF_NUMBER_RE.findall(suffix)
                amounts: list[Decimal] = []
                for token in tokens:
                    try:
                        amounts.append(_number_from_text(token))
                    except SourceLedgerError:
                        continue
                # Note references usually precede the two statement columns;
                # the last two numeric cells are current and comparative.
                current = amounts[-2] if len(amounts) >= 2 else (amounts[-1] if amounts else None)
                comparative = amounts[-1] if len(amounts) >= 2 else None
                if field_name == "capital_expenditure":
                    current = abs(current) if current is not None else None
                    comparative = abs(comparative) if comparative is not None else None
                matches_by_field[field_name].append(
                    {
                        "field_name": field_name,
                        "label": matched_label,
                        "value": decimal_text(current * multiplier) if current is not None and multiplier is not None else None,
                        "comparative_value": (
                            decimal_text(comparative * multiplier)
                            if comparative is not None and multiplier is not None
                            else None
                        ),
                        "currency": currency,
                        "unit": "currency" if multiplier is not None else None,
                        "unit_label": unit_label,
                        "unit_multiplier": decimal_text(multiplier),
                        "page": page_number,
                        "page_line": local_line,
                        "text_line": global_line,
                        "line_excerpt": line.strip()[:500],
                        "section": OFFICIAL_SECTION_MARKERS[field_name][0],
                        "status": "REVIEW_REQUIRED",
                    }
                )
        global_line += 1  # account for form feed in a stable way

    result: list[dict[str, Any]] = []
    for field_name, matches in matches_by_field.items():
        count = len(matches)
        if not matches:
            result.append(
                {
                    **{key: metadata[key] for key in required},
                    "field_name": field_name,
                    "value": None,
                    "comparative_value": None,
                    "currency": None,
                    "unit": None,
                    "match_count": 0,
                    "status": "REVIEW_REQUIRED",
                    "reason": "no unique consolidated-statement line matched",
                }
            )
            continue
        for match in matches:
            match.update({key: metadata[key] for key in required})
            match["match_count"] = count
            if count != 1:
                match["status"] = "CONFLICT"
                match["reason"] = "multiple consolidated-statement lines matched"
            elif match.get("value") is None or not match.get("currency") or not match.get("unit"):
                match["status"] = "REVIEW_REQUIRED"
                match["reason"] = "amount, currency or unit is not fully identified"
            result.append(match)
    return result


def _within_tolerance(left: Decimal, right: Decimal, relative_tolerance: Decimal) -> bool:
    denominator = max(abs(left), abs(right), Decimal("1"))
    return abs(left - right) / denominator <= relative_tolerance


def validate_official_pdf_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    prior_period_values: Mapping[str, Mapping[str, Any]],
    relative_tolerance: Decimal = Decimal("0.0001"),
) -> list[dict[str, Any]]:
    """Approve only unique candidates whose prior comparative column reconciles."""

    validated: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        field_name = str(item.get("field_name") or "")
        if item.get("match_count") != 1 or item.get("status") == "CONFLICT":
            item["status"] = "CONFLICT"
            item.setdefault("reason", "official candidate is not unique")
            validated.append(item)
            continue
        current = decimal_value(item.get("value"))
        comparative = decimal_value(item.get("comparative_value"))
        prior = prior_period_values.get(field_name)
        prior_value = decimal_value((prior or {}).get("value"))
        same_currency = bool(prior and prior.get("currency") == item.get("currency"))
        same_unit = bool(prior and prior.get("unit") == item.get("unit"))
        if current is None or comparative is None or prior_value is None or not same_currency or not same_unit:
            item["status"] = "REVIEW_REQUIRED"
            item["reason"] = "prior-period amount/currency/unit is unavailable for reconciliation"
        elif not _within_tolerance(comparative, prior_value, relative_tolerance):
            item["status"] = "CONFLICT"
            item["reason"] = "comparative amount does not reconcile to the prior annual report"
        else:
            item["status"] = "KNOWN_ZERO" if current == 0 else "VALID"
            item["reason"] = "unique official line and prior-period comparative reconciled"
        validated.append(item)
    return validated


def official_candidates_to_raw_points(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for item in candidates:
        status = str(item.get("status") or "REVIEW_REQUIRED")
        field_name = str(item.get("field_name") or "")
        if field_name not in OFFICIAL_FIELD_LABELS:
            continue
        data_status = status if status in {"VALID", "KNOWN_ZERO", "CONFLICT"} else "MISSING"
        value = decimal_value(item.get("value")) if data_status in {"VALID", "KNOWN_ZERO"} else None
        fiscal_year = int(item["fiscal_year"])
        points.append(
            {
                "company_id": str(item["company_id"]),
                "field_id": f"FY{fiscal_year}.{field_name}",
                "security_id": str(item["security_id"]),
                "share_class": str(item["share_class"]),
                "source_name": "Official annual report",
                "source_document": str(item["source_document"]),
                "source_url_or_local_path": str(item["source_url_or_local_path"]),
                "source_publish_date": str(item["source_publish_date"]),
                "source_fetch_time": str(item["source_fetch_time"]),
                "fiscal_period": str(item["fiscal_period"]),
                "currency": str(item.get("currency") or "") or None,
                "unit": str(item.get("unit") or "currency"),
                "value": decimal_text(value),
                "data_status": data_status,
                "restatement_status": "UNKNOWN_RESTATEMENT_STATUS",
                "review_status": status,
                "evidence_page": item.get("page"),
                "evidence_line": item.get("text_line"),
                "reason": item.get("reason"),
            }
        )
    return points


def merge_raw_points(
    existing: Sequence[Mapping[str, Any]],
    incoming: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Upsert without replacing an accepted number with missing or disagreement."""

    merged = {str(item.get("field_id")): dict(item) for item in existing if item.get("field_id")}
    for candidate in incoming:
        field_id = str(candidate.get("field_id") or "")
        if not field_id:
            raise SourceLedgerError("incoming raw point lacks field_id")
        current = merged.get(field_id)
        if current is None:
            merged[field_id] = dict(candidate)
            continue
        current_numeric = current.get("data_status") in {"VALID", "KNOWN_ZERO"}
        incoming_numeric = candidate.get("data_status") in {"VALID", "KNOWN_ZERO"}
        if current_numeric and not incoming_numeric:
            continue
        if current_numeric and incoming_numeric:
            if decimal_value(current.get("value")) != decimal_value(candidate.get("value")):
                raise SourceLedgerConflict(f"accepted source values disagree for {field_id}")
            # A verified filing outranks provider database snapshots when equal.
            # Do not key this solely to an English display label: the real
            # publisher is normally CNINFO or HKEX and must remain visible.
            if candidate.get("source_level") == "OFFICIAL_FILING" or candidate.get(
                "source_name"
            ) == "Official annual report":
                merged[field_id] = dict(candidate)
            continue
        if incoming_numeric:
            merged[field_id] = dict(candidate)
            continue
        if current.get("data_status") == "CONFLICT":
            continue
        merged[field_id] = dict(candidate)
    return [merged[key] for key in sorted(merged)]


def _coverage_rows(annual: Sequence[Mapping[str, Any]], max_years: int = 5) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in annual[:max_years]:
        values = item.get("values") if isinstance(item.get("values"), Mapping) else {}
        row: dict[str, Any] = {
            "fiscal_year": int(item["fiscal_year"]),
            "fiscal_year_end_date": str(item["fiscal_year_end_date"]),
            "fiscal_period": str(item["fiscal_period"]),
            "period_type": "FULL_YEAR",
        }
        for field_name in (
            "operating_cash_flow",
            "capital_expenditure",
            "lease_principal_repayment",
        ):
            field = values.get(field_name) if isinstance(values, Mapping) else None
            valid = isinstance(field, Mapping) and field.get("data_status") in {"VALID", "KNOWN_ZERO"}
            row[field_name] = str(field.get("value")) if valid else None
        rows.append(row)
    return rows


def apply_ledger_to_staging_record(
    staging: Mapping[str, Any],
    ledger: Mapping[str, Any],
    *,
    industry_kind: str = "NON_FINANCIAL",
    official_points: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Return a patched copy. The caller controls whether it is written."""

    company_id = str(staging.get("company_id") or "")
    if company_id != str(ledger.get("company_id") or ""):
        raise SourceLedgerConflict("staging and ledger company ids differ")
    result = copy.deepcopy(dict(staging))
    annual = ledger.get("annual_source_ledger")
    if not isinstance(annual, Sequence) or isinstance(annual, (str, bytes)):
        raise SourceLedgerError("ledger annual rows are invalid")
    incoming_points = [
        item for item in ledger.get("raw_data_points", []) if isinstance(item, Mapping)
    ]
    incoming_points.extend(item for item in official_points if isinstance(item, Mapping))
    result["raw_data_points"] = merge_raw_points(
        [item for item in result.get("raw_data_points", []) if isinstance(item, Mapping)],
        incoming_points,
    )
    result["annual_source_ledger"] = copy.deepcopy(list(annual))
    if result.get("industry_kind") in {None, "", "UNSUPPORTED"}:
        result["industry_kind"] = industry_kind

    coverage = result.get("coverage")
    coverage = copy.deepcopy(dict(coverage)) if isinstance(coverage, Mapping) else {}
    existing_by_year = {
        int(item["fiscal_year"]): dict(item)
        for item in coverage.get("fcf_years", [])
        if isinstance(item, Mapping) and item.get("fiscal_year") is not None
    }
    for incoming in _coverage_rows(list(annual)):
        year = int(incoming["fiscal_year"])
        current = existing_by_year.get(year, {})
        for field_name in (
            "operating_cash_flow",
            "capital_expenditure",
            "lease_principal_repayment",
        ):
            old = current.get(field_name)
            new = incoming.get(field_name)
            if old is not None and new is not None and decimal_value(old) != decimal_value(new):
                raise SourceLedgerConflict(f"coverage value conflict for FY{year}.{field_name}")
            if old is None and new is not None:
                current[field_name] = new
            elif field_name not in current:
                current[field_name] = None
        for metadata_field in ("fiscal_year", "fiscal_year_end_date", "fiscal_period", "period_type"):
            current.setdefault(metadata_field, incoming[metadata_field])
        existing_by_year[year] = current
    coverage["fcf_years"] = [existing_by_year[year] for year in sorted(existing_by_year, reverse=True)]
    result["coverage"] = coverage
    summary = result.setdefault("source_summary", {})
    summary["futu_financial_ledger"] = {
        "ledger_version": str(ledger.get("ledger_version") or LEDGER_VERSION),
        "full_years_indexed": len(annual),
        "latest_fiscal_period": annual[0].get("fiscal_period") if annual else None,
        "cash_flow_scope": "CFO and capex candidates; lease principal is not isolated",
        "share_count_scope": "share-capital amount only; issued/diluted shares remain missing",
    }
    securities = [item for item in result.get("securities", []) if isinstance(item, Mapping)]
    monitored_security_ids = [str(item.get("security_id") or "") for item in securities]
    known_classes = KNOWN_MULTI_MARKET_SECURITIES.get(str(ledger.get("security_id") or ""))
    summary["share_class_coverage"] = {
        "status": (
            "CROSS_LISTING_REVIEW_REQUIRED"
            if known_classes
            else "COMPLETE_SHARE_CLASS_SCOPE_UNVERIFIED"
        ),
        "monitored_security_ids": monitored_security_ids,
        "known_market_classes": list(known_classes or ()),
        "rights_verified": False,
        "reason": (
            "watchlist contains one monitored security per issuer; it is not proof of all material ordinary share classes"
        ),
    }
    summary["migration_status"] = "SOURCE_BACKFILL_PARTIAL"
    return result


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SourceLedgerError(f"expected JSON object: {path}")
    return payload


def load_sqlite_buyback_evidence(database_path: Path, company_id: str) -> list[dict[str, Any]]:
    """Read Futu buyback rows as review evidence, never as eligible cancellation.

    Corporate-action rows can establish reported cash/shares and process dates,
    but cannot by themselves prove cancellation or the diluted share bridge.
    They therefore remain unallocated event evidence until an official filing
    maps them to a fiscal period and confirms the capital movement.
    """

    connection = sqlite3.connect(f"file:{database_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT event_key, event_date, source, source_url, payload_json,
                   first_seen_at, last_seen_at
            FROM events
            WHERE issuer_id = ? AND event_type = 'buyback'
            ORDER BY event_date DESC, event_key
            """,
            (company_id,),
        ).fetchall()
    finally:
        connection.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, Mapping):
            continue
        shares = decimal_value(payload.get("buy_back_sum"))
        cash = decimal_value(payload.get("buy_back_money"))
        change_registration = str(payload.get("change_reg_date_str") or "") or None
        process = str(payload.get("event_proce_desc") or "") or None
        result.append(
            {
                "event_key": str(row["event_key"]),
                "event_date": str(row["event_date"] or "") or None,
                "fiscal_period": "UNALLOCATED_REQUIRES_FISCAL_MAPPING",
                "source_name": str(row["source"] or "Futu OpenD corporate actions"),
                "source_url": str(row["source_url"] or "") or None,
                "source_first_seen_at": str(row["first_seen_at"]),
                "source_last_seen_at": str(row["last_seen_at"]),
                "reported_buyback_cash": decimal_text(cash),
                "reported_buyback_cash_status": "MISSING" if cash is None else "KNOWN_ZERO" if cash == 0 else "VALID",
                "reported_buyback_shares": decimal_text(shares),
                "reported_buyback_shares_status": "MISSING" if shares is None else "KNOWN_ZERO" if shares == 0 else "VALID",
                "currency": None,
                "record_market": str(payload.get("record_market") or "") or None,
                "share_type": str(payload.get("share_type") or "") or None,
                "process": process,
                "change_registration_date": change_registration,
                "cancellation_verification_status": (
                    "REVIEW_REQUIRED"
                    if process == "实施完成" and change_registration
                    else "MISSING"
                ),
                "eligible_for_core_cancelled_buyback": False,
                "reason": (
                    "Futu corporate action does not prove legal cancellation and diluted net share reduction"
                ),
            }
        )
    return result
