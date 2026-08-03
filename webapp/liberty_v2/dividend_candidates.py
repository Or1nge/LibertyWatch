from __future__ import annotations

import hashlib
import re
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "dividend-candidates-v1.0"


class DividendCandidateError(RuntimeError):
    pass


_DIVIDEND_TERMS = (
    "股息",
    "股利",
    "红利",
    "紅利",
    "现金分红",
    "現金分紅",
    "dividend",
)
_SPECIAL_TERMS = ("特别股息", "特別股息", "特殊股息", "specialdividend")
_EXCLUDED_CONTEXT = (
    "股息收益率",
    "预期股息",
    "預期股息",
    "股息等值",
    "应付股息",
    "應付股息",
    "预扣税",
    "預扣稅",
    "非控股权益",
    "非控股權益",
    "来自权益法",
    "來自權益法",
)
_HARD_EXCLUDED_CONTEXT = (
    "换股价",
    "換股價",
    "转股价",
    "轉股價",
    "每股盈利",
    "每股收益",
    "每股资产",
    "每股資產",
    "股息率",
)
_ACTION_CONTEXT = (
    "派发",
    "派發",
    "派付",
    "支付",
    "宣派",
    "批准",
    "通过",
    "通過",
    "建议",
    "建議",
    "拟",
    "擬",
    "利润分配",
    "利潤分配",
    "现金分红金额",
    "現金分紅金額",
    "普通股息",
    "中期股息",
    "末期股息",
    "特别股息",
    "特別股息",
    "paid",
    "declared",
    "approved",
    "proposed",
    "recommended",
)

_CURRENCY_TOKEN = (
    r"(?:人民币|人民幣|港币|港幣|港元|美元|美金|新加坡元|新加坡幣|"
    r"欧元|歐元|英镑|英鎊|日元|日圓|rmb|cny|hkd|usd|hk\$|us\$|s\$|\$)"
)
_MONEY_UNIT_TOKEN = (
    r"(?:亿元|億元|百万元|百萬元|百万|百萬|万元|萬元|港元|人民币元|"
    r"人民幣元|美元|新加坡元|欧元|歐元|英镑|英鎊|日元|日圓|元|分|仙|美分)"
)
_NUMBER = r"(?P<value>\d[\d,]*(?:\.\d+)?)"

_PER_SHARE_PATTERNS = (
    re.compile(
        rf"每(?P<basis>\d+)股(?:普通股)?(?:派发|派發|派付|分配|派)?"
        rf"(?:现金|現金)?(?:股息|股利|红利|紅利)?"
        rf"(?P<currency_pre>{_CURRENCY_TOKEN})?{_NUMBER}(?P<unit>{_MONEY_UNIT_TOKEN})"
    ),
    re.compile(
        rf"每股(?:普通股)?(?P<currency_pre>{_CURRENCY_TOKEN})?"
        rf"{_NUMBER}(?P<unit>{_MONEY_UNIT_TOKEN})"
    ),
    re.compile(
        rf"(?P<currency_pre>us\$|hk\$|s\$){_NUMBER}"
        rf"(?:per|/)(?:ordinary)?share"
    ),
)

_TOTAL_PATTERNS = (
    re.compile(
        rf"(?:拟|擬|建议|建議|已)?(?:派发|派發|派付|支付|分配|宣派)?"
        rf"(?:普通)?(?:现金|現金)?(?:股息|股利|红利|紅利)"
        rf"(?:总额|總額|合计|合計|合共|金额|金額|共|总计|總計)?"
        rf"(?:为|為|约为|約為)?(?P<currency_pre>{_CURRENCY_TOKEN})?"
        rf"{_NUMBER}(?P<unit>{_MONEY_UNIT_TOKEN})(?P<currency_post>{_CURRENCY_TOKEN})?"
    ),
    re.compile(
        rf"(?:总计|總計|合计|合計|合共)(?:普通)?(?:现金|現金)?"
        rf"(?:股息|股利|红利|紅利)(?:为|為)?(?P<currency_pre>{_CURRENCY_TOKEN})?"
        rf"{_NUMBER}(?P<unit>{_MONEY_UNIT_TOKEN})(?P<currency_post>{_CURRENCY_TOKEN})?"
    ),
    re.compile(
        rf"(?:现金|現金)分红金额(?:\([^)]*\)|（[^）]*）)?"
        rf"(?P<currency_pre>{_CURRENCY_TOKEN})?{_NUMBER}(?P<unit>{_MONEY_UNIT_TOKEN})?"
        rf"(?P<currency_post>{_CURRENCY_TOKEN})?"
    ),
)

