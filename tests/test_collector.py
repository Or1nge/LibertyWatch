from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import collector.push_quotes as collector_module
from collector.push_quotes import (
    CollectorError,
    FetchedQuotes,
    FxRate,
    _validate_remote_path,
    atomic_write_json,
    build_snapshot,
    load_or_fetch_hkd_cny,
    main,
    parse_ecb_hkd_cny,
    push_snapshot,
)


WEBAPP_ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 7, 31, tzinfo=timezone.utc)


def live_config_with_security() -> dict:
    return {
        "schemaVersion": 1,
        "mode": "live",
        "isDemo": False,
        "title": "正式清单",
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
                "targetPrices": {
                    "watch": None,
                    "preferred": None,
                    "deep": None,
                },
                "expectedDividendYieldPct": None,
                "valuationStatus": "unconfigured",
                "investmentThesis": [],
                "risks": [],
                "targetRevisionHistory": [],
                "history": [],
            }
        ],
    }


def test_empty_config_never_calls_futu(tmp_path: Path) -> None:
    called = False

    def forbidden_fetcher(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("empty config must not request quotes")

    config = live_config_with_security()
    config["securities"] = []
    config_path = tmp_path / "empty-watchlist.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    snapshot = build_snapshot(
        config_path,
        fetcher=forbidden_fetcher,
        now=lambda: FIXED_NOW,
    )

    assert called is False
    assert snapshot["securities"] == []
    assert snapshot["marketData"]["status"] == "not_configured"
    assert snapshot["marketData"]["realtime"] is False
    assert snapshot["snapshotGeneratedAt"] == "2026-07-31T00:00:00.000Z"


def test_official_watchlist_requests_67_unique_codes_in_bmp_safe_batches() -> None:
    requested: list[tuple[list[str], str, int, int]] = []

    def fake_fetcher(codes, *, host, port, batch_size):
        requested.append((list(codes), host, port, batch_size))
        return FetchedQuotes(rows=[], global_state={})

    snapshot = build_snapshot(
        WEBAPP_ROOT / "config" / "watchlist.json",
        fetcher=fake_fetcher,
        now=lambda: FIXED_NOW,
    )

    codes, host, port, batch_size = requested[0]
    assert len(codes) == 67
    assert len(set(codes)) == 67
    assert (host, port, batch_size) == ("127.0.0.1", 11111, 20)
    assert len(snapshot["securities"]) == 67
    assert snapshot["marketData"]["status"] == "unavailable"


def test_snapshot_uses_futu_rows_and_global_market_state(tmp_path: Path) -> None:
    config_path = tmp_path / "watchlist.json"
    config_path.write_text(
        json.dumps(live_config_with_security()), encoding="utf-8"
    )
    requested = []

    def fake_fetcher(codes, *, host, port, batch_size):
        requested.append((codes, host, port, batch_size))
        return FetchedQuotes(
            rows=[
                {
                    "code": "SH.600000",
                    "last_price": 9.75,
                    "prev_close_price": 10,
                    "update_time": "2026-07-31 15:00:00",
                    "pe_ratio": 6.5,
                    "pe_ttm_ratio": 6.2,
                    "pb_ratio": 0.72,
                    "dividend_ratio_ttm": 4.1,
                    "total_market_val": 286_000_000_000,
                    "earning_per_share": 1.51,
                    "net_asset_per_share": 13.54,
                }
            ],
            global_state={"market_sh": "MORNING"},
        )

    snapshot = build_snapshot(
        config_path,
        host="127.0.0.1",
        port=11111,
        batch_size=20,
        fetcher=fake_fetcher,
        now=lambda: FIXED_NOW,
    )
    quote = snapshot["securities"][0]["quote"]
    metrics = snapshot["securities"][0]["metrics"]

    assert requested == [(["SH.600000"], "127.0.0.1", 11111, 20)]
    assert quote["currentPrice"] == 9.75
    assert quote["dailyChangePct"] == -2.5
    assert quote["marketState"] == "open"
    assert quote["lastUpdatedAt"] == "2026-07-31T07:00:00.000Z"
    assert quote["status"] == "available"
    assert metrics == {
        "pe": 6.5,
        "peTtm": 6.2,
        "pb": 0.72,
        "dividendYieldTtmPct": 4.1,
        "totalMarketValue": 286_000_000_000,
        "earningsPerShare": 1.51,
        "bookValuePerShare": 13.54,
    }
    assert snapshot["marketData"]["status"] == "ok"
    assert snapshot["marketData"]["realtime"] is True


