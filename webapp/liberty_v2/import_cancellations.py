from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .models import DataStatus, RawDataPoint


EXPECTED_COUNT = 6
RECONCILIATION_SCHEMA = "cancellation-reconciliation-v1.0"


class CancellationImportError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CancellationImportError(f"cannot read reconciliation JSON: {path}") from error
    if not isinstance(value, dict):
        raise CancellationImportError(f"reconciliation JSON must be an object: {path}")
    return value


def _safe_child(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise CancellationImportError(f"unsafe manifest path: {relative!r}")
    path = (root / relative).resolve()
    if root not in path.parents:
        raise CancellationImportError(f"manifest path escapes reconciliation root: {relative}")
    return path


def _verify_bundle_manifest(root: Path) -> dict[str, Path]:
    manifest_path = root / "manifest.json"
    manifest = _load_object(manifest_path)
    if manifest.get("schema_version") != "cancellation-reconciliation-manifest-v1":
        raise CancellationImportError("unsupported cancellation reconciliation manifest")
    listed: dict[str, Path] = {}
    for item in manifest.get("files", []):
        if not isinstance(item, Mapping):
            raise CancellationImportError("manifest files must be objects")
        relative = str(item.get("path") or "")
        if relative in listed:
            raise CancellationImportError(f"duplicate manifest path: {relative}")
        path = _safe_child(root, relative)
        if not path.is_file():
            raise CancellationImportError(f"manifest file is missing: {relative}")
        if path.stat().st_size != int(item.get("size_bytes") or -1):
            raise CancellationImportError(f"manifest size mismatch: {relative}")
        if _sha256(path) != str(item.get("sha256") or ""):
            raise CancellationImportError(f"manifest SHA-256 mismatch: {relative}")
        listed[relative] = path
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json" and not path.name.startswith(".")
    }
    if set(listed) != actual:
        raise CancellationImportError("manifest does not exactly cover the reconciliation bundle")
    if len(listed) != int(manifest.get("file_count") or -1):
        raise CancellationImportError("manifest file_count mismatch")
    required = {"report.json", "review_basis.json"}
    if not required.issubset(listed):
        raise CancellationImportError("reconciliation report or review basis is missing")
    return listed


