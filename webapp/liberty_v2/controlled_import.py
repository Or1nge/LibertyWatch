from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .models import RawDataPoint, jsonable
from .snapshot_store import atomic_write_json
from .source_ledger import (
    SourceLedgerConflict,
    apply_ledger_to_staging_record,
    merge_raw_points,
)
from .validation import validate_raw_provenance_records


IMPORT_VERSION = "controlled-source-ledger-import-v1.0"
ANNUAL_FIELDS = (
    "ordinary_dividend",
    "special_dividend",
    "gross_cancelled_buyback",
    "cancelled_shares",
    "diluted_net_share_reduction",
    "asset_sale_distribution",
    "one_off_buyback",
)
FIELD_UNITS = {
    "ordinary_dividend": "currency",
    "special_dividend": "currency",
    "gross_cancelled_buyback": "currency",
    "cancelled_shares": "shares",
    "diluted_net_share_reduction": "shares",
    "asset_sale_distribution": "currency",
    "one_off_buyback": "currency",
}


class ControlledImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ControlledImportInputs:
    staging_dir: Path
    futu_ledger_root: Path
    cashflow_root: Path
    cashflow_v2_root: Path
    dividend_root: Path
    dividend_v2_root: Path
    cancellation_root: Path
    share_capital_root: Path
    official_annual_root: Path


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ControlledImportError(f"cannot read JSON object: {path}") from error
    if not isinstance(value, dict):
        raise ControlledImportError(f"expected JSON object: {path}")
    return value


def verify_manifest_bundle(root: Path) -> dict[str, Any]:
    """Verify a frozen bundle before any staging mutation."""

    root = root.resolve()
    manifest_path = root / "manifest.json"
    manifest = _load_object(manifest_path)
    declared: set[str] = set()
    for raw in manifest.get("files", []):
        if not isinstance(raw, Mapping):
            raise ControlledImportError(f"invalid manifest entry: {manifest_path}")
        relative = str(raw.get("path") or "")
        if not relative or Path(relative).is_absolute() or relative in declared:
            raise ControlledImportError(f"unsafe/duplicate manifest path: {relative!r}")
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise ControlledImportError(f"missing/unsafe manifest file: {relative}")
        size = raw.get("size_bytes", raw.get("size"))
        if size is None or path.stat().st_size != int(size):
            raise ControlledImportError(f"manifest size mismatch: {relative}")
        if sha256_file(path) != str(raw.get("sha256") or ""):
            raise ControlledImportError(f"manifest SHA-256 mismatch: {relative}")
        declared.add(relative)
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json" and not path.name.startswith(".")
    }
    if declared != actual:
        raise ControlledImportError(f"manifest does not exactly cover bundle: {root}")
    declared_count = manifest.get("file_count")
    if declared_count is not None and int(declared_count) != len(declared):
        raise ControlledImportError(f"manifest file_count mismatch: {root}")
    return {
        "path": str(manifest_path),
        "sha256": sha256_file(manifest_path),
        "schema_version": manifest.get("schema_version"),
        "file_count": len(declared),
    }


def _manifest_created_at(root: Path) -> str:
    value = str(_load_object(root / "manifest.json").get("created_at") or "")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ControlledImportError(f"manifest created_at is invalid: {root}") from error
    if parsed.tzinfo is None:
        raise ControlledImportError(f"manifest created_at must have timezone: {root}")
    return parsed.astimezone(timezone.utc).isoformat()


