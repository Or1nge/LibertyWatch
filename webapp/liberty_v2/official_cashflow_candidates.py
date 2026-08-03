from __future__ import annotations

import hashlib
import re
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from liberty_v2.source_ledger import build_futu_financial_ledger


SCHEMA_VERSION = "official-cashflow-candidates-v1.0"
FIELD_NAMES = ("operating_cash_flow", "capital_expenditure")
ACCEPTED_SOURCE_STATUSES = {"VALID", "KNOWN_ZERO"}


class OfficialCashflowCandidateError(RuntimeError):
    pass


_NUMBER_RE = re.compile(
    r"(?<![\d.])(?:[-\u2212]?\d[\d,]*(?:\.\d+)?|[\(（]\s*\d[\d,]*(?:\.\d+)?\s*[\)）])(?![\d.])"
)

_STATEMENT_TITLES = (
    "合并现金流量表",
    "合併現金流量表",
    "综合现金流量表",
    "綜合現金流量表",
)
_STATEMENT_TITLE_EXCLUSIONS = (
    "附注",
    "附註",
    "补充资料",
    "補充資料",
    "主要项目",
    "主要項目",
)

_CFO_LABELS = (
    "经营活动产生的现金流量净额",
    "经营活动现金流量净额",
    "经营活动产生现金流量净额",
    "經營活動產生的現金流量淨額",
    "經營活動產生之現金流量淨額",
    "經營活動現金流量淨額",
    "經營活動所得之現金淨額",
    "經營活動所得現金淨額",
    "經營活動之現金流入淨額",
    "經營活動產生之現金淨額",
    "netcashfromoperatingactivities",
    "netcashgeneratedfromoperatingactivities",
    "netcashprovidedbyoperatingactivities",
    "netcashinflowfromoperatingactivities",
)

_DIRECT_CAPEX_LABELS = (
    "购建固定资产、无形资产和其他长期资产支付的现金",
    "購建固定資產、無形資產和其他長期資產支付的現金",
)

_FIXED_ASSET_LABELS = (
    "购买物业、厂房及设备",
    "購買物業、廠房及設備",
    "购买物业、厂房及机器设备",
    "購買物業、廠房及機器設備",
    "购置物业、厂房及设备",
    "購置物業、廠房及設備",
    "购入物业、厂房及设备",
    "購入物業、廠房及設備",
    "购入物业、机器及设备",
    "購入物業、機器及設備",
    "添置物业、厂房及设备",
    "添置物業、廠房及設備",
    "购买固定资产",
    "購買固定資產",
    "购入固定资产",
    "購入固定資產",
    "添置固定资产",
    "添置固定資產",
    "purchaseofproperty,plantandequipment",
    "purchasesofproperty,plantandequipment",
    "purchaseofproperty,plantandmachinery",
    "paymentforpurchaseofproperty,plantandequipment",
    "paymentsforpurchaseofproperty,plantandequipment",
)

_INTANGIBLE_LABELS = (
    "购买无形资产",
    "購買無形資產",
    "购入无形资产",
    "購入無形資產",
    "购置无形资产",
    "購置無形資產",
    "添置无形资产",
    "添置無形資產",
    "工程开发成本之资本化开支",
    "工程開發成本之資本化開支",
    "资本化开发支出",
    "資本化開發支出",
    "purchaseofintangibleassets",
    "purchasesofintangibleassets",
    "paymentforpurchaseofintangibleassets",
    "paymentsforpurchaseofintangibleassets",
    "capitaliseddevelopmentcosts",
    "capitalizeddevelopmentcosts",
)

