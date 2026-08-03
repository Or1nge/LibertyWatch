#!/usr/bin/env python3
"""Build and publish a rolling ten-year weekly Futu price history."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import suppress
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.push_quotes import (  # noqa: E402
    CollectorError,
    _env_int,
    _iso_utc,
    _read_live_config,
    _utc_now,
    _validate_host,
    _validate_port,
    _validate_remote_path,
    _validate_ssh_host,
    _validated_quote_codes,
    atomic_write_json,
    push_snapshot,
)
from app.history import normalize_history_document  # noqa: E402


WEBAPP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = WEBAPP_ROOT / "config" / "watchlist.json"
DEFAULT_OUTPUT = WEBAPP_ROOT / "runtime" / "weekly_history.json"
DEFAULT_REMOTE_PATH = "/usr/LibertyWatch/shared/weekly_history.json"
SHANGHAI = ZoneInfo("Asia/Shanghai")


HistoryFetcher = Callable[..., dict[str, list[dict[str, Any]]]]


def _years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _history_quota_details(value: Any) -> tuple[int, int, set[str]] | None:
    if not isinstance(value, tuple) or len(value) != 3:
        return None
    used, remaining, details = value
    if not isinstance(used, int) or not isinstance(remaining, int):
        return None
    codes = {
        str(item.get("code"))
        for item in details
        if isinstance(item, Mapping) and item.get("code")
    } if isinstance(details, list) else set()
    return used, remaining, codes


def _records_from_frame(frame: Any) -> list[dict[str, Any]]:
    try:
        records = frame.to_dict(orient="records")
    except Exception as error:
        raise CollectorError(f"Futu 周线返回格式无效: {error}") from error
    if not isinstance(records, list):
        raise CollectorError("Futu 周线返回值不是记录列表")
    return [dict(item) for item in records if isinstance(item, Mapping)]


def fetch_futu_weekly_history(
    quote_codes: Sequence[str],
    *,
    host: str,
    port: int,
    start: date,
    end: date,
    request_pause_seconds: float = 0.55,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch forward-adjusted weekly closes with quota and rate safeguards."""

    if not quote_codes:
        return {}
    try:
        from futu import (
            AuType,
            KL_FIELD,
            KLType,
            OpenQuoteContext,
            RET_OK,
        )
    except ImportError as error:
        raise CollectorError(
            "未找到 futu-api；请使用 tools/futu-opend/.venv/bin/python 运行"
        ) from error

    try:
        context = OpenQuoteContext(host=host, port=port)
    except Exception as error:
        raise CollectorError(f"无法连接 Futu OpenD {host}:{port}: {error}") from error

    result: dict[str, list[dict[str, Any]]] = {}
    try:
        quota_ret, quota_value = context.get_history_kl_quota(get_detail=True)
        if quota_ret != RET_OK:
            raise CollectorError(f"无法读取 Futu 历史K线额度: {quota_value}")
        quota = _history_quota_details(quota_value)
        if quota is None:
            raise CollectorError("Futu 历史K线额度返回格式无效")
        _, remaining, recently_requested = quota
        missing_quota = [
            code for code in quote_codes if code not in recently_requested
        ]
        if len(missing_quota) > remaining:
            raise CollectorError(
                "Futu 历史K线额度不足："
                f"需要新增 {len(missing_quota)} 只，剩余 {remaining} 只"
            )

        for index, code in enumerate(quote_codes):
            page_key = None
            rows: list[dict[str, Any]] = []
            while True:
                ret, frame, page_key = context.request_history_kline(
                    code,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    ktype=KLType.K_WEEK,
                    autype=AuType.QFQ,
                    fields=[KL_FIELD.DATE_TIME, KL_FIELD.CLOSE],
                    max_count=1000,
                    page_req_key=page_key,
                )
                if ret != RET_OK:
                    raise CollectorError(f"Futu 周线请求失败 {code}: {frame}")
                rows.extend(_records_from_frame(frame))
                if page_key is None:
                    break

            by_date: dict[str, float] = {}
            for row in rows:
                timestamp = str(row.get("time_key") or "")[:10]
                try:
                    price = float(row.get("close"))
                    parsed_date = date.fromisoformat(timestamp)
                except (TypeError, ValueError):
                    continue
                if price > 0 and start <= parsed_date <= end:
                    by_date[timestamp] = price
            points = [
                {
                    "timestamp": timestamp,
                    "label": timestamp[:7],
                    "price": round(price, 4),
                }
                for timestamp, price in sorted(by_date.items())
            ]
            if len(points) < 2:
                raise CollectorError(f"Futu 周线不足两期 {code}")
            result[code] = points

            if index + 1 < len(quote_codes) and request_pause_seconds > 0:
                sleeper(request_pause_seconds)
    finally:
        with suppress(Exception):
            context.close()
    return result