def _security(record: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    securities = [
        item for item in record.get("securities", []) if isinstance(item, Mapping)
    ]
    if not securities:
        return None, None, None
    item = securities[0]
    return (
        str(item.get("security_id") or "") or None,
        str(item.get("share_class") or "") or None,
        str(item.get("currency") or "") or None,
    )


def _missing_point(
    record: Mapping[str, Any],
    *,
    field_id: str,
    fiscal_period: str,
    unit: str,
    reviewed_at: str,
    source_path: Path,
    reason: str,
) -> dict[str, Any]:
    security_id, share_class, currency = _security(record)
    return {
        "company_id": str(record["company_id"]),
        "field_id": field_id,
        "security_id": security_id,
        "share_class": share_class,
        "source_name": "Controlled source-ledger reconciliation",
        "source_document": source_path.name,
        "source_url_or_local_path": str(source_path.resolve()),
        "source_publish_date": reviewed_at[:10],
        "source_fetch_time": reviewed_at,
        "fiscal_period": fiscal_period,
        "currency": currency if unit == "currency" else None,
        "unit": unit,
        "value": None,
        "data_status": "NOT_DISCLOSED",
        "restatement_status": "CONTROLLED_RECONCILIATION_V1",
        "reason": reason,
    }


def _ensure_security_missing_points(
    record: dict[str, Any], *, reviewed_at: str, source_path: Path
) -> None:
    points: list[dict[str, Any]] = []
    for item in record.get("share_classes", []):
        if not isinstance(item, Mapping) or item.get("material") is False:
            continue
        security_id = str(item.get("security_id") or "")
        if not security_id:
            continue
        for field_name, unit in (
            ("issued_shares", "shares"),
            ("economic_rights_factor", "ratio"),
        ):
            field_id = f"SECURITY.{security_id}.{field_name}"
            points.append(
                _missing_point(
                    record,
                    field_id=field_id,
                    fiscal_period="CURRENT_SECURITY_STRUCTURE",
                    unit=unit,
                    reviewed_at=reviewed_at,
                    source_path=source_path,
                    reason=(
                        "all material share classes and economic rights are not yet verified; "
                        "no company-level denominator is authorized"
                    ),
                )
            )
    record["raw_data_points"] = merge_raw_points(record.get("raw_data_points", []), points)


def _ensure_global_missing_points(
    record: dict[str, Any], *, reviewed_at: str, source_path: Path
) -> None:
    """Record reviewed absences without converting them into numeric zeroes."""

    fields = (
        ("VALUATION.current", "multiple"),
        ("VALUATION.historical_median", "multiple"),
        ("RECONCILIATION.dividend_per_share_times_entitled_shares", "currency"),
        ("RECONCILIATION.repurchased_shares_times_average_price", "currency"),
        ("RECONCILIATION.opening_minus_closing_shares", "shares"),
        ("RECONCILIATION.cancelled_minus_issued_and_converted", "shares"),
    )
    points = [
        _missing_point(
            record,
            field_id=field_id,
            fiscal_period="CURRENT_RECONCILIATION_STATUS",
            unit=unit,
            reviewed_at=reviewed_at,
            source_path=source_path,
            reason=(
                "the controlled source-ledger review did not establish this value; "
                "calculation and recommendation remain disabled"
            ),
        )
        for field_id, unit in fields
    ]
    record["raw_data_points"] = merge_raw_points(record.get("raw_data_points", []), points)


def _annual_row(record: Mapping[str, Any], fiscal_year: int) -> dict[str, Any]:
    annual_source = {
        int(item["fiscal_year"]): item
        for item in record.get("annual_source_ledger", [])
        if isinstance(item, Mapping) and item.get("fiscal_year") is not None
    }
    source = annual_source.get(fiscal_year, {})
    fiscal_year_end = str(source.get("fiscal_year_end_date") or f"{fiscal_year}-12-31")
    return {
        "fiscal_year": fiscal_year,
        "fiscal_year_end_date": fiscal_year_end,
        "period_type": "FULL_YEAR",
        "ordinary_dividend_status": "NOT_DISCLOSED",
        "ordinary_dividend": None,
        "special_dividend": None,
        "gross_cancelled_buyback": None,
        "cancelled_shares": None,
        "diluted_net_share_reduction": None,
        "asset_sale_distribution": None,
        "one_off_buyback": None,
    }


def _merge_annual_facts(
    record: dict[str, Any],
    points: Sequence[RawDataPoint],
    *,
    reviewed_at: str,
    source_path: Path,
) -> None:
    rows = {
        int(item["fiscal_year"]): copy.deepcopy(dict(item))
        for item in record.get("annual_distributions", [])
        if isinstance(item, Mapping) and item.get("fiscal_year") is not None
    }
    point_payloads = [jsonable(point) for point in points]
    for point in points:
        prefix, field_name = point.field_id.split(".", 1)
        if field_name not in ANNUAL_FIELDS or not prefix.startswith("FY"):
            raise ControlledImportError(f"unsupported annual fact: {point.field_id}")
        year = int(prefix[2:])
        row = rows.setdefault(year, _annual_row(record, year))
        value = format(point.value, "f") if point.value is not None else None
        existing = row.get(field_name)
        if existing is not None and existing != value:
            raise SourceLedgerConflict(f"annual value conflict: {point.company_id}/{point.field_id}")
        row[field_name] = value
        if field_name == "ordinary_dividend":
            row["ordinary_dividend_status"] = "PAID"
    for year, row in rows.items():
        prefix = f"FY{year}"
        for field_name in ANNUAL_FIELDS:
            field_id = f"{prefix}.{field_name}"
            if not any(str(item.get("field_id")) == field_id for item in point_payloads):
                point_payloads.append(
                    _missing_point(
                        record,
                        field_id=field_id,
                        fiscal_period=f"{year}/FY",
                        unit=FIELD_UNITS[field_name],
                        reviewed_at=reviewed_at,
                        source_path=source_path,
                        reason="field was not established by the accepted reconciliation facts",
                    )
                )
    record["annual_distributions"] = [rows[year] for year in sorted(rows, reverse=True)]
    record["raw_data_points"] = merge_raw_points(
        record.get("raw_data_points", []), point_payloads
    )


def _merge_cashflow_fragment(record: dict[str, Any], fragment: Mapping[str, Any]) -> None:
    coverage = copy.deepcopy(dict(record.get("coverage") or {}))
    rows = {
        int(item["fiscal_year"]): copy.deepcopy(dict(item))
        for item in coverage.get("fcf_years", [])
        if isinstance(item, Mapping) and item.get("fiscal_year") is not None
    }
    for raw in fragment.get("coverage", {}).get("fcf_years", []):
        if not isinstance(raw, Mapping):
            raise ControlledImportError("cashflow coverage row must be an object")
        year = int(raw["fiscal_year"])
        row = rows.setdefault(year, copy.deepcopy(dict(raw)))
        for field_name in ("operating_cash_flow", "capital_expenditure"):
            incoming = raw.get(field_name)
            current = row.get(field_name)
            if (
                incoming is not None
                and current is not None
                and Decimal(str(incoming)) != Decimal(str(current))
            ):
                raise SourceLedgerConflict(f"coverage conflict: {record['company_id']}/FY{year}.{field_name}")
            if incoming is not None:
                row[field_name] = str(incoming)
        row.setdefault("lease_principal_repayment", None)
        for metadata in ("fiscal_year", "fiscal_year_end_date", "fiscal_period", "period_type"):
            row.setdefault(metadata, raw.get(metadata))
    coverage["fcf_years"] = [rows[year] for year in sorted(rows, reverse=True)[:5]]
    record["coverage"] = coverage
    official_points = []
    for item in fragment.get("raw_data_points", []):
        if not isinstance(item, Mapping):
            raise ControlledImportError("cashflow raw point must be an object")
        point = dict(item)
        point["source_level"] = "OFFICIAL_FILING"
        official_points.append(point)
    record["raw_data_points"] = merge_raw_points(
        record.get("raw_data_points", []), official_points
    )


def _merge_share_capital_facts(record: dict[str, Any], facts: Sequence[Any]) -> None:
    """Store official class facts without authorizing a current denominator."""

    points = []
    for fact in facts:
        if fact.company_market_value_denominator_authorized is not False:
            raise ControlledImportError("share-capital fact unexpectedly authorizes denominator")
        point = jsonable(fact.point)
        point["source_level"] = "OFFICIAL_FILING"
        point["legal_share_class_id"] = fact.legal_share_class_id
        point["class_rights_verified"] = fact.rights_verified
        point["economic_rights_factor_reviewed"] = (
            format(fact.economic_rights_factor, "f")
            if fact.economic_rights_factor is not None
            else None
        )
        point["company_market_value_denominator_authorized"] = False
        points.append(point)
    record["raw_data_points"] = merge_raw_points(record.get("raw_data_points", []), points)


def _load_staging(staging_dir: Path) -> dict[str, dict[str, Any]]:
    companies_dir = staging_dir / "companies"
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(companies_dir.glob("*.json")):
        item = _load_object(path)
        company_id = str(item.get("company_id") or "")
        if not company_id or company_id in records or path.stem != company_id:
            raise ControlledImportError(f"invalid/duplicate staging company: {path}")
        records[company_id] = item
    if not records:
        raise ControlledImportError("staging contains no companies")
    return records


def _load_futu_ledgers(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "companies").glob("*.json")):
        value = _load_object(path)
        company_id = str(value.get("company_id") or "")
        if not company_id or path.stem != company_id or company_id in result:
            raise ControlledImportError(f"invalid/duplicate Futu company ledger: {path}")
        result[company_id] = value
    return result