_UNIT_PATTERNS: tuple[tuple[re.Pattern[str], str, Decimal, str], ...] = (
    (re.compile(r"人民币百万元|人民幣百萬元|RMB\s*(?:million|mn)\b", re.I), "CNY", Decimal("1000000"), "CNY million"),
    (re.compile(r"人民币万元|人民幣萬元"), "CNY", Decimal("10000"), "CNY ten-thousand"),
    (re.compile(r"人民币千元|人民幣千元|RMB\s*(?:['’]?000|thousand)\b", re.I), "CNY", Decimal("1000"), "CNY thousand"),
    (re.compile(r"人民币元|人民幣元"), "CNY", Decimal("1"), "CNY yuan"),
    (re.compile(r"港(?:币|幣)百万元|港元百萬元|HK\$\s*(?:million|mn)\b", re.I), "HKD", Decimal("1000000"), "HKD million"),
    (re.compile(r"港(?:币|幣)万元|港元萬元"), "HKD", Decimal("10000"), "HKD ten-thousand"),
    (re.compile(r"千港元|港(?:币|幣)千元|HK\$\s*(?:['’]?000|thousand)\b", re.I), "HKD", Decimal("1000"), "HKD thousand"),
    (re.compile(r"美元百万元|美元百萬元|US\$\s*(?:million|mn)\b|USD\s*(?:million|mn)\b", re.I), "USD", Decimal("1000000"), "USD million"),
    (re.compile(r"美元万元|美元萬元"), "USD", Decimal("10000"), "USD ten-thousand"),
    (re.compile(r"千美元|美元千元|US\$\s*(?:['’]?000|thousand)\b|USD\s*(?:['’]?000|thousand)\b", re.I), "USD", Decimal("1000"), "USD thousand"),
)
_GENERIC_UNIT_PATTERNS: tuple[tuple[re.Pattern[str], Decimal, str], ...] = (
    (re.compile(r"单位\s*[:：]\s*百万元|單位\s*[:：]\s*百萬元"), Decimal("1000000"), "million"),
    (re.compile(r"单位\s*[:：]\s*亿元|單位\s*[:：]\s*億元"), Decimal("100000000"), "hundred-million"),
    (re.compile(r"单位\s*[:：]\s*万元|單位\s*[:：]\s*萬元"), Decimal("10000"), "ten-thousand"),
    (re.compile(r"单位\s*[:：]\s*千元|單位\s*[:：]\s*千元"), Decimal("1000"), "thousand"),
    (re.compile(r"单位\s*[:：]\s*元|單位\s*[:：]\s*元"), Decimal("1"), "yuan"),
)


def _compact(value: str) -> str:
    return (
        re.sub(r"\s+", "", value)
        .replace("：", ":")
        .replace("，", ",")
        .replace("（", "(")
        .replace("）", ")")
        .lower()
    )


def _decimal_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _parse_number(token: str) -> Decimal:
    normalized = (
        token.strip()
        .replace(",", "")
        .replace("−", "-")
        .replace("（", "(")
        .replace("）", ")")
    )
    negative_parentheses = normalized.startswith("(") and normalized.endswith(")")
    normalized = normalized.strip("() ")
    try:
        value = Decimal(normalized)
    except InvalidOperation as error:
        raise OfficialCashflowCandidateError(f"invalid annual-report amount: {token!r}") from error
    if not value.is_finite():
        raise OfficialCashflowCandidateError("NaN and Infinity are forbidden")
    return -value if negative_parentheses else value


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_pdf_sha256(pdf_path: Path, expected: str) -> None:
    if _file_sha256(pdf_path) != str(expected):
        raise OfficialCashflowCandidateError(f"annual-report SHA-256 mismatch: {pdf_path}")


def pdftotext_layout(pdf_path: Path, *, timeout_seconds: int = 120) -> str:
    """Run Poppler with an argv array and never persist the extracted full text."""

    completed = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise OfficialCashflowCandidateError(
            f"pdftotext failed ({completed.returncode}): {completed.stderr[-500:]}"
        )
    return completed.stdout


def build_futu_reference(evidence: Mapping[str, Any], *, evidence_path: Path) -> dict[int, dict[str, Any]]:
    """Return read-only annual references from the immutable Futu response."""

    ledger = build_futu_financial_ledger(evidence, evidence_path=evidence_path)
    evidence_file_sha = _file_sha256(evidence_path)
    result: dict[int, dict[str, Any]] = {}
    for annual in ledger.get("annual_source_ledger", []):
        year = int(annual["fiscal_year"])
        values = annual.get("values") if isinstance(annual.get("values"), Mapping) else {}
        fields: dict[str, Any] = {}
        for field_name in FIELD_NAMES:
            raw = values.get(field_name) if isinstance(values, Mapping) else None
            item = dict(raw) if isinstance(raw, Mapping) else {}
            fields[field_name] = {
                "value": str(item["value"]) if item.get("value") is not None else None,
                "currency": str(annual.get("currency") or "") or None,
                "data_status": str(item.get("data_status") or "MISSING"),
                "provider_fields": list(item.get("provider_fields") or []),
            }
        result[year] = {
            "fiscal_year": year,
            "fiscal_year_end_date": str(annual.get("fiscal_year_end_date") or ""),
            "fiscal_period": str(annual.get("fiscal_period") or ""),
            "source_local_path": str(evidence_path.resolve()),
            "source_file_sha256": evidence_file_sha,
            "source_payload_sha256": str(evidence.get("sha256") or "") or None,
            "fields": fields,
        }
    return result


