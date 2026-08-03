from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .dividend_reconciliation import verify_file_manifest
from .models import DataStatus, RawDataPoint


RECONCILIATION_SCHEMA_VERSION = "dividend-reconciliation-v1.0"
EXPECTED_READY_FACT_COUNT = 11
EXPECTED_COMPONENT_ONLY_ID = "SZ002430-FY2023-ORDINARY-INTERIM"


class DividendImportError(RuntimeError):
    """Raised when a reconciliation artifact is unsafe to convert."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class AnnualDividendFact:
    company_id: str
    company_name: str
    fiscal_year: int
    ordinary_dividend_status: str
    ordinary_dividend: Decimal
    currency: str
    distribution_id: str
    raw_data_point: RawDataPoint
    primary_source: Mapping[str, Any]
    auxiliary_sources: tuple[Mapping[str, Any], ...]


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DividendImportError(f"cannot load JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise DividendImportError(f"JSON root must be an object: {path}")
    return value


def _require_read_only_artifact(value: Mapping[str, Any], *, source: str) -> None:
    if value.get("writes_production") is not False:
        raise DividendImportError(f"artifact is not explicitly read-only: {source}")


def _parse_positive_decimal(value: Any, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise DividendImportError(f"{field} must be a Decimal value") from error
    if not result.is_finite() or result <= 0:
        raise DividendImportError(f"{field} must be finite and positive")
    return result


def _parse_date(value: Any, *, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise DividendImportError(f"{field} must be an ISO date") from error


def _parse_review_time(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise DividendImportError("reviewed_at must be an ISO datetime") from error
    if result.tzinfo is None:
        raise DividendImportError("reviewed_at must include a timezone")
    return result


def _validate_official_source(
    source: Mapping[str, Any],
    *,
    company_id: str,
    field: str,
    official_annual_root: Path | None = None,
) -> None:
    if source.get("source_type") != "OFFICIAL_ANNUAL_REPORT":
        raise DividendImportError(f"{field} is not an official annual report")
    if source.get("identity_status") != "VALID":
        raise DividendImportError(f"{field} source identity is not VALID")
    if source.get("verification_status") != (
        "FULL_ANNUAL_REPORT_SHA256_AND_PAGE_COUNT_OK"
    ):
        raise DividendImportError(f"{field} source verification is not complete")
    if str(source.get("company_id") or "") != company_id:
        raise DividendImportError(f"{field} source company does not match")
    try:
        source_fiscal_year = int(source.get("fiscal_year") or 0)
    except (TypeError, ValueError) as error:
        raise DividendImportError(f"{field}.fiscal_year is invalid") from error
    if source_fiscal_year < 1900:
        raise DividendImportError(f"{field}.fiscal_year is invalid")
    for required in (
        "source_name",
        "source_document",
        "source_publish_date",
        "source_sha256",
    ):
        if not str(source.get(required) or "").strip():
            raise DividendImportError(f"{field}.{required} is missing")
    if not str(source.get("source_url") or source.get("source_local_path") or "").strip():
        raise DividendImportError(f"{field} has neither an official URL nor a local path")
    _parse_date(source.get("source_publish_date"), field=f"{field}.source_publish_date")
    if official_annual_root is not None:
        root = official_annual_root.resolve()
        relative = Path(str(source.get("source_local_path") or ""))
        if relative.is_absolute():
            path = relative.resolve()
        else:
            path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise DividendImportError(f"{field} official PDF is missing or outside root")
        digest = _sha256_file(path)
        if digest != str(source.get("source_sha256") or ""):
            raise DividendImportError(f"{field} official PDF SHA-256 changed")


def _validated_source_evidence(
    distribution: Mapping[str, Any],
    *,
    company_id: str,
    fiscal_year: int,
    official_annual_root: Path | None = None,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    evidence = distribution.get("source_evidence")
    if not isinstance(evidence, Mapping):
        raise DividendImportError("source_evidence must be an object")
    candidate_reports = evidence.get("candidate_annual_report_pages")
    supporting_reports = evidence.get("supporting_annual_reports")
    if not isinstance(candidate_reports, list) or not candidate_reports:
        raise DividendImportError("candidate annual-report evidence is missing")
    if not isinstance(supporting_reports, list):
        raise DividendImportError("supporting annual-report evidence must be an array")
    validated_candidates: list[dict[str, Any]] = []
    for index, source in enumerate(candidate_reports):
        if not isinstance(source, Mapping):
            raise DividendImportError("candidate annual-report evidence must be an object")
        _validate_official_source(
            source,
            company_id=company_id,
            field=f"candidate_annual_report_pages[{index}]",
            official_annual_root=official_annual_root,
        )
        validated_candidates.append(dict(source))
    validated_support: list[dict[str, Any]] = []
    for index, source in enumerate(supporting_reports):
        if not isinstance(source, Mapping):
            raise DividendImportError("supporting annual-report evidence must be an object")
        _validate_official_source(
            source,
            company_id=company_id,
            field=f"supporting_annual_reports[{index}]",
            official_annual_root=official_annual_root,
        )
        validated_support.append(dict(source))

    raw_auxiliary = evidence.get("secondary_implementation_events")
    if not isinstance(raw_auxiliary, list):
        raise DividendImportError("secondary implementation evidence must be an array")
    auxiliary: list[dict[str, Any]] = []
    for index, source in enumerate(raw_auxiliary):
        if not isinstance(source, Mapping):
            raise DividendImportError("secondary implementation evidence must be an object")
        if source.get("source_type") != "SECONDARY_CORPORATE_ACTION_FEED":
            raise DividendImportError(f"secondary_implementation_events[{index}] has wrong type")
        if source.get("verification_status") != "EXACT_EVENT_AND_PAYLOAD_HASH_MATCH":
            raise DividendImportError(
                f"secondary_implementation_events[{index}] is not verified"
            )
        auxiliary.append(dict(source))

    primary_pool = validated_support or [
        source
        for source in validated_candidates
        if int(source.get("fiscal_year") or 0) > fiscal_year
    ]
    if not primary_pool:
        raise DividendImportError(
            "a later or supporting official annual report is required as the primary source"
        )
    primary = max(
        primary_pool,
        key=lambda source: _parse_date(
            source.get("source_publish_date"), field="primary_source.source_publish_date"
        ),
    )
    return primary, tuple(auxiliary)


def convert_ready_distribution(
    distribution: Mapping[str, Any], *, official_annual_root: Path | None = None
) -> AnnualDividendFact:
    """Convert one verified complete distribution without writing any state."""

    distribution_id = str(distribution.get("distribution_id") or "")
    _require_read_only_artifact(distribution, source=distribution_id or "distribution")
    if distribution.get("schema_version") != RECONCILIATION_SCHEMA_VERSION:
        raise DividendImportError(f"unexpected distribution schema: {distribution_id}")
    if distribution.get("ready_for_controlled_ledger_import") is not True:
        raise DividendImportError(
            f"distribution is not approved for controlled import: {distribution_id}"
        )
    if distribution.get("import_scope") != "FISCAL_YEAR_TOTAL":
        raise DividendImportError(
            f"only a complete fiscal-year total may be imported: {distribution_id}"
        )
    if distribution.get("dividend_kind") != "ORDINARY":
        raise DividendImportError(f"only ordinary dividends may be imported: {distribution_id}")
    if distribution.get("lifecycle_status") != "PAID":
        raise DividendImportError(f"only paid dividends may be imported: {distribution_id}")

    company_id = str(distribution.get("company_id") or "")
    company_name = str(distribution.get("company_name") or "")
    fiscal_year = int(distribution.get("fiscal_year") or 0)
    if not company_id or not company_name or fiscal_year < 1900:
        raise DividendImportError(f"invalid company/year identity: {distribution_id}")
    total = distribution.get("ordinary_cash_dividend_total")
    if not isinstance(total, Mapping):
        raise DividendImportError(f"ordinary dividend total is missing: {distribution_id}")
    if total.get("unit") != "currency":
        raise DividendImportError(f"ordinary dividend total has the wrong unit: {distribution_id}")
    currency = str(total.get("currency") or "")
    if not currency:
        raise DividendImportError(f"ordinary dividend currency is missing: {distribution_id}")
    amount = _parse_positive_decimal(
        total.get("value"), field=f"{distribution_id}.ordinary_cash_dividend_total"
    )
    primary_source, auxiliary_sources = _validated_source_evidence(
        distribution,
        company_id=company_id,
        fiscal_year=fiscal_year,
        official_annual_root=official_annual_root,
    )
    reviewed_at = _parse_review_time(distribution.get("reviewed_at"))
    source_location = str(
        primary_source.get("source_url") or primary_source.get("source_local_path") or ""
    )
    raw_point = RawDataPoint(
        company_id=company_id,
        field_id=f"FY{fiscal_year}.ordinary_dividend",
        security_id=None,
        share_class=None,
        source_name=str(primary_source["source_name"]),
        source_document=str(primary_source["source_document"]),
        source_url_or_local_path=source_location,
        source_publish_date=_parse_date(
            primary_source["source_publish_date"],
            field="primary_source.source_publish_date",
        ),
        source_fetch_time=reviewed_at,
        fiscal_period=f"FY{fiscal_year}",
        currency=currency,
        unit="currency",
        value=amount,
        data_status=DataStatus.VALID,
        restatement_status="RECONCILED_FROM_OFFICIAL_ANNUAL_REPORT",
    )
    return AnnualDividendFact(
        company_id=company_id,
        company_name=company_name,
        fiscal_year=fiscal_year,
        ordinary_dividend_status="PAID",
        ordinary_dividend=amount,
        currency=currency,
        distribution_id=distribution_id,
        raw_data_point=raw_point,
        primary_source=primary_source,
        auxiliary_sources=auxiliary_sources,
    )


def load_controlled_dividend_facts(
    reconciliation_root: str | Path,
    official_annual_root: str | Path | None = None,
) -> dict[tuple[str, int], AnnualDividendFact]:
    """Read and validate ``dividend-v1`` and return its 11 importable facts."""

    root = Path(reconciliation_root).resolve()
    try:
        verify_file_manifest(root)
    except Exception as error:
        raise DividendImportError(f"reconciliation manifest validation failed: {error}") from error
    manifest = _load_object(root / "manifest.json")
    report = _load_object(root / "report.json")
    _require_read_only_artifact(manifest, source="manifest.json")
    _require_read_only_artifact(report, source="report.json")
    if manifest.get("schema_version") != RECONCILIATION_SCHEMA_VERSION:
        raise DividendImportError("unexpected reconciliation manifest schema")
    if report.get("schema_version") != RECONCILIATION_SCHEMA_VERSION:
        raise DividendImportError("unexpected reconciliation report schema")
    safety = report.get("safety")
    if not isinstance(safety, Mapping) or safety.get("production_staging_modified") is not False:
        raise DividendImportError("report does not prove that production staging was untouched")
    source_manifest = report.get("source_candidate_manifest")
    if not isinstance(source_manifest, Mapping) or source_manifest.get("status") != "VALID":
        raise DividendImportError("source candidate manifest is not VALID")

    distribution_paths: list[Path] = []
    for item in manifest.get("files") or []:
        if not isinstance(item, Mapping):
            raise DividendImportError("manifest files must be objects")
        relative = Path(str(item.get("path") or ""))
        if (
            len(relative.parts) == 2
            and relative.parts[0] == "distributions"
            and relative.suffix == ".json"
        ):
            distribution_paths.append(root / relative)
    distributions = [_load_object(path) for path in sorted(distribution_paths)]
    scope = report.get("scope")
    if not isinstance(scope, Mapping):
        raise DividendImportError("report scope must be an object")
    expected_distribution_count = int(scope.get("distribution_count") or -1)
    if len(distributions) != expected_distribution_count:
        raise DividendImportError("distribution file count does not match report")

    ready_distributions: list[dict[str, Any]] = []
    component_only: list[dict[str, Any]] = []
    for distribution in distributions:
        _require_read_only_artifact(
            distribution,
            source=str(distribution.get("distribution_id") or "distribution"),
        )
        if distribution.get("schema_version") != RECONCILIATION_SCHEMA_VERSION:
            raise DividendImportError("unexpected distribution schema")
        if not str(distribution.get("distribution_id") or ""):
            raise DividendImportError("distribution_id is missing")
        if distribution.get("ready_for_controlled_ledger_import") is True:
            ready_distributions.append(distribution)
        else:
            component_only.append(distribution)
    if len(ready_distributions) != EXPECTED_READY_FACT_COUNT:
        raise DividendImportError(
            f"expected exactly {EXPECTED_READY_FACT_COUNT} ready dividend facts, "
            f"found {len(ready_distributions)}"
        )
    if int(report.get("ready_for_controlled_ledger_import_count") or -1) != len(
        ready_distributions
    ):
        raise DividendImportError("ready distribution count does not match report")
    if int(report.get("reconciled_complete_fiscal_year_total_count") or -1) != len(
        ready_distributions
    ):
        raise DividendImportError("complete fiscal-year total count does not match report")
    report_ready = report.get("ready_for_controlled_ledger_import")
    if not isinstance(report_ready, list):
        raise DividendImportError("report ready distribution list is missing")
    if any(not isinstance(item, Mapping) for item in report_ready):
        raise DividendImportError("report ready distribution entries must be objects")
    report_ready_ids = [str(item.get("distribution_id") or "") for item in report_ready]
    actual_ready_ids = [str(item.get("distribution_id") or "") for item in ready_distributions]
    if len(report_ready_ids) != len(set(report_ready_ids)) or set(report_ready_ids) != set(
        actual_ready_ids
    ):
        raise DividendImportError("report ready distribution ids do not match files")

    if len(component_only) != 1:
        raise DividendImportError("expected exactly one non-importable component-only distribution")
    if int(report.get("component_only_count") or -1) != len(component_only):
        raise DividendImportError("component-only count does not match report")
    rejected = component_only[0]
    if (
        rejected.get("distribution_id") != EXPECTED_COMPONENT_ONLY_ID
        or rejected.get("company_id") != "SZ002430"
        or int(rejected.get("fiscal_year") or 0) != 2023
        or rejected.get("import_scope") != "COMPONENT_ONLY"
        or rejected.get("ordinary_cash_dividend_total") is not None
    ):
        raise DividendImportError("unexpected component-only distribution")

    facts: dict[tuple[str, int], AnnualDividendFact] = {}
    for distribution in ready_distributions:
        fact = convert_ready_distribution(
            distribution,
            official_annual_root=(
                Path(official_annual_root) if official_annual_root is not None else None
            ),
        )
        key = (fact.company_id, fact.fiscal_year)
        if key in facts:
            raise DividendImportError(f"duplicate company/fiscal-year dividend fact: {key}")
        facts[key] = fact
    if len(facts) != EXPECTED_READY_FACT_COUNT:
        raise DividendImportError("controlled dividend facts are not unique")
    return facts
