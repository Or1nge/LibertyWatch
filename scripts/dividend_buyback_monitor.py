#!/usr/bin/env python3
"""Maintain dividend and buyback histories for Liberty's 67 companies.

The collector stores exact Futu payloads in SQLite and raw JSON.  A baseline
is rendered deterministically.  Later revisions are handed to ``codex exec``
one company at a time; Codex is sandboxed inside that company's output
directory and may only maintain ``分红回购历史.md``.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
COMPANIES_PATH = ROOT / "data" / "source" / "companies.json"
DATABASE_PATH = ROOT / "data" / "monitor" / "liberty_monitor.sqlite3"
RAW_ROOT = ROOT / "data" / "monitor" / "raw"
STAGING_ROOT = ROOT / "data" / "monitor" / "staging"
OUTPUT_ROOT = ROOT / "outputs" / "companies"
LOCK_PATH = ROOT / "data" / "monitor" / "run.lock"
DEFAULT_CODEX = Path("/home/or1ngelinux/.local/bin/codex")

HISTORY_FILENAME = "分红回购历史.md"
NEWS_KEYWORDS = re.compile(
    r"分红|派息|股息|利润分配|回购|购回|注销|库存股|库藏股|"
    r"年度报告|年报|中期报告|中报|dividend|buyback|repurchase|"
    r"share repurchase|annual report|interim report",
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def json_safe(value: Any) -> Any:
    """Convert pandas/numpy values and NaN into JSON-safe builtins."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if hasattr(value, "item"):
        with contextlib.suppress(Exception):
            return json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def stable_json(value: Any) -> str:
    return json.dumps(
        json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def digest(value: Any, length: int = 24) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()[:length]


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(json_safe(payload), ensure_ascii=False, indent=2) + "\n")


@dataclass(frozen=True)
class Company:
    issuer_id: str
    security_id: str
    quote_code: str
    name: str
    ticker: str
    market: str
    currency: str
    sector: str
    industry: str

    @property
    def output_dir(self) -> Path:
        safe_name = self.name.replace("/", "_")
        return OUTPUT_ROOT / f"{self.issuer_id}_{safe_name}"

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "Company":
        return cls(
            issuer_id=str(row["issuerId"]),
            security_id=str(row["securityId"]),
            quote_code=str(row["quoteCode"]),
            name=str(row["name"]),
            ticker=str(row["ticker"]),
            market=str(row["market"]),
            currency=str(row["currency"]),
            sector=str(row.get("sector") or ""),
            industry=str(row.get("industry") or ""),
        )


def load_companies(path: Path = COMPANIES_PATH) -> list[Company]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("companies") if isinstance(payload, dict) else payload
    companies = [Company.from_dict(row) for row in rows]
    if len(companies) != 67:
        raise ValueError(f"正式公司清单必须为67家，当前为{len(companies)}家")
    for field, values in {
        "issuerId": [item.issuer_id for item in companies],
        "securityId": [item.security_id for item in companies],
        "quoteCode": [item.quote_code for item in companies],
    }.items():
        if len(values) != len(set(values)):
            raise ValueError(f"正式公司清单存在重复{field}")
    return companies


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS companies (
    issuer_id TEXT PRIMARY KEY,
    security_id TEXT NOT NULL UNIQUE,
    quote_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    ticker TEXT NOT NULL,
    market TEXT NOT NULL,
    currency TEXT NOT NULL,
    sector TEXT NOT NULL,
    industry TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_key TEXT PRIMARY KEY,
    issuer_id TEXT NOT NULL REFERENCES companies(issuer_id),
    event_type TEXT NOT NULL,
    event_date TEXT,
    source TEXT,
    source_url TEXT,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_issuer_type_idx
    ON events(issuer_id, event_type, event_date);
CREATE TABLE IF NOT EXISTS event_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL REFERENCES events(event_key),
    issuer_id TEXT NOT NULL REFERENCES companies(issuer_id),
    change_kind TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    synced_at TEXT,
    UNIQUE(event_key, payload_hash)
);
CREATE INDEX IF NOT EXISTS revisions_unsynced_idx
    ON event_revisions(issuer_id, synced_at);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    companies_checked INTEGER NOT NULL DEFAULT 0,
    revisions_found INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