def build_history_document(
    config_path: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 11111,
    years: int = 10,
    request_pause_seconds: float = 0.55,
    fetcher: HistoryFetcher = fetch_futu_weekly_history,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Return a complete history document without mutating the watchlist."""

    if not 1 <= years <= 20:
        raise CollectorError("--years 必须在 1..20")
    _, config = _read_live_config(Path(config_path).resolve())
    quote_codes, _ = _validated_quote_codes(config)
    current = now()
    local_date = current.astimezone(SHANGHAI).date()
    start = _years_before(local_date, years)
    series = fetcher(
        quote_codes,
        host=host,
        port=port,
        start=start,
        end=local_date,
        request_pause_seconds=request_pause_seconds,
    )
    if set(series) != set(quote_codes):
        missing = sorted(set(quote_codes) - set(series))
        extra = sorted(set(series) - set(quote_codes))
        raise CollectorError(
            f"周线证券集合不完整：missing={missing[:5]} extra={extra[:5]}"
        )

    securities: dict[str, dict[str, Any]] = {}
    for security in config["securities"]:
        code = security["quoteCode"]
        points = series[code]
        securities[security["id"]] = {
            "quoteCode": code,
            "currency": security["currency"],
            "frequency": "weekly",
            "adjustment": "qfq",
            "windowYears": years,
            "asOf": points[-1]["timestamp"],
            "pointCount": len(points),
            "points": points,
        }

    return {
        "schemaVersion": 1,
        "generatedAt": _iso_utc(current),
        "provider": "futu-opend",
        "frequency": "weekly",
        "adjustment": "qfq",
        "windowYears": years,
        "windowStart": start.isoformat(),
        "windowEnd": local_date.isoformat(),
        "securityIds": [security["id"] for security in config["securities"]],
        "securities": securities,
    }


def retain_compatible_history_document(
    config_path: Path | str,
    history_path: Path | str,
) -> dict[str, Any]:
    """Prune a valid prior Futu document to the current overlapping universe."""

    _, config = _read_live_config(Path(config_path).resolve())
    path = Path(history_path).resolve()
    if not path.is_file():
        raise CollectorError(f"历史文件不存在，无法保留兼容周线: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise CollectorError(f"无法读取既有历史文件: {error}") from error
    raw_securities = raw.get("securities") if isinstance(raw, Mapping) else None
    if not isinstance(raw_securities, Mapping):
        raise CollectorError("既有历史文件 securities 无效")

    current = {
        security["id"]: {
            "quoteCode": security["quoteCode"],
            "currency": security["currency"],
        }
        for security in config["securities"]
    }
    retained_ids = []
    retained: dict[str, Any] = {}
    for security in config["securities"]:
        security_id = security["id"]
        old = raw_securities.get(security_id)
        if (
            isinstance(old, Mapping)
            and old.get("quoteCode") == security["quoteCode"]
            and old.get("currency") == security["currency"]
        ):
            retained_ids.append(security_id)
            retained[security_id] = dict(old)
    if not retained_ids:
        raise CollectorError("新旧观察清单没有可保留的兼容周线")

    document = {
        "schemaVersion": raw.get("schemaVersion"),
        "generatedAt": raw.get("generatedAt"),
        "provider": raw.get("provider"),
        "frequency": raw.get("frequency"),
        "adjustment": raw.get("adjustment"),
        "windowYears": raw.get("windowYears"),
        "windowStart": raw.get("windowStart"),
        "windowEnd": raw.get("windowEnd"),
        "securityIds": retained_ids,
        "securities": retained,
    }
    return normalize_history_document(document, current)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 Futu OpenD 生成十年前复权周线并原子推送到 Ali"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.getenv("LIBERTY_WATCHLIST_FILE", DEFAULT_CONFIG)),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.getenv("LIBERTY_HISTORY_OUTPUT", DEFAULT_OUTPUT)),
    )
    parser.add_argument(
        "--retain-compatible",
        action="store_true",
        help="额度不足换名单时，仅保留新旧名单交集中的既有 Futu 周线",
    )
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument(
        "--futu-host", default=os.getenv("FUTU_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--futu-port", type=int, default=_env_int("FUTU_PORT", 11111)
    )
    parser.add_argument(
        "--years",
        type=int,
        default=_env_int("LIBERTY_HISTORY_YEARS", 10),
    )
    parser.add_argument(
        "--request-pause",
        type=float,
        default=float(os.getenv("LIBERTY_HISTORY_REQUEST_PAUSE", "0.55")),
    )
    parser.add_argument(
        "--ssh-host", default=os.getenv("LIBERTY_SSH_HOST", "ali")
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        default=_env_int("LIBERTY_SSH_PORT", None),
    )
    parser.add_argument(
        "--identity-file",
        type=Path,
        default=(
            Path(value)
            if (value := os.getenv("LIBERTY_SSH_IDENTITY_FILE"))
            else None
        ),
    )
    parser.add_argument(
        "--remote-path",
        default=os.getenv(
            "LIBERTY_HISTORY_REMOTE_PATH", DEFAULT_REMOTE_PATH
        ),
    )
    parser.add_argument(
        "--connect-timeout",
        type=int,
        default=_env_int("LIBERTY_CONNECT_TIMEOUT", 15),
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    config = args.config.resolve()
    output = args.output.resolve()
    if not config.is_file():
        raise CollectorError(f"--config 不存在或不是文件: {config}")
    if output.suffix.lower() != ".json":
        raise CollectorError("--output 必须以 .json 结尾")
    if output == config:
        raise CollectorError("--output 不能覆盖 --config")
    _validate_host(args.futu_host)
    _validate_port(args.futu_port, "--futu-port")
    if not 1 <= args.years <= 20:
        raise CollectorError("--years 必须在 1..20")
    if not 0.5 <= args.request_pause <= 10:
        raise CollectorError("--request-pause 必须在 0.5..10 秒")
    if not 1 <= args.connect_timeout <= 120:
        raise CollectorError("--connect-timeout 必须在 1..120 秒")
    if not args.no_push:
        _validate_ssh_host(args.ssh_host)
        _validate_remote_path(args.remote_path)
        if args.ssh_port is not None:
            _validate_port(args.ssh_port, "--ssh-port")
        if (
            args.identity_file is not None
            and not args.identity_file.expanduser().is_file()
        ):
            raise CollectorError(
                f"--identity-file 不存在或不是文件: {args.identity_file}"
            )


def main(argv: Iterable[str] | None = None) -> int:
    try:
        parser = build_parser()
        args = parser.parse_args(list(argv) if argv is not None else None)
        _validate_args(args)
        if args.retain_compatible:
            document = retain_compatible_history_document(
                args.config, args.output
            )
        else:
            document = build_history_document(
                args.config,
                host=args.futu_host,
                port=args.futu_port,
                years=args.years,
                request_pause_seconds=args.request_pause,
            )
        output = atomic_write_json(args.output, document)
        if not args.no_push:
            push_snapshot(
                output,
                ssh_host=args.ssh_host,
                remote_path=args.remote_path,
                ssh_port=args.ssh_port,
                identity_file=args.identity_file,
                timeout_seconds=args.connect_timeout,
            )
    except (CollectorError, OSError, ValueError) as error:
        print(f"history collector error: {error}", file=sys.stderr)
        return 1

    destination = "本机（--no-push）" if args.no_push else args.ssh_host
    counts = [
        item["pointCount"] for item in document["securities"].values()
    ]
    mode = "retained" if args.retain_compatible else "refreshed"
    print(
        f"weekly history {mode}: {len(counts)} 只，"
        f"{sum(counts)} 点，{min(counts)}..{max(counts)} 点/只，"
        f"output={output}，destination={destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
