from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "dividend_buyback_monitor.py"
SPEC = importlib.util.spec_from_file_location("dividend_buyback_monitor", MODULE_PATH)
assert SPEC and SPEC.loader
monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)


def company() -> monitor.Company:
    return monitor.Company(
        issuer_id="HK0700",
        security_id="HK0700",
        quote_code="HK.00700",
        name="腾讯控股",
        ticker="00700",
        market="HK",
        currency="HKD",
        sector="互联网",
        industry="平台",
    )


def test_dividend_revision_keeps_identity_and_detects_status_change(tmp_path: Path) -> None:
    item = company()
    first = {
        "dividends": [
            {
                "pub_date": "2026/03/18",
                "statement": "末期息5.3港元;",
                "process": "预案",
                "record_date": "2026/05/18",
            }
        ]
    }
    second = json.loads(json.dumps(first))
    second["dividends"][0]["process"] = "方案实施"
    first_event = monitor.normalize_events(item, first)[0]
    second_event = monitor.normalize_events(item, second)[0]
    assert first_event["event_key"] == second_event["event_key"]
    assert first_event["payload_hash"] != second_event["payload_hash"]

    connection = monitor.connect_database(tmp_path / "monitor.sqlite3")
    monitor.sync_companies(connection, [item])
    assert monitor.ingest_events(connection, [first_event], baseline=True) == 1
    assert monitor.unsynced_revisions(connection, item.issuer_id) == []
    assert monitor.ingest_events(connection, [second_event], baseline=False) == 1
    revisions = monitor.unsynced_revisions(connection, item.issuer_id)
    assert len(revisions) == 1
    assert revisions[0]["change_kind"] == "changed"
    assert revisions[0]["payload"]["process"] == "方案实施"


def test_history_markdown_uses_event_dividends_and_annual_buyback_summary(
    tmp_path: Path,
) -> None:
    item = company()
    snapshot = {
        "dividends": [
            {
                "pub_date": "2026/03/18",
                "statement": "末期息5.3港元;",
                "process": "方案实施",
                "record_date": "2026/05/18",
                "ex_date": "2026/05/15",
                "dividend_payable_date": "2026/06/01",
            }
        ],
        "buybacks": [
            {
                "record_market": "HK",
                "publ_date_str": "2026-07-09",
                "end_date_str": "2026-07-09",
                "buy_back_money": 500_000_000.0,
                "buy_back_sum": 1_000_000,
                "share_type": "普通股",
            },
            {
                "record_market": "HK",
                "publ_date_str": "2026-07-10",
                "end_date_str": "2026-07-10",
                "buy_back_money": 300_000_000.0,
                "buy_back_sum": 600_000,
                "share_type": "普通股",
            },
        ],
        "financials": [
            {
                "date_time_str": "2025-12-31",
                "fiscal_year": 2025,
                "period_text": "2025/FY",
                "currency_code": "CNY",
                "accounting_standards": "IFRS",
                "auditor_report": "无保留意见",
            }
        ],
        "news": [
            {
                "title": "腾讯控股回购股份",
                "news_sub_type": "NOTICE",
                "source": "香港交易所",
                "publish_time": "2026-07-10",
                "url": "https://example.invalid/notice",
            }
        ],
    }
    connection = monitor.connect_database(tmp_path / "monitor.sqlite3")
    monitor.sync_companies(connection, [item])
    monitor.ingest_events(
        connection, monitor.normalize_events(item, snapshot), baseline=True
    )
    text = monitor.history_markdown(connection, item)
    assert "末期息5.3港元" in text
    assert "| 2026 | 2 | 800,000,000 | 1,600,000 | 普通股 |" in text
    assert "2025/FY" in text
    assert "腾讯控股回购股份" in text
    assert "## Codex 增量维护记录" in text


