"""FastAPI entrypoint for the Liberty watchlist."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import AsyncIterator

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .store import WatchlistStore
from .published_store import PublishedV2Store


LOGGER = logging.getLogger(__name__)
WEBAPP_ROOT = Path(__file__).resolve().parents[1]


def _path_from_env(name: str, fallback: Path) -> Path:
    raw = os.getenv(name)
    if not raw:
        return fallback.resolve()
    path = Path(raw)
    return (path if path.is_absolute() else WEBAPP_ROOT / path).resolve()


def _positive_int_from_env(name: str) -> int | None:
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _boolean_from_env(name: str, fallback: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return fallback
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )


def _is_demo_query(value: str | None) -> bool:
    """Only the literal query ``?demo=1`` unlocks fictional data."""

    return value == "1"


def create_app(
    *,
    config_path: Path | str | None = None,
    demo_config_path: Path | str | None = None,
    snapshot_path: Path | str | None = None,
    history_path: Path | str | None = None,
    structured_v2_root: Path | str | None = None,
    analysis_v2_root: Path | str | None = None,
    metric_definitions_path: Path | str | None = None,
    v2_enabled: bool | None = None,
    public_dir: Path | str | None = None,
    now=None,
) -> FastAPI:
    live_path = Path(config_path).resolve() if config_path else _path_from_env(
        "WATCHLIST_FILE", WEBAPP_ROOT / "config" / "watchlist.json"
    )
    demo_path = (
        Path(demo_config_path).resolve()
        if demo_config_path
        else _path_from_env(
            "DEMO_WATCHLIST_FILE",
            WEBAPP_ROOT / "config" / "demo-watchlist.json",
        )
    )
    runtime_path = (
        Path(snapshot_path).resolve()
        if snapshot_path
        else _path_from_env(
            "SNAPSHOT_FILE", WEBAPP_ROOT / "runtime" / "latest_snapshot.json"
        )
    )
    weekly_history_path = (
        Path(history_path).resolve()
        if history_path
        else _path_from_env(
            "HISTORY_FILE", WEBAPP_ROOT / "runtime" / "weekly_history.json"
        )
    )
    static_root = (
        Path(public_dir).resolve()
        if public_dir
        else _path_from_env("PUBLIC_DIR", WEBAPP_ROOT / "public")
    )
    clock = now or (lambda: datetime.now(timezone.utc))
    store = WatchlistStore(
        config_path=live_path,
        demo_config_path=demo_path,
        snapshot_path=runtime_path,
        history_path=weekly_history_path,
        refresh_interval_ms=_positive_int_from_env("REFRESH_INTERVAL_MS"),
        now=clock,
    )
    published_store = PublishedV2Store(
        structured_root=(
            Path(structured_v2_root).resolve()
            if structured_v2_root
            else _path_from_env("SHAREHOLDER_V2_STRUCTURED_ROOT", WEBAPP_ROOT / "runtime" / "shareholder-v2" / "structured")
        ),
        analysis_root=(
            Path(analysis_v2_root).resolve()
            if analysis_v2_root
            else _path_from_env("SHAREHOLDER_V2_ANALYSIS_ROOT", WEBAPP_ROOT / "runtime" / "shareholder-v2" / "analysis")
        ),
        definitions_path=(
            Path(metric_definitions_path).resolve()
            if metric_definitions_path
            else _path_from_env("METRIC_DEFINITIONS_V2_FILE", WEBAPP_ROOT / "config" / "metric_definitions_v2.json")
        ),
        enabled=(
            v2_enabled
            if v2_enabled is not None
            else _boolean_from_env("SHAREHOLDER_RETURN_V2_ENABLED", True)
        ),
    )
    started_at = clock()
    started_monotonic = monotonic()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            store.initialize()
        except Exception:
            # Keep /healthz available and expose the concrete error at /readyz.
            LOGGER.exception("watchlist initialization failed")
        try:
            published_store.initialize()
        except Exception:
            LOGGER.exception("shareholder-return v2 initialization failed")
        yield

    application = FastAPI(
        title="Liberty Watch",
        version="2.0.1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.watchlist_store = store
    application.state.published_v2_store = published_store
    application.state.public_dir = static_root

    @application.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        if error.status_code == 405:
            return _error(405, "method_not_allowed", "仅支持 GET 和 HEAD")
        if error.status_code == 404 and request.url.path.startswith("/api/"):
            return _error(404, "api_not_found", "API 路径不存在")
        return _error(error.status_code, "http_error", str(error.detail))

    @application.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'self'; form-action 'self'; "
            "script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'",
        )
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        if request.url.path.startswith("/api/") or request.url.path in {
            "/healthz",
            "/readyz",
        }:
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.api_route("/healthz", methods=["GET", "HEAD"])
    async def healthz() -> dict[str, object]:
        current = clock()
        return {
            "status": "ok",
            "service": "liberty-watch",
            "startedAt": started_at.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "uptimeSeconds": max(0, int(monotonic() - started_monotonic)),
            "serverTime": current.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        }

    @application.api_route("/readyz", methods=["GET", "HEAD"])
    async def readyz() -> JSONResponse:
        state = store.readiness()
        status_code = 200 if state["ready"] else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ready" if state["ready"] else "not_ready",
                "checks": {
                    "configLoaded": state["configLoaded"],
                    "demoConfigLoaded": state["demoConfigLoaded"],
                    "snapshotLoaded": state["snapshotLoaded"],
                    "historyLoaded": state["historyLoaded"],
                    "historySecurityCount": state["historySecurityCount"],
                },
                "mode": state["mode"],
                "isDemo": False,
                "securityCount": state["securityCount"],
                "lastRefreshError": state["lastRefreshError"],
                "lastHistoryError": state["lastHistoryError"],
            },
        )

    @application.api_route("/api/watchlist", methods=["GET", "HEAD"])
    async def watchlist(
        demo: str | None = Query(default=None),
    ) -> Response:
        snapshot = store.snapshot(demo=_is_demo_query(demo))
        if snapshot is None:
            return _error(
                503, "watchlist_not_ready", "观察清单尚未就绪"
            )
        return JSONResponse(content=published_store.enrich_watchlist(snapshot))

    @application.api_route(
        "/api/securities/{security_id}", methods=["GET", "HEAD"]
    )
    async def security_detail(
        security_id: str,
        demo: str | None = Query(default=None),
    ) -> Response:
        result = store.security(security_id, demo=_is_demo_query(demo))
        if result is None:
            return _error(404, "security_not_found", "未找到该证券")
        snapshot, security = result
        return JSONResponse(
            content={
                "meta": snapshot["meta"],
                "market": snapshot["market"],
                "security": security,
            }
        )

    @application.api_route(
        "/api/securities/{security_id}/history", methods=["GET", "HEAD"]
    )
    async def security_history(
        security_id: str,
        demo: str | None = Query(default=None),
    ) -> Response:
        is_demo = _is_demo_query(demo)
        if store.security(security_id, demo=is_demo) is None:
            return _error(404, "security_not_found", "未找到该证券")
        history = store.security_history(security_id, demo=is_demo)
        if history is None:
            return _error(503, "history_not_ready", "十年周线尚未就绪")
        return JSONResponse(content=history)

    @application.api_route("/api/v1/companies", methods=["GET", "HEAD"])
    async def companies_v2() -> Response:
        payload = published_store.companies()
        if payload is None:
            return _error(503, "shareholder_v2_not_ready", "股东回报v2结构化数据尚未就绪")
        return JSONResponse(content=payload)

    @application.api_route("/api/v1/companies/{company_id}", methods=["GET", "HEAD"])
    async def company_v2(company_id: str) -> Response:
        payload = published_store.company(company_id)
        if payload is None:
            return _error(404, "company_not_found", "未找到该公司或v2数据尚未发布")
        return JSONResponse(content=payload)

    @application.api_route(
        "/api/v1/companies/{company_id}/analysis/latest", methods=["GET", "HEAD"]
    )
    async def latest_analysis_v2(company_id: str) -> Response:
        payload = published_store.latest_analysis(company_id)
        if payload is None:
            return _error(404, "analysis_not_found", "该公司尚无合法Codex风险报告")
        return JSONResponse(content=payload)

    @application.api_route("/api/v1/metric-definitions", methods=["GET", "HEAD"])
    async def metric_definitions_v2() -> Response:
        payload = published_store.definitions()
        if payload is None:
            return _error(503, "metric_definitions_not_ready", "v2指标注册表尚未就绪")
        return JSONResponse(content=payload)

    @application.api_route("/api/v1/pipeline/status", methods=["GET", "HEAD"])
    async def pipeline_status_v2() -> Response:
        return JSONResponse(content=published_store.pipeline_status())

    @application.api_route("/api", methods=["GET", "HEAD"])
    async def api_root() -> JSONResponse:
        return _error(404, "api_not_found", "API 路径不存在")

    @application.api_route("/api/{api_path:path}", methods=["GET", "HEAD"])
    async def unknown_api(api_path: str) -> JSONResponse:
        del api_path
        return _error(404, "api_not_found", "API 路径不存在")

    def safe_static_path(path: str) -> Path | None:
        requested = (static_root / path.lstrip("/")).resolve()
        try:
            requested.relative_to(static_root)
        except ValueError:
            return None
        return requested

    @application.api_route("/{path:path}", methods=["GET", "HEAD"])
    async def spa(path: str) -> Response:
        requested = safe_static_path(path or "index.html")
        if requested is None:
            return _error(400, "invalid_path", "请求路径无效")
        if requested.is_file():
            cache_control = (
                "public, max-age=31536000, immutable"
                if path.startswith("assets/")
                else "no-cache"
            )
            return FileResponse(
                requested, headers={"Cache-Control": cache_control}
            )
        if Path(path).suffix:
            return _error(404, "asset_not_found", "静态资源不存在")
        index_path = static_root / "index.html"
        if not index_path.is_file():
            return _error(404, "spa_not_built", "前端静态文件尚未生成")
        return FileResponse(index_path, headers={"Cache-Control": "no-cache"})

    return application


app = create_app()