"""


def connect_database(path: Path = DATABASE_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def sync_companies(connection: sqlite3.Connection, companies: Iterable[Company]) -> None:
    rows = [
        (
            item.issuer_id,
            item.security_id,
            item.quote_code,
            item.name,
            item.ticker,
            item.market,
            item.currency,
            item.sector,
            item.industry,
        )
        for item in companies
    ]
    connection.executemany(
        """
        INSERT INTO companies (
            issuer_id, security_id, quote_code, name, ticker, market,
            currency, sector, industry
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(issuer_id) DO UPDATE SET
            security_id=excluded.security_id,
            quote_code=excluded.quote_code,
            name=excluded.name,
            ticker=excluded.ticker,
            market=excluded.market,
            currency=excluded.currency,
            sector=excluded.sector,
            industry=excluded.industry
        """,
        rows,
    )
    wanted = {item.issuer_id for item in companies}
    existing = {
        str(row[0]) for row in connection.execute("SELECT issuer_id FROM companies")
    }
    unexpected = existing - wanted
    if unexpected:
        raise RuntimeError(f"数据库包含非正式公司：{sorted(unexpected)}")
    connection.commit()


class InterfaceRateLimiter:
    """Apply the independent rolling-window limits used by Futu interfaces."""

    def __init__(
        self,
        maximum: int = 28,
        window_seconds: float = 30.0,
        interface_maximums: Mapping[str, int] | None = None,
    ) -> None:
        self.maximum = maximum
        self.window_seconds = window_seconds
        # Search news is documented by OpenD at 10 calls/30 seconds.  Leave
        # one request of headroom for another local client.
        self.interface_maximums = {"news": 9, **(interface_maximums or {})}
        self.calls: dict[str, deque[float]] = defaultdict(deque)

    def wait(self, interface: str) -> None:
        maximum = self.interface_maximums.get(interface, self.maximum)
        history = self.calls[interface]
        current = time.monotonic()
        while history and current - history[0] >= self.window_seconds:
            history.popleft()
        if len(history) >= maximum:
            delay = self.window_seconds - (current - history[0]) + 0.1
            time.sleep(max(delay, 0.1))
            current = time.monotonic()
            while history and current - history[0] >= self.window_seconds:
                history.popleft()
        history.append(time.monotonic())


class FutuProvider:
    def __init__(self, host: str = "127.0.0.1", port: int = 11111) -> None:
        try:
            from futu import OpenQuoteContext, RET_OK
        except ImportError as error:
            raise RuntimeError(
                "未找到futu-api；请使用tools/futu-opend/.venv/bin/python运行"
            ) from error
        self.ret_ok = RET_OK
        self.context = OpenQuoteContext(host=host, port=port)
        self.limiter = InterfaceRateLimiter()

    def close(self) -> None:
        self.context.close()

    def _call(self, interface: str, function: Any, *args: Any, **kwargs: Any) -> Any:
        for attempt in range(3):
            self.limiter.wait(interface)
            ret, data = function(*args, **kwargs)
            if ret == self.ret_ok:
                return data
            message = str(data)
            if "频率太高" in message and attempt < 2:
                time.sleep(30.5)
                continue
            raise RuntimeError(f"Futu {interface}失败：{message}")
        raise AssertionError("unreachable")

    @staticmethod
    def _frame_records(frame: Any) -> list[dict[str, Any]]:
        if frame is None:
            return []
        if hasattr(frame, "to_dict"):
            return [json_safe(row) for row in frame.to_dict(orient="records")]
        return [json_safe(row) for row in frame]

    def fetch(self, company: Company, *, full_buybacks: bool) -> dict[str, Any]:
        errors: dict[str, str] = {}

        try:
            dividend_data = self._call(
                "dividends",
                self.context.get_corporate_actions_dividends,
                company.quote_code,
            )
            dividends = json_safe(dividend_data.get("dividend_list", []))
        except Exception as error:  # public data availability varies by security
            dividends = []
            errors["dividends"] = str(error)

        buybacks: list[dict[str, Any]] = []
        try:
            next_key: str | None = None
            pages = 0
            while True:
                buyback_data = self._call(
                    "buybacks",
                    self.context.get_corporate_actions_buybacks,
                    company.quote_code,
                    next_key=next_key,
                    num=50,
                )
                hk_rows = self._frame_records(buyback_data.get("hk_buy_back_list"))
                a_rows = self._frame_records(buyback_data.get("a_buy_back_list"))
                buybacks.extend({"record_market": "HK", **row} for row in hk_rows)
                buybacks.extend({"record_market": "A", **row} for row in a_rows)
                pages += 1
                next_key = str(buyback_data.get("next_key", "-1"))
                if next_key == "-1" or not full_buybacks or pages >= 100:
                    break
        except Exception as error:
            errors["buybacks"] = str(error)

        try:
            financial_data = self._call(
                "financials",
                self.context.get_financials_statements,
                company.quote_code,
                statement_type=1,
                financial_type=7,
                num=50,
            )
            financials = []
            for report in financial_data.get("report_list", []):
                financials.append(
                    json_safe(
                        {
                            key: report.get(key)
                            for key in (
                                "date_time_str",
                                "fiscal_year",
                                "financial_type",
                                "period_text",
                                "currency_info",
                                "accounting_standards",
                                "auditor_report",
                                "currency_code",
                            )
                        }
                    )
                )
        except Exception as error:
            financials = []
            errors["financials"] = str(error)

        try:
            news_data = self._call(
                "news",
                self.context.get_search_news,
                company.name.replace("-S", "").replace("-W", ""),
                max_count=50,
            )
            news = [
                row
                for row in self._frame_records(news_data)
                if NEWS_KEYWORDS.search(str(row.get("title") or ""))
            ]
        except Exception as error:
            news = []
            errors["news"] = str(error)

        return {
            "schema_version": 1,
            "fetched_at": now_iso(),
            "company": {
                "issuer_id": company.issuer_id,
                "security_id": company.security_id,
                "quote_code": company.quote_code,
                "name": company.name,
                "currency": company.currency,
            },
            "dividends": dividends,
            "buybacks": buybacks,
            "financials": financials,
            "news": news,
            "errors": errors,
        }


def _event(
    company: Company,
    event_type: str,
    identity: Any,
    event_date: Any,
    source: Any,
    source_url: Any,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    safe_payload = json_safe(payload)
    return {
        "event_key": f"{event_type}:{digest([company.quote_code, identity])}",
        "issuer_id": company.issuer_id,
        "event_type": event_type,
        "event_date": str(event_date or ""),
        "source": str(source or "Futu OpenD"),
        "source_url": str(source_url or ""),
        "payload": safe_payload,
        "payload_hash": digest(safe_payload, 64),
    }


def normalize_events(company: Company, snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in snapshot.get("dividends", []):
        identity = [row.get("pub_date"), row.get("statement"), row.get("record_date")]
        events.append(
            _event(
                company,
                "dividend",
                identity,
                row.get("pub_date") or row.get("ex_date"),
                "Futu OpenD corporate actions",
                "",
                row,
            )
        )
    for row in snapshot.get("buybacks", []):
        if row.get("record_market") == "HK":
            identity = [
                row.get("end_date_str"),
                row.get("buy_back_sum"),
                row.get("buy_back_money"),
                row.get("share_type"),
            ]
            event_date = row.get("end_date_str") or row.get("publ_date_str")
        else:
            identity = [
                row.get("advance_date_str") or row.get("start_date_str"),
                row.get("seller"),
                row.get("buy_back_mode"),
                row.get("share_type"),
            ]
            event_date = (
                row.get("change_date_str")
                or row.get("end_date_str")
                or row.get("advance_date_str")
            )
        events.append(
            _event(
                company,
                "buyback",
                identity,
                event_date,
                "Futu OpenD corporate actions",
                "",
                row,
            )
        )
    for row in snapshot.get("financials", []):
        identity = [row.get("fiscal_year"), row.get("period_text"), "income"]
        events.append(
            _event(
                company,
                "financial",
                identity,
                row.get("date_time_str"),
                "Futu OpenD financial statements",
                "",
                row,
            )
        )
    for row in snapshot.get("news", []):
        identity = row.get("url") or [row.get("publish_time"), row.get("title")]
        # View counts change continuously and are not a corporate-news revision.
        stable_row = {key: value for key, value in row.items() if key != "view_count"}
        events.append(
            _event(
                company,
                "news",
                identity,
                row.get("publish_time"),
                row.get("source") or "Futu资讯搜索",
                row.get("url"),
                stable_row,
            )
        )
    # Futu may repeat one plan/news item with multiple versions in the same
    # response. Keep the final row so one scan cannot oscillate current state.
    deduplicated: dict[str, dict[str, Any]] = {}
    for event in events:
        deduplicated[str(event["event_key"])] = event
    return list(deduplicated.values())


def ingest_events(
    connection: sqlite3.Connection,
    events: Iterable[Mapping[str, Any]],
    *,
    baseline: bool,
) -> int:
    observed_at = now_iso()
    revisions = 0
    for event in events:
        existing = connection.execute(
            "SELECT payload_hash FROM events WHERE event_key=?", (event["event_key"],)
        ).fetchone()
        payload_json = stable_json(event["payload"])
        if existing is None:
            change_kind = "new"
            connection.execute(
                """
                INSERT INTO events (
                    event_key, issuer_id, event_type, event_date, source,
                    source_url, payload_hash, payload_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_key"],
                    event["issuer_id"],
                    event["event_type"],
                    event["event_date"],
                    event["source"],
                    event["source_url"],
                    event["payload_hash"],
                    payload_json,
                    observed_at,
                    observed_at,
                ),
            )
        elif str(existing["payload_hash"]) != str(event["payload_hash"]):
            change_kind = "changed"
            connection.execute(
                """
                UPDATE events SET event_date=?, source=?, source_url=?,
                    payload_hash=?, payload_json=?, last_seen_at=?
                WHERE event_key=?
                """,
                (
                    event["event_date"],
                    event["source"],
                    event["source_url"],
                    event["payload_hash"],
                    payload_json,
                    observed_at,
                    event["event_key"],
                ),
            )
        else:
            connection.execute(
                "UPDATE events SET last_seen_at=? WHERE event_key=?",
                (observed_at, event["event_key"]),
            )
            continue

        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO event_revisions (
                event_key, issuer_id, change_kind, payload_hash, payload_json,
                observed_at, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_key"],
                event["issuer_id"],
                change_kind,
                event["payload_hash"],
                payload_json,
                observed_at,
                observed_at if baseline else None,
            ),
        )
        if cursor.rowcount:
            revisions += 1
    connection.commit()
    return revisions


