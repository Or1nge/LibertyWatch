from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.history import HistoryError, normalize_history_document
from collector.push_history import (
    _history_quota_details,
    build_history_document,
)


FIXED_NOW = datetime(2026, 7, 31, tzinfo=timezone.utc)


def _config() -> dict:
    return {
        "schemaVersion": 1,
        "mode": "live",
        "isDemo": False,
        "title": "测试",
        "description": "",
        "disclaimer": "不构成投资建议。",
        "refreshIntervalMs": 60000,
        "marketData": {
            "provider": None,
            "realtime": False,
            "status": "not_configured",
        },
        "securities": [
            {
                "id": "one",
                "issuerId": "issuer-one",
                "quoteCode": "SH.600000",
                "name": "测试标的",
                "ticker": "600000",
                "market": "CN",
                "currency": "CNY",
                "sector": "金融",
                "industry": "银行",
                "targetPrices": {"watch": None, "preferred": None, "deep": None},
                "expectedDividendYieldPct": None,
                "valuationStatus": "unconfigured",
                "investmentThesis": [],
                "risks": [],
                "targetRevisionHistory": [],
                "history": [],
            }
        ],
    }


def test_history_builder_requests_rolling_ten_year_weekly_series(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "watchlist.json"
    config_path.write_text(json.dumps(_config()), encoding="utf-8")
    requested = []

    def fake_fetcher(codes, **kwargs):
        requested.append((list(codes), kwargs))
        return {
            "SH.600000": [
                {
                    "timestamp": "2016-08-01",
                    "label": "2016-08",
                    "price": 8.0,
                },
                {
                    "timestamp": "2026-07-27",
                    "label": "2026-07",
                    "price": 10.0,
                },
            ]
        }

    document = build_history_document(
        config_path,
        fetcher=fake_fetcher,
        now=lambda: FIXED_NOW,
    )

    codes, options = requested[0]
    assert codes == ["SH.600000"]
    assert options["start"].isoformat() == "2016-07-31"
    assert options["end"].isoformat() == "2026-07-31"
    assert document["frequency"] == "weekly"
    assert document["adjustment"] == "qfq"
    assert document["securities"]["one"]["pointCount"] == 2


def test_history_contract_rejects_non_increasing_dates() -> None:
    raw = {
        "schemaVersion": 1,
        "generatedAt": "2026-07-31T00:00:00.000Z",
        "provider": "futu-opend",
        "frequency": "weekly",
        "adjustment": "qfq",
        "windowYears": 10,
        "securityIds": ["one"],
        "securities": {
            "one": {
                "quoteCode": "SH.600000",
                "currency": "CNY",
                "frequency": "weekly",
                "adjustment": "qfq",
                "windowYears": 10,
                "asOf": "2026-07-20",
                "pointCount": 2,
                "points": [
                    {"timestamp": "2026-07-20", "price": 10},
                    {"timestamp": "2026-07-20", "price": 9},
                ],
            }
        },
    }
    expected = {"one": {"quoteCode": "SH.600000", "currency": "CNY"}}

    with pytest.raises(HistoryError, match="严格递增"):
        normalize_history_document(raw, expected)


def test_history_contract_accepts_ordered_current_universe_subset() -> None:
    raw = {
        "schemaVersion": 1,
        "generatedAt": "2026-07-31T00:00:00.000Z",
        "provider": "futu-opend",
        "frequency": "weekly",
        "adjustment": "qfq",
        "windowYears": 10,
        "securityIds": ["kept"],
        "securities": {
            "kept": {
                "quoteCode": "SH.600000",
                "currency": "CNY",
                "frequency": "weekly",
                "adjustment": "qfq",
                "windowYears": 10,
                "asOf": "2026-07-27",
                "pointCount": 2,
                "points": [
                    {"timestamp": "2026-07-20", "price": 10},
                    {"timestamp": "2026-07-27", "price": 9},
                ],
            }
        },
    }
    expected = {
        "kept": {"quoteCode": "SH.600000", "currency": "CNY"},
        "waiting": {"quoteCode": "HK.00005", "currency": "HKD"},
    }

    normalized = normalize_history_document(raw, expected)
    assert normalized["securityIds"] == ["kept"]
    assert set(normalized["securities"]) == {"kept"}


def test_history_quota_details_tracks_recently_requested_codes() -> None:
    parsed = _history_quota_details(
        (
            1,
            99,
            [{"code": "HK.01052"}, {"code": "SH.600000"}],
        )
    )
    assert parsed == (1, 99, {"HK.01052", "SH.600000"})
