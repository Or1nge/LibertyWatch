#!/usr/bin/env python3
"""Backfill official annual reports for Liberty's formal 67-company scope.

The downloader is intentionally a source-ledger builder, not a financial
calculator.  It preserves the official query response, downloads immutable
PDF evidence, validates each file, and writes an atomic per-company manifest.
Missing reports remain explicit statuses and are never converted to zero.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import fcntl
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests


ROOT = Path(__file__).resolve().parents[2]
COMPANIES_PATH = ROOT / "data" / "source" / "companies.json"
CALENDARS_PATH = (
    ROOT / "data" / "source" / "annual_report_fiscal_calendars_v1.json"
)
LEGACY_ROOT = ROOT / "data" / "raw" / "annual_reports"
DEFAULT_OUTPUT = LEGACY_ROOT / "official_backfill_v1"

SCHEMA_VERSION = "annual-report-source-ledger-v1"
CNINFO_STOCKS_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_BASE = "https://static.cninfo.com.cn/"
HKEX_STOCKS_URL = (
    "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_c.json"
)
HKEX_QUERY_URL = "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh"

HTTP_HEADERS = {
    "User-Agent": "LibertyAnnualReportBackfill/1.0 Mozilla/5.0",
}
CNINFO_HEADERS = {
    **HTTP_HEADERS,
    "Referer": "https://www.cninfo.com.cn/",
}
HKEX_HEADERS = {
    **HTTP_HEADERS,
    "Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh",
}

ANNUAL_MARKERS = re.compile(
    r"年度报告|年度報告|年报|年報|annual\s+report", re.I
)
EXCLUDED_TITLE_MARKERS = re.compile(
    r"摘要|英文版|英文版本|通知信函|发布通知|發佈通知|回条|回條|申请表格|"
    r"變更申請|变更申请|補充公告|补充公告|中期|半年度|季度|季报|季報|"
    r"業績公告|业绩公告|環境|环境|ESG|社會責任|社会责任|通函|"
    r"更正公告|更正說明|更正说明",
    re.I,
)
RESTATEMENT_MARKERS = re.compile(r"修订|修訂|更正|重述|更新版", re.I)
PDF_ALLOWED_HOSTS = {
    "static.cninfo.com.cn",
    "www1.hkexnews.hk",
    "www.hkexnews.hk",
}
MIN_SELECTED_REPORT_PAGES = 60


@dataclass(frozen=True)
class Company:
    issuer_id: str
    security_id: str
    ticker: str
    market: str
    name: str
    currency: str


class BackfillError(RuntimeError):
    """Expected source or validation failure that should be recorded."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_text(value: Any) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value).strip()


def safe_component(value: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value).strip("_.")
    return cleaned or "unnamed"


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fields = [
        "company_id",
        "company_name",
        "market",
        "ticker",
        "archive_kind",
        "coverage_status",
        "verified_reports",
        "selected_reports",
        "latest_fiscal_year",
        "latest_fiscal_year_end_date",
        "latest_publish_date",
        "error_code",
        "error_message",
    ]
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_companies() -> list[Company]:
    payload = load_json(COMPANIES_PATH, {})
    rows = payload.get("companies") or []
    companies = [
        Company(
            issuer_id=str(row["issuerId"]),
            security_id=str(row["securityId"]),
            ticker=str(row["ticker"]),
            market=str(row["market"]),
            name=str(row["name"]),
            currency=str(row["currency"]),
        )
        for row in rows
    ]
    if len(companies) != 67:
        raise BackfillError(
            f"COMPANY_SCOPE_MISMATCH: expected 67, found {len(companies)}"
        )
    if len({row.issuer_id for row in companies}) != len(companies):
        raise BackfillError("DUPLICATE_COMPANY_ID")
    return companies


def existing_legacy_manifests() -> list[Path]:
    return sorted(
        path
        for path in LEGACY_ROOT.rglob("manifest.csv")
        if DEFAULT_OUTPUT not in path.parents
    )


def legacy_manifest_for(company: Company) -> Path | None:
    for path in existing_legacy_manifests():
        directory_name = path.parent.name
        if company.name in directory_name:
            return path
        if company.name == "腾讯控股" and "腾讯控股" in directory_name:
            return path
    return None


