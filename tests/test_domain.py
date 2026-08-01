from __future__ import annotations

import json
from pathlib import Path

from app.domain import build_watchlist_data, normalize_config


WEBAPP_ROOT = Path(__file__).resolve().parents[1]


def security(
    security_id: str,
    sector: str,
    *,
    issuer_id: str | None = None,
    current_price: float | None = 100,
    preferred_price: float | None = 100,
    daily_change_pct: float | None = 0,
    technical: dict | None = None,
) -> dict:
    result = {
        "id": security_id,
        "issuerId": issuer_id or security_id,
        "name": f"{security_id}（虚构）",
        "ticker": f"DEMO-{security_id}",
        "market": "CN",
        "currency": "CNY",
        "sector": sector,
        "industry": f"{sector}（虚构）",
        "quote": {
            "currentPrice": current_price,
            "dailyChangePct": daily_change_pct,
            "marketState": "demo",
            "lastUpdatedAt": None,
            "status": "fictional",
        },
        "targetPrices": {
            "watch": preferred_price * 1.1 if preferred_price else None,
            "preferred": preferred_price,
            "deep": preferred_price * 0.8 if preferred_price else None,
        },
        "expectedDividendYieldPct": 5,
        "valuationStatus": "attractive",
        "investmentThesis": [],
        "risks": [],
        "history": [],
        "targetRevisionHistory": [],
    }
    if technical is not None:
        result["technicalIndicators"] = technical
    return result


def demo_config(securities: list[dict], sector_signals: dict | None = None) -> dict:
    return {
        "schemaVersion": 1,
        "mode": "demo",
        "isDemo": True,
        "title": "虚构测试",
        "disclaimer": "虚构测试数据，不是实时行情。",
        "refreshIntervalMs": 60000,
        "marketData": {
            "provider": "fictional_fixture",
            "realtime": False,
            "status": "fictional_demo",
        },
        "sectorSignals": sector_signals or {},
        "securities": securities,
    }


def sector_signal(
    *,
    r1: float,
    r5: float,
    r20: float,
    breadth5: float,
    above_ma20: float,
) -> dict:
    return {
        "return1dPct": r1,
        "return5dPct": r5,
        "return20dPct": r20,
        "advancers5dPct": breadth5,
        "aboveMa20Pct": above_ma20,
        "issuerCount": 3,
        "coveredIssuerCount": 3,
        "coveragePct": 100,
        "asOf": "虚构时点",
    }


def test_official_live_config_has_exact_requested_universe_and_never_demo() -> None:
    raw = json.loads((WEBAPP_ROOT / "config" / "watchlist.json").read_text())
    config = normalize_config(raw, "official live config")
    result = build_watchlist_data(config)

    assert config["mode"] == "live"
    assert config["isDemo"] is False
    assert config["refreshIntervalMs"] == 60000
    assert len(config["securities"]) == 67
    assert len({item["issuerId"] for item in config["securities"]}) == 67
    assert len({item["id"] for item in config["securities"]}) == 67
    assert len({item["quoteCode"] for item in config["securities"]}) == 67
    assert sum(item["market"] == "CN" for item in config["securities"]) == 53
    assert sum(item["market"] == "HK" for item in config["securities"]) == 14
    assert sum(
        item["yieldBasis"]["annualAveragePerShareCny"] is not None
        for item in config["securities"]
    ) == 22
    assert config["securities"][0]["quoteCode"] == "SH.600900"
    assert config["securities"][-1]["quoteCode"] == "SH.688235"
    assert result["summary"]["totalSecurities"] == 67
    assert result["summary"]["priceAvailableCount"] == 0
    assert result["summary"]["peAvailableCount"] == 0
    assert result["summary"]["pbAvailableCount"] == 0
    assert result["summary"]["futuMetricCompleteCount"] == 0
    assert result["summary"]["averageDistanceToPreferredPct"] is None