def test_news_filter_only_keeps_relevant_titles() -> None:
    item = company()
    snapshot = {
        "news": [
            {
                "title": "公司宣布股份回购",
                "url": "https://example.invalid/a",
                "view_count": 100,
            },
            {"title": "新游戏上线", "url": "https://example.invalid/b"},
        ]
    }
    # The provider performs title filtering; normalize must preserve whatever
    # the provider has already admitted without inventing additional fields.
    filtered = {
        **snapshot,
        "news": [row for row in snapshot["news"] if monitor.NEWS_KEYWORDS.search(row["title"])],
    }
    events = monitor.normalize_events(item, filtered)
    assert len(events) == 1
    assert events[0]["event_type"] == "news"
    assert "view_count" not in events[0]["payload"]


def test_news_rate_limit_keeps_one_call_of_headroom() -> None:
    limiter = monitor.InterfaceRateLimiter()
    assert limiter.maximum == 28
    assert limiter.interface_maximums["news"] == 9


def test_same_day_hk_buybacks_remain_distinct() -> None:
    item = company()
    snapshot = {
        "buybacks": [
            {
                "record_market": "HK",
                "end_date_str": "2026-07-29",
                "buy_back_money": 1_695_420.19,
                "buy_back_sum": 4_700,
                "share_type": "普通股",
            },
            {
                "record_market": "HK",
                "end_date_str": "2026-07-29",
                "buy_back_money": 7_703_991.23,
                "buy_back_sum": 21_600,
                "share_type": "普通股",
            },
        ]
    }
    events = monitor.normalize_events(item, snapshot)
    assert len(events) == 2
    assert events[0]["event_key"] != events[1]["event_key"]


def test_duplicate_event_versions_in_one_snapshot_keep_last() -> None:
    item = company()
    base = {
        "record_market": "A",
        "advance_date_str": "2026-01-01",
        "seller": "公司",
        "buy_back_mode": "集中竞价",
        "share_type": "普通股",
    }
    events = monitor.normalize_events(
        item,
        {
            "buybacks": [
                {**base, "event_proce_desc": "预案"},
                {**base, "event_proce_desc": "实施完成"},
            ]
        },
    )
    assert len(events) == 1
    assert events[0]["payload"]["event_proce_desc"] == "实施完成"


def test_codex_uses_staging_and_only_applies_history(tmp_path: Path, monkeypatch) -> None:
    item = company()
    monkeypatch.setattr(monitor, "OUTPUT_ROOT", tmp_path / "outputs")
    monkeypatch.setattr(monitor, "STAGING_ROOT", tmp_path / "staging")
    item.output_dir.mkdir(parents=True)
    history = item.output_dir / monitor.HISTORY_FILENAME
    history.write_text(
        "\n".join(
            [
                "# 测试",
                "## 分红事件",
                "## 回购年度汇总",
                "## 财报数据期",
                "## 相关公告与资讯",
                "## Codex 增量维护记录",
            ]
        ),
        encoding="utf-8",
    )
    readme = item.output_dir / "README.md"
    readme.write_text("keep", encoding="utf-8")
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import sys
from pathlib import Path
root = Path(sys.argv[sys.argv.index('--cd') + 1])
path = root / '分红回购历史.md'
path.write_text(path.read_text(encoding='utf-8') + '\\n已维护\\n', encoding='utf-8')
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)

    connection = monitor.connect_database(tmp_path / "monitor.sqlite3")
    monitor.sync_companies(connection, [item])
    event = monitor.normalize_events(
        item,
        {"dividends": [{"pub_date": "2026/07/31", "statement": "测试分红"}]},
    )[0]
    monitor.ingest_events(connection, [event], baseline=False)
    assert monitor.run_codex_for_company(
        connection,
        item,
        codex_binary=fake_codex,
        timeout_seconds=10,
    )
    assert history.read_text(encoding="utf-8").endswith("已维护\n")
    assert readme.read_text(encoding="utf-8") == "keep"
    assert monitor.unsynced_revisions(connection, item.issuer_id) == []
    assert list(monitor.STAGING_ROOT.iterdir()) == []