def event_rows(
    connection: sqlite3.Connection, issuer_id: str, event_type: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT event_key, event_date, source, source_url, payload_json
        FROM events WHERE issuer_id=? AND event_type=?
        ORDER BY event_date DESC, event_key DESC
        """,
        (issuer_id, event_type),
    ).fetchall()
    return [
        {
            "event_key": row["event_key"],
            "event_date": row["event_date"],
            "source": row["source"],
            "source_url": row["source_url"],
            "payload": json.loads(row["payload_json"]),
        }
        for row in rows
    ]


def display(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _year(value: Any) -> str:
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return match.group(0) if match else "日期不明"


def annual_buyback_summary(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = row["payload"]
        year = _year(row.get("event_date"))
        target = grouped.setdefault(
            year,
            {"year": year, "records": 0, "money_cents": 0, "shares": 0, "processes": set()},
        )
        target["records"] += 1
        money = Decimal(str(payload.get("buy_back_money") or 0))
        target["money_cents"] += int(
            (money * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        target["shares"] += int(payload.get("buy_back_sum") or 0)
        process = payload.get("event_proce_desc") or payload.get("share_type")
        if process:
            target["processes"].add(str(process))
    result = []
    for year in sorted(grouped, reverse=True):
        row = grouped[year]
        amount = Decimal(row["money_cents"]) / Decimal(100)
        formatted_amount = f"{amount:,.2f}".rstrip("0").rstrip(".")
        result.append(
            {
                **{key: value for key, value in row.items() if key != "money_cents"},
                "money": formatted_amount,
                "processes": "；".join(sorted(row["processes"])),
            }
        )
    return result


def history_markdown(connection: sqlite3.Connection, company: Company) -> str:
    dividends = event_rows(connection, company.issuer_id, "dividend")
    buybacks = event_rows(connection, company.issuer_id, "buyback")
    financials = event_rows(connection, company.issuer_id, "financial")
    news = event_rows(connection, company.issuer_id, "news")
    generated_at = now_iso()
    lines = [
        f"# {company.name}分红回购历史",
        "",
        f"- 证券：`{company.security_id}`（Futu：`{company.quote_code}`）",
        f"- 市场/币种：{company.market} / {company.currency}",
        f"- 行业：{company.sector} / {company.industry}",
        f"- 最近基线生成：{generated_at}",
        "- 口径：分红与回购字段按 Futu OpenD 原始记录保存；回购金额不做币种推断或换算。",
        "- 注意：回购记录不自动等同于净注销回购；员工激励、库存股及注销仍需公告佐证。",
        "",
        "## 分红事件",
        "",
        "| 公告日 | 方案 | 进展 | 登记日 | 除权除息日 | 派息日 |",
        "|---|---|---|---|---|---|",
    ]
    if dividends:
        for row in dividends:
            payload = row["payload"]
            lines.append(
                "| "
                + " | ".join(
                    display(payload.get(key))
                    for key in (
                        "pub_date",
                        "statement",
                        "process",
                        "record_date",
                        "ex_date",
                        "dividend_payable_date",
                    )
                )
                + " |"
            )
    else:
        lines.append("| — | 暂无可用记录 | — | — | — | — |")

    lines += [
        "",
        "## 回购年度汇总",
        "",
        "港股通常为逐日成交记录，A股通常为方案/实施记录。金额是富途逐条原始金额的",
        "机械合计，接口未逐条标注币种且未做换算；双重上市公司的金额不可直接跨市场比较。",
        "",
        "| 年度 | 记录数 | 回购金额 | 回购股数 | 记录类型/进展 |",
        "|---:|---:|---:|---:|---|",
    ]
    summary = annual_buyback_summary(buybacks)
    if summary:
        for row in summary:
            lines.append(
                f"| {row['year']} | {row['records']} | {display(row['money'])} | "
                f"{display(row['shares'])} | {display(row['processes'])} |"
            )
    else:
        lines.append("| — | 0 | 0 | 0 | 暂无可用记录 |")

    lines += [
        "",
        "## 财报数据期",
        "",
        "这里只记录富途结构化财报是否出现新报告期，不替代正式年报 PDF。",
        "",
        "| 报告期 | 截止日 | 币种 | 会计准则 | 审计意见 |",
        "|---|---|---|---|---|",
    ]
    if financials:
        for row in financials[:12]:
            payload = row["payload"]
            lines.append(
                "| "
                + " | ".join(
                    display(payload.get(key))
                    for key in (
                        "period_text",
                        "date_time_str",
                        "currency_code",
                        "accounting_standards",
                        "auditor_report",
                    )
                )
                + " |"
            )
    else:
        lines.append("| — | — | — | — | 暂无可用记录 |")

    lines += [
        "",
        "## 相关公告与资讯",
        "",
        "仅保留标题命中分红、派息、回购、注销或财报关键词的结果。",
        "",
        "| 发布时间 | 类型 | 标题 | 来源 | 链接 |",
        "|---|---|---|---|---|",
    ]
    if news:
        for row in news[:50]:
            payload = row["payload"]
            url = str(payload.get("url") or row.get("source_url") or "")
            link = f"[查看]({url})" if url else "—"
            lines.append(
                "| "
                + " | ".join(
                    [
                        display(payload.get("publish_time")),
                        display(payload.get("news_sub_type")),
                        display(payload.get("title")),
                        display(payload.get("source")),
                        link,
                    ]
                )
                + " |"
            )
    else:
        lines.append("| — | — | 暂无相关记录 | — | — |")

    lines += [
        "",
        "## Codex 增量维护记录",
        "",
        "基线之后发现的新增或修订内容由 `codex exec` 写入本节，并同步更新上方对应表格。",
        "",
        "- 尚无基线后的增量记录。",
        "",
    ]
    return "\n".join(lines)


def ensure_company_readme(company: Company) -> None:
    path = company.output_dir / "README.md"
    history_link = f"[{HISTORY_FILENAME}]({HISTORY_FILENAME})"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if HISTORY_FILENAME not in text:
            marker = f"\n## 分红与回购\n\n- 独立历史文件：{history_link}\n"
            first_heading = text.find("\n## ")
            if first_heading >= 0:
                text = text[:first_heading] + marker + text[first_heading:]
            else:
                text = text.rstrip() + marker + "\n"
        atomic_write_text(path, text)
        return
    atomic_write_text(
        path,
        "\n".join(
            [
                f"# {company.name}",
                "",
                f"- 证券：`{company.security_id}`（Futu：`{company.quote_code}`）",
                f"- 市场/币种：{company.market} / {company.currency}",
                f"- 行业：{company.sector} / {company.industry}",
                f"- 分红回购历史：{history_link}",
                "",
                "本目录只保存与该公司有关的长期研究和分红回购更新。",
                "",
            ]
        ),
    )


def render_start_here(companies: Iterable[Company]) -> None:
    rows = sorted(companies, key=lambda item: (item.market, item.security_id))
    lines = [
        "# 67 家公司分红回购索引",
        "",
        "本索引只覆盖 `data/source/companies.json` 中的正式 67 家公司。每家公司都有独立的",
        f"`{HISTORY_FILENAME}`，初始化基线由程序生成，之后的新增或修订事实由 `codex exec` 维护。",
        "",
        "| 公司 | 证券 | 市场 | 行业 | 分红回购历史 |",
        "|---|---|---|---|---|",
    ]
    for company in rows:
        relative = f"companies/{company.output_dir.name}/{HISTORY_FILENAME}"
        lines.append(
            f"| {display(company.name)} | `{company.security_id}` | {display(company.market)} | "
            f"{display(company.sector)} / {display(company.industry)} | [打开]({relative}) |"
        )
    lines.append("")
    atomic_write_text(ROOT / "outputs" / "START_HERE.md", "\n".join(lines))


def render_baseline(connection: sqlite3.Connection, companies: Iterable[Company]) -> None:
    company_list = list(companies)
    for company in company_list:
        company.output_dir.mkdir(parents=True, exist_ok=True)
        ensure_company_readme(company)
        atomic_write_text(
            company.output_dir / HISTORY_FILENAME,
            history_markdown(connection, company),
        )
    render_start_here(company_list)


def unsynced_revisions(
    connection: sqlite3.Connection, issuer_id: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT revision_id, event_key, change_kind, payload_json, observed_at
        FROM event_revisions
        WHERE issuer_id=? AND synced_at IS NULL
        ORDER BY revision_id
        """,
        (issuer_id,),
    ).fetchall()
    return [
        {
            "revision_id": int(row["revision_id"]),
            "event_key": row["event_key"],
            "change_kind": row["change_kind"],
            "payload": json.loads(row["payload_json"]),
            "observed_at": row["observed_at"],
        }
        for row in rows
    ]