def _statement_page(page: str) -> bool:
    for line in page.splitlines():
        compact = _compact(line)
        if any(exclusion in compact for exclusion in _STATEMENT_TITLE_EXCLUSIONS):
            continue
        if any(_compact(title) in compact for title in _STATEMENT_TITLES):
            return True
    return False


def _statement_title_line(line: str) -> bool:
    compact = _compact(line)
    if any(exclusion in compact for exclusion in _STATEMENT_TITLE_EXCLUSIONS):
        return False
    for title in _STATEMENT_TITLES:
        marker = _compact(title)
        if marker not in compact:
            continue
        prefix, suffix = compact.split(marker, 1)
        if re.sub(r"[0-9一二三四五六七八九十、.()（）]", "", prefix):
            continue
        # A bilingual title is allowed; a table-of-contents trailing page
        # number is not a statement boundary.
        if suffix and not re.fullmatch(r"[a-z]+", suffix):
            continue
        return True
    return False


def _ends_consolidated_cashflow(line: str) -> bool:
    compact = _compact(line)
    cashflow_title = "现金流量表" in compact or "現金流量表" in compact
    if cashflow_title and not any(_compact(title) in compact for title in _STATEMENT_TITLES):
        return True
    return any(
        marker in compact
        for marker in (
            "所有者权益变动表",
            "所有者權益變動表",
            "股东权益变动表",
            "股東權益變動表",
            "權益變動表",
            "权益变动表",
        )
    )


def _unit_context(page: str, metadata_currency: str | None, market: str) -> dict[str, Any]:
    contexts: set[tuple[str, Decimal, str]] = set()
    for pattern, currency, multiplier, label in _UNIT_PATTERNS:
        if pattern.search(page):
            contexts.add((currency, multiplier, label))
    # Bilingual headers often express the same context twice; the set removes it.
    normalized = {(currency, multiplier) for currency, multiplier, _label in contexts}
    if len(normalized) == 1:
        currency, multiplier = next(iter(normalized))
        labels = sorted(label for item_currency, item_multiplier, label in contexts if (item_currency, item_multiplier) == (currency, multiplier))
        return {
            "currency": currency,
            "multiplier": multiplier,
            "unit_label": " / ".join(labels),
            "basis": "statement_page_explicit_currency_and_unit",
            "status": "VALID",
        }
    if len(normalized) > 1:
        return {
            "currency": None,
            "multiplier": None,
            "unit_label": None,
            "basis": "multiple_currency_or_unit_contexts_on_statement_page",
            "status": "CONFLICT",
        }

    generic: set[tuple[Decimal, str]] = set()
    for pattern, multiplier, label in _GENERIC_UNIT_PATTERNS:
        if pattern.search(page):
            generic.add((multiplier, label))
    generic_multipliers = {item[0] for item in generic}
    if len(generic_multipliers) == 1 and market == "CN" and metadata_currency == "CNY":
        multiplier = next(iter(generic_multipliers))
        return {
            "currency": "CNY",
            "multiplier": multiplier,
            "unit_label": " / ".join(sorted(item[1] for item in generic)),
            "basis": "statement_page_unit_plus_official_CN_manifest_currency",
            "status": "VALID",
        }
    return {
        "currency": None,
        "multiplier": None,
        "unit_label": None,
        "basis": "statement_currency_or_unit_is_not_unambiguous",
        "status": "REVIEW",
    }


def _labels_in_line(line: str, labels: Sequence[str]) -> list[str]:
    compact = _compact(line)
    return [label for label in labels if _compact(label) in compact]


