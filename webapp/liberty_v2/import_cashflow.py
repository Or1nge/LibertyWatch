from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cashflow_reconciliation import SCHEMA_VERSION, sha256_file
from .models import DataStatus, RawDataPoint, jsonable


REQUIRED_DECISION_COUNT = 268
REQUIRED_FIELDS = {"operating_cash_flow", "capital_expenditure"}


class CashflowImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewedCashflowImport:
    company_id: str
    coverage_rows: tuple[dict[str, Any], ...]
    raw_data_points: tuple[RawDataPoint, ...]

    def staging_fragment(self) -> dict[str, Any]:
        """Return a new staging-compatible fragment; this method never writes it."""

        return {
            "company_id": self.company_id,
            "coverage": {"fcf_years": [dict(row) for row in self.coverage_rows]},
            "raw_data_points": [jsonable(point) for point in self.raw_data_points],
        }


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CashflowImportError(f"cannot read JSON object: {path}") from error
    if not isinstance(value, dict):
        raise CashflowImportError(f"expected JSON object: {path}")
    return value


def _decimal(value: Any, *, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise CashflowImportError(f"invalid Decimal for {label}: {value!r}") from error
    if not parsed.is_finite():
        raise CashflowImportError(f"NaN/Infinity forbidden for {label}")
    return parsed


def _parse_date(value: Any, *, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise CashflowImportError(f"invalid date for {label}: {value!r}") from error


def _parse_datetime(value: Any, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise CashflowImportError(f"invalid datetime for {label}: {value!r}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CashflowImportError(f"timezone required for {label}")
    return parsed.astimezone(timezone.utc)


def _verify_reconciliation_manifest(
    root: Path, *, expected_manifest_sha256: str | None
) -> tuple[dict[str, Any], Path]:
    manifest_path = root / "manifest.json"
    if expected_manifest_sha256 and sha256_file(manifest_path) != expected_manifest_sha256:
        raise CashflowImportError("reconciliation manifest does not match pinned SHA-256")
    manifest = _load_object(manifest_path)
    declared: set[str] = set()
    for raw in manifest.get("files", []):
        if not isinstance(raw, Mapping):
            raise CashflowImportError("reconciliation manifest file entry is invalid")
        relative = str(raw.get("path") or "")
        path = (root / relative).resolve()
        if not relative or root not in path.parents or not path.is_file():
            raise CashflowImportError(f"unsafe/missing reconciled input: {path}")
        if path.stat().st_size != int(raw.get("size_bytes") or -1):
            raise CashflowImportError(f"reconciled input size mismatch: {path}")
        if sha256_file(path) != str(raw.get("sha256") or ""):
            raise CashflowImportError(f"reconciled input SHA-256 mismatch: {path}")
        declared.add(relative)
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if declared != actual or len(declared) != int(manifest.get("file_count") or -1):
        raise CashflowImportError("reconciliation manifest does not exactly cover its files")
    if "ledger.json" not in declared:
        raise CashflowImportError("reconciliation manifest does not include ledger.json")
    return manifest, manifest_path


def _official_index(
    official_root: Path,
) -> tuple[dict[tuple[str, int, str], dict[str, Any]], str]:
    index: dict[tuple[str, int, str], dict[str, Any]] = {}
    manifest_hashes: dict[str, str] = {}
    for manifest_path in sorted(official_root.glob("companies/*/manifest.json")):
        manifest = _load_object(manifest_path)
        manifest_sha = sha256_file(manifest_path)
        manifest_hashes[str(manifest_path.resolve())] = manifest_sha
        for raw in manifest.get("documents", []):
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            if (
                item.get("selection_status") != "SELECTED_CURRENT"
                or item.get("data_status") != "VERIFIED"
            ):
                continue
            item["source_manifest_path"] = str(manifest_path.resolve())
            item["source_manifest_sha256"] = manifest_sha
            item["resolved_local_path"] = str(
                (official_root / str(item.get("local_path") or "")).resolve()
            )
            key = (
                str(item.get("company_id") or ""),
                int(item.get("fiscal_year") or 0),
                str(item.get("sha256") or ""),
            )
            if not all(key) or key in index:
                raise CashflowImportError(f"duplicate/incomplete official source key: {key}")
            index[key] = item
    serialized = json.dumps(
        manifest_hashes, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return index, hashlib.sha256(serialized).hexdigest()


def _verify_candidate_input_manifest(
    ledger: Mapping[str, Any], *, expected_candidate_manifest_sha256: str | None
) -> None:
    descriptor = ledger.get("candidate_input_manifest")
    if not isinstance(descriptor, Mapping):
        raise CashflowImportError("ledger candidate_input_manifest is missing")
    path = Path(str(descriptor.get("path") or "")).resolve()
    expected = str(descriptor.get("sha256") or "")
    if not path.is_file() or not expected:
        raise CashflowImportError("candidate input manifest path/SHA-256 is incomplete")
    actual = sha256_file(path)
    if actual != expected:
        raise CashflowImportError("candidate input manifest changed after reconciliation")
    if expected_candidate_manifest_sha256 and actual != expected_candidate_manifest_sha256:
        raise CashflowImportError("candidate input manifest does not match pinned SHA-256")


def _official_source_for_decision(
    decision: Mapping[str, Any],
    official_index: Mapping[tuple[str, int, str], Mapping[str, Any]],
    verified_pdf_hashes: set[tuple[str, str]],
) -> dict[str, Any]:
    source = decision.get("current_official_source")
    if not isinstance(source, Mapping):
        raise CashflowImportError("decision has no current official source")
    company_id = str(decision.get("company_id") or "")
    fiscal_year = int(decision.get("fiscal_year") or 0)
    source_sha = str(source.get("source_sha256") or "")
    metadata = official_index.get((company_id, fiscal_year, source_sha))
    if metadata is None:
        raise CashflowImportError(
            f"official source is not the selected manifest document: {company_id}/FY{fiscal_year}"
        )
    exact = {
        "source_document": str(source.get("source_document"))
        == str(metadata.get("source_document")),
        "source_url": str(source.get("source_url")) == str(metadata.get("source_url")),
        "source_publish_date": str(source.get("source_publish_date"))
        == str(metadata.get("source_publish_date")),
        "source_manifest_path": Path(str(source.get("source_manifest_path"))).resolve()
        == Path(str(metadata.get("source_manifest_path"))).resolve(),
        "source_manifest_sha256": str(source.get("source_manifest_sha256"))
        == str(metadata.get("source_manifest_sha256")),
        "fiscal_year_end_date": str(source.get("fiscal_year_end_date"))
        == str(metadata.get("fiscal_year_end_date")),
    }
    failed = sorted(key for key, passed in exact.items() if not passed)
    if failed:
        raise CashflowImportError(
            f"official source/manifest conflict for {company_id}/FY{fiscal_year}: {failed}"
        )
    pdf_path = Path(str(metadata["resolved_local_path"]))
    hash_key = (str(pdf_path), source_sha)
    if hash_key not in verified_pdf_hashes:
        if not pdf_path.is_file() or sha256_file(pdf_path) != source_sha:
            raise CashflowImportError(f"official PDF SHA-256 mismatch: {pdf_path}")
        verified_pdf_hashes.add(hash_key)
    return dict(metadata)


def _point_from_decision(
    decision: Mapping[str, Any], metadata: Mapping[str, Any]
) -> tuple[RawDataPoint, str]:
    decision_id = str(decision.get("decision_id") or "")
    if decision.get("decision") != "ACCEPT":
        raise CashflowImportError(f"non-ACCEPT decision forbidden: {decision_id}")
    checks = decision.get("checks")
    if not isinstance(checks, Mapping) or not checks or any(value is not True for value in checks.values()):
        raise CashflowImportError(f"not all reconciliation checks are true: {decision_id}")
    if decision.get("eligible_for_core_write") is not False:
        raise CashflowImportError(f"unexpected mutable candidate flag: {decision_id}")
    if decision.get("candidate_only") is not True:
        raise CashflowImportError(f"reviewed input is not marked candidate_only: {decision_id}")
    field_name = str(decision.get("field") or "")
    if field_name not in REQUIRED_FIELDS:
        raise CashflowImportError(f"unsupported cashflow field: {field_name}")
    fiscal_year = int(decision.get("fiscal_year") or 0)
    accepted = _decimal(decision.get("accepted_value"), label=f"{decision_id}.accepted_value")
    official = decision.get("current_official_source") or {}
    futu = decision.get("futu_source") or {}
    official_value = _decimal(official.get("value"), label=f"{decision_id}.official_value")
    futu_value = _decimal(futu.get("field_value"), label=f"{decision_id}.futu_value")
    if accepted != official_value or accepted != futu_value:
        raise CashflowImportError(f"official/Futu amount conflict: {decision_id}")
    currencies = {
        str(decision.get("currency") or ""),
        str(official.get("currency") or ""),
        str(futu.get("field_currency") or ""),
    }
    if "" in currencies or len(currencies) != 1:
        raise CashflowImportError(f"official/Futu currency conflict: {decision_id}")
    fiscal_period = str(futu.get("fiscal_period") or "")
    if not fiscal_period or str(fiscal_year) not in fiscal_period:
        raise CashflowImportError(f"Futu fiscal period conflict: {decision_id}")
    status = DataStatus.KNOWN_ZERO if accepted == 0 else DataStatus.VALID
    point = RawDataPoint(
        company_id=str(decision.get("company_id") or ""),
        field_id=f"FY{fiscal_year}.{field_name}",
        security_id=str(metadata.get("security_id") or "") or None,
        share_class=str(metadata.get("share_class") or "") or None,
        source_name=str(metadata.get("source_name") or ""),
        source_document=str(metadata.get("source_document") or ""),
        source_url_or_local_path=str(metadata.get("source_url") or ""),
        source_publish_date=_parse_date(
            metadata.get("source_publish_date"), label=f"{decision_id}.source_publish_date"
        ),
        source_fetch_time=_parse_datetime(
            metadata.get("source_fetch_time"), label=f"{decision_id}.source_fetch_time"
        ),
        fiscal_period=fiscal_period,
        currency=next(iter(currencies)),
        unit="currency",
        value=accepted,
        data_status=status,
        restatement_status=str(metadata.get("restatement_status") or ""),
    )
    if not point.restatement_status:
        raise CashflowImportError(f"restatement status missing: {decision_id}")
    return point, str(metadata.get("fiscal_year_end_date") or "")


def _group_company_imports(
    points: Sequence[RawDataPoint],
    fiscal_year_ends: Mapping[tuple[str, int], str],
) -> dict[str, ReviewedCashflowImport]:
    grouped: dict[str, list[RawDataPoint]] = {}
    seen: set[tuple[str, str]] = set()
    for point in points:
        key = (point.company_id, point.field_id)
        if key in seen:
            raise CashflowImportError(f"duplicate reviewed field: {key}")
        seen.add(key)
        grouped.setdefault(point.company_id, []).append(point)
    result: dict[str, ReviewedCashflowImport] = {}
    for company_id, company_points in sorted(grouped.items()):
        by_year: dict[int, dict[str, Any]] = {}
        for point in company_points:
            prefix, field_name = point.field_id.split(".", 1)
            fiscal_year = int(prefix.removeprefix("FY"))
            row = by_year.setdefault(
                fiscal_year,
                {
                    "fiscal_year": fiscal_year,
                    "fiscal_year_end_date": str(
                        fiscal_year_ends.get((company_id, fiscal_year)) or ""
                    ),
                    "fiscal_period": point.fiscal_period,
                    "period_type": "FULL_YEAR",
                    "operating_cash_flow": None,
                    "capital_expenditure": None,
                    "lease_principal_repayment": None,
                },
            )
            if row["fiscal_period"] != point.fiscal_period:
                raise CashflowImportError(
                    f"fiscal period conflict: {company_id}/FY{fiscal_year}"
                )
            if row[field_name] is not None:
                raise CashflowImportError(f"duplicate coverage field: {point.field_id}")
            row[field_name] = format(point.value, "f") if point.value is not None else None
        for fiscal_year, row in by_year.items():
            if not row["fiscal_year_end_date"]:
                raise CashflowImportError(
                    f"fiscal year end missing: {company_id}/FY{fiscal_year}"
                )
        result[company_id] = ReviewedCashflowImport(
            company_id=company_id,
            coverage_rows=tuple(by_year[year] for year in sorted(by_year, reverse=True)),
            raw_data_points=tuple(sorted(company_points, key=lambda point: point.field_id)),
        )
    return result


def _load_reviewed_cashflow_imports(
    reconciliation_root: Path,
    official_root: Path,
    *,
    required_decision_count: int,
    expected_reconciliation_manifest_sha256: str | None,
    expected_candidate_manifest_sha256: str | None,
) -> dict[str, ReviewedCashflowImport]:
    reconciliation_root = reconciliation_root.resolve()
    official_root = official_root.resolve()
    _verify_reconciliation_manifest(
        reconciliation_root,
        expected_manifest_sha256=expected_reconciliation_manifest_sha256,
    )
    ledger = _load_object(reconciliation_root / "ledger.json")
    if ledger.get("schema_version") != SCHEMA_VERSION:
        raise CashflowImportError("unsupported reconciliation ledger schema")
    decisions = ledger.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != required_decision_count:
        raise CashflowImportError(
            f"reconciliation must contain exactly {required_decision_count} decisions"
        )
    if int(ledger.get("decision_count") or -1) != required_decision_count:
        raise CashflowImportError("ledger decision_count mismatch")
    _verify_candidate_input_manifest(
        ledger,
        expected_candidate_manifest_sha256=expected_candidate_manifest_sha256,
    )
    official_index, manifest_set_sha = _official_index(official_root)
    if manifest_set_sha != str(ledger.get("official_source_manifest_set_sha256") or ""):
        raise CashflowImportError("official company manifest set changed after reconciliation")
    verified_pdf_hashes: set[tuple[str, str]] = set()
    points: list[RawDataPoint] = []
    fiscal_year_ends: dict[tuple[str, int], str] = {}
    for raw in decisions:
        if not isinstance(raw, Mapping):
            raise CashflowImportError("ledger decision is not an object")
        metadata = _official_source_for_decision(raw, official_index, verified_pdf_hashes)
        point, fiscal_year_end = _point_from_decision(raw, metadata)
        fiscal_year = int(point.field_id.split(".", 1)[0].removeprefix("FY"))
        end_key = (point.company_id, fiscal_year)
        previous_end = fiscal_year_ends.get(end_key)
        if previous_end is not None and previous_end != fiscal_year_end:
            raise CashflowImportError(f"fiscal year end conflict: {end_key}")
        fiscal_year_ends[end_key] = fiscal_year_end
        points.append(point)
    if len(points) != required_decision_count:
        raise CashflowImportError("converted point count mismatch")
    return _group_company_imports(points, fiscal_year_ends)


def load_reviewed_cashflow_imports(
    reconciliation_root: Path,
    official_root: Path,
    *,
    expected_reconciliation_manifest_sha256: str | None = None,
    expected_candidate_manifest_sha256: str | None = None,
) -> dict[str, ReviewedCashflowImport]:
    """Load the fixed 268-decision ledger into read-only company fragments."""

    return _load_reviewed_cashflow_imports(
        reconciliation_root,
        official_root,
        required_decision_count=REQUIRED_DECISION_COUNT,
        expected_reconciliation_manifest_sha256=expected_reconciliation_manifest_sha256,
        expected_candidate_manifest_sha256=expected_candidate_manifest_sha256,
    )


def cashflow_import_payloads(
    imports: Mapping[str, ReviewedCashflowImport],
) -> dict[str, dict[str, Any]]:
    """Serialize verified imports for an orchestrator without performing I/O."""

    return {
        company_id: reviewed.staging_fragment()
        for company_id, reviewed in sorted(imports.items())
    }