def legacy_coverage(company: Company) -> dict[str, Any] | None:
    manifest = legacy_manifest_for(company)
    if manifest is None:
        return None
    rows = list(csv.DictReader(manifest.open(encoding="utf-8-sig")))
    valid_rows: list[dict[str, Any]] = []
    for row in rows:
        year = parse_fiscal_year(row.get("年份", ""))
        filename = row.get("文件名")
        if year is None or not filename:
            continue
        path = manifest.parent / filename
        expected = (row.get("SHA256") or "").lower()
        if not path.is_file() or not expected:
            continue
        actual = sha256_file(path)
        if actual != expected:
            continue
        valid_rows.append(
            {
                "fiscal_year": year,
                "fiscal_year_end_date": f"{year:04d}-12-31",
                "publish_date": extract_date_from_url(row.get("官方URL", "")),
                "path": str(path.relative_to(ROOT)),
                "sha256": actual,
            }
        )
    return {
        "manifest": str(manifest.relative_to(ROOT)),
        "verified_rows": valid_rows,
    }


def parse_fiscal_year(title: Any) -> int | None:
    value = clean_text(title)
    range_match = re.search(r"(?<!\d)(20\d{2})\s*[/／-]\s*(\d{2,4})", value)
    if range_match:
        start = int(range_match.group(1))
        raw_end = range_match.group(2)
        end = int(raw_end) if len(raw_end) == 4 else (start // 100) * 100 + int(raw_end)
        if end < start:
            end += 100
        return end if 2000 <= end <= 2100 else None
    contextual = re.search(
        r"(?<!\d)(20\d{2})(?!\d)\s*年?\s*(?:度\s*)?(?:年度报告|年度報告|年报|年報)",
        value,
        re.I,
    )
    if contextual:
        return int(contextual.group(1))
    suffix_year = re.search(
        r"(?:年度报告|年度報告|年报|年報)\s*(20\d{2})(?!\d)",
        value,
        re.I,
    )
    if suffix_year:
        return int(suffix_year.group(1))
    chinese_year = re.search(
        r"([二〇零○Ｏ一二三四五六七八九]{4})\s*年?\s*(?:度\s*)?"
        r"(?:年度报告|年度報告|年报|年報)",
        value,
        re.I,
    )
    if chinese_year:
        digit_map = {
            "〇": "0",
            "零": "0",
            "○": "0",
            "Ｏ": "0",
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
        return int("".join(digit_map[char] for char in chinese_year.group(1)))
    english = re.search(
        r"(?:annual\s+report\D{0,12}(20\d{2})|(20\d{2})\D{0,12}annual\s+report)",
        value,
        re.I,
    )
    if english:
        return int(english.group(1) or english.group(2))
    if re.fullmatch(r"20\d{2}", value):
        return int(value)
    return None


def is_full_annual_report(title: str) -> bool:
    return bool(ANNUAL_MARKERS.search(title)) and not bool(
        EXCLUDED_TITLE_MARKERS.search(title)
    )


def extract_date_from_url(url: Any) -> str | None:
    match = re.search(r"/(20\d{2}-\d{2}-\d{2})/", str(url or ""))
    return match.group(1) if match else None


def document_score(document: dict[str, Any]) -> tuple[int, int, int, str, str]:
    title = document["source_document"]
    year = document["fiscal_year"]
    exact = bool(
        re.fullmatch(
            rf"{year}(?:年|年度)?(?:年度报告|年度報告|年报|年報)(?:（?(?:修订|修訂|更正|重述|更新版)[^）)]*）?)?",
            re.sub(r"\s+", "", title),
            re.I,
        )
    )
    restated = bool(RESTATEMENT_MARKERS.search(title))
    # CNInfo can publish two files with the same title and date. Prefer the
    # substantially larger official attachment; the shorter one may be only
    # a cover/online-view stub despite its identical title.
    size_hint = int(document.get("source_size_hint_kb") or 0)
    return (
        2 if exact else 1,
        1 if restated else 0,
        size_hint,
        document["publish_date"],
        str(document.get("source_announcement_id") or ""),
    )


def select_latest_ten(documents: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    today = date.today()
    for document in documents:
        fiscal_year = int(document["fiscal_year"])
        try:
            fiscal_end = date.fromisoformat(document["fiscal_year_end_date"])
            published = date.fromisoformat(document["publish_date"])
        except (TypeError, ValueError):
            continue
        if fiscal_end > today or published > today:
            continue
        grouped.setdefault(fiscal_year, []).append(document)
    selected: list[dict[str, Any]] = []
    for fiscal_year, candidates in grouped.items():
        chosen = max(candidates, key=document_score).copy()
        chosen["restatement_status"] = (
            "RESTATED" if RESTATEMENT_MARKERS.search(chosen["source_document"]) else "ORIGINAL"
        )
        chosen["candidate_count_for_fiscal_year"] = len(candidates)
        chosen["selection_status"] = "SELECTED_CURRENT"
        selected.append(chosen)
    return sorted(selected, key=lambda item: item["fiscal_year"], reverse=True)[:10]


def fiscal_calendar(company: Company, fiscal_year: int) -> tuple[str, str]:
    payload = load_json(CALENDARS_PATH, {})
    rule = (payload.get("company_overrides") or {}).get(company.issuer_id)
    if rule is None:
        rule = (payload.get("defaults") or {}).get(company.market)
    if not rule or not rule.get("fiscal_year_end_month_day"):
        raise BackfillError(f"FISCAL_CALENDAR_UNAVAILABLE: {company.issuer_id}")
    end = f"{fiscal_year:04d}-{rule['fiscal_year_end_month_day']}"
    return end, str(rule.get("status") or "UNKNOWN")


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    attempts: int = 4,
    timeout: int = 60,
    **kwargs: Any,
) -> requests.Response:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001 - recorded after bounded retries
            error = exc
            if attempt + 1 < attempts:
                time.sleep(min(8.0, 0.8 * (2**attempt)))
    raise BackfillError(f"HTTP_FAILED: {method} {url}: {type(error).__name__}: {error}") from error


def save_source_snapshot(
    output: Path,
    provider: str,
    company: Company,
    request_metadata: dict[str, Any],
    response_body: bytes,
) -> dict[str, Any]:
    digest = sha256_bytes(response_body)
    suffix = "json" if response_body.lstrip().startswith((b"{", b"[")) else "html"
    relative = Path("metadata") / company.issuer_id / f"{provider}_{digest[:16]}.{suffix}"
    path = output / relative
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(response_body)
        os.replace(temporary, path)
    metadata_path = path.with_suffix(path.suffix + ".metadata.json")
    atomic_write_json(
        metadata_path,
        {
            "schema_version": SCHEMA_VERSION,
            "company_id": company.issuer_id,
            "provider": provider,
            "fetch_time": utc_now(),
            "request": request_metadata,
            "response_sha256": digest,
            "response_bytes": len(response_body),
            "local_path": str(relative),
        },
    )
    return {
        "sha256": digest,
        "local_path": str(relative),
        "metadata_path": str(metadata_path.relative_to(output)),
    }


def cninfo_stock_map(session: requests.Session, output: Path) -> dict[str, dict[str, Any]]:
    response = request_with_retry(
        session, "GET", CNINFO_STOCKS_URL, headers=CNINFO_HEADERS
    )
    payload = response.json()
    rows = payload.get("stockList") or []
    mapping = {str(row["code"]): row for row in rows}
    atomic_write_json(
        output / "source_index" / "cninfo_stock_map.json",
        {
            "schema_version": SCHEMA_VERSION,
            "source_url": CNINFO_STOCKS_URL,
            "fetch_time": utc_now(),
            "sha256": sha256_bytes(response.content),
            "entries": {
                code: {
                    "code": str(row.get("code")),
                    "org_id": str(row.get("orgId")),
                    "name": str(row.get("zwjc")),
                    "category": str(row.get("category")),
                }
                for code, row in mapping.items()
            },
        },
    )
    return mapping


def hkex_stock_map(session: requests.Session, output: Path) -> dict[str, dict[str, Any]]:
    response = request_with_retry(
        session, "GET", HKEX_STOCKS_URL, headers=HKEX_HEADERS
    )
    rows = response.json()
    mapping = {str(row["c"]): row for row in rows}
    atomic_write_json(
        output / "source_index" / "hkex_stock_map.json",
        {
            "schema_version": SCHEMA_VERSION,
            "source_url": HKEX_STOCKS_URL,
            "fetch_time": utc_now(),
            "sha256": sha256_bytes(response.content),
            "entries": {
                code: {
                    "code": str(row.get("c")),
                    "stock_id": int(row.get("i")),
                    "name": str(row.get("n")),
                }
                for code, row in mapping.items()
            },
        },
    )
    return mapping


def discover_cninfo(
    session: requests.Session,
    output: Path,
    company: Company,
    stock_map: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    code = company.ticker.zfill(6)
    stock = stock_map.get(code)
    if not stock or not stock.get("orgId"):
        raise BackfillError(f"CNINFO_ORG_ID_NOT_FOUND: {code}")
    column = "sse" if code.startswith(("6", "9")) else "szse"
    form = {
        "pageNum": 1,
        "pageSize": 50,
        "column": column,
        "tabName": "fulltext",
        "plate": "sh" if column == "sse" else "sz",
        "stock": f"{code},{stock['orgId']}",
        "searchkey": "年度报告",
        "secid": "",
        "category": "category_ndbg_szsh",
        "trade": "",
        "seDate": f"2014-01-01~{date.today().isoformat()}",
        "sortName": "time",
        "sortType": "desc",
        "isHLtitle": "true",
    }
    response = request_with_retry(
        session,
        "POST",
        CNINFO_QUERY_URL,
        headers=CNINFO_HEADERS,
        data=form,
    )
    source_snapshot = save_source_snapshot(
        output,
        "cninfo",
        company,
        {
            "url": CNINFO_QUERY_URL,
            "method": "POST",
            "form": form,
        },
        response.content,
    )
    announcements = response.json().get("announcements") or []
    documents: list[dict[str, Any]] = []
    for item in announcements:
        title = clean_text(item.get("announcementTitle"))
        if not is_full_annual_report(title):
            continue
        fiscal_year = parse_fiscal_year(title)
        if fiscal_year is None:
            continue
        adjunct = str(item.get("adjunctUrl") or "")
        source_url = urljoin(CNINFO_STATIC_BASE, adjunct.lstrip("/"))
        publish_date = extract_date_from_url(source_url)
        if not publish_date:
            continue
        fiscal_end, calendar_status = fiscal_calendar(company, fiscal_year)
        documents.append(
            {
                "schema_version": SCHEMA_VERSION,
                "company_id": company.issuer_id,
                "security_id": company.security_id,
                "share_class": "A",
                "company_name": company.name,
                "market": company.market,
                "ticker": company.ticker,
                "source_name": "巨潮资讯网（法定信息披露平台）",
                "source_level": "OFFICIAL_DISCLOSURE_PLATFORM",
                "source_document": title,
                "source_url": source_url,
                "source_publish_date": publish_date,
                "publish_date": publish_date,
                "source_fetch_time": utc_now(),
                "source_announcement_id": str(item.get("announcementId") or ""),
                "source_size_hint_kb": int(item.get("adjunctSize") or 0),
                "fiscal_year": fiscal_year,
                "fiscal_year_label": f"FY{fiscal_year}",
                "fiscal_year_end_date": fiscal_end,
                "fiscal_year_end_status": calendar_status,
                "currency": company.currency,
                "data_status": "DISCOVERED",
                "discovery_snapshot": source_snapshot,
            }
        )
    return select_latest_ten(documents), source_snapshot


def parse_hkex_rows(body: str, company: Company) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", body, flags=re.I | re.S):
        link = re.search(
            r'<a\s+href="([^"]+\.pdf)"[^>]*>(.*?)</a>',
            row_html,
            flags=re.I | re.S,
        )
        if not link:
            continue
        title = clean_text(link.group(2))
        if not is_full_annual_report(title):
            continue
        date_match = re.search(
            r"release-time[^>]*>.*?</span>\s*(\d{2}/\d{2}/\d{4})",
            row_html,
            flags=re.I | re.S,
        )
        if not date_match:
            continue
        fiscal_year = parse_fiscal_year(title)
        if fiscal_year is None:
            continue
        publish_date = datetime.strptime(date_match.group(1), "%d/%m/%Y").date().isoformat()
        fiscal_end, calendar_status = fiscal_calendar(company, fiscal_year)
        source_url = urljoin("https://www1.hkexnews.hk", html.unescape(link.group(1)))
        announcement_id = Path(urlparse(source_url).path).stem
        documents.append(
            {
                "schema_version": SCHEMA_VERSION,
                "company_id": company.issuer_id,
                "security_id": company.security_id,
                "share_class": "H",
                "company_name": company.name,
                "market": company.market,
                "ticker": company.ticker,
                "source_name": "香港交易所披露易",
                "source_level": "OFFICIAL_EXCHANGE",
                "source_document": title,
                "source_url": source_url,
                "source_publish_date": publish_date,
                "publish_date": publish_date,
                "source_fetch_time": utc_now(),
                "source_announcement_id": announcement_id,
                "fiscal_year": fiscal_year,
                "fiscal_year_label": title,
                "fiscal_year_end_date": fiscal_end,
                "fiscal_year_end_status": calendar_status,
                "currency": company.currency,
                "data_status": "DISCOVERED",
            }
        )
    return documents


def discover_hkex(
    session: requests.Session,
    output: Path,
    company: Company,
    stock_map: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    code = company.ticker.zfill(5)
    stock = stock_map.get(code)
    if not stock or stock.get("i") is None:
        raise BackfillError(f"HKEX_STOCK_ID_NOT_FOUND: {code}")
    form = {
        "lang": "ZH",
        "category": "0",
        "market": "SEHK",
        "searchType": "1",
        "documentType": "-1",
        "t1code": "40000",
        "t2Gcode": "-1",
        "t2code": "40100",
        "stockId": str(stock["i"]),
        "from": "20140101",
        "to": date.today().strftime("%Y%m%d"),
        "title": "",
    }
    response = request_with_retry(
        session,
        "POST",
        HKEX_QUERY_URL,
        headers=HKEX_HEADERS,
        data=form,
    )
    source_snapshot = save_source_snapshot(
        output,
        "hkex",
        company,
        {
            "url": HKEX_QUERY_URL,
            "method": "POST",
            "form": form,
        },
        response.content,
    )
    documents = parse_hkex_rows(response.text, company)
    for document in documents:
        document["discovery_snapshot"] = source_snapshot
    return select_latest_ten(documents), source_snapshot


def company_directory(output: Path, company: Company) -> Path:
    return output / "companies" / f"{company.issuer_id}_{safe_component(company.name)}"


def manifest_path(output: Path, company: Company) -> Path:
    return company_directory(output, company) / "manifest.json"


def base_manifest(company: Company) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "company_id": company.issuer_id,
        "security_id": company.security_id,
        "company_name": company.name,
        "market": company.market,
        "ticker": company.ticker,
        "currency": company.currency,
        "updated_at": None,
        "discovery_status": "PENDING",
        "selected_documents": [],
        "documents": [],
        "errors": [],
    }


def update_discovery_manifest(
    output: Path,
    company: Company,
    selected: list[dict[str, Any]],
    source_snapshot: dict[str, Any],
) -> dict[str, Any]:
    path = manifest_path(output, company)
    manifest = load_json(path, base_manifest(company))
    manifest["updated_at"] = utc_now()
    manifest["discovery_status"] = "DISCOVERED" if selected else "NO_REPORT_FOUND"
    manifest["source_snapshot"] = source_snapshot
    manifest["selected_documents"] = selected
    selected_urls = {str(item.get("source_url")) for item in selected}
    selected_years = {int(item["fiscal_year"]) for item in selected}
    for document in manifest.get("documents", []):
        if (
            int(document.get("fiscal_year") or 0) in selected_years
            and str(document.get("source_url")) not in selected_urls
            and document.get("selection_status") == "SELECTED_CURRENT"
        ):
            # Preserve the old immutable PDF for provenance, but prevent all
            # downstream candidate extractors from treating it as current.
            document["selection_status"] = "SUPERSEDED_SOURCE_SELECTION"
    manifest["errors"] = [
        error for error in manifest.get("errors", []) if error.get("stage") != "DISCOVERY"
    ]
    atomic_write_json(path, manifest)
    return manifest


def record_discovery_error(
    output: Path, company: Company, exc: Exception
) -> None:
    path = manifest_path(output, company)
    manifest = load_json(path, base_manifest(company))
    manifest["updated_at"] = utc_now()
    manifest["discovery_status"] = "ERROR"
    manifest["errors"] = [
        error for error in manifest.get("errors", []) if error.get("stage") != "DISCOVERY"
    ] + [
        {
            "stage": "DISCOVERY",
            "error_code": type(exc).__name__,
            "error_message": str(exc)[:1000],
            "occurred_at": utc_now(),
        }
    ]
    atomic_write_json(path, manifest)


def pdf_info(path: Path) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise BackfillError(f"PDFINFO_FAILED: {path.name}: {exc}") from exc
    match = re.search(r"^Pages:\s*(\d+)", result.stdout, flags=re.M)
    if not match:
        raise BackfillError(f"PDF_PAGE_COUNT_MISSING: {path.name}")
    return int(match.group(1)), result.stdout


def validate_pdf(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size < 20_000:
        raise BackfillError(f"PDF_TOO_SMALL: {size}")
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise BackfillError("INVALID_PDF_HEADER")
    pages, _ = pdf_info(path)
    return {
        "sha256": sha256_file(path),
        "size_bytes": size,
        "pdf_pages": pages,
    }


def validate_selected_document_semantics(
    document: dict[str, Any], evidence: dict[str, Any]
) -> None:
    """Reject a valid PDF container that is not a plausible full report."""

    title = str(document.get("source_document") or "")
    if not is_full_annual_report(title):
        raise BackfillError(f"NOT_FULL_ANNUAL_REPORT_TITLE: {title}")
    pages = int(evidence.get("pdf_pages") or 0)
    if pages < MIN_SELECTED_REPORT_PAGES:
        raise BackfillError(
            f"ANNUAL_REPORT_TOO_SHORT: {pages} < {MIN_SELECTED_REPORT_PAGES} pages"
        )


def verified_document_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("source_url")): item
        for item in manifest.get("documents", [])
        if item.get("data_status") == "VERIFIED" and item.get("source_url")
    }


def download_document(
    session: requests.Session,
    output: Path,
    company: Company,
    selected: dict[str, Any],
) -> dict[str, Any]:
    source_url = str(selected["source_url"])
    host = (urlparse(source_url).hostname or "").lower()
    if host not in PDF_ALLOWED_HOSTS:
        raise BackfillError(f"UNAPPROVED_PDF_HOST: {host}")
    company_dir = company_directory(output, company)
    fiscal_dir = company_dir / "documents" / f"FY{selected['fiscal_year']}"
    fiscal_dir.mkdir(parents=True, exist_ok=True)
    announcement = safe_component(str(selected["source_announcement_id"]))
    target = fiscal_dir / f"{announcement}_annual_report.pdf"
    if target.exists():
        evidence = validate_pdf(target)
        validate_selected_document_semantics(selected, evidence)
        result = dict(selected)
        result.update(
            evidence,
            local_path=str(target.relative_to(output)),
            data_status="VERIFIED",
            verification_status="EXISTING_FILE_REVERIFIED",
            verified_at=utc_now(),
        )
        return result
    temporary = fiscal_dir / f".{target.name}.{uuid.uuid4().hex}.part"
    try:
        response = request_with_retry(
            session,
            "GET",
            source_url,
            headers=HKEX_HEADERS if "hkexnews.hk" in host else CNINFO_HEADERS,
            timeout=180,
        )
        with temporary.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        evidence = validate_pdf(temporary)
        validate_selected_document_semantics(selected, evidence)
        if target.exists():
            existing_sha = sha256_file(target)
            if existing_sha != evidence["sha256"]:
                conflict = target.with_name(
                    f"{target.stem}_{evidence['sha256'][:12]}{target.suffix}"
                )
                os.replace(temporary, conflict)
                raise BackfillError(
                    f"SOURCE_CONTENT_CONFLICT: retained {target.name} and {conflict.name}"
                )
        else:
            os.replace(temporary, target)
        result = dict(selected)
        result.update(
            evidence,
            local_path=str(target.relative_to(output)),
            data_status="VERIFIED",
            verification_status="PDF_HEADER_PDFINFO_SHA256_OK",
            verified_at=utc_now(),
        )
        return result
    finally:
        temporary.unlink(missing_ok=True)


def download_company(output: Path, company: Company) -> tuple[str, int, list[str]]:
    path = manifest_path(output, company)
    manifest = load_json(path, base_manifest(company))
    selected = manifest.get("selected_documents") or []
    existing = verified_document_map(manifest)
    errors: list[str] = []
    documents = [
        item
        for item in manifest.get("documents", [])
        if item.get("data_status") == "VERIFIED"
    ]
    session = requests.Session()
    for candidate in sorted(selected, key=lambda item: item["fiscal_year"]):
        source_url = str(candidate["source_url"])
        previous = existing.get(source_url)
        if previous:
            local = output / str(previous.get("local_path"))
            try:
                evidence = validate_pdf(local)
                validate_selected_document_semantics(candidate, evidence)
                if evidence["sha256"] == previous.get("sha256"):
                    continue
                raise BackfillError("LOCAL_SHA256_MISMATCH")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"FY{candidate['fiscal_year']}: {exc}")
                continue
        try:
            result = download_document(session, output, company, candidate)
            documents = [
                item for item in documents if item.get("source_url") != source_url
            ] + [result]
            manifest["documents"] = sorted(
                documents, key=lambda item: item["fiscal_year"], reverse=True
            )
            manifest["updated_at"] = utc_now()
            atomic_write_json(path, manifest)
        except Exception as exc:  # noqa: BLE001 - continue other fiscal years
            errors.append(f"FY{candidate['fiscal_year']}: {exc}")
    manifest["documents"] = sorted(
        documents, key=lambda item: item["fiscal_year"], reverse=True
    )
    manifest["errors"] = [
        error for error in manifest.get("errors", []) if error.get("stage") != "DOWNLOAD"
    ] + [
        {
            "stage": "DOWNLOAD",
            "error_code": "DOCUMENT_DOWNLOAD_FAILED",
            "error_message": message[:1000],
            "occurred_at": utc_now(),
        }
        for message in errors
    ]
    manifest["updated_at"] = utc_now()
    atomic_write_json(path, manifest)
    selected_urls = {
        str(item.get("source_url")) for item in selected if item.get("source_url")
    }
    current_verified_count = sum(
        item.get("data_status") == "VERIFIED"
        and item.get("selection_status") == "SELECTED_CURRENT"
        and str(item.get("source_url")) in selected_urls
        for item in documents
    )
    return company.issuer_id, current_verified_count, errors


def build_coverage(output: Path, companies: list[Company]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for company in companies:
        legacy = legacy_coverage(company)
        manifest = load_json(manifest_path(output, company), None)
        if manifest:
            selected = manifest.get("selected_documents") or []
            selected_urls = {
                str(item.get("source_url"))
                for item in selected
                if item.get("source_url")
            }
            verified = [
                item
                for item in (manifest.get("documents") or [])
                if item.get("data_status") == "VERIFIED"
                and item.get("selection_status") == "SELECTED_CURRENT"
                and str(item.get("source_url")) in selected_urls
            ]
            errors = manifest.get("errors") or []
            latest = max(verified, key=lambda item: item["fiscal_year"], default={})
            status = (
                "VERIFIED"
                if selected and len(verified) == len(selected) and not errors
                else "PARTIAL"
                if verified or selected
                else "ERROR"
                if errors
                else "PENDING"
            )
            row = {
                "company_id": company.issuer_id,
                "company_name": company.name,
                "market": company.market,
                "ticker": company.ticker,
                "archive_kind": "official_backfill_v1",
                "coverage_status": status,
                "verified_reports": len(verified),
                "selected_reports": len(selected),
                "latest_fiscal_year": latest.get("fiscal_year"),
                "latest_fiscal_year_end_date": latest.get("fiscal_year_end_date"),
                "latest_publish_date": latest.get("publish_date"),
                "error_code": errors[0].get("error_code") if errors else None,
                "error_message": errors[0].get("error_message") if errors else None,
            }
        elif legacy:
            verified = legacy["verified_rows"]
            latest = max(verified, key=lambda item: item["fiscal_year"], default={})
            row = {
                "company_id": company.issuer_id,
                "company_name": company.name,
                "market": company.market,
                "ticker": company.ticker,
                "archive_kind": "legacy_verified",
                "coverage_status": "VERIFIED" if verified else "PARTIAL",
                "verified_reports": len(verified),
                "selected_reports": len(verified),
                "latest_fiscal_year": latest.get("fiscal_year"),
                "latest_fiscal_year_end_date": latest.get("fiscal_year_end_date"),
                "latest_publish_date": latest.get("publish_date"),
                "error_code": None,
                "error_message": None,
            }
        else:
            row = {
                "company_id": company.issuer_id,
                "company_name": company.name,
                "market": company.market,
                "ticker": company.ticker,
                "archive_kind": "none",
                "coverage_status": "PENDING",
                "verified_reports": 0,
                "selected_reports": 0,
                "latest_fiscal_year": None,
                "latest_fiscal_year_end_date": None,
                "latest_publish_date": None,
                "error_code": None,
                "error_message": None,
            }
        rows.append(row)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["coverage_status"]] = counts.get(row["coverage_status"], 0) + 1
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "company_count": len(companies),
        "status_counts": counts,
        "companies": rows,
    }
    atomic_write_json(output / "coverage.json", payload)
    atomic_write_csv(output / "coverage.csv", rows)
    return payload