_ARABIC_YEAR = re.compile(r"(?<!\d)(20\d{2})(?:年|年度|財政年度|财政年度)?")
_CHINESE_YEAR = re.compile(r"([二〇零一二三四五六七八九]{4})年")
_CHINESE_DIGITS = {
    "〇": "0",
    "零": "0",
    "一": "1",
    "二": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value).lower().replace("：", ":")


def _decimal_string(raw: str) -> str | None:
    normalized = raw.replace(",", "")
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0:
        return None
    return format(value, "f")


def _currency_and_unit(
    currency_token: str | None,
    unit_token: str | None,
    *,
    metadata: Mapping[str, Any],
    amount_kind: str,
    share_basis: int | None,
) -> tuple[str | None, str | None, str]:
    currency_raw = _compact(currency_token or "")
    unit_raw = _compact(unit_token or "")
    joined = currency_raw + unit_raw
    if any(token in joined for token in ("人民币", "人民幣", "rmb", "cny")):
        currency = "CNY"
        currency_basis = "EXPLICIT"
    elif any(token in joined for token in ("港币", "港幣", "港元", "hkd", "hk$")):
        currency = "HKD"
        currency_basis = "EXPLICIT"
    elif any(token in joined for token in ("美元", "美金", "usd", "us$")):
        currency = "USD"
        currency_basis = "EXPLICIT"
    elif any(token in joined for token in ("新加坡元", "新加坡幣", "s$")):
        currency = "SGD"
        currency_basis = "EXPLICIT"
    elif any(token in joined for token in ("欧元", "歐元")):
        currency = "EUR"
        currency_basis = "EXPLICIT"
    elif any(token in joined for token in ("英镑", "英鎊")):
        currency = "GBP"
        currency_basis = "EXPLICIT"
    elif any(token in joined for token in ("日元", "日圓")):
        currency = "JPY"
        currency_basis = "EXPLICIT"
    elif "$" in joined:
        currency = None
        currency_basis = "AMBIGUOUS_DOLLAR_SYMBOL"
    elif unit_raw in {"元", "分"} and str(metadata.get("market")) == "CN":
        currency = "CNY"
        currency_basis = "CN_REPORT_CONTEXT"
    else:
        currency = None
        currency_basis = "MISSING"

    if amount_kind == "PER_SHARE":
        if unit_raw in {"分", "仙", "美分"}:
            unit = f"{unit_raw}_per_{share_basis or 1}_shares"
        elif unit_raw:
            unit = f"currency_per_{share_basis or 1}_shares"
        else:
            unit = None
    else:
        if unit_raw in {"亿元", "億元"}:
            unit = "hundred_million_currency"
        elif unit_raw in {"百万元", "百萬元", "百万", "百萬"}:
            unit = "million_currency"
        elif unit_raw in {"万元", "萬元"}:
            unit = "ten_thousand_currency"
        elif unit_raw:
            unit = "currency"
        else:
            unit = None
    return currency, unit, currency_basis


def _extract_year(value: str, report_year: int) -> tuple[int | None, str]:
    # Ignore parenthetical prior-year comparatives when the primary phrase is
    # explicit.  The evidence excerpt is preserved for reviewers.
    primary = re.split(r"[（(]", _compact(value), maxsplit=1)[0]
    arabic = [int(item) for item in _ARABIC_YEAR.findall(primary)]
    chinese = [
        int("".join(_CHINESE_DIGITS[character] for character in item))
        for item in _CHINESE_YEAR.findall(primary)
    ]
    years = list(dict.fromkeys(arabic + chinese))
    if len(years) == 1:
        return years[0], "EXPLICIT"
    if len(years) > 1:
        return None, "AMBIGUOUS_MULTIPLE_YEARS"
    compact = _compact(value)
    if any(
        token in compact
        for token in ("本报告期", "本報告期", "本年度", "本财政年度", "本財政年度")
    ):
        return report_year, "REPORT_CONTEXT"
    return None, "MISSING"


