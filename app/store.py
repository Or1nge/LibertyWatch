"""Thread-safe watchlist store backed by atomic JSON snapshots."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from .domain import (
    DEFAULT_REFRESH_INTERVAL_MS,
    FUTU_SNAPSHOT_METRIC_KEYS,
    OPPORTUNITY_METHODOLOGY,
    build_watchlist_data,
    normalize_config,
)
from .history import normalize_history_document


LOGGER = logging.getLogger(__name__)
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _safe_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _watchlist_signature(config: dict[str, Any]) -> tuple[str, ...]:
    """Fingerprint static security metadata while deliberately ignoring quotes."""

    signatures: list[str] = []
    for security in config["securities"]:
        static_security = {
            key: value for key, value in security.items() if key != "quote"
        }
        metrics = dict(static_security.get("metrics") or {})
        for key in FUTU_SNAPSHOT_METRIC_KEYS:
            metrics.pop(key, None)
        static_security["metrics"] = metrics
        signatures.append(
            json.dumps(
                static_security,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return tuple(signatures)


class WatchlistStore:
    """Keep the latest valid live snapshot while tolerating bad replacements."""

    def __init__(
        self,
        *,
        config_path: Path | str,
        demo_config_path: Path | str,
        snapshot_path: Path | str,
        history_path: Path | str | None = None,
        refresh_interval_ms: int | None = None,
        now: Clock = _utc_now,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.demo_config_path = Path(demo_config_path).resolve()
        self.snapshot_path = Path(snapshot_path).resolve()
        self.history_path = (
            Path(history_path).resolve()
            if history_path is not None
            else self.snapshot_path.with_name("weekly_history.json")
        )
        self.interval_override = (
            min(int(refresh_interval_ms), 3_600_000)
            if isinstance(refresh_interval_ms, (int, float))
            and not isinstance(refresh_interval_ms, bool)
            and refresh_interval_ms >= 30_000
            else None
        )
        self.now = now
        self._lock = RLock()
        self._ready = False
        self._live_config: dict[str, Any] | None = None
        self._live_data: dict[str, Any] | None = None
        self._configured_watchlist_signature: tuple[str, ...] | None = None
        self._demo_config: dict[str, Any] | None = None
        self._demo_data: dict[str, Any] | None = None
        self._config_loaded_at: datetime | None = None
        self._live_completed_at: datetime | None = None
        self._demo_loaded_at: datetime | None = None
        self._runtime_loaded = False
        self._runtime_attempt_signature: tuple[int, int, int] | None = None
        self._runtime_missing_after_load = False
        self._last_refresh_error: str | None = None
        self._history_document: dict[str, Any] | None = None
        self._history_attempt_signature: tuple[int, int, int] | None = None
        self._last_history_error: str | None = None
        self._startup_error: str | None = None

    @property
    def refresh_interval_ms(self) -> int:
        with self._lock:
            return (
                self.interval_override
                or (self._live_config or {}).get("refreshIntervalMs")
                or DEFAULT_REFRESH_INTERVAL_MS
            )

    def initialize(self) -> None:
        """Load immutable configs and optionally the first runtime snapshot."""

        loaded_at = _as_utc(self.now())
        try:
            live = normalize_config(
                _read_json(self.config_path), str(self.config_path)
            )
            if live["isDemo"]:
                raise ValueError("正式配置不能是 demo 模式")
            demo = normalize_config(
                _read_json(self.demo_config_path), str(self.demo_config_path)
            )
            if not demo["isDemo"]:
                raise ValueError("演示配置必须是 demo 模式")
        except Exception as error:
            with self._lock:
                self._startup_error = _safe_error(error)
                self._ready = False
            raise

        with self._lock:
            self._live_config = live
            self._live_data = build_watchlist_data(live)
            self._configured_watchlist_signature = _watchlist_signature(live)
            self._demo_config = demo
            self._demo_data = build_watchlist_data(demo)
            self._config_loaded_at = loaded_at
            self._live_completed_at = self._source_time(live, loaded_at)
            self._demo_loaded_at = loaded_at
            self._startup_error = None
            self._ready = True

        # A missing runtime file is expected before the first collector run.
        self.refresh_runtime(force=True)
        self.refresh_history(force=True)

    @staticmethod
    def _source_time(config: dict[str, Any], fallback: datetime) -> datetime:
        return (
            _parse_datetime(config.get("snapshotGeneratedAt"))
            or _parse_datetime(config.get("marketData", {}).get("collectedAt"))
            or fallback
        )

    def _runtime_signature(self) -> tuple[int, int, int] | None:
        return self._file_signature(self.snapshot_path, "runtime snapshot")

    @staticmethod
    def _file_signature(
        path: Path, label: str
    ) -> tuple[int, int, int] | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        if not path.is_file():
            raise ValueError(f"{label} 不是普通文件: {path}")
        return (stat.st_ino, stat.st_mtime_ns, stat.st_size)

    def _history_signature(self) -> tuple[int, int, int] | None:
        return self._file_signature(self.history_path, "weekly history")

    def refresh_runtime(self, *, force: bool = False) -> bool:
        """Load a changed atomic snapshot.

        Returns ``True`` only when a new valid snapshot was installed. An
        invalid or disappearing file leaves the previous valid data intact and
        records a human-readable error for ``/readyz`` and response metadata.
        """

        with self._lock:
            if not self._ready:
                return False
        try:
            signature = self._runtime_signature()
        except Exception as error:
            with self._lock:
                self._last_refresh_error = _safe_error(error)
            return False

        if signature is None:
            with self._lock:
                if self._runtime_loaded:
                    self._runtime_missing_after_load = True
                    self._last_refresh_error = (
                        f"FileNotFoundError: runtime snapshot 不存在: "
                        f"{self.snapshot_path}"
                    )
                elif force:
                    self._runtime_attempt_signature = None
            return False

        with self._lock:
            if not force and signature == self._runtime_attempt_signature:
                return False
            self._runtime_attempt_signature = signature

        try:
            parsed = _read_json(self.snapshot_path)
            config = normalize_config(parsed, str(self.snapshot_path))
            if config["isDemo"] or config["mode"] != "live":
                raise ValueError("runtime snapshot 必须是 live 且 isDemo=false")
            with self._lock:
                expected_signature = self._configured_watchlist_signature
            if (
                expected_signature is None
                or _watchlist_signature(config) != expected_signature
            ):
                raise ValueError(
                    "runtime snapshot 与当前正式观察清单不匹配；"
                    "等待 collector 生成新快照"
                )
            data = build_watchlist_data(config)
            completed_at = self._source_time(config, _as_utc(self.now()))
        except Exception as error:
            with self._lock:
                self._last_refresh_error = _safe_error(error)
            LOGGER.warning("runtime snapshot rejected: %s", _safe_error(error))
            return False

        with self._lock:
            self._live_config = config
            self._live_data = data
            self._live_completed_at = completed_at
            self._runtime_loaded = True
            self._runtime_missing_after_load = False
            self._last_refresh_error = None
        return True

    def refresh_history(self, *, force: bool = False) -> bool:
        """Install a changed, complete weekly-history document."""

        with self._lock:
            live = self._live_config
            if not self._ready or live is None:
                return False
        try:
            signature = self._history_signature()
        except Exception as error:
            with self._lock:
                self._last_history_error = _safe_error(error)
            return False
        if signature is None:
            return False
        with self._lock:
            if not force and signature == self._history_attempt_signature:
                return False
            self._history_attempt_signature = signature
            expected = {
                security["id"]: {
                    "quoteCode": security["quoteCode"],
                    "currency": security["currency"],
                }
                for security in live["securities"]
            }
        try:
            document = normalize_history_document(
                _read_json(self.history_path), expected
            )
        except Exception as error:
            with self._lock:
                self._last_history_error = _safe_error(error)
            LOGGER.warning("weekly history rejected: %s", _safe_error(error))
            return False
        with self._lock:
            self._history_document = document
            self._last_history_error = None
        return True

    def _snapshot_locked(self, *, demo: bool) -> dict[str, Any] | None:
        config = self._demo_config if demo else self._live_config
        data = self._demo_data if demo else self._live_data
        if config is None or data is None:
            return None

        server_time = _as_utc(self.now())
        completed_at = (
            self._demo_loaded_at
            if demo
            else self._live_completed_at
        ) or server_time
        interval = (
            self.interval_override
            or config["refreshIntervalMs"]
            or DEFAULT_REFRESH_INTERVAL_MS
        )
        next_refresh = completed_at + timedelta(milliseconds=interval)
        prices_refreshed = (
            not demo
            and self._runtime_loaded
            and data["summary"]["priceAvailableCount"] > 0
        )
        refresh_kind = (
            "fictional_demo"
            if demo
            else "quote_snapshot"
            if self._runtime_loaded
            else "configuration_load"
        )
        is_realtime = (
            bool(config["marketData"]["realtime"])
            and not demo
            and prices_refreshed
        )
        meta = {
            "snapshotVersion": _iso(completed_at),
            "serverTime": _iso(server_time),
            "schemaVersion": config["schemaVersion"],
            "mode": config["mode"],
            "isDemo": config["isDemo"],
            "isRealtime": is_realtime,
            "title": config["title"],
            "description": config["description"],
            "disclaimer": config["disclaimer"],
            "dataSource": config["marketData"]["provider"],
            "dataStatus": config["marketData"]["status"],
            "dataAsOfLabel": config["marketData"]["asOfLabel"],
            "refreshIntervalMs": interval,
            "lastRefreshAt": _iso(completed_at),
            "lastConfigurationReloadAt": (
                _iso(self._config_loaded_at)
                if self._config_loaded_at is not None
                else None
            ),
            "lastQuoteRefreshStartedAt": (
                (
                    config["marketData"]["collectionStartedAt"]
                    or config["marketData"]["collectedAt"]
                )
                if prices_refreshed
                else None
            ),
            "lastQuoteRefreshSucceededAt": (
                config["marketData"]["collectedAt"] if prices_refreshed else None
            ),
            "nextRefreshAt": _iso(next_refresh),
            "refreshKind": refresh_kind,
            "pricesRefreshed": prices_refreshed,
            "lastRefreshError": None if demo else self._last_refresh_error,
            "opportunityMethodology": OPPORTUNITY_METHODOLOGY,
            "methodology": OPPORTUNITY_METHODOLOGY,
            "historyGeneratedAt": (
                self._history_document.get("generatedAt")
                if not demo and self._history_document is not None
                else None
            ),
            "historyAvailableCount": (
                len(self._history_document["securities"])
                if not demo and self._history_document is not None
                else len(config["securities"])
                if demo
                else 0
            ),
            "lastHistoryError": None if demo else self._last_history_error,
        }
        return {
            "meta": meta,
            "coverage": {
                "totalSecurities": data["summary"]["totalSecurities"],
                "targetConfiguredCount": data["summary"]["targetConfiguredCount"],
                "priceAvailableCount": data["summary"]["priceAvailableCount"],
                "peAvailableCount": data["summary"]["peAvailableCount"],
                "pbAvailableCount": data["summary"]["pbAvailableCount"],
                "futuMetricCompleteCount": data["summary"][
                    "futuMetricCompleteCount"
                ],
            },
            **deepcopy(data),
        }

    def snapshot(self, *, demo: bool = False) -> dict[str, Any] | None:
        if not demo:
            self.refresh_runtime()
        with self._lock:
            return self._snapshot_locked(demo=demo)

    def security(
        self, security_id: str, *, demo: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        snapshot = self.snapshot(demo=demo)
        if snapshot is None:
            return None
        security = next(
            (
                item
                for item in snapshot["securities"]
                if item["id"] == security_id
            ),
            None,
        )
        if security is None:
            return None
        return snapshot, security

    def security_history(
        self, security_id: str, *, demo: bool = False
    ) -> dict[str, Any] | None:
        """Return one security's history without inflating the watchlist API."""

        if demo:
            with self._lock:
                config = self._demo_config
                if config is None:
                    return None
                security = next(
                    (
                        item
                        for item in config["securities"]
                        if item["id"] == security_id
                    ),
                    None,
                )
                if security is None:
                    return None
                points = deepcopy(security["history"])
                return {
                    "securityId": security_id,
                    "currency": security["currency"],
                    "provider": "fictional_fixture",
                    "frequency": "fictional",
                    "adjustment": "fictional",
                    "windowYears": None,
                    "generatedAt": None,
                    "asOf": points[-1]["timestamp"] if points else None,
                    "pointCount": len(points),
                    "points": points,
                }

        self.refresh_history()
        with self._lock:
            if self._history_document is None:
                return None
            raw = self._history_document["securities"].get(security_id)
            if raw is None:
                return None
            return {
                "securityId": security_id,
                "quoteCode": raw["quoteCode"],
                "currency": raw["currency"],
                "provider": self._history_document["provider"],
                "frequency": raw["frequency"],
                "adjustment": raw["adjustment"],
                "windowYears": raw["windowYears"],
                "windowStart": self._history_document["windowStart"],
                "windowEnd": self._history_document["windowEnd"],
                "generatedAt": self._history_document["generatedAt"],
                "asOf": raw["asOf"],
                "pointCount": raw["pointCount"],
                "points": deepcopy(raw["points"]),
            }

    def readiness(self, *, refresh: bool = True) -> dict[str, Any]:
        if refresh:
            self.refresh_runtime()
            self.refresh_history()
        with self._lock:
            ready = bool(
                self._ready
                and self._live_config is not None
                and self._live_data is not None
                and self._demo_config is not None
                and self._demo_data is not None
            )
            return {
                "ready": ready,
                "configLoaded": self._live_config is not None,
                "demoConfigLoaded": self._demo_config is not None,
                "snapshotLoaded": self._runtime_loaded,
                "snapshotMissingAfterLoad": self._runtime_missing_after_load,
                "historyLoaded": self._history_document is not None,
                "historySecurityCount": (
                    len(self._history_document["securities"])
                    if self._history_document is not None
                    else 0
                ),
                "mode": (
                    self._live_config["mode"]
                    if self._live_config is not None
                    else None
                ),
                "isDemo": False,
                "securityCount": (
                    len(self._live_data["securities"])
                    if self._live_data is not None
                    else 0
                ),
                "lastRefreshError": (
                    self._last_refresh_error or self._startup_error
                ),
                "lastHistoryError": self._last_history_error,
            }