def _row_matches(
    text: str,
    metadata: Mapping[str, Any],
    labels: Sequence[str],
    *,
    absolute_value: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    global_line = 0
    active_statement = False
    active_unit: dict[str, Any] | None = None
    for page_number, page in enumerate(text.split("\f"), start=1):
        page_lines = page.splitlines()
        page_unit = _unit_context(
            page,
            str(metadata.get("currency") or "") or None,
            str(metadata.get("market") or ""),
        )
        for page_line, line in enumerate(page_lines, start=1):
            global_line += 1
            if _statement_title_line(line):
                active_statement = True
                active_unit = page_unit
            elif active_statement and _ends_consolidated_cashflow(line):
                active_statement = False
                active_unit = None
            if not active_statement:
                continue
            matched = _labels_in_line(line, labels)
            if not matched:
                continue
            # Do not consume the next financial row. A wrapped bilingual label
            # may place its two cells on the immediately following line, so one
            # continuation is allowed only when the label line has no numbers.
            excerpt_lines = [line]
            tokens = _NUMBER_RE.findall(line)
            if not tokens and page_line < len(page_lines):
                continuation = page_lines[page_line]
                if not any(
                    _labels_in_line(continuation, known)
                    for known in (_CFO_LABELS, _DIRECT_CAPEX_LABELS, _FIXED_ASSET_LABELS, _INTANGIBLE_LABELS)
                ):
                    excerpt_lines.append(continuation)
                    tokens = _NUMBER_RE.findall(continuation)
            amounts = [_parse_number(token) for token in tokens]
            current = amounts[-2] if len(amounts) >= 2 else None
            comparative = amounts[-1] if len(amounts) >= 2 else None
            if absolute_value:
                current = abs(current) if current is not None else None
                comparative = abs(comparative) if comparative is not None else None
            multiplier = active_unit.get("multiplier") if active_unit else None
            rows.append(
                {
                    "matched_labels": matched,
                    "raw_current_value": _decimal_text(current),
                    "raw_comparative_value": _decimal_text(comparative),
                    "normalized_current_value": _decimal_text(current * multiplier) if current is not None and multiplier is not None else None,
                    "normalized_comparative_value": _decimal_text(comparative * multiplier) if comparative is not None and multiplier is not None else None,
                    "currency": active_unit.get("currency") if active_unit else None,
                    "unit": "currency" if multiplier is not None else None,
                    "source_unit_label": active_unit.get("unit_label") if active_unit else None,
                    "unit_multiplier": _decimal_text(multiplier),
                    "unit_context_basis": active_unit.get("basis") if active_unit else None,
                    "unit_context_status": active_unit.get("status") if active_unit else "REVIEW",
                    "page": page_number,
                    "page_line": page_line,
                    "text_line": global_line,
                    "line_excerpt": " | ".join(item.strip() for item in excerpt_lines)[:500],
                }
            )
        global_line += 1
    return rows


def _select_unique_rows(rows: Sequence[Mapping[str, Any]], *, component_name: str) -> dict[str, Any]:
    copied = [dict(row) for row in rows]
    if not copied:
        return {
            "component": component_name,
            "status": "REVIEW",
            "reason": "no uniquely scoped consolidated cash-flow row found; unknown is not zero",
            "current_value": None,
            "comparative_value": None,
            "currency": None,
            "unit": None,
            "unit_multiplier": None,
            "evidence_rows": [],
            "match_count": 0,
        }
    if len(copied) != 1:
        return {
            "component": component_name,
            "status": "CONFLICT",
            "reason": "multiple consolidated cash-flow rows matched",
            "current_value": None,
            "comparative_value": None,
            "currency": None,
            "unit": None,
            "unit_multiplier": None,
            "evidence_rows": copied,
            "match_count": len(copied),
        }
    row = copied[0]
    complete = (
        row.get("normalized_current_value") is not None
        and row.get("normalized_comparative_value") is not None
        and row.get("currency")
        and row.get("unit") == "currency"
        and row.get("unit_context_status") == "VALID"
    )
    return {
        "component": component_name,
        "status": "REVIEW",
        "reason": (
            "unique official row awaits Futu and comparative-report reconciliation"
            if complete
            else "amount, comparative amount, currency or unit is not unambiguous"
        ),
        "current_value": str(row["normalized_current_value"]) if complete else None,
        "comparative_value": str(row["normalized_comparative_value"]) if complete else None,
        "currency": str(row["currency"]) if complete else None,
        "unit": "currency" if complete else None,
        "unit_multiplier": str(row["unit_multiplier"]) if complete else None,
        "evidence_rows": copied,
        "match_count": 1,
    }


def _capex_components(futu_field: Mapping[str, Any]) -> tuple[str, ...]:
    field_ids = {
        int(item["field_id"])
        for item in futu_field.get("provider_fields", [])
        if isinstance(item, Mapping) and item.get("field_id") is not None
    }
    if 3043 in field_ids:
        return ("direct",)
    components: list[str] = []
    if 5071 in field_ids:
        components.append("fixed_assets")
    if 5073 in field_ids:
        components.append("intangible_assets")
    return tuple(components)


def _sum_components(parts: Sequence[Mapping[str, Any]], required: Sequence[str]) -> dict[str, Any]:
    by_name = {str(item["component"]): dict(item) for item in parts}
    selected = [by_name[name] for name in required if name in by_name]
    conflicts = [item for item in selected if item.get("status") == "CONFLICT"]
    if conflicts:
        return {
            "status": "CONFLICT",
            "reason": "one or more required capital-expenditure components have multiple matches",
            "current_value": None,
            "comparative_value": None,
            "currency": None,
            "unit": None,
            "components": selected,
        }
    if not required:
        return {
            "status": "REVIEW",
            "reason": "Futu has no complete capex component definition for this fiscal year",
            "current_value": None,
            "comparative_value": None,
            "currency": None,
            "unit": None,
            "components": list(parts),
        }
    if len(selected) != len(required) or any(item.get("current_value") is None or item.get("comparative_value") is None for item in selected):
        return {
            "status": "REVIEW",
            "reason": "one or more required capital-expenditure components are missing or ambiguous",
            "current_value": None,
            "comparative_value": None,
            "currency": None,
            "unit": None,
            "components": selected,
        }
    currencies = {str(item.get("currency")) for item in selected}
    units = {str(item.get("unit")) for item in selected}
    if len(currencies) != 1 or units != {"currency"}:
        return {
            "status": "CONFLICT",
            "reason": "capital-expenditure components do not share one currency/unit",
            "current_value": None,
            "comparative_value": None,
            "currency": None,
            "unit": None,
            "components": selected,
        }
    current = sum(Decimal(str(item["current_value"])) for item in selected)
    comparative = sum(Decimal(str(item["comparative_value"])) for item in selected)
    return {
        "status": "REVIEW",
        "reason": "complete official capex component set awaits deterministic reconciliation",
        "current_value": _decimal_text(current),
        "comparative_value": _decimal_text(comparative),
        "currency": next(iter(currencies)),
        "unit": "currency",
        "components": selected,
    }


def extract_official_cashflow_report(
    text: str,
    metadata: Mapping[str, Any],
    futu_year: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Extract conservative CFO/capex candidates from one official report."""

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
        raise OfficialCashflowCandidateError(f"annual-report metadata is incomplete: {missing}")

    futu_fields = (futu_year or {}).get("fields") if isinstance((futu_year or {}).get("fields"), Mapping) else {}
    cfo = _select_unique_rows(
        _row_matches(text, metadata, _CFO_LABELS, absolute_value=False),
        component_name="operating_cash_flow",
    )
    direct = _select_unique_rows(
        _row_matches(text, metadata, _DIRECT_CAPEX_LABELS, absolute_value=True),
        component_name="direct",
    )
    fixed = _select_unique_rows(
        _row_matches(text, metadata, _FIXED_ASSET_LABELS, absolute_value=True),
        component_name="fixed_assets",
    )
    intangible = _select_unique_rows(
        _row_matches(text, metadata, _INTANGIBLE_LABELS, absolute_value=True),
        component_name="intangible_assets",
    )
    required_capex = _capex_components(
        futu_fields.get("capital_expenditure", {})
        if isinstance(futu_fields.get("capital_expenditure"), Mapping)
        else {}
    )
    if required_capex == ("direct",):
        capex = _sum_components([direct], required_capex)
    else:
        capex = _sum_components([fixed, intangible], required_capex)

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
        "candidate_only": True,
        "writes_production": False,
        "fields": {
            "operating_cash_flow": {
                **cfo,
                "futu_reference": dict(futu_fields.get("operating_cash_flow") or {}),
                "futu_reconciliation": "NOT_CHECKED",
                "comparative_report_reconciliation": "NOT_CHECKED",
                "value": None,
            },
            "capital_expenditure": {
                **capex,
                "required_component_basis": list(required_capex),
                "futu_reference": dict(futu_fields.get("capital_expenditure") or {}),
                "futu_reconciliation": "NOT_CHECKED",
                "comparative_report_reconciliation": "NOT_CHECKED",
                "value": None,
            },
        },
        "futu_source": {
            "source_local_path": (futu_year or {}).get("source_local_path"),
            "source_file_sha256": (futu_year or {}).get("source_file_sha256"),
            "source_payload_sha256": (futu_year or {}).get("source_payload_sha256"),
            "fiscal_year_end_date": (futu_year or {}).get("fiscal_year_end_date"),
            "fiscal_period": (futu_year or {}).get("fiscal_period"),
        },
    }


def _same_amount(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError, TypeError):
        return False


def reconcile_company_reports(reports: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Require exact Futu and adjacent-report comparative reconciliation."""

    rows = [dict(item) for item in sorted(reports, key=lambda item: int(item["fiscal_year"]))]
    for index, report in enumerate(rows):
        report["fields"] = {
            field_name: dict(report["fields"][field_name]) for field_name in FIELD_NAMES
        }
        prior = rows[index - 1] if index > 0 else None
        for field_name in FIELD_NAMES:
            field = report["fields"][field_name]
            extracted_current = field.get("current_value")
            extracted_comparative = field.get("comparative_value")
            currency = field.get("currency")
            futu = field.get("futu_reference") if isinstance(field.get("futu_reference"), Mapping) else {}

            if extracted_current is None:
                field["futu_reconciliation"] = "NOT_CHECKED"
            elif futu.get("data_status") not in ACCEPTED_SOURCE_STATUSES or futu.get("value") is None:
                field["futu_reconciliation"] = "FUTU_MISSING"
            elif str(futu.get("currency") or "") != str(currency or ""):
                field["futu_reconciliation"] = "CURRENCY_MISMATCH"
            elif _same_amount(extracted_current, futu.get("value")):
                field["futu_reconciliation"] = "MATCH"
            else:
                field["futu_reconciliation"] = "AMOUNT_MISMATCH"

            if prior is None:
                field["comparative_report_reconciliation"] = "NO_PRIOR_REPORT"
            elif int(report["fiscal_year"]) != int(prior["fiscal_year"]) + 1:
                field["comparative_report_reconciliation"] = "YEAR_GAP"
            else:
                prior_field = prior.get("fields", {}).get(field_name, {})
                prior_current = prior_field.get("current_value") if isinstance(prior_field, Mapping) else None
                prior_currency = prior_field.get("currency") if isinstance(prior_field, Mapping) else None
                if extracted_comparative is None or prior_current is None:
                    field["comparative_report_reconciliation"] = "VALUE_UNAVAILABLE"
                elif str(currency or "") != str(prior_currency or ""):
                    field["comparative_report_reconciliation"] = "CURRENCY_MISMATCH"
                elif _same_amount(extracted_comparative, prior_current):
                    field["comparative_report_reconciliation"] = "MATCH"
                else:
                    field["comparative_report_reconciliation"] = "AMOUNT_MISMATCH"

            extraction_conflict = field.get("status") == "CONFLICT"
            reconciliation_conflict = field["futu_reconciliation"] in {
                "CURRENCY_MISMATCH",
                "AMOUNT_MISMATCH",
            } or field["comparative_report_reconciliation"] in {
                "CURRENCY_MISMATCH",
                "AMOUNT_MISMATCH",
            }
            if extraction_conflict or reconciliation_conflict:
                field["status"] = "CONFLICT"
                field["reason"] = "official/Futu or cross-report amounts do not reconcile uniquely"
                field["value"] = None
            elif (
                extracted_current is not None
                and field["futu_reconciliation"] == "MATCH"
                and field["comparative_report_reconciliation"] == "MATCH"
            ):
                field["status"] = "VALID"
                field["reason"] = "unique official row matches Futu and the prior report comparative amount"
                field["value"] = str(extracted_current)
            else:
                field["status"] = "REVIEW"
                field["value"] = None
                if extracted_current is not None:
                    field["reason"] = "candidate awaits complete Futu and prior-report reconciliation"
            # No candidate is allowed to write a core raw data point.
            field["eligible_for_core_write"] = False
        report["status"] = (
            "CONFLICT"
            if any(report["fields"][name]["status"] == "CONFLICT" for name in FIELD_NAMES)
            else "VALID"
            if all(report["fields"][name]["status"] == "VALID" for name in FIELD_NAMES)
            else "REVIEW"
        )
    return rows
