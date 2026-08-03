#!/usr/bin/env python3
"""Collect Futu snapshots locally and atomically publish them to Ali.

The script is intentionally pull-free on the public server: Futu OpenD remains
bound to localhost, while this trusted Linux host pushes a complete JSON
snapshot over the existing SSH connection.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import xml.etree.ElementTree as ElementTree
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain import normalize_config  # noqa: E402


WEBAPP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = WEBAPP_ROOT / "config" / "watchlist.json"
DEFAULT_OUTPUT = WEBAPP_ROOT / "runtime" / "latest_snapshot.json"
DEFAULT_FX_CACHE = WEBAPP_ROOT / "runtime" / "ecb_hkd_cny.json"
DEFAULT_REMOTE_PATH = "/usr/LibertyWatch/shared/latest_snapshot.json"
ECB_DAILY_FX_URL = (
    "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
)
QUOTE_CODE_RE = re.compile(r"^(HK|SH|SZ|US)\.[A-Z0-9][A-Z0-9.-]{0,31}$")
SSH_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.-]*$"
)
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")


class CollectorError(RuntimeError):
    """A safe, user-facing collector error."""


def _utc_now() -> datetime:
    return datetime.now(tz=ZoneInfo("UTC"))


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return (
        value.astimezone(ZoneInfo("UTC"))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _json_scalar(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _finite(value: Any) -> float | None:
    value = _json_scalar(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _nonnegative(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number >= 0 else None


def _futu_snapshot_metrics(row: Mapping[str, Any]) -> dict[str, float | None]:
    """Map Futu equity snapshot fields into the public metrics contract."""

    return {
        "pe": _positive(row.get("pe_ratio", row.get("peRatio"))),
        "peTtm": _positive(
            row.get("pe_ttm_ratio", row.get("peTtmRatio"))
        ),
        "pb": _positive(row.get("pb_ratio", row.get("pbRatio"))),
        "dividendYieldTtmPct": _nonnegative(
            row.get(
                "dividend_ratio_ttm",
                row.get("dividendRatioTtm"),
            )
        ),
        "totalMarketValue": _positive(
            row.get("total_market_val", row.get("totalMarketVal"))
        ),
        "earningsPerShare": _finite(
            row.get("earning_per_share", row.get("earningPerShare"))
        ),
        "bookValuePerShare": _finite(
            row.get(
                "net_asset_per_share",
                row.get("netAssetPerShare"),
            )
        ),
    }


def _quote_timezone(code: str) -> ZoneInfo:
    prefix = code.split(".", 1)[0]
    if prefix == "US":
        return ZoneInfo("America/New_York")
    return ZoneInfo("Asia/Shanghai")


def _quote_timestamp(value: Any, code: str) -> str | None:
    raw = _json_scalar(value)
    if not isinstance(raw, str) or not raw.strip() or raw.strip() in {"N/A", "--"}:
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_quote_timezone(code))
    return _iso_utc(parsed)


def _market_state(value: Any) -> str:
    raw = _json_scalar(value)
    if not isinstance(raw, str):
        return "unknown"
    token = raw.strip().upper().rsplit(".", 1)[-1]
    if token in {
        "OPEN",
        "AUCTION",
        "MORNING",
        "AFTERNOON",
        "PRE_MARKET_BEGIN",
        "AFTER_HOURS_BEGIN",
        "NIGHT_OPEN",
        "FUTURE_DAY_OPEN",
        "HK_CAS",
        "FUTURE_AFTERNOON",
        "FUTURE_OPEN",
        "FUTURE_BREAK_OVER",
        "STIB_AFTER_HOURS_BEGIN",
        "NIGHT",
        "TRADE_AT_LAST",
        "OVERNIGHT",
    }:
        return "open"
    if token in {
        "CLOSED",
        "REST",
        "NONE",
        "WAITING_OPEN",
        "PRE_MARKET_END",
        "AFTER_HOURS_END",
        "NIGHT_END",
        "FUTURE_DAY_BREAK",
        "FUTURE_DAY_CLOSE",
        "FUTURE_DAY_WAIT_OPEN",
        "FUTURE_NIGHT_WAIT",
        "FUTURE_SWITCH_DATE",
        "FUTURE_BREAK",
        "FUTURE_CLOSE",
        "STIB_AFTER_HOURS_WAIT",
        "STIB_AFTER_HOURS_END",
    }:
        return "closed"
    return "unknown"


def _daily_change(row: Mapping[str, Any]) -> float | None:
    direct = _finite(row.get("change_rate", row.get("changeRate")))
    if direct is not None:
        return round(direct, 4)
    current = _positive(row.get("last_price"))
    previous = _positive(row.get("prev_close_price"))
    if current is None or previous is None:
        return None
    return round((current - previous) / previous * 100, 4)


def _rows_from_frame(frame: Any) -> list[dict[str, Any]]:
    try:
        raw_rows = frame.to_dict(orient="records")
    except Exception as error:
        raise CollectorError(f"Futu 快照返回格式无效: {error}") from error
    if not isinstance(raw_rows, list):
        raise CollectorError("Futu 快照返回值不是记录列表")
    return [
        {str(key): _json_scalar(value) for key, value in dict(row).items()}
        for row in raw_rows
        if isinstance(row, Mapping)
    ]


@dataclass(frozen=True)
class FetchedQuotes:
    rows: list[dict[str, Any]]
    global_state: dict[str, Any]


@dataclass(frozen=True)
class FxRate:
    rate: float
    as_of: str
    fetched_at: str
    source: str = "European Central Bank"
    status: str = "available"

    def as_payload(self) -> dict[str, Any]:
        return {
            "rate": self.rate,
            "asOf": self.as_of,
            "fetchedAt": self.fetched_at,
            "source": self.source,
            "status": self.status,
        }


def parse_ecb_hkd_cny(
    payload: bytes, *, fetched_at: datetime | None = None
) -> FxRate:
    """Parse ECB EUR cross rates into CNY per HKD."""

    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise CollectorError(f"ECB 汇率 XML 无效: {error}") from error
    as_of = ""
    rates: dict[str, float] = {}
    for element in root.iter():
        if not element.tag.endswith("Cube"):
            continue
        if element.attrib.get("time"):
            as_of = element.attrib["time"]
        currency = element.attrib.get("currency")
        raw_rate = element.attrib.get("rate")
        if currency and raw_rate:
            try:
                rates[currency] = float(raw_rate)
            except ValueError:
                continue
    if not as_of or rates.get("CNY", 0) <= 0 or rates.get("HKD", 0) <= 0:
        raise CollectorError("ECB 汇率缺少日期、CNY 或 HKD")
    rate = rates["CNY"] / rates["HKD"]
    return FxRate(
        rate=round(rate, 8),
        as_of=as_of,
        fetched_at=_iso_utc(fetched_at or _utc_now()),
    )


def fetch_ecb_hkd_cny(
    *,
    timeout_seconds: int = 8,
    opener: Callable[..., Any] = urllib.request.urlopen,
    now: Callable[[], datetime] = _utc_now,
) -> FxRate:
    request = urllib.request.Request(
        ECB_DAILY_FX_URL,
        headers={"User-Agent": "LibertyWatch/1.0 (+read-only FX conversion)"},
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            payload = response.read()
    except Exception as error:
        raise CollectorError(f"无法获取 ECB HKD/CNY 参考汇率: {error}") from error
    return parse_ecb_hkd_cny(payload, fetched_at=now())


def _fx_from_payload(payload: Any) -> FxRate | None:
    if not isinstance(payload, Mapping):
        return None
    rate = _positive(payload.get("rate"))
    as_of = payload.get("asOf")
    fetched_at = payload.get("fetchedAt")
    if (
        rate is None
        or not isinstance(as_of, str)
        or not as_of
        or not isinstance(fetched_at, str)
        or not fetched_at
    ):
        return None
    return FxRate(
        rate=rate,
        as_of=as_of,
        fetched_at=fetched_at,
        source=str(payload.get("source") or "European Central Bank"),
        status=str(payload.get("status") or "cached"),
    )


def load_or_fetch_hkd_cny(
    cache_path: Path | str,
    *,
    max_age: timedelta = timedelta(hours=6),
    now: Callable[[], datetime] = _utc_now,
    fetcher: Callable[..., FxRate] = fetch_ecb_hkd_cny,
) -> FxRate | None:
    """Use a six-hour cache; an old valid rate is safer than no conversion."""

    path = Path(cache_path).resolve()
    cached: FxRate | None = None
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            cached = _fx_from_payload(json.load(handle))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        cached = None

    current = now()
    if cached is not None:
        try:
            fetched = datetime.fromisoformat(
                cached.fetched_at.replace("Z", "+00:00")
            )
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=ZoneInfo("UTC"))
            if current.astimezone(ZoneInfo("UTC")) - fetched <= max_age:
                return FxRate(
                    **{
                        **cached.__dict__,
                        "status": "cached",
                    }
                )
        except ValueError:
            pass

    try:
        fresh = fetcher(now=now)
        atomic_write_json(path, fresh.as_payload())
        return fresh
    except (CollectorError, OSError) as error:
        if cached is None:
            print(f"collector warning: {error}", file=sys.stderr)
            return None
        print(
            f"collector warning: {error}; 继续使用缓存汇率 {cached.as_of}",
            file=sys.stderr,
        )
        return FxRate(
            **{
                **cached.__dict__,
                "status": "stale_cache",
            }
        )


def fetch_market_snapshots(
    quote_codes: Sequence[str],
    *,
    host: str,
    port: int,
    batch_size: int,
) -> FetchedQuotes:
    """Fetch quote codes in bounded batches using the existing futu-api."""

    if not quote_codes:
        return FetchedQuotes(rows=[], global_state={})
    try:
        from futu import OpenQuoteContext, RET_OK
    except ImportError as error:
        raise CollectorError(
            "未找到 futu-api；请使用 tools/futu-opend/.venv/bin/python 运行"
        ) from error

    try:
        quote_context = OpenQuoteContext(host=host, port=port)
    except Exception as error:
        raise CollectorError(f"无法连接 Futu OpenD {host}:{port}: {error}") from error
    rows: list[dict[str, Any]] = []
    global_state: dict[str, Any] = {}
    try:
        try:
            state_result, raw_state = quote_context.get_global_state()
            if state_result == RET_OK and isinstance(raw_state, Mapping):
                global_state = {
                    str(key): _json_scalar(value)
                    for key, value in raw_state.items()
                }
        except Exception:
            # Quote rows remain useful when the optional state call is absent
            # or temporarily unavailable.
            global_state = {}
        for offset in range(0, len(quote_codes), batch_size):
            batch = list(quote_codes[offset : offset + batch_size])
            result, frame = quote_context.get_market_snapshot(batch)
            if result != RET_OK:
                raise CollectorError(
                    f"Futu get_market_snapshot 失败（批次 {offset // batch_size + 1}）: "
                    f"{frame}"
                )
            rows.extend(_rows_from_frame(frame))
    except CollectorError:
        raise
    except Exception as error:
        raise CollectorError(f"Futu 行情调用异常: {error}") from error
    finally:
        with suppress(Exception):
            quote_context.close()
    return FetchedQuotes(rows=rows, global_state=global_state)


SnapshotFetcher = Callable[..., FetchedQuotes | list[dict[str, Any]]]


def _read_live_config(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise CollectorError(f"无法读取正式清单 {path}: {error}") from error
    try:
        normalized = normalize_config(raw, str(path))
    except (TypeError, ValueError) as error:
        raise CollectorError(f"正式清单校验失败: {error}") from error
    if normalized["isDemo"] or normalized["mode"] != "live":
        raise CollectorError("collector 只接受 mode=live、isDemo=false 的正式清单")
    return raw, normalized


def _validated_quote_codes(
    normalized: Mapping[str, Any],
) -> tuple[list[str], dict[str, str]]:
    codes: list[str] = []
    ids_by_code: dict[str, str] = {}
    for security in normalized["securities"]:
        code = security["quoteCode"]
        if not isinstance(code, str) or not QUOTE_CODE_RE.fullmatch(code):
            raise CollectorError(
                f"标的 {security['id']} 缺少有效 quoteCode；"
                "格式示例：HK.00700、SH.600000、SZ.000001"
            )
        if code in ids_by_code:
            raise CollectorError(
                f"quoteCode 重复: {code} "
                f"({ids_by_code[code]}, {security['id']})"
            )
        ids_by_code[code] = security["id"]
        codes.append(code)
    return codes, ids_by_code


def _empty_quote() -> dict[str, Any]:
    return {
        "currentPrice": None,
        "dailyChangePct": None,
        "marketState": "unknown",
        "lastUpdatedAt": None,
        "status": "unavailable",
    }


def _global_market_state(global_state: Mapping[str, Any], code: str) -> str:
    prefix = code.split(".", 1)[0]
    state_key = {
        "HK": "market_hk",
        "SH": "market_sh",
        "SZ": "market_sz",
        "US": "market_us",
    }.get(prefix)
    return _market_state(global_state.get(state_key)) if state_key else "unknown"


def build_snapshot(
    config_path: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 11111,
    batch_size: int = 20,
    hkd_cny_rate: FxRate | None = None,
    fetcher: SnapshotFetcher = fetch_market_snapshots,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Return a complete live document suitable for atomic publication."""

    path = Path(config_path).resolve()
    raw, normalized = _read_live_config(path)
    quote_codes, _ = _validated_quote_codes(normalized)
    collection_started_at = _iso_utc(now())

    # This branch intentionally avoids importing or connecting to futu-api.
    try:
        fetched = (
            fetcher(
                quote_codes,
                host=host,
                port=port,
                batch_size=batch_size,
            )
            if quote_codes
            else FetchedQuotes(rows=[], global_state={})
        )
    except CollectorError:
        raise
    except Exception as error:
        raise CollectorError(f"行情采集失败: {error}") from error
    if isinstance(fetched, FetchedQuotes):
        rows = fetched.rows
        global_state = fetched.global_state
    elif isinstance(fetched, list):
        # A list-only injected fetcher remains supported for deterministic
        # tests; market state then falls back to "unknown".
        rows = fetched
        global_state = {}
    else:
        raise CollectorError("行情 fetcher 返回格式无效")
    collected_at = _iso_utc(now())
    rows_by_code: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        code = _json_scalar(row.get("code"))
        if isinstance(code, str) and code in quote_codes:
            if code in rows_by_code:
                raise CollectorError(f"Futu 返回重复 quoteCode: {code}")
            rows_by_code[code] = row

    snapshot = deepcopy(raw)
    snapshot["snapshotGeneratedAt"] = collected_at
    securities = snapshot.get("securities")
    assert isinstance(securities, list)
    available_count = 0
    for raw_security, normalized_security in zip(
        securities, normalized["securities"], strict=True
    ):
        if not isinstance(raw_security, dict):
            raise CollectorError("正式清单 securities 项必须是对象")
        code = normalized_security["quoteCode"]
        quote = _empty_quote()
        row = rows_by_code.get(code)
        if row is not None:
            configured_metrics = raw_security.get("metrics")
            if not isinstance(configured_metrics, Mapping):
                configured_metrics = {}
            raw_security["metrics"] = {
                **dict(configured_metrics),
                **_futu_snapshot_metrics(row),
            }
            current_price = _positive(
                row.get("last_price", row.get("lastPrice"))
            )
            if current_price is not None:
                available_count += 1
                quote = {
                    "currentPrice": current_price,
                    "dailyChangePct": _daily_change(row),
                    "marketState": (
                        row_state
                        if (
                            row_state := _market_state(
                                row.get(
                                    "market_state", row.get("marketState")
                                )
                            )
                        )
                        != "unknown"
                        else _global_market_state(global_state, code)
                    ),
                    "lastUpdatedAt": _quote_timestamp(
                        row.get("update_time", row.get("updateTime")), code
                    ),
                    "status": "available",
                }
        raw_security["quote"] = quote

    total = len(quote_codes)
    if total == 0:
        status = "not_configured"
    elif available_count == total:
        status = "ok"
    elif available_count:
        status = "partial"
    else:
        status = "unavailable"
    snapshot["marketData"] = {
        **(
            snapshot.get("marketData")
            if isinstance(snapshot.get("marketData"), dict)
            else {}
        ),
        "provider": "futu-opend",
        "realtime": bool(total and available_count),
        "status": status,
        "asOfLabel": None,
        "collectionStartedAt": collection_started_at,
        "collectedAt": collected_at,
        "fxRates": {
            "HKD_CNY": (
                hkd_cny_rate.as_payload() if hkd_cny_rate is not None else None
            )
        },
    }

    # A second full validation prevents a malformed output from being pushed.
    try:
        normalize_config(snapshot, "generated snapshot")
        json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise CollectorError(f"生成的快照未通过完整校验: {error}") from error
    return snapshot


