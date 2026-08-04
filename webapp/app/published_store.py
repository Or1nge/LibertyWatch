"""Read-only access to manifest-verified shareholder-return v2 releases."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Mapping

from .v2_contract import V2CanarySummary, validate_public_index


SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
SENSITIVE_ABSOLUTE_PATH = re.compile(r"/(?:home|var|etc|opt|root)/[^\s,;]+")


class PublishedDataError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(value: str) -> Path:
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise PublishedDataError("manifest contains an unsafe path")
    return Path(*pure.parts)


def _verify_release(path: Path, expected_channel: str) -> dict[str, Any]:
    manifest_path = path / "manifest.json"
    checksums_path = path / "SHA256SUMS"
    if not manifest_path.is_file() or not checksums_path.is_file():
        raise PublishedDataError("manifest or SHA256SUMS is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("channel") != expected_channel or not isinstance(manifest.get("files"), list):
        raise PublishedDataError("release channel or file list is invalid")
    expected_paths: set[str] = set()
    for item in manifest["files"]:
        relative = _relative(str(item.get("path") or ""))
        target = path / relative
        if (
            not target.is_file()
            or target.stat().st_size != item.get("size")
            or _sha256(target) != item.get("sha256")
        ):
            raise PublishedDataError(f"release checksum mismatch: {item.get('path')}")
        expected_paths.add(relative.as_posix())
    checksum_paths: set[str] = set()
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            digest, name = line.split("  ", 1)
        except ValueError as error:
            raise PublishedDataError("invalid SHA256SUMS") from error
        relative = _relative(name)
        target = path / relative
        if not target.is_file() or _sha256(target) != digest:
            raise PublishedDataError(f"SHA256SUMS mismatch: {name}")
        checksum_paths.add(relative.as_posix())
    if checksum_paths != expected_paths | {"manifest.json"}:
        raise PublishedDataError("SHA256SUMS does not cover the exact release file set")
    actual_paths = {
        item.relative_to(path).as_posix()
        for item in path.rglob("*")
        if item.is_file() and item.name not in {"manifest.json", "SHA256SUMS"}
    }
    if actual_paths != expected_paths:
        raise PublishedDataError("release contains unmanifested or missing files")
    return manifest


class PublishedV2Store:
    def __init__(
        self,
        *,
        structured_root: Path,
        analysis_root: Path,
        definitions_path: Path,
        enabled: bool = True,
        analysis_enabled: bool = True,
    ) -> None:
        self.structured_root = structured_root
        self.analysis_root = analysis_root
        self.definitions_path = definitions_path
        self.enabled = enabled
        self.analysis_enabled = analysis_enabled
        self._lock = RLock()
        self._structured_signature: tuple[str, int, int] | None = None
        self._analysis_signature: tuple[str, int, int] | None = None
        self._structured_path: Path | None = None
        self._analysis_path: Path | None = None
        self._companies_index: dict[str, Any] | None = None
        self._structured_summary: V2CanarySummary | None = None
        self._definitions: dict[str, Any] | None = None
        self._pipeline_status: dict[str, Any] | None = None
        self._analysis_index: dict[str, Any] | None = None
        self._last_error: str | None = None

    @staticmethod
    def _signature(root: Path) -> tuple[str, int, int] | None:
        current = root / "current"
        if not current.exists():
            return None
        resolved = current.resolve()
        manifest = resolved / "manifest.json"
        if not manifest.is_file():
            return None
        stat = manifest.stat()
        return str(resolved), stat.st_mtime_ns, stat.st_size

    def initialize(self) -> None:
        if not self.enabled:
            return
        self.refresh(force=True)

    def refresh(self, *, force: bool = False) -> bool:
        if not self.enabled:
            return False
        changed = False
        try:
            structured_signature = self._signature(self.structured_root)
            if structured_signature and (force or structured_signature != self._structured_signature):
                path = Path(structured_signature[0])
                _verify_release(path, "structured")
                index = json.loads((path / "companies.json").read_text(encoding="utf-8"))
                definitions = json.loads((path / "metric_definitions.json").read_text(encoding="utf-8"))
                pipeline = json.loads((path / "pipeline_status.json").read_text(encoding="utf-8"))
                if index.get("schema_version") not in {"shareholder-screen-v2", "shareholder-return-v2"}:
                    raise PublishedDataError("unsupported structured schema version")
                try:
                    summary = validate_public_index(index)
                except ValueError as error:
                    raise PublishedDataError(str(error)) from error
                with self._lock:
                    self._structured_path = path
                    self._structured_signature = structured_signature
                    self._companies_index = index
                    self._structured_summary = summary
                    self._definitions = definitions
                    self._pipeline_status = pipeline
                changed = True
            elif structured_signature is None and self._definitions is None and self.definitions_path.is_file():
                definitions = json.loads(self.definitions_path.read_text(encoding="utf-8"))
                with self._lock:
                    self._definitions = definitions

            analysis_signature = self._signature(self.analysis_root) if self.analysis_enabled else None
            if analysis_signature and (force or analysis_signature != self._analysis_signature):
                path = Path(analysis_signature[0])
                _verify_release(path, "analysis")
                index = json.loads((path / "analyses.json").read_text(encoding="utf-8"))
                with self._lock:
                    self._analysis_path = path
                    self._analysis_signature = analysis_signature
                    self._analysis_index = index
                changed = True
            with self._lock:
                self._last_error = None
        except Exception as error:
            with self._lock:
                self._last_error = f"{type(error).__name__}: {error}"[:1000]
            return False
        return changed

    def definitions(self) -> dict[str, Any] | None:
        self.refresh()
        with self._lock:
            return deepcopy(self._definitions)

    def companies(self) -> dict[str, Any] | None:
        self.refresh()
        with self._lock:
            payload = deepcopy(self._companies_index)
        if payload is None:
            return None
        for item in payload.get("companies", []):
            company_id = str(item.get("company_id") or "")
            item["analysis_status"] = self.combined_analysis_status(
                company_id,
                fallback=item.get("analysis_status"),
            )
        return payload

    def company(self, company_id: str) -> dict[str, Any] | None:
        if not SAFE_ID.fullmatch(company_id):
            return None
        self.refresh()
        with self._lock:
            root = self._structured_path
        if root is None:
            return None
        path = root / "companies" / f"{company_id}.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        if value.get("company_id") != company_id or value.get("schema_version") not in {"shareholder-screen-v2", "shareholder-return-v2"}:
            return None
        value["analysis_status"] = self.combined_analysis_status(
            company_id,
            fallback=value.get("analysis_status"),
        )
        return value

    def analysis_status(self, company_id: str) -> dict[str, Any] | None:
        if not SAFE_ID.fullmatch(company_id):
            return None
        self.refresh()
        with self._lock:
            root = self._analysis_path
        if root is None:
            return None
        path = root / "companies" / company_id / "status.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None

    def latest_analysis(self, company_id: str) -> dict[str, Any] | None:
        if not SAFE_ID.fullmatch(company_id):
            return None
        self.refresh()
        with self._lock:
            root = self._analysis_path
        if root is None:
            return None
        path = root / "companies" / company_id / "latest.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None

    def combined_analysis_status(
        self,
        company_id: str,
        *,
        fallback: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        value = deepcopy(self.analysis_status(company_id) or dict(fallback or {}))
        analysis = self.latest_analysis(company_id)
        if analysis is None:
            return value or {"status": "NOT_REQUESTED"}
        success = {
            "analysis_id": analysis.get("analysis_id"),
            "as_of_date": analysis.get("as_of_date"),
            "verdict": analysis.get("verdict"),
            "risk_overlay": analysis.get("risk_overlay"),
            "one_sentence_conclusion": analysis.get("one_sentence_conclusion"),
            "price_assessment": analysis.get("price_assessment"),
            "opportunity_or_trap": analysis.get("opportunity_or_trap"),
            "trigger_validity": analysis.get("trigger_validity"),
            "cash_return_sustainability": analysis.get("cash_return_sustainability"),
            "top_risks": analysis.get("top_risks", []),
            "sources": analysis.get("sources", []),
        }
        if not value or value.get("status") == "NOT_REQUESTED":
            value["status"] = "SUCCEEDED"
        value.setdefault("latest_analysis_id", analysis.get("analysis_id"))
        value.setdefault("latest_analysis_as_of_date", analysis.get("as_of_date"))
        value.update(success)
        value["last_success"] = success
        return value

    def pipeline_status(self) -> dict[str, Any]:
        self.refresh()
        with self._lock:
            last_error = (
                SENSITIVE_ABSOLUTE_PATH.sub("[internal path]", self._last_error)
                if self._last_error
                else None
            )
            return {
                **deepcopy(self._pipeline_status or {}),
                "enabled": self.enabled,
                "structured_release": self._structured_path.name if self._structured_path else None,
                "analysis_release": self._analysis_path.name if self._analysis_path else None,
                "last_error": last_error,
                "release_validity": (
                    self._companies_index.get("release_validity")
                    if self._companies_index
                    else None
                ),
                "company_status_counts": (
                    dict(self._structured_summary.tier_counts)
                    if self._structured_summary
                    else {}
                ),
                "opportunity_score_count": (
                    self._structured_summary.opportunity_score_count
                    if self._structured_summary
                    else 0
                ),
                "financial_resilience_score_count": (
                    self._structured_summary.financial_resilience_score_count
                    if self._structured_summary
                    else 0
                ),
                "analysis_public_enabled": self.analysis_enabled,
            }

    def enrich_watchlist(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(snapshot))
        if not self.enabled:
            result["shareholderReturnV2"] = {"enabled": False, "available": False}
            return result
        index = self.companies()
        definitions = self.definitions()
        if index is None:
            result["shareholderReturnV2"] = {
                "enabled": True,
                "available": False,
                "schemaVersion": "shareholder-screen-v2",
                "message": "v2结构化数据尚未发布，继续显示v1。",
            }
            result["metricDefinitionsV2"] = (definitions or {}).get("metrics", [])
            return result
        by_company = {str(item.get("company_id")): item for item in index.get("companies", [])}
        for security in result.get("securities", []):
            issuer_id = str(security.get("issuerId") or "")
            security["shareholderReturnV2"] = deepcopy(by_company.get(issuer_id))
            if security["shareholderReturnV2"]:
                security["shareholderReturnV2"]["analysis"] = self.combined_analysis_status(
                    issuer_id,
                    fallback=security["shareholderReturnV2"].get("analysis_status"),
                )
        result["shareholderReturnV2"] = {
            "enabled": True,
            "available": True,
            "schemaVersion": index.get("schema_version"),
            "calculationVersion": index.get("calculation_version"),
            "metricDefinitionVersion": index.get("metric_definition_version"),
            "companyCount": index.get("company_count"),
            "releaseValidity": index.get("release_validity"),
            "statusCounts": (
                dict(self._structured_summary.tier_counts)
                if self._structured_summary
                else {}
            ),
            "opportunityScoreCount": (
                self._structured_summary.opportunity_score_count
                if self._structured_summary
                else 0
            ),
            "financialResilienceScoreCount": (
                self._structured_summary.financial_resilience_score_count
                if self._structured_summary
                else 0
            ),
            "tierCounts": (
                dict(self._structured_summary.tier_counts)
                if self._structured_summary
                else {}
            ),
            "scoredCompanyCount": (
                len(self._structured_summary.scored_company_ids)
                if self._structured_summary
                else 0
            ),
            "releaseId": self._structured_path.name if self._structured_path else None,
            "analysisReleaseId": self._analysis_path.name if self._analysis_path else None,
        }
        result["metricDefinitionsV2"] = (definitions or {}).get("metrics", [])
        return result
