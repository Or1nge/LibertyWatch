from __future__ import annotations

import hashlib
import json
import re
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from liberty_v2.official_cashflow_candidates import FIELD_NAMES


SCHEMA_VERSION = "cashflow-reviewed-decision-ledger-v1.0"
OFFICIAL_HOSTS = {"static.cninfo.com.cn", "www1.hkexnews.hk", "www.hkexnews.hk"}
MIN_FULL_REPORT_PAGES = 50


class CashflowReconciliationError(RuntimeError):
    pass


_NON_FULL_REPORT_TITLE = re.compile(
    r"(?:更正公告|更正通知|補充公告|补充公告|摘要|通函|中期|半年度|季度|季報|季报)"
)
_ANNUAL_REPORT = re.compile(r"年度报告|年度報告|年报|年報|annual\s+report", re.I)
_AUDITOR_REPORT = re.compile(
    r"审计报告|審計報告|独立审计师报告|獨立審計師報告|独立核数师报告|獨立核數師報告|"
    r"independent\s+auditor(?:'s|s’|s')?\s+report",
    re.I,
)
_CONSOLIDATED_CASHFLOW = re.compile(
    r"合并现金流量表|合併現金流量表|综合现金流量表|綜合現金流量表|"
    r"consolidated\s+(?:statement\s+of\s+cash\s+flows|cash\s+flow\s+statement)",
    re.I,
)