def codex_prompt(
    connection: sqlite3.Connection,
    company: Company,
    revisions: list[dict[str, Any]],
) -> str:
    buyback_summary = annual_buyback_summary(
        event_rows(connection, company.issuer_id, "buyback")
    )
    package = {
        "company": {
            "name": company.name,
            "security_id": company.security_id,
            "quote_code": company.quote_code,
            "currency": company.currency,
        },
        "revisions": revisions,
        "current_buyback_annual_summary": buyback_summary,
    }
    return f"""你在维护当前目录中的 `{HISTORY_FILENAME}`。只允许编辑这一个文件，不得创建、删除或修改其他文件。

任务：把下面 JSON 中经过 Futu OpenD 检出的新增或修订事实写入历史文件。

要求：
1. 只陈述 JSON 明确提供的事实，不推测动机、未来分红或回购承诺。
2. dividend：新增或更新“分红事件”表格；相同公告日和方案不得重复。
3. buyback：按给定的 current_buyback_annual_summary 更新“回购年度汇总”对应年份。
4. financial：更新“财报数据期”；它只代表结构化财报出现新报告期。
5. news：若确与分红、回购、注销或财报有关，加入“相关公告与资讯”；保留来源链接。
6. 在“Codex 增量维护记录”最上方增加一个以 observed_at 日期命名的小节，用简洁中文说明本次入库内容。不要输出投资建议。
7. 保留文件中其他历史内容、口径说明和既有链接。
8. 完成写入后直接退出，不运行 Git 命令，不创建额外文件，也无需向用户发送消息。

JSON：
{json.dumps(package, ensure_ascii=False, indent=2)}
"""