def _lifecycle(value: str, page_context: str) -> tuple[str, str]:
    compact = _compact(value)
    paid = any(
        token in compact
        for token in ("已派付", "已支付", "已派发", "已派發", "派发完毕", "派發完畢", "paid")
    )
    approved = any(
        token in compact
        for token in ("股东大会审议通过", "股東大會審議通過", "获批准", "獲批准", "approved")
    )
    proposed = any(
        token in compact
        for token in ("拟派", "擬派", "建议", "建議", "proposed", "recommended")
    )
    conditional_approval = bool(
        re.search(
            r"(?:尚需|尚须|尚須|尚待|拟提交|擬提交|将提交|將提交|"
            r"拟提请|擬提請|将提请|將提請|需|须|須|待)"
            r"[^。；;]{0,100}(?:股东大会|股東大會)"
            r"[^。；;]{0,60}(?:审议通过|審議通過|审议批准|審議批准|批准)",
            compact,
        )
    )
    conditional_after_approval = bool(
        re.search(
            r"(?:预案|預案)[^。；;]{0,120}(?:经|經)"
            r"[^。；;]{0,60}(?:股东大会|股東大會)"
            r"[^。；;]{0,40}(?:审议通过|審議通過|审议批准|審議批准|批准)"
            r"(?:后|後)(?:实施|實施|生效)",
            compact,
        )
    )
    conditional_effective_date = bool(
        re.search(
            r"自[^。；;]{0,80}(?:股东大会|股東大會)"
            r"[^。；;]{0,50}(?:审议通过|審議通過|审议批准|審議批准|批准)"
            r"[^。；;]{0,40}(?:利润分配方案预案|利潤分配方案預案|"
            r"之日|之日起|當日|当日)",
            compact,
        )
    )
    explicit_past_approval = bool(
        re.search(
            r"20\d{2}年\d{1,2}月\d{1,2}日(?:经|經)"
            r"[^。；;]{0,40}(?:股东大会|股東大會)"
            r"[^。；;]{0,30}(?:审议通过|審議通過|审议批准|審議批准|批准)",
            compact,
        )
    )
    conditional_or_unfinished = conditional_approval or conditional_effective_date or (
        conditional_after_approval and not explicit_past_approval
    )
    if conditional_or_unfinished:
        proposed = True
    declared = any(token in compact for token in ("宣派", "宣佈", "宣布", "declared"))
    statuses = [
        name
        for name, present in (
            ("PAID", paid),
            ("APPROVED", approved),
            ("PROPOSED", proposed),
            ("DECLARED", declared),
        )
        if present
    ]
    explicitly_declared_and_paid = any(
        token in compact for token in ("已宣派及派付", "已宣派和派付", "declaredandpaid")
    )
    if paid and declared and not explicitly_declared_and_paid:
        return "AMBIGUOUS", "DECLARED_AND_PAID_COMPONENTS_MIXED"
    if paid:
        return "PAID", "EXPLICIT"
    # An unfinished condition contains the substring "审议通过", but it is not
    # evidence that approval has happened.  Resolve this before the approved
    # branch so future conditions cannot become paid/approved candidates.
    if conditional_or_unfinished:
        return "PROPOSED", "CONDITIONAL_OR_PENDING_APPROVAL"
    if approved and not proposed:
        return "APPROVED", "EXPLICIT"
    if proposed and not approved:
        return "PROPOSED", "EXPLICIT"
    if declared and not proposed:
        return "DECLARED", "EXPLICIT"
    if len(statuses) > 1:
        return "AMBIGUOUS", "MULTIPLE_LIFECYCLE_MARKERS"

    page = _compact(page_context)
    if any(token in page for token in ("本报告期利润分配预案", "本報告期利潤分配預案")):
        return "PROPOSED", "PAGE_CONTEXT"
    return "UNKNOWN", "MISSING"


def _dividend_kind(value: str) -> tuple[str, str]:
    compact = _compact(value)
    if any(term in compact for term in _SPECIAL_TERMS):
        if "普通股息" in compact:
            return "AMBIGUOUS", "ORDINARY_AND_SPECIAL_IN_SAME_EVIDENCE"
        return "SPECIAL", "EXPLICIT"
    if any(term in compact for term in ("普通股息", "普通现金", "普通現金", "中期股息", "末期股息")):
        return "ORDINARY", "EXPLICIT"
    return "ORDINARY", "NO_SPECIAL_MARKER_DEFAULT"