def test_snapshot_metrics_preserve_missing_values_as_null(tmp_path: Path) -> None:
    config_path = tmp_path / "watchlist.json"
    config_path.write_text(
        json.dumps(live_config_with_security()), encoding="utf-8"
    )

    snapshot = build_snapshot(
        config_path,
        fetcher=lambda *args, **kwargs: FetchedQuotes(
            rows=[
                {
                    "code": "SH.600000",
                    "last_price": 9.75,
                    "pe_ratio": "N/A",
                    "pe_ttm_ratio": -1,
                    "pb_ratio": None,
                    "dividend_ratio_ttm": 0,
                    "total_market_val": 0,
                    "earning_per_share": -0.2,
                    "net_asset_per_share": -0.5,
                }
            ],
            global_state={},
        ),
        now=lambda: FIXED_NOW,
    )

    assert snapshot["securities"][0]["metrics"] == {
        "pe": None,
        "peTtm": None,
        "pb": None,
        "dividendYieldTtmPct": 0,
        "totalMarketValue": None,
        "earningsPerShare": -0.2,
        "bookValuePerShare": -0.5,
    }


def test_ecb_cross_rate_is_parsed_and_cached_without_repeated_fetch(
    tmp_path: Path,
) -> None:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Envelope>
      <Cube>
        <Cube time="2026-07-30">
          <Cube currency="CNY" rate="7.2000"/>
          <Cube currency="HKD" rate="8.0000"/>
        </Cube>
      </Cube>
    </Envelope>
    """
    parsed = parse_ecb_hkd_cny(xml, fetched_at=FIXED_NOW)
    assert parsed.rate == 0.9
    assert parsed.as_of == "2026-07-30"

    cache = tmp_path / "fx-cache.json"
    calls = 0

    def fake_fetcher(*, now):
        nonlocal calls
        calls += 1
        return parsed

    first = load_or_fetch_hkd_cny(
        cache,
        now=lambda: FIXED_NOW,
        fetcher=fake_fetcher,
    )
    second = load_or_fetch_hkd_cny(
        cache,
        max_age=timedelta(hours=6),
        now=lambda: FIXED_NOW,
        fetcher=fake_fetcher,
    )

    assert first is not None and first.rate == 0.9
    assert second is not None and second.status == "cached"
    assert calls == 1
    assert json.loads(cache.read_text())["asOf"] == "2026-07-30"


def test_snapshot_carries_hkd_cny_reference_rate(tmp_path: Path) -> None:
    config = live_config_with_security()
    config["securities"][0].update(
        {
            "id": "hk-one",
            "quoteCode": "HK.00005",
            "market": "HK",
            "currency": "HKD",
        }
    )
    config_path = tmp_path / "watchlist.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    fx = FxRate(
        rate=0.91,
        as_of="2026-07-30",
        fetched_at="2026-07-31T00:00:00.000Z",
    )

    snapshot = build_snapshot(
        config_path,
        hkd_cny_rate=fx,
        fetcher=lambda *args, **kwargs: FetchedQuotes(
            rows=[], global_state={}
        ),
        now=lambda: FIXED_NOW,
    )

    assert snapshot["marketData"]["fxRates"]["HKD_CNY"] == fx.as_payload()


def test_no_push_output_is_atomic_and_cli_testable(tmp_path: Path) -> None:
    config = live_config_with_security()
    config["securities"] = []
    config_path = tmp_path / "empty-watchlist.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    output = tmp_path / "latest_snapshot.json"
    exit_code = main(
        [
            "--config",
            str(config_path),
            "--output",
            str(output),
            "--no-push",
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["mode"] == "live"
    assert payload["securities"] == []
    assert list(tmp_path.glob(f".{output.name}.*.tmp")) == []
    assert stat.S_IMODE(output.stat().st_mode) == 0o640

    replacement = {**payload, "snapshotGeneratedAt": "replacement"}
    atomic_write_json(output, replacement)
    assert json.loads(output.read_text())["snapshotGeneratedAt"] == "replacement"


@pytest.mark.parametrize(
    "path",
    [
        "relative/latest_snapshot.json",
        "/usr/LibertyWatch/../escape.json",
        "/usr/LibertyWatch/shared/not-json.txt",
        "/usr/Liberty Watch/shared/latest_snapshot.json",
    ],
)
def test_remote_path_validation_rejects_unsafe_destinations(path: str) -> None:
    with pytest.raises(CollectorError):
        _validate_remote_path(path)


def test_push_makes_remote_snapshot_world_readable_before_atomic_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "snapshot.json"
    source.write_text("{}\n", encoding="utf-8")
    commands: list[list[str]] = []

    monkeypatch.setattr(
        collector_module.shutil,
        "which",
        lambda binary: f"/usr/bin/{binary}",
    )

    def fake_run(command, *, check):
        assert check is True
        commands.append(command)

    monkeypatch.setattr(collector_module.subprocess, "run", fake_run)

    push_snapshot(
        source,
        ssh_host="ali",
        remote_path="/usr/LibertyWatch/shared/latest_snapshot.json",
    )

    assert len(commands) == 3
    assert commands[0][0] == "/usr/bin/scp"
    assert "latest_snapshot.json.upload-" in commands[0][-1]
    assert commands[1][0] == "/usr/bin/ssh"
    assert commands[1][-3] == "chmod"
    assert commands[1][-2] == "0644"
    assert commands[2][-4] == "mv"
    assert commands[2][-1] == "/usr/LibertyWatch/shared/latest_snapshot.json"