def run_codex_for_company(
    connection: sqlite3.Connection,
    company: Company,
    *,
    codex_binary: Path,
    timeout_seconds: int,
) -> bool:
    revisions = unsynced_revisions(connection, company.issuer_id)
    if not revisions:
        return True
    history_path = company.output_dir / HISTORY_FILENAME
    if not history_path.exists():
        raise RuntimeError(f"历史文件不存在：{history_path}")
    prompt = codex_prompt(connection, company, revisions)
    before = hashlib.sha256(history_path.read_bytes()).hexdigest()
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"{company.issuer_id}-", dir=STAGING_ROOT
    ) as temporary:
        staging_dir = Path(temporary)
        staging_history = staging_dir / HISTORY_FILENAME
        shutil.copy2(history_path, staging_history)
        command = [
            str(codex_binary),
            "--ask-for-approval",
            "never",
            "exec",
            "--ephemeral",
            "--model",
            "gpt-5.6-sol",
            "--config",
            'model_reasoning_effort="xhigh"',
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "--cd",
            str(staging_dir),
            "-",
        ]
        process = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        if process.returncode != 0:
            print(
                f"Codex更新失败 {company.name}: {process.stderr[-2000:]}",
                file=sys.stderr,
                flush=True,
            )
            return False
        staged_entries = list(staging_dir.iterdir())
        if staged_entries != [staging_history]:
            print(f"Codex创建了额外文件 {company.name}", file=sys.stderr, flush=True)
            return False
        after = hashlib.sha256(staging_history.read_bytes()).hexdigest()
        if before == after:
            print(f"Codex未修改历史文件 {company.name}", file=sys.stderr, flush=True)
            return False
        required = (
            "## 分红事件",
            "## 回购年度汇总",
            "## 财报数据期",
            "## 相关公告与资讯",
            "## Codex 增量维护记录",
        )
        updated = staging_history.read_text(encoding="utf-8")
        if any(marker not in updated for marker in required):
            print(f"Codex破坏了历史文件结构 {company.name}", file=sys.stderr, flush=True)
            return False
        atomic_write_text(history_path, updated)
    ids = [int(row["revision_id"]) for row in revisions]
    placeholders = ",".join("?" for _ in ids)
    connection.execute(
        f"UPDATE event_revisions SET synced_at=? WHERE revision_id IN ({placeholders})",
        [now_iso(), *ids],
    )
    connection.commit()
    return True