def _component(value: str, amount_kind: str) -> str:
    compact = _compact(value)
    if any(term in compact for term in _SPECIAL_TERMS):
        return "SPECIAL"
    if "中期" in compact or "interim" in compact:
        return "INTERIM"
    if "末期" in compact or "finaldividend" in compact:
        return "FINAL"
    if amount_kind == "TOTAL" and any(
        term in compact for term in ("合计", "合計", "合共", "总计", "總計", "总额", "總額")
    ):
        return "ANNUAL_TOTAL"
    if "季度" in compact or "quarter" in compact:
        return "QUARTERLY"
    return "UNSPECIFIED"


def _relevant_line(value: str) -> bool:
    compact = _compact(value)
    if not any(term in compact for term in _DIVIDEND_TERMS):
        return False
    if any(term in compact for term in _HARD_EXCLUDED_CONTEXT):
        return False
    if any(term in compact for term in _EXCLUDED_CONTEXT) and not any(
        term in compact for term in ("派发", "派發", "派付", "支付", "宣派", "普通股息")
    ):
        return False
    return any(term in compact for term in _ACTION_CONTEXT)


def _evidence_record(
    *,
    match: re.Match[str],
    amount_kind: str,
    forward_text: str,
    page_context: str,
    metadata: Mapping[str, Any],
    page_number: int,
    page_line_start: int,
    page_line_end: int,
    text_line_start: int,
    text_line_end: int,
) -> dict[str, Any]:
    raw_value = match.groupdict().get("value") or ""
    value = _decimal_string(raw_value)
    raw_currency = match.groupdict().get("currency_pre") or match.groupdict().get("currency_post")
    raw_unit = match.groupdict().get("unit")
    basis = int(match.groupdict().get("basis") or 1) if amount_kind == "PER_SHARE" else None
    currency, unit, currency_basis = _currency_and_unit(
        raw_currency,
        raw_unit,
        metadata=metadata,
        amount_kind=amount_kind,
        share_basis=basis,
    )
    fiscal_year, fiscal_year_basis = _extract_year(forward_text, int(metadata["fiscal_year"]))
    lifecycle, lifecycle_basis = _lifecycle(forward_text, page_context)
    dividend_kind, dividend_kind_basis = _dividend_kind(forward_text)
    excerpt = " ".join(line.strip() for line in forward_text.splitlines() if line.strip())[:500]
    semantic_status = "REVIEW"
    reasons: list[str] = ["candidate requires human review before any ledger import"]
    if value is None:
        reasons.append("numeric amount is not a finite non-negative Decimal")
    if currency is None or unit is None:
        reasons.append("currency or unit is not explicit enough")
    if fiscal_year is None:
        reasons.append("candidate fiscal year is not uniquely associated")
    if dividend_kind == "AMBIGUOUS" or lifecycle in {"AMBIGUOUS", "UNKNOWN"}:
        reasons.append("dividend kind or lifecycle is ambiguous")

    identity = "|".join(
        (
            str(metadata["sha256"]),
            str(page_number),
            str(page_line_start),
            amount_kind,
            dividend_kind,
            raw_value,
            raw_currency or "",
            raw_unit or "",
        )
    )
    evidence_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    eligible_after_review = bool(
        dividend_kind == "ORDINARY"
        and lifecycle in {"PAID", "APPROVED"}
        and fiscal_year == int(metadata["fiscal_year"])
        and value is not None
        and currency is not None
        and unit is not None
    )
    return {
        "evidence_id": evidence_id,
        "dividend_kind": dividend_kind,
        "dividend_kind_basis": dividend_kind_basis,
        "lifecycle_status": lifecycle,
        "lifecycle_basis": lifecycle_basis,
        "component": _component(forward_text, amount_kind),
        "amount_kind": amount_kind,
        "value": value if currency is not None and unit is not None else None,
        "raw_value": raw_value,
        "currency": currency,
        "currency_basis": currency_basis,
        "unit": unit,
        "share_basis": basis,
        "associated_fiscal_year": fiscal_year,
        "fiscal_year_basis": fiscal_year_basis,
        "status": semantic_status,
        "reason": "; ".join(reasons),
        "eligible_after_manual_review": eligible_after_review,
        "core_import_allowed": False,
        "source_name": str(metadata["source_name"]),
        "source_document": str(metadata["source_document"]),
        "source_url": str(metadata["source_url"]),
        "source_publish_date": str(metadata["source_publish_date"]),
        "source_fetch_time": str(metadata["source_fetch_time"]),
        "source_local_path": str(metadata["local_path"]),
        "source_sha256": str(metadata["sha256"]),
        "page": page_number,
        "page_line_start": page_line_start,
        "page_line_end": page_line_end,
        "text_line_start": text_line_start,
        "text_line_end": text_line_end,
        "line_excerpt": excerpt,
    }


