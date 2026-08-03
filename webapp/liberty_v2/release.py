from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .constants import CALCULATION_VERSION, METRIC_DEFINITION_VERSION, SCHEMA_VERSION


class ReleaseError(RuntimeError):
    pass


SAFE_RELEASE_ID = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
SAFE_COMPANY_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
PUBLIC_ANALYSIS_STATUS_FIELDS = {
    "status",
    "job_id",
    "analysis_mode",
    "trigger_type",
    "created_at",
    "started_at",
    "finished_at",
    "next_retry_at",
    "attempt_count",
    "prompt_version",
    "model",
    "reasoning_effort",
    "latest_success_at",
    "latest_analysis_id",
    "latest_analysis_as_of_date",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> Path:
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ReleaseError(f"unsafe release path: {value}")
    return Path(*pure.parts)


def atomic_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.parent / f".{link.name}.{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def verify_release(path: Path) -> dict[str, Any]:
    manifest_path = path / "manifest.json"
    checksums_path = path / "SHA256SUMS"
    if not manifest_path.is_file() or not checksums_path.is_file():
        raise ReleaseError("release manifest or SHA256SUMS is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise ReleaseError("invalid release manifest")
    expected_paths: set[str] = set()
    for item in manifest["files"]:
        relative = _safe_relative(item["path"])
        target = path / relative
        if not target.is_file():
            raise ReleaseError(f"manifest file is missing: {relative}")
        digest = sha256_file(target)
        if digest != item["sha256"] or target.stat().st_size != item["size"]:
            raise ReleaseError(f"manifest checksum mismatch: {relative}")
        expected_paths.add(relative.as_posix())
    checksum_lines = [line for line in checksums_path.read_text(encoding="utf-8").splitlines() if line]
    for line in checksum_lines:
        try:
            digest, relative_text = line.split("  ", 1)
        except ValueError as error:
            raise ReleaseError("invalid SHA256SUMS line") from error
        relative = _safe_relative(relative_text)
        target = path / relative
        if not target.is_file() or sha256_file(target) != digest:
            raise ReleaseError(f"SHA256SUMS mismatch: {relative}")
    actual_payloads = {
        file.relative_to(path).as_posix()
        for file in path.rglob("*")
        if file.is_file() and file.name not in {"manifest.json", "SHA256SUMS"}
    }
    if actual_payloads != expected_paths:
        raise ReleaseError(
            f"release file set mismatch: missing={sorted(expected_paths-actual_payloads)} extra={sorted(actual_payloads-expected_paths)}"
        )
    return manifest


class AtomicReleaseBuilder:
    def __init__(self, channel_root: Path, *, keep_releases: int = 5) -> None:
        self.channel_root = channel_root
        self.keep_releases = max(1, keep_releases)

    def build(
        self,
        files: Mapping[str, bytes],
        *,
        channel: str,
        metadata: Mapping[str, Any] | None = None,
        release_id: str | None = None,
        activate: bool = True,
    ) -> Path:
        if not files:
            raise ReleaseError("cannot build an empty release")
        canonical_hash = hashlib.sha256()
        for name in sorted(files):
            canonical_hash.update(name.encode("utf-8"))
            canonical_hash.update(files[name])
        identifier = release_id or (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + canonical_hash.hexdigest()[:12]
        )
        if not SAFE_RELEASE_ID.fullmatch(identifier):
            raise ReleaseError("unsafe release ID")
        releases = self.channel_root / "releases"
        incoming_root = releases / ".incoming"
        incoming = incoming_root / identifier
        final = releases / identifier
        if final.exists() or incoming.exists():
            raise ReleaseError(f"release already exists: {identifier}")
        incoming.mkdir(parents=True, exist_ok=False)
        manifest_files: list[dict[str, Any]] = []
        try:
            for name, content in sorted(files.items()):
                relative = _safe_relative(name)
                target = incoming / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                manifest_files.append(
                    {
                        "path": relative.as_posix(),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
                )
            manifest = {
                "manifest_version": "1.0",
                "release_id": identifier,
                "channel": channel,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "files": manifest_files,
                "metadata": dict(metadata or {}),
            }
            manifest_bytes = json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8") + b"\n"
            (incoming / "manifest.json").write_bytes(manifest_bytes)
            checksum_entries = [
                (item["sha256"], item["path"]) for item in manifest_files
            ] + [(hashlib.sha256(manifest_bytes).hexdigest(), "manifest.json")]
            (incoming / "SHA256SUMS").write_text(
                "".join(f"{digest}  {name}\n" for digest, name in checksum_entries),
                encoding="utf-8",
            )
            verify_release(incoming)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(incoming, final)
            if activate:
                atomic_symlink(final, self.channel_root / "current")
                self.prune()
            return final
        except Exception:
            if incoming.exists():
                shutil.rmtree(incoming)
            raise

    def activate(self, release_id: str) -> Path:
        if not SAFE_RELEASE_ID.fullmatch(release_id):
            raise ReleaseError("unsafe release ID")
        release = self.channel_root / "releases" / release_id
        verify_release(release)
        atomic_symlink(release, self.channel_root / "current")
        return release

    def prune(self) -> None:
        releases_root = self.channel_root / "releases"
        current = (self.channel_root / "current").resolve() if (self.channel_root / "current").exists() else None
        releases = sorted(
            (path for path in releases_root.iterdir() if path.is_dir() and path.name != ".incoming"),
            key=lambda path: path.name,
            reverse=True,
        ) if releases_root.exists() else []
        keep = {path.resolve() for path in releases[: self.keep_releases]}
        if current:
            keep.add(current)
        for path in releases:
            if path.resolve() not in keep:
                shutil.rmtree(path)


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"


def build_structured_release(
    channel_root: Path,
    *,
    companies: list[Mapping[str, Any]],
    metric_definitions: Mapping[str, Any],
    pipeline_status: Mapping[str, Any],
    activate: bool = True,
) -> Path:
    identifiers = [str(company.get("company_id") or "") for company in companies]
    if (
        any(not SAFE_COMPANY_ID.fullmatch(identifier) for identifier in identifiers)
        or len(identifiers) != len(set(identifiers))
    ):
        raise ReleaseError("company IDs must be safe and unique")
    index = {
        "schema_version": SCHEMA_VERSION,
        "calculation_version": CALCULATION_VERSION,
        "metric_definition_version": METRIC_DEFINITION_VERSION,
        "company_count": len(companies),
        "companies": [
            {
                "company_id": company["company_id"],
                "company_name": company.get("company_name"),
                "securities": company.get("securities", []),
                "as_of_date": company.get("as_of_date"),
                "price_timestamp": company.get("price_timestamp"),
                "data_status": company.get("data_status"),
                "update_status": company.get("update_status"),
                "metrics": company.get("metrics", {}),
                "security_metrics": company.get("security_metrics", {}),
                "scores": company.get("scores", {}),
                "classification": company.get("classification"),
                "return_type": company.get("return_type"),
                "veto_flags": company.get("veto_flags", []),
                "analysis_status": company.get("analysis_status", {}),
                "coverage_adapter": company.get("coverage_adapter", {}),
            }
            for company in companies
        ],
    }
    files: dict[str, bytes] = {
        "companies.json": json_bytes(index),
        "metric_definitions.json": json_bytes(metric_definitions),
        "pipeline_status.json": json_bytes(pipeline_status),
    }
    for company in companies:
        files[f"companies/{company['company_id']}.json"] = json_bytes(company)
    return AtomicReleaseBuilder(channel_root).build(
        files,
        channel="structured",
        metadata={
            "schema_version": SCHEMA_VERSION,
            "calculation_version": CALCULATION_VERSION,
            "metric_definition_version": METRIC_DEFINITION_VERSION,
            "company_count": len(companies),
        },
        activate=activate,
    )


def build_analysis_release(
    channel_root: Path,
    *,
    analyses: Mapping[str, tuple[Mapping[str, Any], str]],
    statuses: Mapping[str, Mapping[str, Any]] | None = None,
    activate: bool = True,
) -> Path:
    files: dict[str, bytes] = {}
    index: list[dict[str, Any]] = []
    for company_id, (payload, report) in sorted(analyses.items()):
        if not SAFE_COMPANY_ID.fullmatch(company_id):
            raise ReleaseError("unsafe company ID in analysis release")
        public = dict(payload)
        # Candidate score configuration is a Linux-private reviewed overlay. It
        # must never be confused with the public qualitative report or accepted
        # by the read-only FastAPI host as an authoritative score write.
        public.pop("reviewed_overlay_candidates", None)
        files[f"companies/{company_id}/latest.json"] = json_bytes(public)
        files[f"companies/{company_id}/report.md"] = report.encode("utf-8")
        files[f"companies/{company_id}/source_summary.json"] = json_bytes(
            {"analysis_id": payload["analysis_id"], "sources": payload["sources"]}
        )
        index.append(
            {
                "company_id": company_id,
                "analysis_id": payload["analysis_id"],
                "as_of_date": payload["as_of_date"],
                "input_snapshot_hash": payload["input_snapshot_hash"],
                "verdict": payload["verdict"],
                "risk_overlay": payload["risk_overlay"],
            }
        )
    status_index: list[dict[str, Any]] = []
    for company_id, raw_status in sorted((statuses or {}).items()):
        if not SAFE_COMPANY_ID.fullmatch(company_id):
            raise ReleaseError("unsafe company ID in analysis status release")
        public_status = {
            key: raw_status.get(key)
            for key in sorted(PUBLIC_ANALYSIS_STATUS_FIELDS)
            if key in raw_status
        }
        public_status["company_id"] = company_id
        files[f"companies/{company_id}/status.json"] = json_bytes(public_status)
        status_index.append(public_status)
    files["analyses.json"] = json_bytes(
        {
            "schema_version": "1.0",
            "analyses": index,
            "statuses": status_index,
        }
    )
    return AtomicReleaseBuilder(channel_root).build(
        files,
        channel="analysis",
        metadata={
            "schema_version": "1.0",
            "analysis_count": len(index),
            "status_count": len(status_index),
        },
        activate=activate,
    )