def test_shareholder_yield_model_derives_hk_targets_cny_price_and_status() -> None:
    item = security("hk-yield", "公用事业", current_price=10)
    item.update(
        {
            "market": "HK",
            "currency": "HKD",
            "targetPrices": {
                "watch": 999,
                "preferred": 999,
                "deep": 999,
            },
            "yieldBasis": {
                "annualAveragePerShareCny": 0.36,
                "windowYears": 10,
                "startYear": 2016,
                "endYear": 2025,
                "method": "测试口径",
            },
        }
    )
    raw = demo_config([item])
    raw.update(
        {
            "mode": "live",
            "isDemo": False,
            "disclaimer": "不构成投资建议。",
            "marketData": {
                "provider": "test",
                "realtime": True,
                "status": "ok",
                "fxRates": {
                    "HKD_CNY": {
                        "rate": 0.9,
                        "asOf": "2026-07-30",
                        "fetchedAt": "2026-07-31T00:00:00Z",
                        "source": "test",
                    }
                },
            },
        }
    )

    result = build_watchlist_data(normalize_config(raw, "yield model test"))
    derived = result["securities"][0]

    assert derived["quote"]["currentPriceCny"] == 9
    assert derived["targetPricesCny"] == {
        "watch": 12,
        "preferred": 9,
        "deep": 7.2,
    }
    assert derived["targetPrices"] == {
        "watch": 13.3333,
        "preferred": 10,
        "deep": 8,
    }
    assert derived["currentShareholderYieldPct"] == 4
    assert derived["valuationStatus"] == "attractive"
    assert derived["derived"]["targetStatus"] == "reached"
    assert derived["derived"]["alertStatus"] == "buy_zone"
    assert result["summary"]["targetConfiguredCount"] == 1
    assert result["market"]["fxRates"]["HKD_CNY"]["rate"] == 0.9


def test_official_demo_is_explicitly_fictional_and_exercises_strict_signals() -> None:
    raw = json.loads(
        (WEBAPP_ROOT / "config" / "demo-watchlist.json").read_text()
    )
    config = normalize_config(raw, "official demo config")
    result = build_watchlist_data(config)

    assert config["isDemo"] is True
    assert config["marketData"]["realtime"] is False
    assert "虚构" in config["disclaimer"]
    assert len(result["securities"]) == 6
    assert len(result["sectors"]) == 2
    assert all(item["issuerCount"] == 3 for item in result["sectors"])
    assert all(item["heatScore"] is not None for item in result["sectors"])
    assert result["summary"]["hotSectorDislocationCount"] >= 1
    for item in result["securities"]:
        assert "虚构" in item["name"]
        assert item["ticker"].startswith("DEMO-")
        assert item["quote"]["status"] == "fictional"
        assert item["quote"]["lastUpdatedAt"] is None


def test_target_buckets_are_mutually_exclusive_and_sorted() -> None:
    raw = demo_config(
        [
            security("far", "行业", current_price=125),
            security("within10", "行业", current_price=107),
            security("within3", "行业", current_price=102),
            security("reached", "行业", current_price=95),
            security(
                "unconfigured",
                "行业",
                current_price=20,
                preferred_price=None,
            ),
        ]
    )
    result = build_watchlist_data(normalize_config(raw, "target test"))

    assert [item["id"] for item in result["securities"]] == [
        "reached",
        "within3",
        "within10",
        "far",
        "unconfigured",
    ]
    assert [
        item["derived"]["targetStatus"] for item in result["securities"]
    ] == ["reached", "within_3", "within_10", "far", "unconfigured"]
    assert result["summary"]["reachedTargetCount"] == 1
    assert result["summary"]["within3PctCount"] == 1
    assert result["summary"]["within10PctCount"] == 1
    assert result["summary"]["atOrWithin10PctCount"] == 3