def _group_points(points: Iterable[RawDataPoint]) -> dict[str, list[RawDataPoint]]:
    grouped: dict[str, list[RawDataPoint]] = {}
    for point in points:
        grouped.setdefault(point.company_id, []).append(point)
    return grouped


def build_desired_records(inputs: ControlledImportInputs) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Build every post-import record in memory; never write from this function."""

    from .import_cancellations import load_confirmed_cancellation_points
    from .import_cashflow import cashflow_import_payloads, load_reviewed_cashflow_imports
    from .import_cashflow_v2 import load_reviewed_cashflow_v2_imports
    from .import_dividends import load_controlled_dividend_facts
    from .import_dividends_v2 import load_controlled_dividend_facts_v2
    from .import_share_capital import load_confirmed_share_capital_facts

    bundle_info = {
        name: verify_manifest_bundle(path)
        for name, path in (
            ("futu_ledger", inputs.futu_ledger_root),
            ("cashflow", inputs.cashflow_root),
            ("cashflow_v2", inputs.cashflow_v2_root),
            ("dividend", inputs.dividend_root),
            ("dividend_v2", inputs.dividend_v2_root),
            ("cancellation", inputs.cancellation_root),
            ("share_capital", inputs.share_capital_root),
        )
    }
    records = _load_staging(inputs.staging_dir)
    futu_ledgers = _load_futu_ledgers(inputs.futu_ledger_root)
    missing_targets = set(futu_ledgers) - set(records)
    if missing_targets:
        raise ControlledImportError(f"Futu ledgers are outside staging: {sorted(missing_targets)}")
    if len(futu_ledgers) != 56:
        raise ControlledImportError(f"expected 56 Futu company ledgers, got {len(futu_ledgers)}")

    cashflow = cashflow_import_payloads(
        load_reviewed_cashflow_imports(inputs.cashflow_root, inputs.official_annual_root)
    )
    cashflow_v2 = cashflow_import_payloads(
        load_reviewed_cashflow_v2_imports(
            inputs.cashflow_v2_root, inputs.official_annual_root
        )
    )
    dividend_v1_facts = load_controlled_dividend_facts(
        inputs.dividend_root, inputs.official_annual_root
    )
    dividend_v2_facts = load_controlled_dividend_facts_v2(
        inputs.dividend_v2_root,
        official_annual_root=inputs.official_annual_root,
    )
    dividend_facts = dict(dividend_v1_facts)
    for key, v2_fact in dividend_v2_facts.items():
        v1_fact = dividend_facts.get(key)
        if v1_fact is not None and (
            v1_fact.ordinary_dividend != v2_fact.ordinary_dividend
            or v1_fact.currency != v2_fact.currency
        ):
            raise SourceLedgerConflict(
                f"dividend-v1/v2 conflict: {key[0]}/FY{key[1]}"
            )
        dividend_facts[key] = v2_fact
    dividends = _group_points(
        fact.raw_data_point for fact in dividend_facts.values()
    )
    cancellations = _group_points(
        load_confirmed_cancellation_points(
            inputs.cancellation_root, inputs.official_annual_root
        )
    )
    share_capital: dict[str, list[Any]] = {}
    for fact in load_confirmed_share_capital_facts(
        inputs.share_capital_root, inputs.official_annual_root
    ):
        share_capital.setdefault(fact.point.company_id, []).append(fact)
    scoped_ids = (
        set(futu_ledgers)
        | set(cashflow)
        | set(cashflow_v2)
        | set(dividends)
        | set(cancellations)
        | set(share_capital)
    )
    if not scoped_ids.issubset(records):
        raise ControlledImportError(
            f"reconciled facts are outside staging: {sorted(scoped_ids - set(records))}"
        )

    reviewed_at = max(
        _manifest_created_at(inputs.cashflow_root),
        _manifest_created_at(inputs.cashflow_v2_root),
        _manifest_created_at(inputs.dividend_root),
        _manifest_created_at(inputs.dividend_v2_root),
        _manifest_created_at(inputs.cancellation_root),
        _manifest_created_at(inputs.share_capital_root),
    )
    evidence_path = inputs.dividend_root / "manifest.json"
    desired: dict[str, dict[str, Any]] = {}
    for company_id in sorted(scoped_ids):
        record = copy.deepcopy(records[company_id])
        if company_id in futu_ledgers:
            record = apply_ledger_to_staging_record(record, futu_ledgers[company_id])
            if "buyback_event_evidence" in futu_ledgers[company_id]:
                record["buyback_event_evidence"] = copy.deepcopy(
                    futu_ledgers[company_id]["buyback_event_evidence"]
                )
        if company_id in cashflow:
            _merge_cashflow_fragment(record, cashflow[company_id])
        if company_id in cashflow_v2:
            _merge_cashflow_fragment(record, cashflow_v2[company_id])
        if company_id in share_capital:
            _merge_share_capital_facts(record, share_capital[company_id])
        annual_points = [*dividends.get(company_id, ()), *cancellations.get(company_id, ())]
        if annual_points:
            _merge_annual_facts(
                record,
                annual_points,
                reviewed_at=reviewed_at,
                source_path=evidence_path,
            )
        _ensure_security_missing_points(
            record,
            reviewed_at=reviewed_at,
            source_path=inputs.futu_ledger_root / "manifest.json",
        )
        _ensure_global_missing_points(
            record,
            reviewed_at=reviewed_at,
            source_path=inputs.futu_ledger_root / "manifest.json",
        )
        result = validate_raw_provenance_records(
            record.get("raw_data_points"), expected_company_id=company_id
        )
        errors = [issue for issue in result.issues if issue.severity == "ERROR"]
        if errors:
            raise ControlledImportError(
                f"post-import provenance invalid for {company_id}: "
                + ", ".join(f"{item.code}:{item.field}" for item in errors[:5])
            )
        summary = record.setdefault("source_summary", {})
        cashflow_v1_ids = {
            str(item.get("field_id"))
            for item in cashflow.get(company_id, {}).get("raw_data_points", [])
            if isinstance(item, Mapping)
        }
        cashflow_v2_ids = {
            str(item.get("field_id"))
            for item in cashflow_v2.get(company_id, {}).get("raw_data_points", [])
            if isinstance(item, Mapping)
        }
        summary["controlled_reconciliation_import"] = {
            "version": IMPORT_VERSION,
            "bundle_manifest_sha256": {
                name: item["sha256"] for name, item in sorted(bundle_info.items())
            },
            "official_cashflow_points": len(cashflow_v1_ids | cashflow_v2_ids),
            "official_cashflow_v1_points": len(cashflow_v1_ids),
            "official_cashflow_v2_points": len(cashflow_v2_ids),
            "ordinary_dividend_points": len(dividends.get(company_id, ())),
            "ordinary_dividend_v1_points": sum(
                key[0] == company_id for key in dividend_v1_facts
            ),
            "ordinary_dividend_v2_points": sum(
                key[0] == company_id for key in dividend_v2_facts
            ),
            "confirmed_cancellation_points": len(cancellations.get(company_id, ())),
            "confirmed_issued_share_class_points": len(share_capital.get(company_id, ())),
            "confirmed_class_rights_points": sum(
                fact.rights_verified for fact in share_capital.get(company_id, ())
            ),
            "company_market_value_denominator_authorized": False,
            "recommendation_authorized": False,
            "status": "PARTIAL",
        }
        desired[company_id] = record
    metadata = {
        "import_version": IMPORT_VERSION,
        "bundle_manifests": bundle_info,
        "staging_company_count": len(records),
        "scoped_company_count": len(scoped_ids),
        "futu_company_count": len(futu_ledgers),
        "cashflow_company_count": len(cashflow),
        "cashflow_point_count": sum(
            len(item.get("raw_data_points", [])) for item in cashflow.values()
        ),
        "cashflow_v2_company_count": len(cashflow_v2),
        "cashflow_v2_point_count": sum(
            len(item.get("raw_data_points", [])) for item in cashflow_v2.values()
        ),
        "cashflow_unique_point_count": len(
            {
                (company_id, str(point.get("field_id")))
                for source in (cashflow, cashflow_v2)
                for company_id, item in source.items()
                for point in item.get("raw_data_points", [])
                if isinstance(point, Mapping)
            }
        ),
        "ordinary_dividend_company_count": len(dividends),
        "ordinary_dividend_point_count": sum(map(len, dividends.values())),
        "ordinary_dividend_v1_point_count": len(dividend_v1_facts),
        "ordinary_dividend_v2_point_count": len(dividend_v2_facts),
        "cancellation_company_count": len(cancellations),
        "cancellation_point_count": sum(map(len, cancellations.values())),
        "share_capital_company_count": len(share_capital),
        "issued_share_class_point_count": sum(map(len, share_capital.values())),
        "class_rights_verified_point_count": sum(
            fact.rights_verified
            for facts in share_capital.values()
            for fact in facts
        ),
    }
    return desired, metadata


def build_plan(inputs: ControlledImportInputs) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    desired, metadata = build_desired_records(inputs)
    changes: list[dict[str, Any]] = []
    for company_id, payload in sorted(desired.items()):
        path = inputs.staging_dir / "companies" / f"{company_id}.json"
        pre_sha = sha256_file(path)
        post_sha = canonical_sha256(payload)
        if pre_sha != post_sha:
            changes.append(
                {
                    "company_id": company_id,
                    "path": str(path.resolve()),
                    "pre_sha256": pre_sha,
                    "post_sha256": post_sha,
                    "pre_size_bytes": path.stat().st_size,
                    "post_size_bytes": len(canonical_bytes(payload)),
                }
            )
    plan = {
        "schema_version": "controlled-import-plan-v1",
        "mode": "dry-run",
        **metadata,
        "changed_company_count": len(changes),
        "unchanged_scoped_company_count": len(desired) - len(changes),
        "changes": changes,
        "writes_staging": False,
        "recommendation_authorized": False,
    }
    return desired, plan


@contextmanager
def import_lock(staging_dir: Path):
    lock_path = staging_dir / ".controlled-import.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_run_id(value: str) -> str:
    if not value or any(character not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_." for character in value):
        raise ControlledImportError("unsafe run id")
    return value


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(source.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, source.stat().st_mode)
    os.replace(temporary, target)


def apply_import(
    inputs: ControlledImportInputs,
    *,
    backup_root: Path,
    run_root: Path,
    run_id: str | None = None,
) -> dict[str, Any]:
    if backup_root is None:
        raise ControlledImportError("apply requires an explicit backup root")
    value = _safe_run_id(run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    backup_dir = backup_root.resolve() / value
    run_dir = run_root.resolve() / value
    if backup_dir.exists() or run_dir.exists():
        raise ControlledImportError(f"run id already exists: {value}")
    with import_lock(inputs.staging_dir):
        desired, plan = build_plan(inputs)
        changes = list(plan["changes"])
        if not changes:
            return {**plan, "mode": "apply", "run_id": None, "applied": False}
        backup_companies = backup_dir / "companies"
        backup_companies.mkdir(parents=True, exist_ok=False)
        for item in changes:
            source = Path(item["path"])
            target = backup_companies / source.name
            shutil.copy2(source, target)
            if sha256_file(target) != item["pre_sha256"]:
                raise ControlledImportError(f"backup verification failed: {source}")
        backup_manifest = {
            "schema_version": "controlled-import-backup-v1",
            "run_id": value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": changes,
        }
        atomic_write_json(backup_dir / "manifest.json", backup_manifest)
        completed: list[dict[str, Any]] = []
        try:
            for item in changes:
                path = Path(item["path"])
                if sha256_file(path) != item["pre_sha256"]:
                    raise ControlledImportError(f"staging changed during import: {path}")
                atomic_write_json(path, desired[item["company_id"]])
                if sha256_file(path) != item["post_sha256"]:
                    raise ControlledImportError(f"post-write verification failed: {path}")
                completed.append(item)
        except Exception:
            for item in reversed(completed):
                source = backup_companies / Path(item["path"]).name
                _atomic_copy(source, Path(item["path"]))
            raise
        run_manifest = {
            **plan,
            "mode": "apply",
            "run_id": value,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "backup_dir": str(backup_dir),
            "applied": True,
            "writes_staging": True,
        }
        run_dir.mkdir(parents=True, exist_ok=False)
        atomic_write_json(run_dir / "manifest.json", run_manifest)
        return run_manifest


def verify_run(run_root: Path, run_id: str) -> dict[str, Any]:
    value = _safe_run_id(run_id)
    manifest = _load_object(run_root.resolve() / value / "manifest.json")
    failures = []
    for item in manifest.get("changes", []):
        path = Path(str(item["path"]))
        actual = sha256_file(path) if path.is_file() else None
        if actual != item.get("post_sha256"):
            failures.append({"path": str(path), "expected": item.get("post_sha256"), "actual": actual})
    return {
        "run_id": value,
        "verified": not failures,
        "checked_file_count": len(manifest.get("changes", [])),
        "failures": failures,
    }


def rollback_import(run_root: Path, run_id: str) -> dict[str, Any]:
    value = _safe_run_id(run_id)
    run_dir = run_root.resolve() / value
    manifest = _load_object(run_dir / "manifest.json")
    backup_dir = Path(str(manifest.get("backup_dir") or "")).resolve()
    changes = list(manifest.get("changes", []))
    if (run_dir / "rollback.json").exists():
        raise ControlledImportError(f"run was already rolled back: {value}")
    lock_dir = Path(str(changes[0]["path"])).parent.parent if changes else run_dir
    with import_lock(lock_dir):
        for item in changes:
            path = Path(str(item["path"]))
            backup = backup_dir / "companies" / path.name
            if not path.is_file() or sha256_file(path) != item.get("post_sha256"):
                raise ControlledImportError(f"refusing rollback because staging drifted: {path}")
            if not backup.is_file() or sha256_file(backup) != item.get("pre_sha256"):
                raise ControlledImportError(f"backup is missing or corrupt: {backup}")
        for item in changes:
            path = Path(str(item["path"]))
            backup = backup_dir / "companies" / path.name
            _atomic_copy(backup, path)
    result = {
        "run_id": value,
        "rolled_back": True,
        "restored_file_count": len(changes),
        "rolled_back_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_json(run_dir / "rollback.json", result)
    return result