def _review_map(review_basis: Mapping[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
    if review_basis.get("schema_version") != "cancellation-reconciliation-review-v1.0":
        raise CancellationImportError("unsupported cancellation review basis")
    policy = review_basis.get("policy")
    if not isinstance(policy, Mapping) or policy.get("production_write") is not False:
        raise CancellationImportError("review basis is not marked read-only")
    reviews: dict[tuple[str, int], dict[str, Any]] = {}
    for raw in review_basis.get("reviews", []):
        if not isinstance(raw, Mapping):
            raise CancellationImportError("review entry must be an object")
        item = dict(raw)
        key = (str(item.get("company_id") or ""), int(item.get("fiscal_year") or 0))
        if not key[0] or key[1] <= 0 or key in reviews:
            raise CancellationImportError(f"invalid or duplicate review scope: {key}")
        if item.get("cancellation_fact_decision") != "ACCEPT":
            raise CancellationImportError(f"unaccepted cancellation fact: {key}")
        if item.get("diluted_share_bridge_status") == "ACCEPT":
            raise CancellationImportError(f"diluted-share bridge must not be accepted: {key}")
        if item.get("net_reduction_factor") is not None:
            raise CancellationImportError(f"net_reduction_factor must be null: {key}")
        if item.get("b_eligible_authorized") is not False:
            raise CancellationImportError(f"B_eligible must remain unauthorized: {key}")
        bridge = item.get("issued_share_bridge")
        if not isinstance(bridge, Mapping) or bridge.get("verified_cancelled_shares") in (None, ""):
            raise CancellationImportError(f"verified cancellation count is missing: {key}")
        reviews[key] = item
    if len(reviews) != EXPECTED_COUNT:
        raise CancellationImportError(
            f"expected {EXPECTED_COUNT} reviewed cancellation facts, got {len(reviews)}"
        )
    return reviews


def _decision_map(files: Mapping[str, Path]) -> dict[tuple[str, int], dict[str, Any]]:
    decisions: dict[tuple[str, int], dict[str, Any]] = {}
    for relative, path in sorted(files.items()):
        if not relative.startswith("decisions/") or not relative.endswith(".json"):
            continue
        item = _load_object(path)
        if item.get("schema_version") != RECONCILIATION_SCHEMA:
            raise CancellationImportError(f"unsupported decision schema: {relative}")
        key = (str(item.get("company_id") or ""), int(item.get("fiscal_year") or 0))
        if not key[0] or key[1] <= 0 or key in decisions:
            raise CancellationImportError(f"invalid or duplicate decision scope: {key}")
        decisions[key] = item
    if len(decisions) != EXPECTED_COUNT:
        raise CancellationImportError(
            f"expected {EXPECTED_COUNT} decision files, got {len(decisions)}"
        )
    return decisions


def _decimal_shares(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise CancellationImportError(f"invalid share count in {field}") from error
    if not result.is_finite() or result <= 0 or result != result.to_integral_value():
        raise CancellationImportError(f"share count must be a positive integer in {field}")
    return result


def _validate_decision(
    key: tuple[str, int],
    decision: Mapping[str, Any],
    review: Mapping[str, Any],
) -> Decimal:
    if decision.get("cancellation_fact_decision") != "ACCEPT":
        raise CancellationImportError(f"decision does not accept the cancellation fact: {key}")
    if decision.get("writes_production") is not False:
        raise CancellationImportError(f"decision is not read-only: {key}")
    if decision.get("diluted_share_bridge_status") == "ACCEPT":
        raise CancellationImportError(f"decision accepts an unverified diluted bridge: {key}")
    if decision.get("net_reduction_factor") is not None:
        raise CancellationImportError(f"decision contains net_reduction_factor: {key}")
    if decision.get("b_eligible") is not None or decision.get("b_eligible_authorized") is not False:
        raise CancellationImportError(f"decision contains or authorizes B_eligible: {key}")
    forbidden_numeric = (
        "gross_cancelled_buyback",
        "gross_buyback",
        "diluted_net_share_reduction",
        "net_diluted_share_reduction",
    )
    if any(decision.get(field) is not None for field in forbidden_numeric):
        raise CancellationImportError(f"decision contains a prohibited numeric field: {key}")
    value = _decimal_shares(decision.get("verified_cancelled_shares"), "decision")
    reviewed = _decimal_shares(
        review["issued_share_bridge"]["verified_cancelled_shares"], "review basis"
    )
    if value != reviewed:
        raise CancellationImportError(f"decision and review basis disagree: {key}")
    if key == ("HK2020", 2025) and value != Decimal("26570200"):
        raise CancellationImportError("ANTA FY2025 must use the corrected 26,570,200 shares")
    return value


def _source_point(
    key: tuple[str, int],
    value: Decimal,
    decision: Mapping[str, Any],
    annual_root: Path,
) -> RawDataPoint:
    company_id, fiscal_year = key
    matching_checks = [
        dict(item)
        for item in decision.get("source_checks", [])
        if isinstance(item, Mapping) and int(item.get("fiscal_year") or 0) == fiscal_year
    ]
    if len(matching_checks) != 1 or matching_checks[0].get("identity_status") != "VALID":
        raise CancellationImportError(f"one verified current-year source is required: {key}")
    source_check = matching_checks[0]
    manifest_paths = sorted((annual_root / "companies").glob(f"{company_id}_*/manifest.json"))
    if len(manifest_paths) != 1:
        raise CancellationImportError(f"one official annual-report manifest is required: {key}")
    annual_manifest = _load_object(manifest_paths[0])
    documents = [
        dict(item)
        for item in annual_manifest.get("documents", [])
        if isinstance(item, Mapping)
        and int(item.get("fiscal_year") or 0) == fiscal_year
        and item.get("selection_status") == "SELECTED_CURRENT"
        and item.get("data_status") == "VERIFIED"
    ]
    if len(documents) != 1:
        raise CancellationImportError(f"one selected verified annual report is required: {key}")
    document = documents[0]
    comparisons = {
        "source_document": document.get("source_document"),
        "source_url": document.get("source_url"),
        "source_publish_date": document.get("source_publish_date"),
        "sha256": document.get("sha256"),
        "pdf_pages": document.get("pdf_pages"),
    }
    for field, actual in comparisons.items():
        if str(source_check.get(field) or "") != str(actual or ""):
            raise CancellationImportError(f"official source {field} mismatch: {key}")
    source_path = (annual_root / str(document.get("local_path") or "")).resolve()
    if annual_root not in source_path.parents or not source_path.is_file():
        raise CancellationImportError(f"official annual report is missing or unsafe: {key}")
    if str(source_check.get("source_local_path") or "") != str(source_path):
        raise CancellationImportError(f"official source path mismatch: {key}")

    try:
        publish_date = date.fromisoformat(str(document["source_publish_date"]))
        fetch_time = datetime.fromisoformat(
            str(document["source_fetch_time"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError) as error:
        raise CancellationImportError(f"official source date metadata is invalid: {key}") from error
    if fetch_time.tzinfo is None:
        fetch_time = fetch_time.replace(tzinfo=timezone.utc)
    restatement_status = str(document.get("restatement_status") or "")
    if not restatement_status:
        raise CancellationImportError(f"official restatement status is missing: {key}")
    security_id = str(document.get("security_id") or "") or None
    share_class = str(document.get("share_class") or "") or None
    return RawDataPoint(
        company_id=company_id,
        field_id=f"FY{fiscal_year}.cancelled_shares",
        security_id=security_id,
        share_class=share_class,
        source_name="HKEX official annual report",
        source_document=str(document["source_document"]),
        source_url_or_local_path=str(document["source_url"]),
        source_publish_date=publish_date,
        source_fetch_time=fetch_time,
        fiscal_period=f"FY{fiscal_year}",
        currency=None,
        unit="shares",
        value=value,
        data_status=DataStatus.VALID,
        restatement_status=restatement_status,
    )


def load_confirmed_cancellation_points(
    reconciliation_root: Path,
    annual_report_root: Path,
) -> tuple[RawDataPoint, ...]:
    """Read six controlled cancellation facts without mutating any data store.

    The function deliberately returns only ``FY{year}.cancelled_shares`` raw
    points.  It never returns gross buyback cash, diluted net reduction,
    ``net_reduction_factor`` or ``B_eligible``.
    """

    root = reconciliation_root.resolve()
    annual_root = annual_report_root.resolve()
    files = _verify_bundle_manifest(root)
    report = _load_object(files["report.json"])
    review_basis = _load_object(files["review_basis.json"])
    if report.get("schema_version") != RECONCILIATION_SCHEMA:
        raise CancellationImportError("unsupported reconciliation report")
    if report.get("writes_production") is not False:
        raise CancellationImportError("reconciliation report is not read-only")
    if int(report.get("candidate_count") or 0) != EXPECTED_COUNT:
        raise CancellationImportError("reconciliation report candidate_count mismatch")
    if report.get("cancellation_fact_counts", {}).get("ACCEPT") != EXPECTED_COUNT:
        raise CancellationImportError("report does not accept all six cancellation facts")
    if report.get("diluted_share_bridge_counts", {}).get("ACCEPT") != 0:
        raise CancellationImportError("report accepts a diluted-share bridge")
    if report.get("net_reduction_factor_calculated_count") != 0:
        raise CancellationImportError("report contains a net_reduction_factor")
    if report.get("b_eligible_authorized_count") != 0:
        raise CancellationImportError("report authorizes B_eligible")
    # The report records the SHA-256 of the human-authored review config,
    # whereas review_basis.json is an atomically re-serialized copy.  Their
    # byte hashes therefore differ even when their JSON values are identical.
    # The bundle manifest authenticates the copied basis; here we additionally
    # require a well-formed source-config hash and validate its full semantics
    # against every decision below.
    if re.fullmatch(r"[0-9a-f]{64}", str(report.get("review_config_sha256") or "")) is None:
        raise CancellationImportError("report review_config_sha256 is invalid")

    reviews = _review_map(review_basis)
    decisions = _decision_map(files)
    if set(reviews) != set(decisions):
        raise CancellationImportError("review and decision scopes disagree")
    summaries = {
        (str(item.get("company_id") or ""), int(item.get("fiscal_year") or 0)): dict(item)
        for item in report.get("summary", [])
        if isinstance(item, Mapping)
    }
    if set(summaries) != set(decisions):
        raise CancellationImportError("report summary and decision scopes disagree")

    points: list[RawDataPoint] = []
    for key in sorted(decisions):
        decision = decisions[key]
        value = _validate_decision(key, decision, reviews[key])
        summary = summaries[key]
        if str(summary.get("verified_cancelled_shares") or "") != format(value, "f"):
            raise CancellationImportError(f"report summary value mismatch: {key}")
        if summary.get("cancellation_fact_decision") != "ACCEPT":
            raise CancellationImportError(f"report summary does not accept fact: {key}")
        if summary.get("diluted_share_bridge_status") == "ACCEPT":
            raise CancellationImportError(f"report summary accepts diluted bridge: {key}")
        if summary.get("net_reduction_factor") is not None:
            raise CancellationImportError(f"report summary contains net_reduction_factor: {key}")
        points.append(_source_point(key, value, decision, annual_root))

    scoped_field_ids = {(point.company_id, point.field_id) for point in points}
    if len(points) != EXPECTED_COUNT or len(scoped_field_ids) != EXPECTED_COUNT:
        raise CancellationImportError("controlled import did not produce six unique facts")
    if any(not point.field_id.endswith(".cancelled_shares") for point in points):
        raise CancellationImportError("controlled import produced a forbidden field")
    return tuple(points)