def atomic_write_json(path: Path | str, payload: Mapping[str, Any]) -> Path:
    output = Path(path).resolve()
    if output.suffix.lower() != ".json":
        raise CollectorError("--output 必须以 .json 结尾")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, output)
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return output


def _validate_ssh_host(value: str) -> str:
    if not SSH_HOST_RE.fullmatch(value) or value.startswith("-"):
        raise CollectorError("--ssh-host 格式无效")
    return value


def _validate_host(value: str) -> str:
    if not HOST_RE.fullmatch(value) or value.startswith("-"):
        raise CollectorError("--futu-host 格式无效")
    return value


def _validate_remote_path(value: str) -> str:
    if not REMOTE_PATH_RE.fullmatch(value):
        raise CollectorError("--remote-path 必须是不含空格的绝对 POSIX 路径")
    pure = PurePosixPath(value)
    if ".." in pure.parts or pure.name in {"", ".", ".."}:
        raise CollectorError("--remote-path 不能包含 .. 或指向目录")
    if pure.suffix.lower() != ".json":
        raise CollectorError("--remote-path 必须以 .json 结尾")
    return str(pure)


def _validate_port(value: int, label: str) -> int:
    if not 1 <= value <= 65_535:
        raise CollectorError(f"{label} 必须在 1..65535")
    return value


