from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.main import create_app


FIXED_NOW = datetime(2026, 7, 31, tzinfo=timezone.utc)


class ASGIClient:
    """Small sync facade over httpx's ASGI transport.

    Starlette 1.3 has moved its bundled TestClient to httpx2; using the
    transport directly keeps this test compatible with the locked httpx 0.28
    development dependency.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.app.state.watchlist_store.initialize()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def request(self, method: str, path: str) -> httpx.Response:
        async def run() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.request(method, path)

        return asyncio.run(run())

    def get(self, path: str) -> httpx.Response:
        return self.request("GET", path)

    def post(self, path: str) -> httpx.Response:
        return self.request("POST", path)

    def head(self, path: str) -> httpx.Response:
        return self.request("HEAD", path)


def live_config(*, with_security: bool = False) -> dict:
    securities = []
    if with_security:
        securities.append(
            {
                "id": "live-one",
                "issuerId": "issuer-one",
                "quoteCode": "SH.600000",
                "name": "正式测试标的",
                "ticker": "600000",
                "market": "CN",
                "currency": "CNY",
                "sector": "金融",
                "industry": "银行",
                "quote": {
                    "currentPrice": None,
                    "dailyChangePct": None,
                    "marketState": "unknown",
                    "lastUpdatedAt": None,
                    "status": "unavailable",
                },
                "targetPrices": {
                    "watch": 11,
                    "preferred": 10,
                    "deep": 8,
                },
                "expectedDividendYieldPct": None,
                "valuationStatus": "unconfigured",
                "investmentThesis": [],
                "risks": [],
                "targetRevisionHistory": [],
                "history": [],
            }
        )
    return {
        "schemaVersion": 1,
        "mode": "live",
        "isDemo": False,
        "title": "正式观察清单",
        "description": "",
        "disclaimer": "不构成投资建议。",
        "refreshIntervalMs": 60000,
        "marketData": {
            "provider": None,
            "realtime": False,
            "status": "not_configured",
        },
        "securities": securities,
    }


def demo_config() -> dict:
    return {
        "schemaVersion": 1,
        "mode": "demo",
        "isDemo": True,
        "title": "虚构 API 演示",
        "description": "虚构测试",
        "disclaimer": "虚构测试数据，不是实时行情。",
        "refreshIntervalMs": 60000,
        "marketData": {
            "provider": "fictional_fixture",
            "realtime": False,
            "status": "fictional_demo",
        },
        "securities": [
            {
                "id": "demo-one",
                "name": "演示一号（虚构）",
                "ticker": "DEMO-ONE",
                "market": "CN",
                "currency": "CNY",
                "sector": "公用事业（虚构）",
                "industry": "电力（虚构）",
                "quote": {
                    "currentPrice": 9.8,
                    "dailyChangePct": -1,
                    "marketState": "demo",
                    "lastUpdatedAt": None,
                    "status": "fictional",
                },
                "targetPrices": {
                    "watch": 11,
                    "preferred": 10,
                    "deep": 8,
                },
                "expectedDividendYieldPct": 5,
                "valuationStatus": "attractive",
                "investmentThesis": ["虚构逻辑"],
                "risks": ["虚构风险"],
                "targetRevisionHistory": [],
                "history": [{"label": "T", "price": 9.8}],
            }
        ],
    }


def history_document() -> dict:
    points = [
        {"timestamp": "2026-07-20", "label": "2026-07", "price": 10.0},
        {"timestamp": "2026-07-27", "label": "2026-07", "price": 9.8},
    ]
    return {
        "schemaVersion": 1,
        "generatedAt": "2026-07-31T00:00:00.000Z",
        "provider": "futu-opend",
        "frequency": "weekly",
        "adjustment": "qfq",
        "windowYears": 10,
        "windowStart": "2016-07-31",
        "windowEnd": "2026-07-31",
        "securityIds": ["live-one"],
        "securities": {
            "live-one": {
                "quoteCode": "SH.600000",
                "currency": "CNY",
                "frequency": "weekly",
                "adjustment": "qfq",
                "windowYears": 10,
                "asOf": "2026-07-27",
                "pointCount": len(points),
                "points": points,
            }
        },
    }


def build_client(
    tmp_path: Path,
    *,
    with_security: bool = False,
    with_history: bool = False,
):
    config_dir = tmp_path / "config"
    public_dir = tmp_path / "public"
    runtime_dir = tmp_path / "runtime"
    config_dir.mkdir()
    public_dir.mkdir()
    runtime_dir.mkdir()
    live_path = config_dir / "watchlist.json"
    demo_path = config_dir / "demo-watchlist.json"
    snapshot_path = runtime_dir / "latest_snapshot.json"
    history_path = runtime_dir / "weekly_history.json"
    live_path.write_text(
        json.dumps(live_config(with_security=with_security)),
        encoding="utf-8",
    )
    demo_path.write_text(json.dumps(demo_config()), encoding="utf-8")
    (public_dir / "index.html").write_text(
        "<!doctype html><title>Liberty test SPA</title>", encoding="utf-8"
    )
    (public_dir / "app.js").write_text(
        "globalThis.libertyTest = true;", encoding="utf-8"
    )
    if with_history:
        history_path.write_text(json.dumps(history_document()), encoding="utf-8")
    app = create_app(
        config_path=live_path,
        demo_config_path=demo_path,
        snapshot_path=snapshot_path,
        history_path=history_path,
        public_dir=public_dir,
        now=lambda: FIXED_NOW,
    )
    return ASGIClient(app), snapshot_path


def test_health_readiness_live_contract_and_explicit_demo(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    with client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["checks"]["configLoaded"] is True

        live = client.get("/api/watchlist")
        assert live.status_code == 200
        payload = live.json()
        assert set(("meta", "market", "summary", "sectors", "securities")) <= set(
            payload
        )
        assert payload["meta"]["isDemo"] is False
        assert payload["meta"]["refreshIntervalMs"] == 60000
        assert payload["securities"] == []
        assert payload["summary"]["averageDistanceToPreferredPct"] is None

        not_demo = client.get("/api/watchlist?demo=true")
        assert not_demo.json()["meta"]["isDemo"] is False

        demo = client.get("/api/watchlist?demo=1")
        assert demo.status_code == 200
        assert demo.json()["meta"]["isDemo"] is True
        assert demo.json()["meta"]["isRealtime"] is False
        assert "虚构" in demo.json()["meta"]["disclaimer"]
        assert demo.json()["securities"][0]["id"] == "demo-one"

        detail = client.get("/api/securities/demo-one?demo=1")
        assert detail.status_code == 200
        assert detail.json()["security"]["targetLines"][1]["key"] == "preferred"

        missing = client.get("/api/securities/demo-one")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "security_not_found"

        assert live.headers["cache-control"] == "no-store"
        assert live.headers["x-content-type-options"] == "nosniff"
        assert live.headers["referrer-policy"] == "same-origin"
        assert live.headers["x-frame-options"] == "SAMEORIGIN"
        assert "script-src 'self'" in live.headers["content-security-policy"]


def test_runtime_snapshot_replaces_live_data_and_invalid_file_keeps_last_valid(
    tmp_path: Path,
) -> None:
    client, snapshot_path = build_client(tmp_path, with_security=True)
    snapshot = live_config(with_security=True)
    snapshot["snapshotGeneratedAt"] = "2026-07-31T00:00:00.000Z"
    snapshot["marketData"] = {
        "provider": "futu-opend",
        "realtime": True,
        "status": "ok",
        "collectedAt": "2026-07-31T00:00:00.000Z",
    }
    snapshot["securities"][0]["quote"] = {
        "currentPrice": 9.75,
        "dailyChangePct": -1.2,
        "marketState": "open",
        "lastUpdatedAt": "2026-07-31T00:00:00.000Z",
        "status": "available",
    }
    snapshot["securities"][0]["metrics"] = {
        "pe": 6.5,
        "peTtm": 6.2,
        "pb": 0.72,
        "dividendYieldTtmPct": 4.1,
        "totalMarketValue": 286_000_000_000,
        "earningsPerShare": 1.51,
        "bookValuePerShare": 13.54,
    }
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    with client:
        first = client.get("/api/watchlist").json()
        assert first["securities"][0]["currentPrice"] == 9.75
        assert first["securities"][0]["metrics"]["peTtm"] == 6.2
        assert first["summary"]["peAvailableCount"] == 1
        assert first["summary"]["pbAvailableCount"] == 1
        assert first["summary"]["futuMetricCompleteCount"] == 1
        assert first["coverage"]["peAvailableCount"] == 1
        assert first["meta"]["pricesRefreshed"] is True
        assert first["meta"]["isRealtime"] is True

        invalid = snapshot_path.with_name("invalid.tmp")
        invalid.write_text("{not valid json", encoding="utf-8")
        os.replace(invalid, snapshot_path)

        retained = client.get("/api/watchlist").json()
        assert retained["securities"][0]["currentPrice"] == 9.75
        assert "JSONDecodeError" in retained["meta"]["lastRefreshError"]
        assert client.get("/readyz").status_code == 200


def test_weekly_history_is_loaded_separately_and_served_per_security(
    tmp_path: Path,
) -> None:
    client, _ = build_client(
        tmp_path, with_security=True, with_history=True
    )
    with client:
        watchlist = client.get("/api/watchlist")
        assert watchlist.status_code == 200
        assert watchlist.json()["meta"]["historyAvailableCount"] == 1
        assert (
            watchlist.json()["meta"]["historyGeneratedAt"]
            == "2026-07-31T00:00:00.000Z"
        )
        assert watchlist.json()["securities"][0]["history"] == []

        history = client.get("/api/securities/live-one/history")
        assert history.status_code == 200
        assert history.json()["quoteCode"] == "SH.600000"
        assert history.json()["frequency"] == "weekly"
        assert history.json()["adjustment"] == "qfq"
        assert history.json()["pointCount"] == 2
        assert history.json()["points"][-1]["price"] == 9.8
        assert history.headers["cache-control"] == "no-store"

        demo = client.get("/api/securities/demo-one/history?demo=1")
        assert demo.status_code == 200
        assert demo.json()["provider"] == "fictional_fixture"


def test_missing_weekly_history_does_not_block_quotes(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path, with_security=True)
    with client:
        watchlist = client.get("/api/watchlist")
        assert watchlist.status_code == 200
        assert watchlist.json()["meta"]["historyAvailableCount"] == 0

        history = client.get("/api/securities/live-one/history")
        assert history.status_code == 503
        assert history.json()["error"]["code"] == "history_not_ready"


def test_stale_runtime_snapshot_cannot_replace_a_changed_watchlist(
    tmp_path: Path,
) -> None:
    client, snapshot_path = build_client(tmp_path, with_security=True)
    stale = live_config(with_security=False)
    stale["snapshotGeneratedAt"] = "2026-07-31T00:00:00.000Z"
    snapshot_path.write_text(json.dumps(stale), encoding="utf-8")

    with client:
        payload = client.get("/api/watchlist").json()
        assert [item["id"] for item in payload["securities"]] == ["live-one"]
        assert "与当前正式观察清单不匹配" in payload["meta"]["lastRefreshError"]


def test_unknown_api_and_read_only_methods(tmp_path: Path) -> None:
    client, _ = build_client(tmp_path)
    with client:
        unknown_api = client.get("/api/unknown")
        assert unknown_api.status_code == 404
        assert unknown_api.json()["error"]["code"] == "api_not_found"
        assert client.get("/api").json()["error"]["code"] == "api_not_found"
        method_error = client.post("/api/watchlist")
        assert method_error.status_code == 405
        assert method_error.json()["error"]["code"] == "method_not_allowed"
        assert client.head("/healthz").status_code == 200
