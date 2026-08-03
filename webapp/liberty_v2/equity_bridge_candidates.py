from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "equity-bridge-candidates-v1.0"


class EquityBridgeCandidateError(RuntimeError):
    pass


_NUMBER_RE = re.compile(
    r"(?<![\d.])(?P<sign>[-\u2212]?)(?P<number>\d[\d,]*(?:\.\d+)?)(?P<percent>\s*[%\uff05]?)(?![\d.])"
)
_ROW_RE = re.compile(r"^(?:[\u4e00-\u9fff]*[\u3001.\uff0e]?\s*)?\u80a1\u4efd\u603b\u6570")
_ACTION = r"(?:\u5b9e\u65bd|\u5b8c\u6210|\u8fdb\u884c|\u53d1\u751f|\u672c\u6b21|\u62a5\u544a\u671f\u5185)"
_SPLIT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "STOCK_SPLIT",
        re.compile(
            _ACTION
            + r".{0,30}(?:\u62c6\u80a1|\u80a1\u4efd\u62c6\u7ec6|\u80a1\u4efd\u62c6\u5206|\u80a1\u7968\u62c6\u7ec6|\u80a1\u7968\u62c6\u5206)"
        ),
    ),
    (
        "REVERSE_SPLIT",
        re.compile(
            _ACTION
            + r".{0,30}(?:\u5e76\u80a1(?!\u4e1c)|\u7f29\u80a1|\u80a1\u4efd\u5408\u5e76|\u80a1\u7968\u5408\u5e76)"
        ),
    ),
)
_HK_SIGNED_NUMBER_RE = re.compile(
    r"(?<![\d.])(?P<open>[（(])?\s*(?P<sign>[-−]?)"
    r"(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?P<close>[）)])?(?![\d.])"
)
_CHINESE_DIGITS = str.maketrans("〇零一二三四五六七八九", "00123456789")
_HK_ISSUED_LABELS = ("已發行及繳足", "已发行及缴足")
_HK_AUTHORISED_LABELS = ("法定股本", "法定：", "法定:")
_HK_CANCEL_LABELS = (
    "回購股份",
    "回购股份",
    "購回和註銷股份",
    "购回和注销股份",
    "購回及註銷股份",
    "回購及註銷股份",
    "股份回購及註銷",
)
_HK_EXPLICIT_CANCEL_PHRASES = (
    "回購及註銷",
    "回购及注销",
    "購回和註銷",
    "购回和注销",
    "購回及註銷",
)


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("\uff1a", ":")


def _positive_integer_tokens(line: str) -> list[dict[str, Any]]:
    tokens: list[dict[str, Any]] = []
    for match in _NUMBER_RE.finditer(line):
        if match.group("sign"):
            continue
        raw = match.group("number")
        if match.group("percent").strip():
            continue
        normalized = raw.replace(",", "")
        try:
            numeric = int(normalized)
        except ValueError:
            # A share count may be rendered as 123.00, but a non-integral
            # decimal is a ratio/percentage and must never be a share count.
            try:
                decimal_part = normalized.split(".", 1)[1]
                if not decimal_part or set(decimal_part) != {"0"}:
                    continue
                numeric = int(normalized.split(".", 1)[0])
            except (IndexError, ValueError):
                continue
        if numeric <= 0:
            continue
        tokens.append({"value": numeric, "raw": raw, "start": match.start()})
    # On the total row, 100/100.00 is the opening/closing percentage.  Remove
    # it only when there are enough cells to prove this is a multi-column row.
    if len(tokens) >= 3:
        tokens = [item for item in tokens if item["value"] != 100]
    return tokens