def push_snapshot(
    local_path: Path | str,
    *,
    ssh_host: str,
    remote_path: str,
    ssh_port: int | None = None,
    identity_file: Path | str | None = None,
    timeout_seconds: int = 15,
) -> None:
    """SCP to a sibling temp path, then atomically rename through SSH."""

    source = Path(local_path).resolve()
    if not source.is_file():
        raise CollectorError(f"待推送快照不存在: {source}")
    destination_host = _validate_ssh_host(ssh_host)
    destination = _validate_remote_path(remote_path)
    if ssh_port is not None:
        _validate_port(ssh_port, "--ssh-port")
    if not 1 <= timeout_seconds <= 120:
        raise CollectorError("--connect-timeout 必须在 1..120 秒")

    identity: Path | None = None
    if identity_file is not None:
        identity = Path(identity_file).expanduser().resolve()
        if not identity.is_file():
            raise CollectorError(f"SSH 私钥不存在或不是文件: {identity}")

    scp = shutil.which("scp")
    ssh = shutil.which("ssh")
    if not scp or not ssh:
        raise CollectorError("系统中找不到 scp 或 ssh")

    remote_temporary = f"{destination}.upload-{uuid.uuid4().hex}.tmp"
    common_options = [
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout_seconds}",
    ]
    identity_options = ["-i", str(identity)] if identity else []
    scp_port = ["-P", str(ssh_port)] if ssh_port is not None else []
    ssh_port_option = ["-p", str(ssh_port)] if ssh_port is not None else []

    try:
        subprocess.run(
            [
                scp,
                "-q",
                *common_options,
                *identity_options,
                *scp_port,
                "--",
                str(source),
                f"{destination_host}:{remote_temporary}",
            ],
            check=True,
        )
        subprocess.run(
            [
                ssh,
                *common_options,
                *identity_options,
                *ssh_port_option,
                "--",
                destination_host,
                "chmod",
                "0644",
                remote_temporary,
            ],
            check=True,
        )
        subprocess.run(
            [
                ssh,
                *common_options,
                *identity_options,
                *ssh_port_option,
                "--",
                destination_host,
                "mv",
                "--",
                remote_temporary,
                destination,
            ],
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise CollectorError(
            f"SSH 快照推送失败（退出码 {error.returncode}）"
        ) from error


def _env_int(name: str, fallback: int | None) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        return int(raw)
    except ValueError as error:
        raise CollectorError(f"环境变量 {name} 必须是整数") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从本机 Futu OpenD 生成完整快照并经 SSH 原子推送到 Ali"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.getenv("LIBERTY_WATCHLIST_FILE", DEFAULT_CONFIG)),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.getenv("LIBERTY_SNAPSHOT_OUTPUT", DEFAULT_OUTPUT)),
    )
    parser.add_argument(
        "--v2-output",
        type=Path,
        default=(
            Path(value)
            if (value := os.getenv("SHAREHOLDER_V2_QUOTE_SNAPSHOT"))
            else None
        ),
        help="可选：为低权限v2计算服务原子写入同一份Linux行情快照",
    )
    parser.add_argument(
        "--fx-cache",
        type=Path,
        default=Path(os.getenv("LIBERTY_FX_CACHE", DEFAULT_FX_CACHE)),
        help="ECB HKD/CNY 日参考汇率缓存",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="只在本机原子写入快照，不运行 scp/ssh",
    )
    parser.add_argument(
        "--futu-host", default=os.getenv("FUTU_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--futu-port",
        type=int,
        default=_env_int("FUTU_PORT", 11111),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_env_int("FUTU_BATCH_SIZE", 20),
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
        default=os.getenv("LIBERTY_REMOTE_PATH", DEFAULT_REMOTE_PATH),
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
    fx_cache = args.fx_cache.resolve()
    v2_output = args.v2_output.resolve() if args.v2_output else None
    if not config.is_file():
        raise CollectorError(f"--config 不存在或不是文件: {config}")
    if output.suffix.lower() != ".json":
        raise CollectorError("--output 必须以 .json 结尾")
    if output == config:
        raise CollectorError("--output 不能覆盖 --config")
    if fx_cache.suffix.lower() != ".json":
        raise CollectorError("--fx-cache 必须以 .json 结尾")
    if fx_cache in {config, output}:
        raise CollectorError("--fx-cache 不能覆盖 config 或 output")
    if v2_output is not None:
        if v2_output.suffix.lower() != ".json":
            raise CollectorError("--v2-output 必须以 .json 结尾")
        if v2_output in {config, output, fx_cache}:
            raise CollectorError("--v2-output 必须是独立文件")
    _validate_host(args.futu_host)
    _validate_port(args.futu_port, "--futu-port")
    if not 1 <= args.batch_size <= 400:
        raise CollectorError("--batch-size 必须在 1..400")
    if not 1 <= args.connect_timeout <= 120:
        raise CollectorError("--connect-timeout 必须在 1..120 秒")
    if not args.no_push:
        _validate_ssh_host(args.ssh_host)
        _validate_remote_path(args.remote_path)
        if args.ssh_port is not None:
            _validate_port(args.ssh_port, "--ssh-port")
        if args.identity_file is not None and not args.identity_file.expanduser().is_file():
            raise CollectorError(
                f"--identity-file 不存在或不是文件: {args.identity_file}"
            )


def main(argv: Iterable[str] | None = None) -> int:
    try:
        parser = build_parser()
        args = parser.parse_args(list(argv) if argv is not None else None)
        _validate_args(args)
        _, normalized_config = _read_live_config(args.config.resolve())
        hkd_cny_rate = (
            load_or_fetch_hkd_cny(args.fx_cache)
            if any(
                security["currency"] == "HKD"
                for security in normalized_config["securities"]
            )
            else None
        )
        snapshot = build_snapshot(
            args.config,
            host=args.futu_host,
            port=args.futu_port,
            batch_size=args.batch_size,
            hkd_cny_rate=hkd_cny_rate,
        )
        output = atomic_write_json(args.output, snapshot)
        if args.v2_output is not None:
            atomic_write_json(args.v2_output, snapshot)
        if not args.no_push:
            push_snapshot(
                output,
                ssh_host=args.ssh_host,
                remote_path=args.remote_path,
                ssh_port=args.ssh_port,
                identity_file=args.identity_file,
                timeout_seconds=args.connect_timeout,
            )
    except (CollectorError, OSError) as error:
        print(f"collector error: {error}", file=sys.stderr)
        return 1

    count = len(snapshot["securities"])
    available = sum(
        item.get("quote", {}).get("status") == "available"
        for item in snapshot["securities"]
        if isinstance(item, Mapping)
    )
    destination = "本机（--no-push）" if args.no_push else args.ssh_host
    print(
        f"snapshot ok: {available}/{count} 行情可用，"
        f"output={output}，destination={destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