# Issuer names in the formal list are simplified Chinese while HK annual
# reports normally use traditional Chinese.  This deliberately small fold is
# only used to confirm document identity; it never changes financial data.
_TRADITIONAL_FOLD = str.maketrans(
    {
        "華": "华",
        "潤": "润",
        "啤": "啤",
        "電": "电",
        "機": "机",
        "師": "师",
        "統": "统",
        "一": "一",
        "企": "企",
        "業": "业",
        "團": "团",
        "國": "国",
        "創": "创",
        "實": "实",
        "體": "体",
        "騰": "腾",
        "訊": "讯",
        "聯": "联",
        "想": "想",
        "藥": "药",
        "產": "产",
        "麗": "丽",
        "萬": "万",
        "達": "达",
        "氣": "气",
        "龍": "龙",
        "長": "长",
        "遠": "远",
        "東": "东",
        "廣": "广",
        "門": "门",
        "來": "来",
        "亞": "亚",
        "順": "顺",
        "豐": "丰",
        "臺": "台",
        "灣": "湾",
        "資": "资",
        "控": "控",
        "股": "股",
    }
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_payload_sha256(payload: Mapping[str, Any]) -> str:
    copied = dict(payload)
    copied.pop("sha256", None)
    encoded = json.dumps(
        copied, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def exact_decimal_equal(left: Any, right: Any) -> bool:
    try:
        first = Decimal(str(left))
        second = Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return first.is_finite() and second.is_finite() and first == second


def verify_futu_payload(payload: Mapping[str, Any], path: Path) -> dict[str, Any]:
    declared = str(payload.get("sha256") or "")
    actual_payload = canonical_payload_sha256(payload)
    company = payload.get("company") if isinstance(payload.get("company"), Mapping) else {}
    checks = {
        "schema_version": payload.get("schema_version") == "futu-financial-evidence-v1",
        "payload_sha256": bool(declared) and declared == actual_payload,
        "company_identity_present": bool(company.get("issuer_id")) and bool(company.get("security_id")),
        "cashflow_statement_present": isinstance(
            (payload.get("statements") or {}).get("cash_flow"), Mapping
        ),
    }
    return {
        "checks": checks,
        "all_passed": all(checks.values()),
        "source_local_path": str(path.resolve()),
        "source_file_sha256": sha256_file(path),
        "declared_payload_sha256": declared or None,
        "actual_payload_sha256": actual_payload,
        "issuer_id": company.get("issuer_id"),
        "security_id": company.get("security_id"),
    }


def pdfinfo_pages(path: Path, *, timeout_seconds: int = 60) -> int:
    completed = subprocess.run(
        ["pdfinfo", str(path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise CashflowReconciliationError(
            f"pdfinfo failed ({completed.returncode}): {completed.stderr[-500:]}"
        )
    match = re.search(r"^Pages:\s*(\d+)\s*$", completed.stdout, re.M)
    if not match:
        raise CashflowReconciliationError(f"pdfinfo did not return Pages: {path}")
    return int(match.group(1))


def _fold(value: Any) -> str:
    return re.sub(
        r"[^a-z0-9\u4e00-\u9fff]",
        "",
        str(value or "").translate(_TRADITIONAL_FOLD).lower(),
    )


def _issuer_name_present(text: str, company_name: str) -> bool:
    folded_text = _fold(text)
    folded_name = _fold(company_name)
    aliases = {folded_name}
    for suffix in ("股份有限公司", "控股有限公司", "有限公司", "股份", "控股", "集团"):
        if folded_name.endswith(suffix) and len(folded_name) > len(suffix) + 1:
            aliases.add(folded_name[: -len(suffix)])
    return any(len(alias) >= 3 and alias in folded_text for alias in aliases)


def _labelled_ticker_present(text: str, ticker: str) -> bool:
    number = str(ticker or "").lstrip("0") or "0"
    # Keep punctuation because a code label and value may be separated by a
    # colon, semicolon or bilingual wording.  A bounded region prevents an
    # unrelated financial amount elsewhere in the report from qualifying.
    label = (
        r"(?:公司代码|股票代码|证券代码|股份代号|股份代號|股份编号|股份編號|"
        r"stock\s*codes?)"
    )
    return bool(re.search(label + rf"[^\n]{{0,100}}?0*{re.escape(number)}(?!\d)", text, re.I))


def _year_tokens(year: int) -> tuple[str, str]:
    digits = "零一二三四五六七八九"
    return str(year), "".join(digits[int(character)] for character in str(year))


def statement_year_present(text: str, pages: Sequence[int], fiscal_year: int) -> bool:
    split_pages = text.split("\f")
    selected: list[str] = []
    for page in pages:
        for page_number in range(max(1, int(page) - 1), min(len(split_pages), int(page) + 1) + 1):
            selected.append(split_pages[page_number - 1])
    context = "\n".join(selected)
    return any(token in context for token in _year_tokens(fiscal_year))


def evidence_rows(field_name: str, field: Mapping[str, Any]) -> list[dict[str, Any]]:
    if field_name == "operating_cash_flow":
        return [dict(row) for row in field.get("evidence_rows", []) if isinstance(row, Mapping)]
    rows: list[dict[str, Any]] = []
    for component in field.get("components", []):
        if not isinstance(component, Mapping):
            continue
        rows.extend(
            dict(row)
            for row in component.get("evidence_rows", [])
            if isinstance(row, Mapping)
        )
    return rows


def official_document_checks(
    text: str,
    metadata: Mapping[str, Any],
    pdf_path: Path,
    *,
    actual_pages: int,
    actual_sha256: str,
) -> dict[str, Any]:
    parsed_url = urlparse(str(metadata.get("source_url") or ""))
    title = str(metadata.get("source_document") or "")
    declared_pages = int(metadata.get("pdf_pages") or 0)
    declared_size = int(metadata.get("size_bytes") or 0)
    ticker = str(metadata.get("ticker") or metadata.get("security_id") or "")
    if ticker.upper().startswith(("SH", "SZ", "HK")):
        ticker = ticker[2:]
    identity_by_name = _issuer_name_present(text, str(metadata.get("company_name") or ""))
    identity_by_ticker = _labelled_ticker_present(text, ticker)
    year_tokens = _year_tokens(int(metadata.get("fiscal_year") or 0))
    checks = {
        "selected_current": metadata.get("selection_status") == "SELECTED_CURRENT",
        "manifest_data_verified": metadata.get("data_status") == "VERIFIED",
        "official_source_level": metadata.get("source_level")
        in {"OFFICIAL_DISCLOSURE_PLATFORM", "OFFICIAL_EXCHANGE"},
        "official_https_url": parsed_url.scheme == "https" and parsed_url.hostname in OFFICIAL_HOSTS,
        "annual_report_title": bool(_ANNUAL_REPORT.search(title)),
        "not_correction_summary_or_circular": not bool(_NON_FULL_REPORT_TITLE.search(title)),
        "full_report_page_count": actual_pages >= MIN_FULL_REPORT_PAGES,
        "manifest_page_count_exact": declared_pages == actual_pages,
        "manifest_file_size_exact": declared_size == pdf_path.stat().st_size,
        "manifest_sha256_exact": str(metadata.get("sha256") or "") == actual_sha256,
        "pdf_issuer_identity": identity_by_name or identity_by_ticker,
        "pdf_annual_report_marker": bool(_ANNUAL_REPORT.search(text)),
        "pdf_fiscal_year_marker": any(token in text for token in year_tokens),
        "pdf_independent_auditor_report": bool(_AUDITOR_REPORT.search(text)),
        "pdf_consolidated_cashflow_statement": bool(_CONSOLIDATED_CASHFLOW.search(text)),
    }
    return {
        "checks": checks,
        "all_passed": all(checks.values()),
        "identity_evidence": {
            "company_name_match": identity_by_name,
            "labelled_ticker_match": identity_by_ticker,
        },
        "actual_pdf_pages": actual_pages,
        "actual_size_bytes": pdf_path.stat().st_size,
        "actual_sha256": actual_sha256,
    }


def metadata_semantic_anomaly(metadata: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    title = str(metadata.get("source_document") or "")
    if not _ANNUAL_REPORT.search(title):
        reasons.append("TITLE_IS_NOT_ANNUAL_REPORT")
    if _NON_FULL_REPORT_TITLE.search(title):
        reasons.append("TITLE_IS_CORRECTION_SUMMARY_OR_CIRCULAR")
    if int(metadata.get("pdf_pages") or 0) < MIN_FULL_REPORT_PAGES:
        reasons.append("PDF_TOO_SHORT_FOR_FULL_ANNUAL_REPORT")
    return reasons


def metadata_matches_candidate(
    candidate_report: Mapping[str, Any], metadata: Mapping[str, Any]
) -> dict[str, bool]:
    return {
        "company_id": str(candidate_report.get("company_id")) == str(metadata.get("company_id")),
        "security_id": str(candidate_report.get("security_id")) == str(metadata.get("security_id")),
        "fiscal_year": int(candidate_report.get("fiscal_year") or 0) == int(metadata.get("fiscal_year") or 0),
        "fiscal_year_end_date": str(candidate_report.get("fiscal_year_end_date"))
        == str(metadata.get("fiscal_year_end_date")),
        "source_document": str(candidate_report.get("source_document"))
        == str(metadata.get("source_document")),
        "source_url": str(candidate_report.get("source_url")) == str(metadata.get("source_url")),
        "source_sha256": str(candidate_report.get("source_sha256")) == str(metadata.get("sha256")),
        "source_local_path": Path(str(candidate_report.get("source_local_path"))).resolve()
        == Path(str(metadata.get("resolved_local_path"))).resolve(),
    }


def compare_reextracted_field(
    original: Mapping[str, Any], reviewed: Mapping[str, Any]
) -> dict[str, bool]:
    return {
        "reviewed_status_valid": reviewed.get("status") == "VALID",
        "candidate_value_exact": exact_decimal_equal(original.get("value"), reviewed.get("value")),
        "current_value_exact": exact_decimal_equal(
            original.get("current_value"), reviewed.get("current_value")
        ),
        "comparative_value_exact": exact_decimal_equal(
            original.get("comparative_value"), reviewed.get("comparative_value")
        ),
        "currency_exact": str(original.get("currency")) == str(reviewed.get("currency")),
        "futu_match_repeated": reviewed.get("futu_reconciliation") == "MATCH",
        "adjacent_report_match_repeated": reviewed.get("comparative_report_reconciliation") == "MATCH",
    }


def decision_from_checks(
    checks: Mapping[str, bool], *, hard_reject_keys: Sequence[str] = ()
) -> tuple[str, list[str]]:
    failed = sorted(key for key, passed in checks.items() if not passed)
    if not failed:
        return "ACCEPT", ["ALL_EXACT_CHECKS_PASSED"]
    if any(key in set(hard_reject_keys) for key in failed):
        return "REJECT", failed
    return "REVIEW", failed