def _split_markers(text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    global_line = 0
    for page_number, page in enumerate(text.split("\f"), start=1):
        compact_page = _compact(page)
        relevant_page = (
            "\u80a1\u4efd\u53d8\u52a8\u60c5\u51b5" in compact_page
            or "\u80a1\u672c\u53d8\u52a8\u60c5\u51b5" in compact_page
        )
        for page_line, line in enumerate(page.splitlines(), start=1):
            global_line += 1
            if not relevant_page:
                continue
            for code, pattern in _SPLIT_PATTERNS:
                # Preserve layout whitespace.  Removing it can concatenate
                # unrelated table columns (for example “综合  股” -> “合股”)
                # and create a false reverse-split marker.
                if pattern.search(line.replace("\uff1a", ":")):
                    found.append(
                        {
                            "code": code,
                            "page": page_number,
                            "page_line": page_line,
                            "text_line": global_line,
                            "line_excerpt": line.strip()[:300],
                        }
                    )
        global_line += 1
    return found


def _years_in_text(value: str) -> set[int]:
    compact = _compact(value)
    # Keep layout whitespace for Arabic years so adjacent table cells such as
    # ``2,203   4,292`` cannot be concatenated into a false year ``2034``.
    years = {int(item) for item in re.findall(r"(?<!\d)(20\d{2})(?!\d)", value)}
    for item in re.findall(r"[二〇零一二三四五六七八九]{4}", compact):
        translated = item.translate(_CHINESE_DIGITS)
        if translated.isdigit() and translated.startswith("20"):
            years.add(int(translated))
    return years


def _normalize_year_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return match.group(0).translate(_CHINESE_DIGITS)

    return re.sub(r"[二〇零一二三四五六七八九]{4}", replace, _compact(value))


def _hk_signed_integer_tokens(value: str) -> list[dict[str, Any]]:
    tokens: list[dict[str, Any]] = []
    for match in _HK_SIGNED_NUMBER_RE.finditer(value):
        raw = match.group("number").replace(",", "")
        try:
            if "." in raw:
                whole, fraction = raw.split(".", 1)
                if not fraction or set(fraction) != {"0"}:
                    continue
                numeric = int(whole)
            else:
                numeric = int(raw)
        except ValueError:
            continue
        negative = bool(match.group("sign")) or bool(match.group("open") and match.group("close"))
        tokens.append(
            {
                "value": -numeric if negative else numeric,
                "raw": match.group(0).strip(),
                "start": match.start(),
            }
        )
    return tokens


def _hk_page_multiplier(page: str) -> tuple[int, str]:
    compact = _compact(page)
    labels = ("股份數目", "股份数量", "股份數量")
    lines = page.splitlines()
    for index, line in enumerate(lines):
        label = next((item for item in labels if item in line), None)
        if label is None:
            continue
        position = line.index(label)
        for unit_line in lines[index : min(len(lines), index + 4)]:
            segment = unit_line[max(0, position - 5) : position + len(label) + 8]
            if "百萬股" in segment or "百万股" in segment:
                return 1_000_000, "million_shares"
            if "千股" in segment or re.search(r"(?<![\u4e00-\u9fff])千(?![\u4e00-\u9fff])", segment):
                return 1_000, "thousand_shares"
    return 1, "shares"


def _hk_capital_page(page: str) -> bool:
    compact = _compact(page)
    has_issued = any(label in compact for label in _HK_ISSUED_LABELS)
    if not has_issued:
        return False
    heading = any(
        re.match(r"^\s*\d+(?:\.\d+)?[.、]?\s*(?:發\s*行\s*)?股\s*本", line)
        for line in page.splitlines()
    )
    return heading or "本公司已發行股本變動如下" in compact or "本公司已发行股本变动如下" in compact


def _hk_under_issued(lines: Sequence[str], index: int) -> bool:
    for prior in range(index, max(-1, index - 45), -1):
        compact = _compact(lines[prior])
        if any(label in compact for label in _HK_ISSUED_LABELS):
            return True
        if any(label in compact for label in _HK_AUTHORISED_LABELS):
            return False
    return False


def _hk_targets_current_year(value: str, fiscal_year: int) -> bool:
    mentioned = _years_in_text(value)
    return not mentioned or fiscal_year in mentioned


def _hk_share_value(value: str, multiplier: int, fiscal_year: int) -> int | None:
    without_dates = re.sub(
        r"20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|"
        r"\d{1,2}\s+(?:January|December)\s+20\d{2}",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    candidates = [
        item["value"]
        for item in _hk_signed_integer_tokens(without_dates)
        if item["value"] > 0 and item["value"] not in {fiscal_year, fiscal_year - 1}
    ]
    if not candidates:
        return None
    # In HK share-capital tables the share-count column precedes nominal
    # capital.  The first sufficiently large integer is therefore retained;
    # small unitless figures may be rounded millions and are rejected.
    minimum = 1_000 if multiplier > 1 else 100_000
    reported = next((item for item in candidates if item >= minimum), None)
    return reported * multiplier if reported is not None else None


def _hk_evidence(
    *,
    value: int,
    page: int,
    page_line: int,
    text_line: int,
    excerpt: str,
    multiplier: int,
    unit_label: str,
) -> dict[str, Any]:
    return {
        "value": str(value),
        "page": page,
        "page_line": page_line,
        "text_line": text_line,
        "line_excerpt": excerpt[:500],
        "reported_unit_label": unit_label,
        "reported_unit_multiplier": str(multiplier),
    }


def _extract_hk_equity_bridge_candidate(
    text: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    fiscal_year = int(metadata["fiscal_year"])
    markers = _split_markers(text)
    opening_rows: list[dict[str, Any]] = []
    closing_rows: list[dict[str, Any]] = []
    cancel_rows: list[dict[str, Any]] = []
    global_line = 0

    for page_number, page in enumerate(text.split("\f"), start=1):
        lines = page.splitlines()
        relevant = _hk_capital_page(page)
        multiplier, unit_label = _hk_page_multiplier(page)
        explicit_cancel = any(label in _compact(page) for label in _HK_EXPLICIT_CANCEL_PHRASES)
        for page_line, line in enumerate(lines, start=1):
            global_line += 1
            if not relevant:
                continue
            index = page_line - 1
            if not _hk_under_issued(lines, index):
                continue
            window_lines = lines[index : min(len(lines), index + 2)]
            window = " ".join(item.strip() for item in window_lines)
            compact_window = _compact(window)
            compact_line = _compact(line)
            if not _hk_targets_current_year(window, fiscal_year):
                continue
            value = _hk_share_value(window, multiplier, fiscal_year)
            starts = (
                "於年初" in compact_line
                or "于年初" in compact_line
                or "一月一日" in compact_line
                or "1月1日" in compact_line
            )
            ends_on_line = (
                "於年末" in compact_line
                or "于年末" in compact_line
                or "於結算日" in compact_line
                or "于结算日" in compact_line
                or "十二月三十一日" in compact_line
                or "12月31日" in compact_line
            )
            normalized_window = _normalize_year_text(window)
            mentioned_years = _years_in_text(window)
            current_december = (
                f"{fiscal_year}年十二月三十一日" in normalized_window
                or f"{fiscal_year}年12月31日" in normalized_window
            )
            ends_current = ends_on_line and (not mentioned_years or current_december)
            spans_year = starts and current_december and (
                "至" in compact_window or "及" in compact_window
            )
            if value is not None and starts:
                opening_rows.append(
                    _hk_evidence(
                        value=value,
                        page=page_number,
                        page_line=page_line,
                        text_line=global_line,
                        excerpt=window,
                        multiplier=multiplier,
                        unit_label=unit_label,
                    )
                )
            if value is not None and (ends_current or spans_year):
                closing_rows.append(
                    _hk_evidence(
                        value=value,
                        page=page_number,
                        page_line=page_line,
                        text_line=global_line,
                        excerpt=window,
                        multiplier=multiplier,
                        unit_label=unit_label,
                    )
                )

            if explicit_cancel and any(label in compact_line for label in _HK_CANCEL_LABELS):
                negative = [abs(item["value"]) for item in _hk_signed_integer_tokens(line) if item["value"] < 0]
                if not negative:
                    continue
                preceding = " ".join(lines[max(0, index - 5) : index + 1])
                mentioned = _years_in_text(preceding)
                if mentioned and fiscal_year not in mentioned:
                    continue
                cancel_rows.append(
                    _hk_evidence(
                        value=negative[0] * multiplier,
                        page=page_number,
                        page_line=page_line,
                        text_line=global_line,
                        excerpt=line.strip(),
                        multiplier=multiplier,
                        unit_label=unit_label,
                    )
                )
        global_line += 1

    # De-duplicate a row that is intentionally used for both opening/closing,
    # while preserving genuinely different table matches as a conflict.
    def unique(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[Any, ...]] = set()
        result: list[dict[str, Any]] = []
        for row in rows:
            key = (row["value"], row["page"], row["page_line"])
            if key not in seen:
                seen.add(key)
                result.append(dict(row))
        return result

    opening_rows = unique(opening_rows)
    closing_rows = unique(closing_rows)
    cancel_rows = unique(cancel_rows)
    opening = opening_rows[0] if len(opening_rows) == 1 else None
    closing = closing_rows[0] if len(closing_rows) == 1 else None
    row_conflict = len(opening_rows) > 1 or len(closing_rows) > 1
    status = "CONFLICT" if row_conflict else "REVIEW"
    reason = (
        "multiple HK issued-share opening/closing rows matched"
        if row_conflict
        else "unique HK issued-share rows await adjacent-report reconciliation"
        if opening and closing
        else "HK issued-share table is missing, rounded, multi-class or not uniquely scoped; unknown is not zero"
    )
    if markers and status != "CONFLICT":
        reason = "split/consolidation marker requires normalized-share review"

    opening_value = opening["value"] if opening else None
    closing_value = closing["value"] if closing else None
    reported_change = (
        str(int(closing_value) - int(opening_value))
        if opening_value is not None and closing_value is not None
        else None
    )
    if len(cancel_rows) == 1 and not markers:
        cancelled_candidate = cancel_rows[0]
        cancelled_status = "VALID"
        cancelled_reason = "official annual-report table explicitly states shares repurchased and cancelled"
    elif len(cancel_rows) > 1:
        cancelled_candidate = None
        cancelled_status = "CONFLICT"
        cancelled_reason = "multiple current-year cancellation rows matched"
    elif cancel_rows:
        cancelled_candidate = cancel_rows[0]
        cancelled_status = "REVIEW"
        cancelled_reason = "split/consolidation requires cancellation normalization review"
    else:
        cancelled_candidate = None
        cancelled_status = "MISSING"
        cancelled_reason = "no unique explicit actual-cancellation row found"

    selected_evidence = closing or opening
    return {
        "schema_version": SCHEMA_VERSION,
        "company_id": str(metadata["company_id"]),
        "company_name": str(metadata["company_name"]),
        "security_id": str(metadata["security_id"]),
        "share_class": str(metadata["share_class"]),
        "market": "HK",
        "currency": str(metadata.get("currency") or "") or None,
        "fiscal_year": fiscal_year,
        "fiscal_year_end_date": str(metadata["fiscal_year_end_date"]),
        "source_document": str(metadata["source_document"]),
        "source_url": str(metadata["source_url"]),
        "source_publish_date": str(metadata["source_publish_date"]),
        "source_fetch_time": str(metadata["source_fetch_time"]),
        "source_local_path": str(metadata["local_path"]),
        "source_sha256": str(metadata["sha256"]),
        "unit": "shares",
        "opening_issued_shares": opening_value,
        "closing_issued_shares": closing_value,
        "opening_evidence": opening,
        "closing_evidence": closing,
        "page": selected_evidence["page"] if selected_evidence else None,
        "page_line": selected_evidence["page_line"] if selected_evidence else None,
        "text_line": selected_evidence["text_line"] if selected_evidence else None,
        "line_excerpt": selected_evidence["line_excerpt"] if selected_evidence else None,
        "reported_unit_label": selected_evidence["reported_unit_label"] if selected_evidence else None,
        "reported_unit_multiplier": selected_evidence["reported_unit_multiplier"] if selected_evidence else None,
        "row_match_count": max(len(opening_rows), len(closing_rows)),
        "status": status,
        "reason": reason,
        "reported_net_issued_share_change": reported_change,
        "reported_net_issued_share_change_status": "CONFLICT" if row_conflict else "REVIEW" if reported_change else "MISSING",
        "corporate_action_markers": markers,
        "opening_reconciliation": "NOT_CHECKED",
        "closing_reconciliation": "NOT_CHECKED",
        "eligible_for_issued_share_candidate": False,
        "eligible_for_diluted_share_core": False,
        "cancelled_shares_candidate": cancelled_candidate["value"] if cancelled_candidate else None,
        "cancelled_shares_candidate_status": cancelled_status,
        "cancelled_shares_candidate_reason": cancelled_reason,
        "cancelled_shares_evidence": cancelled_candidate,
        "eligible_for_cancelled_shares_candidate": cancelled_status == "VALID",
        "cancelled_shares": None,
        "diluted_total_shares": None,
        "diluted_net_share_reduction": None,
        "share_class_scope_status": "REVIEW_REQUIRED",
    }


def extract_equity_bridge_candidate(
    text: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Extract an issued-share bridge candidate from an official report.

    This function never claims diluted shares or cancelled shares.  A unique
    official row remains REVIEW until adjacent annual reports reconcile.
    """

    required = (
        "company_id",
        "company_name",
        "security_id",
        "share_class",
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
        raise EquityBridgeCandidateError(f"annual-report metadata is incomplete: {missing}")
    if str(metadata.get("market") or "").upper() == "HK":
        return _extract_hk_equity_bridge_candidate(text, metadata)

    markers = _split_markers(text)
    rows: list[dict[str, Any]] = []
    global_line = 0
    for page_number, page in enumerate(text.split("\f"), start=1):
        compact_page = _compact(page)
        has_table_context = (
            "\u80a1\u4efd\u53d8\u52a8\u60c5\u51b5" in compact_page
            and "\u672c\u6b21\u53d8\u52a8\u524d" in compact_page
            and "\u672c\u6b21\u53d8\u52a8\u540e" in compact_page
            and ("\u5355\u4f4d:\u80a1" in compact_page or "\u5355\u4f4d\u4e3a\u80a1" in compact_page)
        )
        for page_line, line in enumerate(page.splitlines(), start=1):
            global_line += 1
            if not has_table_context:
                continue
            stripped = line.strip()
            if not _ROW_RE.match(_compact(stripped)):
                continue
            tokens = _positive_integer_tokens(stripped)
            if len(tokens) < 2:
                rows.append(
                    {
                        "opening_issued_shares": None,
                        "closing_issued_shares": None,
                        "page": page_number,
                        "page_line": page_line,
                        "text_line": global_line,
                        "line_excerpt": stripped[:500],
                        "parse_status": "REVIEW",
                        "parse_reason": "share-total row does not contain two unambiguous positive integers",
                    }
                )
                continue
            rows.append(
                {
                    "opening_issued_shares": str(tokens[0]["value"]),
                    "closing_issued_shares": str(tokens[-1]["value"]),
                    "page": page_number,
                    "page_line": page_line,
                    "text_line": global_line,
                    "line_excerpt": stripped[:500],
                    "parse_status": "REVIEW",
                    "parse_reason": "unique table row awaits adjacent-report reconciliation",
                }
            )
        global_line += 1

    numeric_rows = [
        row
        for row in rows
        if row["opening_issued_shares"] is not None and row["closing_issued_shares"] is not None
    ]
    if len(numeric_rows) == 1 and len(rows) == 1:
        selected = dict(numeric_rows[0])
        status = "REVIEW"
        reason = selected["parse_reason"]
    elif not rows:
        selected = {
            "opening_issued_shares": None,
            "closing_issued_shares": None,
            "page": None,
            "page_line": None,
            "text_line": None,
            "line_excerpt": None,
        }
        status = "REVIEW"
        reason = "no uniquely scoped share-total table row found; unknown is not zero"
    elif len(rows) == 1:
        selected = dict(rows[0])
        status = "REVIEW"
        reason = rows[0]["parse_reason"]
    else:
        selected = {
            "opening_issued_shares": None,
            "closing_issued_shares": None,
            "page": None,
            "page_line": None,
            "text_line": None,
            "line_excerpt": None,
        }
        status = "CONFLICT"
        reason = "multiple or ambiguous share-total rows found"

    if markers and status != "CONFLICT":
        status = "REVIEW"
        reason = "split/consolidation marker requires normalized-share review"

    opening = selected.get("opening_issued_shares")
    closing = selected.get("closing_issued_shares")
    reported_change = (
        str(int(str(closing)) - int(str(opening)))
        if opening is not None and closing is not None
        else None
    )
    change_status = "CONFLICT" if status == "CONFLICT" else "REVIEW" if reported_change is not None else "MISSING"

    return {
        "schema_version": SCHEMA_VERSION,
        "company_id": str(metadata["company_id"]),
        "company_name": str(metadata["company_name"]),
        "security_id": str(metadata["security_id"]),
        "share_class": str(metadata["share_class"]),
        "market": str(metadata.get("market") or "CN"),
        "currency": str(metadata.get("currency") or "") or None,
        "fiscal_year": int(metadata["fiscal_year"]),
        "fiscal_year_end_date": str(metadata["fiscal_year_end_date"]),
        "source_document": str(metadata["source_document"]),
        "source_url": str(metadata["source_url"]),
        "source_publish_date": str(metadata["source_publish_date"]),
        "source_fetch_time": str(metadata["source_fetch_time"]),
        "source_local_path": str(metadata["local_path"]),
        "source_sha256": str(metadata["sha256"]),
        "unit": "shares",
        **selected,
        "row_match_count": len(rows),
        "status": status,
        "reason": reason,
        "reported_net_issued_share_change": reported_change,
        "reported_net_issued_share_change_status": change_status,
        "corporate_action_markers": markers,
        "opening_reconciliation": "NOT_CHECKED",
        "closing_reconciliation": "NOT_CHECKED",
        "eligible_for_issued_share_candidate": False,
        "eligible_for_diluted_share_core": False,
        "cancelled_shares_candidate": None,
        "cancelled_shares_candidate_status": "MISSING",
        "cancelled_shares_candidate_reason": "A-share cancellation is not inferred from the issued-share bridge",
        "cancelled_shares_evidence": None,
        "eligible_for_cancelled_shares_candidate": False,
        "cancelled_shares": None,
        "diluted_total_shares": None,
        "diluted_net_share_reduction": None,
    }


def reconcile_company_reports(reports: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Reconcile adjacent fiscal years without inferring dilution/cancellation."""

    rows = [dict(item) for item in sorted(reports, key=lambda item: int(item["fiscal_year"]))]
    for index in range(len(rows) - 1):
        prior = rows[index]
        current = rows[index + 1]
        if int(current["fiscal_year"]) != int(prior["fiscal_year"]) + 1:
            prior["closing_reconciliation"] = "YEAR_GAP"
            current["opening_reconciliation"] = "YEAR_GAP"
            continue
        prior_close = prior.get("closing_issued_shares")
        current_open = current.get("opening_issued_shares")
        if prior_close is None or current_open is None:
            continue
        has_marker = bool(prior.get("corporate_action_markers") or current.get("corporate_action_markers"))
        if str(prior_close) == str(current_open):
            result = "MATCH_REVIEW_SPLIT" if has_marker else "MATCH"
            prior["closing_reconciliation"] = result
            current["opening_reconciliation"] = result
        else:
            result = "MISMATCH_REVIEW_SPLIT" if has_marker else "MISMATCH"
            prior["closing_reconciliation"] = result
            current["opening_reconciliation"] = result

    for row in rows:
        if row.get("status") == "CONFLICT":
            continue
        edges = {row.get("opening_reconciliation"), row.get("closing_reconciliation")}
        if "MISMATCH" in edges:
            row["status"] = "CONFLICT"
            row["reason"] = "adjacent annual reports disagree on closing/opening issued shares"
            row["reported_net_issued_share_change_status"] = "CONFLICT"
        elif row.get("corporate_action_markers"):
            row["status"] = "REVIEW"
            row["reason"] = "split/consolidation marker prevents automatic normalization"
            row["reported_net_issued_share_change_status"] = (
                "REVIEW" if row.get("reported_net_issued_share_change") is not None else "MISSING"
            )
        elif "MATCH" in edges:
            row["status"] = "VALID"
            row["reason"] = "unique official row reconciles with an adjacent annual report"
            row["eligible_for_issued_share_candidate"] = True
            row["reported_net_issued_share_change_status"] = "VALID"
        # Even VALID here means only a reviewed issued-share candidate.
        row["eligible_for_diluted_share_core"] = False
    return rows


def pdftotext_layout(pdf_path: Path, *, timeout_seconds: int = 120) -> str:
    """Run Poppler through an argv array and keep extracted text in memory only."""

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
        raise EquityBridgeCandidateError(
            f"pdftotext failed ({completed.returncode}): {completed.stderr[-500:]}"
        )
    return completed.stdout


def verify_pdf_sha256(pdf_path: Path, expected: str) -> None:
    hasher = hashlib.sha256()
    with pdf_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest != expected:
        raise EquityBridgeCandidateError(f"annual-report SHA-256 mismatch: {pdf_path}")
