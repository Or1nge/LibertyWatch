from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .cashflow_reconciliation import sha256_file
from .cashflow_reconciliation_v2 import SCHEMA_VERSION, accepted_status
from .import_cashflow import ReviewedCashflowImport
from .models import DataStatus, RawDataPoint


REQUIRED_DECISION_COUNT = 560


class CashflowV2ImportError(RuntimeError):
    pass


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CashflowV2ImportError(f"cannot read JSON: {path}") from error
    if not isinstance(value, dict):
        raise CashflowV2ImportError(f"expected object: {path}")
    return value


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise CashflowV2ImportError(f"invalid Decimal {label}: {value!r}") from error
    if not result.is_finite():
        raise CashflowV2ImportError(f"NaN/Infinity forbidden: {label}")
    return result


def _exact(left: Any, right: Any) -> bool:
    try:
        return _decimal(left, "left") == _decimal(right, "right")
    except CashflowV2ImportError:
        return False


def _verify_manifest(root: Path, expected_sha256: str | None) -> None:
    path = root / "manifest.json"
    if expected_sha256 and sha256_file(path) != expected_sha256:
        raise CashflowV2ImportError("cashflow-v2 manifest does not match pinned SHA-256")
    manifest = _object(path)
    declared = set()
    for raw in manifest.get("files", []):
        item = dict(raw)
        target = (root / str(item.get("path") or "")).resolve()
        if root not in target.parents or not target.is_file():
            raise CashflowV2ImportError(f"unsafe/missing v2 input: {target}")
        if target.stat().st_size != int(item.get("size_bytes") or -1):
            raise CashflowV2ImportError(f"v2 input size mismatch: {target}")
        if sha256_file(target) != str(item.get("sha256") or ""):
            raise CashflowV2ImportError(f"v2 input SHA mismatch: {target}")
        declared.add(target.relative_to(root).as_posix())
    actual = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*")
        if item.is_file() and item.name != "manifest.json"
    }
    if declared != actual or len(declared) != int(manifest.get("file_count") or -1):
        raise CashflowV2ImportError("v2 manifest coverage mismatch")


def _verify_upstream(descriptor: Any, label: str) -> None:
    if not isinstance(descriptor, Mapping):
        raise CashflowV2ImportError(f"{label} descriptor missing")
    path = Path(str(descriptor.get("path") or ""))
    expected = str(descriptor.get("sha256") or "")
    if not path.is_file() or not expected or sha256_file(path) != expected:
        raise CashflowV2ImportError(f"{label} changed after reconciliation")


def _official_index(root: Path) -> tuple[dict[tuple[str, int, str], dict[str, Any]], str]:
    index = {}
    hashes = {}
    for manifest_path in sorted(root.glob("companies/*/manifest.json")):
        manifest = _object(manifest_path)
        manifest_sha = sha256_file(manifest_path)
        hashes[str(manifest_path.resolve())] = manifest_sha
        for raw in manifest.get("documents", []):
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            if item.get("selection_status") != "SELECTED_CURRENT" or item.get("data_status") != "VERIFIED":
                continue
            item["source_manifest_path"] = str(manifest_path.resolve())
            item["source_manifest_sha256"] = manifest_sha
            item["resolved_local_path"] = str((root / str(item["local_path"])).resolve())
            key = (str(item["company_id"]), int(item["fiscal_year"]), str(item["sha256"]))
            if key in index:
                raise CashflowV2ImportError(f"duplicate official source: {key}")
            index[key] = item
    digest = hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return index, digest