def write_raw_snapshot(company: Company, snapshot: Mapping[str, Any], *, changed: bool) -> None:
    company_root = RAW_ROOT / company.issuer_id
    atomic_write_json(company_root / "latest.json", snapshot)
    if changed:
        timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
        atomic_write_json(company_root / "snapshots" / f"{timestamp}.json", snapshot)


def selected_companies(companies: list[Company], selector: str | None) -> list[Company]:
    if not selector:
        return companies
    matches = [
        item
        for item in companies
        if selector in {item.issuer_id, item.security_id, item.quote_code, item.name}
    ]
    if len(matches) != 1:
        raise ValueError(f"--company应唯一匹配一家公司，当前匹配{len(matches)}家")
    return matches


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterable[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("已有分红回购监控任务正在运行") from error
        yield


def collect(
    mode: str,
    *,
    selector: str | None,
    no_codex: bool,
    host: str,
    port: int,
    codex_binary: Path,
    codex_timeout: int,
) -> int:
    companies = load_companies()
    chosen = selected_companies(companies, selector)
    with exclusive_lock(LOCK_PATH), connect_database() as connection:
        sync_companies(connection, companies)
        existing_events = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        if mode == "bootstrap" and existing_events:
            raise RuntimeError("数据库已有事件；bootstrap只允许在空数据库上运行")
        run_id = connection.execute(
            "INSERT INTO runs(started_at, mode, status) VALUES (?, ?, 'running')",
            (now_iso(), mode),
        ).lastrowid
        connection.commit()
        provider = FutuProvider(host=host, port=port)
        total_revisions = 0
        failures: list[str] = []
        try:
            for index, company in enumerate(chosen, start=1):
                print(f"[{index}/{len(chosen)}] {company.quote_code} {company.name}", flush=True)
                snapshot = provider.fetch(company, full_buybacks=(mode == "bootstrap"))
                events = normalize_events(company, snapshot)
                revisions = ingest_events(
                    connection, events, baseline=(mode == "bootstrap")
                )
                total_revisions += revisions
                write_raw_snapshot(
                    company,
                    snapshot,
                    changed=(mode == "bootstrap" or revisions > 0),
                )
                if snapshot.get("errors"):
                    print(
                        f"  部分接口不可用: {stable_json(snapshot['errors'])}",
                        file=sys.stderr,
                        flush=True,
                    )
            if mode == "bootstrap":
                render_baseline(connection, companies)
            elif not no_codex:
                for company in chosen:
                    if not run_codex_for_company(
                        connection,
                        company,
                        codex_binary=codex_binary,
                        timeout_seconds=codex_timeout,
                    ):
                        failures.append(company.name)
            status = "success" if not failures else "partial"
            connection.execute(
                """
                UPDATE runs SET finished_at=?, status=?, companies_checked=?,
                    revisions_found=?, error=? WHERE run_id=?
                """,
                (
                    now_iso(),
                    status,
                    len(chosen),
                    total_revisions,
                    "Codex待重试：" + "、".join(failures) if failures else None,
                    run_id,
                ),
            )
            connection.commit()
            print(
                f"完成：公司{len(chosen)}家，新增/修订{total_revisions}条，"
                f"Codex待重试{len(failures)}家",
                flush=True,
            )
            return 0 if not failures else 2
        except Exception as error:
            connection.execute(
                "UPDATE runs SET finished_at=?, status='failed', error=? WHERE run_id=?",
                (now_iso(), str(error), run_id),
            )
            connection.commit()
            raise
        finally:
            provider.close()


def check_project() -> int:
    companies = load_companies()
    expected_dirs = {item.output_dir.name for item in companies}
    actual_dirs = {
        path.name for path in OUTPUT_ROOT.iterdir() if path.is_dir()
    } if OUTPUT_ROOT.exists() else set()
    errors: list[str] = []
    if actual_dirs != expected_dirs:
        errors.append(
            f"公司目录不一致，缺少={sorted(expected_dirs-actual_dirs)}，"
            f"多余={sorted(actual_dirs-expected_dirs)}"
        )
    for company in companies:
        for filename in ("README.md", HISTORY_FILENAME):
            if not (company.output_dir / filename).is_file():
                errors.append(f"缺少 {company.output_dir / filename}")
    if DATABASE_PATH.exists():
        with connect_database() as connection:
            company_count = int(connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
            if company_count != 67:
                errors.append(f"数据库公司数应为67，当前为{company_count}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("检查通过：67家公司目录和历史文件完整")
    return 0


def show_status() -> int:
    with connect_database() as connection:
        counts = {
            "companies": connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
            "events": connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "unsynced_revisions": connection.execute(
                "SELECT COUNT(*) FROM event_revisions WHERE synced_at IS NULL"
            ).fetchone()[0],
        }
        latest = connection.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    print(json.dumps({"counts": counts, "latest_run": dict(latest) if latest else None}, ensure_ascii=False, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="67家公司分红回购增量监控")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("bootstrap", "run"):
        command = subparsers.add_parser(name)
        command.add_argument("--company", help="只运行一个issuerId/securityId/公司名")
        command.add_argument("--futu-host", default=os.getenv("FUTU_HOST", "127.0.0.1"))
        command.add_argument("--futu-port", type=int, default=int(os.getenv("FUTU_PORT", "11111")))
        command.add_argument("--no-codex", action="store_true")
        command.add_argument("--codex-binary", type=Path, default=DEFAULT_CODEX)
        command.add_argument("--codex-timeout", type=int, default=900)
    subparsers.add_parser("check")
    subparsers.add_parser("status")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "check":
        return check_project()
    if args.command == "status":
        return show_status()
    return collect(
        args.command,
        selector=args.company,
        no_codex=args.no_codex,
        host=args.futu_host,
        port=args.futu_port,
        codex_binary=args.codex_binary,
        codex_timeout=args.codex_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
