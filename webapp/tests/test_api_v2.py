from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from app.main import create_app
from liberty_v2.registry import load_metric_definitions
from liberty_v2.release import build_analysis_release, build_structured_release


PROJECT = Path(__file__).resolve().parents[1]


class ASGIClient:
    """Sync facade for the project's Starlette/httpx compatibility boundary."""

    def __init__(self, app) -> None:
        self.app = app
        self.app.state.watchlist_store.initialize()
        self.app.state.published_v2_store.initialize()

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


def v1_config(tmp_path: Path) -> Path:
    payload = json.loads((PROJECT / "config" / "demo-watchlist.json").read_text(encoding="utf-8"))
    payload["isDemo"] = False
    payload["mode"] = "live"
    payload["marketData"]["realtime"] = False
    payload["securities"] = payload["securities"][:1]
    payload["securities"][0]["issuerId"] = "issuer-v2"
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def company() -> dict:
    empty_metric = {"value": None, "status": "INSUFFICIENT_DATA", "display": "数据不足", "reason": "等待披露", "unit": "ratio"}
    return {
        "schema_version": "shareholder-return-v2",
        "calculation_version": "shareholder-return-v2.0.2",
        "metric_definition_version": "shareholder-return-v2.0.2",
        "company_id": "issuer-v2",
        "company_name": "V2测试公司",
        "securities": [{"security_id": "demo-cn", "ticker": "000001", "market": "CN"}],
        "as_of_date": "2026-08-01",
        "price_timestamp": None,
        "data_status": "PARTIAL",
        "update_status": "CURRENT",
        "metrics": {"sustainable_shareholder_yield": empty_metric},
        "scores": {"recommendation_index": empty_metric, "entry_risk_index": empty_metric},
        "classification": "C",
        "return_type": None,
        "veto_flags": [],
        "analysis_status": {"status": "NOT_REQUESTED"},
        "source_summary": {},
    }


def analysis() -> dict:
    return {
        "analysis_id": "analysis-v2",
        "as_of_date": "2026-08-01",
        "input_snapshot_hash": "a" * 64,
        "verdict": "WATCH",
        "risk_overlay": "MEDIUM",
        "one_sentence_conclusion": "继续观察。",
        "sources": [{"title": "测试来源"}],
        "report_markdown": "# 风险报告",
    }


def build_app(tmp_path: Path, *, enabled: bool = True):
    structured = tmp_path / "published" / "structured"
    analyses = tmp_path / "published" / "analysis"
    build_structured_release(
        structured,
        companies=[company()],
        metric_definitions=load_metric_definitions(),
        pipeline_status={
            "last_structured_calculation_at": "2026-08-01T12:00:00Z",
            "structured_release": "stale-previous-release",
        },
    )
    build_analysis_release(
        analyses,
        analyses={"issuer-v2": (analysis(), "# 风险报告")},
        statuses={
            "issuer-v2": {
                "status": "WAITING_RETRY",
                "job_id": "analysis-next",
                "analysis_mode": "MATERIAL_CHANGE_REVIEW",
                "latest_analysis_id": "analysis-v2",
                "latest_success_at": "2026-08-01T12:05:00Z",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "xhigh",
            }
        },
    )
    config = v1_config(tmp_path)
    return create_app(
        config_path=config,
        demo_config_path=PROJECT / "config" / "demo-watchlist.json",
        snapshot_path=tmp_path / "missing-snapshot.json",
        history_path=tmp_path / "missing-history.json",
        public_dir=PROJECT / "public",
        structured_v2_root=structured,
        analysis_v2_root=analyses,
        metric_definitions_path=PROJECT / "config" / "metric_definitions_v2.json",
        v2_enabled=enabled,
    )


def test_v2_read_only_api_contract_and_v1_enrichment(tmp_path: Path) -> None:
    with ASGIClient(build_app(tmp_path)) as client:
        companies = client.get("/api/v1/companies")
        assert companies.status_code == 200
        assert companies.json()["schema_version"] == "shareholder-return-v2"
        detail = client.get("/api/v1/companies/issuer-v2")
        assert detail.status_code == 200
        assert detail.json()["data_status"] == "PARTIAL"
        assert detail.json()["analysis_status"]["status"] == "WAITING_RETRY"
        assert detail.json()["analysis_status"]["last_success"]["analysis_id"] == "analysis-v2"
        report = client.get("/api/v1/companies/issuer-v2/analysis/latest")
        assert report.status_code == 200 and report.json()["analysis_id"] == "analysis-v2"
        definitions = client.get("/api/v1/metric-definitions")
        assert definitions.status_code == 200 and len(definitions.json()["metrics"]) >= 20
        pipeline = client.get("/api/v1/pipeline/status")
        assert pipeline.status_code == 200 and pipeline.json()["structured_release"]
        assert pipeline.json()["structured_release"] != "stale-previous-release"
        watchlist = client.get("/api/watchlist")
        assert watchlist.status_code == 200
        assert watchlist.json()["shareholderReturnV2"]["available"] is True
        security = watchlist.json()["securities"][0]
        assert security["shareholderReturnV2"]["company_id"] == "issuer-v2"
        assert security["shareholderReturnV2"]["analysis"]["status"] == "WAITING_RETRY"
        assert security["shareholderReturnV2"]["analysis"]["last_success"]["analysis_id"] == "analysis-v2"


def test_feature_flag_keeps_v1_and_disables_enrichment(tmp_path: Path) -> None:
    with ASGIClient(build_app(tmp_path, enabled=False)) as client:
        payload = client.get("/api/watchlist").json()
        assert payload["shareholderReturnV2"] == {"enabled": False, "available": False}
        assert client.get("/api/v1/companies").status_code == 503


def test_api_rejects_unsafe_company_path(tmp_path: Path) -> None:
    with ASGIClient(build_app(tmp_path)) as client:
        assert client.get("/api/v1/companies/%2E%2E").status_code in {404, 307}