def resolve_candidate_slot(
    candidates: Sequence[Mapping[str, Any]],
    *,
    fiscal_year: int,
    dividend_kind: str,
    lifecycle_group: str,
    amount_kind: str,
) -> dict[str, Any]:
    """Resolve one candidate slot without summing components or guessing.

    A repeated identical value remains REVIEW.  Different legitimate
    components also remain REVIEW with a null selected value.  Conflicting
    values for the same component become CONFLICT with a null value.
    """

    lifecycle_values = {
        "PAID_OR_APPROVED": {"PAID", "APPROVED"},
        "DECLARED": {"DECLARED"},
        "PROPOSED": {"PROPOSED"},
    }[lifecycle_group]
    matched = [
        dict(item)
        for item in candidates
        if item.get("dividend_kind") == dividend_kind
        and item.get("lifecycle_status") in lifecycle_values
        and item.get("amount_kind") == amount_kind
        and item.get("associated_fiscal_year") == fiscal_year
        and item.get("value") is not None
        and item.get("currency") is not None
        and item.get("unit") is not None
    ]
    if not matched:
        return {
            "status": "MISSING",
            "candidate": None,
            "match_count": 0,
            "evidence_ids": [],
            "reason": "no uniquely scoped candidate; unknown is not zero",
            "core_import_allowed": False,
        }
    signatures: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for item in matched:
        signature = (
            item["value"],
            item["currency"],
            item["unit"],
            item.get("share_basis"),
        )
        signatures.setdefault(signature, []).append(item)
    evidence_ids = sorted({str(item["evidence_id"]) for item in matched})
    if len(signatures) == 1:
        selected = next(iter(signatures.values()))[0]
        candidate = {
            key: selected.get(key)
            for key in (
                "value",
                "currency",
                "unit",
                "share_basis",
                "component",
                "associated_fiscal_year",
            )
        }
        return {
            "status": "REVIEW",
            "candidate": candidate,
            "match_count": len(matched),
            "evidence_ids": evidence_ids,
            "reason": "single value signature retained as a review candidate; no automatic core import",
            "core_import_allowed": False,
        }

    components = {str(item.get("component")) for item in matched}
    same_component = len(components) == 1
    return {
        "status": "CONFLICT" if same_component else "REVIEW",
        "candidate": None,
        "match_count": len(matched),
        "evidence_ids": evidence_ids,
        "reason": (
            "multiple values claim the same component"
            if same_component
            else "multiple dividend components must not be automatically aggregated"
        ),
        "core_import_allowed": False,
    }