def test_sector_heat_requires_history_three_issuers_and_80_percent_coverage() -> None:
    securities = [
        security(f"hot-{index}", "热行业", issuer_id=f"hot-issuer-{index}")
        for index in range(3)
    ] + [
        security(f"cold-{index}", "冷行业", issuer_id=f"cold-issuer-{index}")
        for index in range(3)
    ]
    signals = {
        "热行业": sector_signal(
            r1=2, r5=5, r20=10, breadth5=100, above_ma20=100
        ),
        "冷行业": sector_signal(
            r1=-2, r5=-5, r20=-10, breadth5=0, above_ma20=0
        ),
    }
    result = build_watchlist_data(
        normalize_config(demo_config(securities, signals), "heat test")
    )
    sectors = {item["sector"]: item for item in result["sectors"]}

    assert sectors["热行业"]["heatScore"] == 100
    assert sectors["热行业"]["heatStatus"] == "computed"
    assert sectors["冷行业"]["heatScore"] == 0
    assert sectors["热行业"]["rank"] == 1

    signals["热行业"]["coveredIssuerCount"] = 2
    signals["热行业"]["coveragePct"] = 66.7
    insufficient = build_watchlist_data(
        normalize_config(demo_config(securities, signals), "coverage test")
    )
    heat = next(
        item for item in insufficient["sectors"] if item["sector"] == "热行业"
    )
    assert heat["heatScore"] is None
    assert heat["heatStatus"] == "insufficient_coverage"
    assert heat["rank"] is None


def test_daily_change_does_not_fabricate_sector_heat() -> None:
    securities = [
        security(
            f"issuer-{index}",
            "无历史行业",
            daily_change_pct=9 - index,
        )
        for index in range(3)
    ]
    result = build_watchlist_data(
        normalize_config(demo_config(securities), "no history test")
    )
    sector = result["sectors"][0]

    assert sector["averageDailyChangePct"] == 8
    assert sector["heatScore"] is None
    assert sector["heatStatus"] == "missing_history"
    for item in result["securities"]:
        assert item["derived"]["sectorHeatScore"] is None
        assert item["derived"]["opportunityTechnical"] is None
        assert item["derived"]["hotSectorDislocation"] is None

    securities[0]["expectedDividendYieldPct"] = None
    securities[0]["valuationStatus"] = "unconfigured"
    separated = build_watchlist_data(
        normalize_config(demo_config(securities), "price-only test")
    )
    first = next(
        item for item in separated["securities"] if item["id"] == "issuer-0"
    )
    assert first["derived"]["opportunityScore"] is not None
    assert first["derived"]["opportunityComponents"]["expectedDividend"] is None
    assert first["derived"]["opportunityComponents"]["valuation"] is None


def test_technical_oversold_and_hot_sector_dislocation_need_all_inputs() -> None:
    complete = {
        "rsi14": 31,
        "drawdown60dPct": -18,
        "relativeSector5dPct": -6.3,
        "historyTradingDays": 120,
        "asOf": "虚构时点",
    }
    securities = [
        security(
            "oversold",
            "热行业",
            issuer_id="issuer-1",
            current_price=98,
            technical=complete,
        ),
        security("peer-2", "热行业", issuer_id="issuer-2"),
        security("peer-3", "热行业", issuer_id="issuer-3"),
        security("cold-1", "冷行业", issuer_id="cold-1"),
        security("cold-2", "冷行业", issuer_id="cold-2"),
        security("cold-3", "冷行业", issuer_id="cold-3"),
    ]
    signals = {
        "热行业": sector_signal(
            r1=2, r5=5, r20=10, breadth5=100, above_ma20=100
        ),
        "冷行业": sector_signal(
            r1=-2, r5=-5, r20=-10, breadth5=0, above_ma20=0
        ),
    }
    result = build_watchlist_data(
        normalize_config(demo_config(securities, signals), "technical test")
    )
    oversold = next(
        item for item in result["securities"] if item["id"] == "oversold"
    )
    peer = next(item for item in result["securities"] if item["id"] == "peer-2")

    assert oversold["derived"]["technical"] is not None
    assert oversold["derived"]["technicalOversold"] is True
    assert oversold["derived"]["opportunityTechnical"] is not None
    assert oversold["derived"]["hotSectorDislocation"] is True
    assert peer["derived"]["technical"] is None
    assert peer["derived"]["opportunityTechnical"] is None
    assert peer["derived"]["hotSectorDislocation"] is None