@contextlib.contextmanager
def exclusive_lock(output: Path) -> Iterable[None]:
    output.mkdir(parents=True, exist_ok=True)
    lock_path = output / ".backfill.lock"
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackfillError("BACKFILL_ALREADY_RUNNING") from exc
        yield


def filter_targets(
    companies: list[Company], args: argparse.Namespace
) -> list[Company]:
    issuer_filter = set(args.issuer or [])
    targets: list[Company] = []
    for company in companies:
        if issuer_filter and company.issuer_id not in issuer_filter:
            continue
        if args.market != "ALL" and company.market != args.market:
            continue
        if not args.include_legacy and legacy_manifest_for(company):
            continue
        targets.append(company)
    if args.limit is not None:
        targets = targets[: args.limit]
    return targets


def discover_targets(
    output: Path, targets: list[Company]
) -> tuple[list[Company], list[tuple[str, str]]]:
    session = requests.Session()
    cn_targets = [company for company in targets if company.market == "CN"]
    hk_targets = [company for company in targets if company.market == "HK"]
    cn_map = cninfo_stock_map(session, output) if cn_targets else {}
    hk_map = hkex_stock_map(session, output) if hk_targets else {}
    discovered: list[Company] = []
    errors: list[tuple[str, str]] = []
    for index, company in enumerate(targets, start=1):
        try:
            if company.market == "CN":
                selected, snapshot = discover_cninfo(
                    session, output, company, cn_map
                )
            else:
                selected, snapshot = discover_hkex(
                    session, output, company, hk_map
                )
            update_discovery_manifest(
                output, company, selected, snapshot
            )
            discovered.append(company)
            print(
                f"DISCOVERED {index}/{len(targets)} {company.issuer_id} "
                f"{company.name}: {len(selected)} report(s)",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - isolate each company
            record_discovery_error(output, company, exc)
            errors.append((company.issuer_id, str(exc)))
            print(
                f"DISCOVERY_FAILED {company.issuer_id} {company.name}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(0.15)
    return discovered, errors


def verify_archive(output: Path, companies: list[Company]) -> list[str]:
    errors: list[str] = []
    for company in companies:
        manifest = load_json(manifest_path(output, company), None)
        if not manifest:
            continue
        selected_urls = {
            str(item.get("source_url"))
            for item in (manifest.get("selected_documents") or [])
            if item.get("source_url")
        }
        current_urls = {
            str(item.get("source_url"))
            for item in (manifest.get("documents") or [])
            if item.get("data_status") == "VERIFIED"
            and item.get("selection_status") == "SELECTED_CURRENT"
            and item.get("source_url")
        }
        if selected_urls != current_urls:
            errors.append(
                f"{company.issuer_id}: CURRENT_SELECTION_MISMATCH "
                f"missing={sorted(selected_urls - current_urls)} "
                f"extra={sorted(current_urls - selected_urls)}"
            )
        for document in manifest.get("documents") or []:
            local = output / str(document.get("local_path") or "")
            try:
                evidence = validate_pdf(local)
                if document.get("selection_status") == "SELECTED_CURRENT":
                    validate_selected_document_semantics(document, evidence)
                if evidence["sha256"] != document.get("sha256"):
                    raise BackfillError("SHA256_MISMATCH")
                if evidence["size_bytes"] != document.get("size_bytes"):
                    raise BackfillError("SIZE_MISMATCH")
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"{company.issuer_id} FY{document.get('fiscal_year')}: {exc}"
                )
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("discover", "fetch", "verify", "summary")
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--market", choices=("ALL", "CN", "HK"), default="ALL")
    parser.add_argument("--issuer", action="append")
    parser.add_argument("--include-legacy", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output.resolve()
    companies = load_companies()
    targets = filter_targets(companies, args)
    if args.workers < 1 or args.workers > 4:
        raise BackfillError("workers must be between 1 and 4")
    with exclusive_lock(output):
        if args.command in {"discover", "fetch"}:
            print(
                f"TARGETS {len(targets)} (market={args.market}, "
                f"include_legacy={args.include_legacy})",
                flush=True,
            )
            discovered, discovery_errors = discover_targets(output, targets)
            download_errors: list[tuple[str, str]] = []
            if args.command == "fetch":
                with ThreadPoolExecutor(max_workers=args.workers) as executor:
                    futures = {
                        executor.submit(download_company, output, company): company
                        for company in discovered
                    }
                    for future in as_completed(futures):
                        company = futures[future]
                        try:
                            issuer_id, count, errors = future.result()
                            print(
                                f"FETCHED {issuer_id} {company.name}: "
                                f"{count} verified, {len(errors)} error(s)",
                                flush=True,
                            )
                            download_errors.extend(
                                (issuer_id, message) for message in errors
                            )
                        except Exception as exc:  # noqa: BLE001
                            download_errors.append((company.issuer_id, str(exc)))
                            print(
                                f"FETCH_FAILED {company.issuer_id} "
                                f"{company.name}: {exc}",
                                file=sys.stderr,
                            )
            coverage = build_coverage(output, companies)
            total_errors = len(discovery_errors) + len(download_errors)
            print(
                f"COVERAGE {json.dumps(coverage['status_counts'], ensure_ascii=False, sort_keys=True)}"
            )
            print(f"RUN_ERRORS {total_errors}")
            return 2 if total_errors else 0
        if args.command == "verify":
            errors = verify_archive(output, companies)
            coverage = build_coverage(output, companies)
            for error in errors:
                print(f"VERIFY_FAILED {error}", file=sys.stderr)
            print(
                f"COVERAGE {json.dumps(coverage['status_counts'], ensure_ascii=False, sort_keys=True)}"
            )
            return 2 if errors else 0
        coverage = build_coverage(output, companies)
        print(json.dumps(coverage["status_counts"], ensure_ascii=False, sort_keys=True))
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BackfillError as exc:
        print(f"BACKFILL_ERROR {exc}", file=sys.stderr)
        raise SystemExit(2)