def build_dividend_ledger(
    candidates: Sequence[Mapping[str, Any]], fiscal_year: int
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for dividend_kind in ("ORDINARY", "SPECIAL"):
        by_lifecycle: dict[str, Any] = {}
        for lifecycle_group in ("PAID_OR_APPROVED", "DECLARED", "PROPOSED"):
            by_lifecycle[lifecycle_group.lower()] = {
                amount_kind.lower(): resolve_candidate_slot(
                    candidates,
                    fiscal_year=fiscal_year,
                    dividend_kind=dividend_kind,
                    lifecycle_group=lifecycle_group,
                    amount_kind=amount_kind,
                )
                for amount_kind in ("TOTAL", "PER_SHARE")
            }
        result[dividend_kind.lower()] = by_lifecycle
    return result


def extract_dividend_report_candidates(
    text: str, metadata: Mapping[str, Any]
) -> dict[str, Any]:
    required = (
        "company_id",
        "company_name",
        "security_id",
        "share_class",
        "market",
        "fiscal_year",
        "fiscal_year_end_date",
        "source_name",
        "source_document",
        "source_url",
        "source_publish_date",
        "source_fetch_time",
        "local_path",
        "sha256",
    )
    missing = [key for key in required if metadata.get(key) in (None, "")]
    if missing:
        raise DividendCandidateError(f"annual-report metadata is incomplete: {missing}")

    candidates: list[dict[str, Any]] = []
    global_line = 0
    seen: set[tuple[Any, ...]] = set()
    for page_number, page in enumerate(text.split("\f"), start=1):
        lines = page.splitlines()
        page_context = page[:20000]
        for page_line, line in enumerate(lines, start=1):
            global_line += 1
            forward_lines = lines[page_line - 1 : min(len(lines), page_line + 1)]
            forward_text = "\n".join(forward_lines)
            if not _relevant_line(forward_text):
                continue
            compact = _compact(forward_text)
            line_end = page_line + len(forward_lines) - 1
            text_end = global_line + len(forward_lines) - 1
            for amount_kind, patterns in (
                ("PER_SHARE", _PER_SHARE_PATTERNS),
                ("TOTAL", _TOTAL_PATTERNS),
            ):
                for pattern in patterns:
                    for match in pattern.finditer(compact):
                        if amount_kind == "TOTAL":
                            prefix = compact[max(0, match.start() - 12) : match.start()]
                            matched_text = match.group(0)
                            if "每股" in prefix + matched_text or re.search(
                                r"每\d+股", prefix + matched_text
                            ):
                                continue
                        record = _evidence_record(
                            match=match,
                            amount_kind=amount_kind,
                            forward_text=forward_text,
                            page_context=page_context,
                            metadata=metadata,
                            page_number=page_number,
                            page_line_start=page_line,
                            page_line_end=line_end,
                            text_line_start=global_line,
                            text_line_end=text_end,
                        )
                        dedupe = (
                            page_number,
                            record["amount_kind"],
                            record["dividend_kind"],
                            record["lifecycle_status"],
                            record["raw_value"],
                            record["currency"],
                            record["unit"],
                            record["component"],
                        )
                        if dedupe in seen:
                            continue
                        seen.add(dedupe)
                        candidates.append(record)
        global_line += 1

    fiscal_year = int(metadata["fiscal_year"])
    candidates.sort(key=lambda item: (int(item["page"]), int(item["page_line_start"]), item["evidence_id"]))
    exact_year = [item for item in candidates if item.get("associated_fiscal_year") == fiscal_year]
    return {
        "schema_version": SCHEMA_VERSION,
        "company_id": str(metadata["company_id"]),
        "company_name": str(metadata["company_name"]),
        "security_id": str(metadata["security_id"]),
        "share_class": str(metadata["share_class"]),
        "market": str(metadata["market"]),
        "fiscal_year": fiscal_year,
        "fiscal_year_end_date": str(metadata["fiscal_year_end_date"]),
        "source_name": str(metadata["source_name"]),
        "source_document": str(metadata["source_document"]),
        "source_url": str(metadata["source_url"]),
        "source_publish_date": str(metadata["source_publish_date"]),
        "source_fetch_time": str(metadata["source_fetch_time"]),
        "source_local_path": str(metadata["local_path"]),
        "source_sha256": str(metadata["sha256"]),
        "candidate_only": True,
        "writes_production": False,
        "candidate_count": len(candidates),
        "exact_fiscal_year_candidate_count": len(exact_year),
        "candidates": candidates,
        "ledger": build_dividend_ledger(candidates, fiscal_year),
        "safety": {
            "ordinary_proposed_core_import_allowed": False,
            "special_dividend_core_import_allowed": False,
            "automatic_aggregation_allowed": False,
            "unknown_is_zero": False,
        },
    }


def pdftotext_layout(pdf_path: Path, *, timeout_seconds: int = 120) -> str:
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
        raise DividendCandidateError(
            f"pdftotext failed ({completed.returncode}): {completed.stderr[-500:]}"
        )
    return completed.stdout


def verify_pdf_sha256(pdf_path: Path, expected: str) -> None:
    hasher = hashlib.sha256()
    with pdf_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    if hasher.hexdigest() != expected:
        raise DividendCandidateError(f"annual-report SHA-256 mismatch: {pdf_path}")