def _date(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise CashflowV2ImportError(f"invalid source date: {value!r}") from error


def _datetime(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise CashflowV2ImportError(f"invalid source datetime: {value!r}") from error
    if result.tzinfo is None:
        raise CashflowV2ImportError("source_fetch_time must include timezone")
    return result.astimezone(timezone.utc)


def _accepted_point(
    decision: Mapping[str, Any],
    official_index: Mapping[tuple[str, int, str], Mapping[str, Any]],
    verified_pdfs: set[tuple[str, str]],
) -> tuple[RawDataPoint, str]:
    decision_id = str(decision.get("decision_id") or "")
    status = str(decision.get("status") or "")
    if not accepted_status(status) or decision.get("eligible_for_read_only_import") is not True:
        raise CashflowV2ImportError(f"decision is not importable: {decision_id}")
    checks = decision.get("official_document_checks")
    if not isinstance(checks, Mapping) or not checks or any(value is not True for value in checks.values()):
        raise CashflowV2ImportError(f"official document checks failed: {decision_id}")
    if decision.get("statement_fiscal_year_check") is not True:
        raise CashflowV2ImportError(f"statement fiscal year failed: {decision_id}")
    source = decision.get("official_source")
    if not isinstance(source, Mapping):
        raise CashflowV2ImportError(f"official source missing: {decision_id}")
    key = (
        str(decision.get("company_id") or ""),
        int(decision.get("fiscal_year") or 0),
        str(source.get("source_sha256") or ""),
    )
    metadata = official_index.get(key)
    if metadata is None:
        raise CashflowV2ImportError(f"source is not selected official report: {decision_id}")
    exact_metadata = (
        str(source.get("source_document")) == str(metadata.get("source_document"))
        and str(source.get("source_url")) == str(metadata.get("source_url"))
        and str(source.get("source_manifest_sha256")) == str(metadata.get("source_manifest_sha256"))
        and str(source.get("source_fetch_time")) == str(metadata.get("source_fetch_time"))
        and str(source.get("restatement_status")) == str(metadata.get("restatement_status"))
    )
    if not exact_metadata:
        raise CashflowV2ImportError(f"official metadata conflict: {decision_id}")
    pdf_path = Path(str(metadata["resolved_local_path"]))
    pdf_key = (str(pdf_path), key[2])
    if pdf_key not in verified_pdfs:
        if not pdf_path.is_file() or sha256_file(pdf_path) != key[2]:
            raise CashflowV2ImportError(f"official PDF hash mismatch: {decision_id}")
        verified_pdfs.add(pdf_key)
    value = _decimal(decision.get("accepted_value"), decision_id)
    if not _exact(value, source.get("current_value")):
        raise CashflowV2ImportError(f"accepted/official value conflict: {decision_id}")
    adjacent = decision.get("adjacent_reconciliation") or {}
    futu = decision.get("futu_source") or {}
    futu_status = str(futu.get("field_data_status") or "")
    futu_numeric = futu_status in {"VALID", "KNOWN_ZERO"} and futu.get("field_value") is not None
    if futu_numeric and (
        str(futu.get("field_currency") or "") != str(source.get("currency") or "")
        or not _exact(value, futu.get("field_value"))
    ):
        raise CashflowV2ImportError(f"accepted/Futu value conflict: {decision_id}")
    if status == "ACCEPT_OFFICIAL_ADJACENT" and "MATCH" not in {
        adjacent.get("backward"),
        adjacent.get("forward"),
    }:
        raise CashflowV2ImportError(f"adjacent acceptance lacks exact match: {decision_id}")
    if status in {"ACCEPT_V1", "ACCEPT_OFFICIAL_PLUS_FUTU"} and not futu_numeric:
        raise CashflowV2ImportError(f"Futu-backed acceptance lacks Futu value: {decision_id}")
    point = RawDataPoint(
        company_id=key[0],
        field_id=f"FY{key[1]}.{decision['field']}",
        security_id=str(metadata.get("security_id") or "") or None,
        share_class=str(metadata.get("share_class") or "") or None,
        source_name=str(metadata.get("source_name") or ""),
        source_document=str(metadata.get("source_document") or ""),
        source_url_or_local_path=str(metadata.get("source_url") or ""),
        source_publish_date=_date(metadata.get("source_publish_date")),
        source_fetch_time=_datetime(metadata.get("source_fetch_time")),
        fiscal_period=str(futu.get("fiscal_period") or f"{key[1]}/FY"),
        currency=str(source.get("currency") or "") or None,
        unit="currency",
        value=value,
        data_status=DataStatus.KNOWN_ZERO if value == 0 else DataStatus.VALID,
        restatement_status=str(metadata.get("restatement_status") or ""),
    )
    return point, str(metadata.get("fiscal_year_end_date") or "")


def _group(
    points: list[tuple[RawDataPoint, str]],
) -> dict[str, ReviewedCashflowImport]:
    by_company: dict[str, list[tuple[RawDataPoint, str]]] = {}
    seen = set()
    for point, end_date in points:
        key = (point.company_id, point.field_id)
        if key in seen:
            raise CashflowV2ImportError(f"duplicate accepted field: {key}")
        seen.add(key)
        by_company.setdefault(point.company_id, []).append((point, end_date))
    result = {}
    for company_id, items in sorted(by_company.items()):
        years = {}
        for point, end_date in items:
            prefix, field = point.field_id.split(".", 1)
            year = int(prefix[2:])
            row = years.setdefault(
                year,
                {
                    "fiscal_year": year,
                    "fiscal_year_end_date": end_date,
                    "fiscal_period": point.fiscal_period,
                    "period_type": "FULL_YEAR",
                    "operating_cash_flow": None,
                    "capital_expenditure": None,
                    "lease_principal_repayment": None,
                },
            )
            if row["fiscal_year_end_date"] != end_date or row["fiscal_period"] != point.fiscal_period:
                raise CashflowV2ImportError(f"fiscal metadata conflict: {company_id}/FY{year}")
            row[field] = format(point.value, "f")
        result[company_id] = ReviewedCashflowImport(
            company_id=company_id,
            coverage_rows=tuple(years[year] for year in sorted(years, reverse=True)),
            raw_data_points=tuple(sorted((item[0] for item in items), key=lambda item: item.field_id)),
        )
    return result


def _load(
    reconciliation_root: Path,
    official_root: Path,
    *,
    required_decisions: int,
    expected_manifest_sha256: str | None,
) -> dict[str, ReviewedCashflowImport]:
    reconciliation_root = reconciliation_root.resolve()
    official_root = official_root.resolve()
    _verify_manifest(reconciliation_root, expected_manifest_sha256)
    ledger = _object(reconciliation_root / "ledger.json")
    if ledger.get("schema_version") != SCHEMA_VERSION:
        raise CashflowV2ImportError("unsupported cashflow-v2 schema")
    decisions = ledger.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != required_decisions:
        raise CashflowV2ImportError(f"cashflow-v2 must contain exactly {required_decisions} decisions")
    _verify_upstream(ledger.get("candidate_input_manifest"), "candidate manifest")
    _verify_upstream(ledger.get("cashflow_v1_input_manifest"), "cashflow-v1 manifest")
    index, manifest_set_sha = _official_index(official_root)
    if manifest_set_sha != ledger.get("official_source_manifest_set_sha256"):
        raise CashflowV2ImportError("official manifest set changed after cashflow-v2")
    verified_pdfs = set()
    points = [
        _accepted_point(item, index, verified_pdfs)
        for item in decisions
        if isinstance(item, Mapping) and accepted_status(str(item.get("status") or ""))
    ]
    return _group(points)


def load_reviewed_cashflow_v2_imports(
    reconciliation_root: Path,
    official_root: Path,
    *,
    expected_manifest_sha256: str | None = None,
) -> dict[str, ReviewedCashflowImport]:
    """Pure-read conversion of the fixed 560-decision cashflow-v2 ledger."""

    return _load(
        reconciliation_root,
        official_root,
        required_decisions=REQUIRED_DECISION_COUNT,
        expected_manifest_sha256=expected_manifest_sha256,
    )
