from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from .dividend_reconciliation import sha256_file, verify_file_manifest
from .dividend_reconciliation_v2 import SCHEMA_VERSION, distribution_total
from .import_dividends import AnnualDividendFact, DividendImportError
from .models import DataStatus, RawDataPoint


EXPECTED_READY_FACT_COUNT = 16
EXPECTED_BLOCKED_COUNT = 261
EXPECTED_COMPANY_COUNT = 56
EXPECTED_TARGET_SLOT_COUNT = 277


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DividendImportError(f"cannot load JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise DividendImportError(f"JSON root must be an object: {path}")
    return value


def _require_read_only(value: Mapping[str, Any], *, source: str) -> None:
    if value.get("writes_production") is not False:
        raise DividendImportError(f"artifact is not explicitly read-only: {source}")


def _iso_date(value: Any, *, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise DividendImportError(f"{field} must be an ISO date") from error


def _review_time(value: Any) -> datetime:
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
    index: int,
    official_annual_root: Path,
) -> None:
    field = f"official_annual_report_pages[{index}]"
    if source.get("source_type") != "OFFICIAL_ANNUAL_REPORT":
        raise DividendImportError(f"{field} is not an official annual report")
    if source.get("identity_status") != "VALID":
        raise DividendImportError(f"{field} identity is not VALID")
    if source.get("verification_status") != "FULL_ANNUAL_REPORT_SHA256_AND_PAGE_COUNT_OK":
        raise DividendImportError(f"{field} PDF verification is incomplete")
    if str(source.get("company_id") or "") != company_id:
        raise DividendImportError(f"{field} company does not match")
    for required in (
        "source_name",
        "source_document",
        "source_publish_date",
        "source_sha256",
        "source_local_path",
    ):
        if not str(source.get(required) or "").strip():
            raise DividendImportError(f"{field}.{required} is missing")
    if len(str(source.get("source_sha256"))) != 64:
        raise DividendImportError(f"{field}.source_sha256 is invalid")
    if int(source.get("fiscal_year") or 0) < 1900:
        raise DividendImportError(f"{field}.fiscal_year is invalid")
    _iso_date(source.get("source_publish_date"), field=f"{field}.source_publish_date")
    markers = source.get("matched_markers")
    if not isinstance(markers, list) or not markers:
        raise DividendImportError(f"{field} has no matched evidence markers")
    relative = Path(str(source["source_local_path"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise DividendImportError(f"{field}.source_local_path must stay under annual root")
    annual_root = official_annual_root.resolve()
    pdf_path = (annual_root / relative).resolve()
    if pdf_path != annual_root and annual_root not in pdf_path.parents:
        raise DividendImportError(f"{field}.source_local_path escapes annual root")
    if not pdf_path.is_file():
        raise DividendImportError(f"{field} official PDF is missing")
    if sha256_file(pdf_path) != str(source["source_sha256"]):
        raise DividendImportError(f"{field} official PDF SHA-256 mismatch")


def _validated_sources(
    distribution: Mapping[str, Any],
    *,
    company_id: str,
    official_annual_root: Path,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    evidence = distribution.get("source_evidence")
    if not isinstance(evidence, Mapping):
        raise DividendImportError("source_evidence must be an object")
    raw_official = evidence.get("official_annual_report_pages")
    if not isinstance(raw_official, list) or not raw_official:
        raise DividendImportError("official annual-report evidence is missing")
    official: list[dict[str, Any]] = []
    for index, source in enumerate(raw_official):
        if not isinstance(source, Mapping):
            raise DividendImportError("official annual-report evidence must be an object")
        _validate_official_source(
            source,
            company_id=company_id,
            index=index,
            official_annual_root=official_annual_root,
        )
        official.append(dict(source))
    if not any(int(source.get("page") or 0) > 0 for source in official):
        raise DividendImportError("at least one official source must bind an exact PDF page")

    raw_events = evidence.get("secondary_implementation_events")
    if not isinstance(raw_events, list) or not raw_events:
        raise DividendImportError("verified implementation event is missing")
    events: list[dict[str, Any]] = []
    for index, event in enumerate(raw_events):
        if not isinstance(event, Mapping):
            raise DividendImportError("implementation event must be an object")
        if event.get("source_type") != "SECONDARY_CORPORATE_ACTION_FEED":
            raise DividendImportError(f"implementation event {index} has wrong source type")
        if event.get("verification_status") != "EXACT_EVENT_AND_PAYLOAD_HASH_MATCH":
            raise DividendImportError(f"implementation event {index} is not exact-match verified")
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("process") != "方案实施":
            raise DividendImportError(f"implementation event {index} is not an implemented plan")
        events.append(dict(event))
    primary = max(
        official,
        key=lambda source: _iso_date(
            source.get("source_publish_date"), field="primary_source.source_publish_date"
        ),
    )
    return primary, tuple(events)


def convert_ready_distribution_v2(
    distribution: Mapping[str, Any],
    *,
    official_annual_root: str | Path,
) -> AnnualDividendFact:
    """Convert one verified v2 distribution without writing files or databases."""

    distribution_id = str(distribution.get("distribution_id") or "")
    _require_read_only(distribution, source=distribution_id or "distribution")
    if distribution.get("schema_version") != SCHEMA_VERSION:
        raise DividendImportError(f"unexpected distribution schema: {distribution_id}")
    if distribution.get("ready_for_controlled_ledger_import") is not True:
        raise DividendImportError(f"distribution is not import-ready: {distribution_id}")
    if distribution.get("import_scope") != "FISCAL_YEAR_TOTAL":
        raise DividendImportError(f"distribution is not a complete fiscal-year total: {distribution_id}")
    if distribution.get("dividend_kind") != "ORDINARY":
        raise DividendImportError(f"distribution is not ordinary: {distribution_id}")
    if distribution.get("lifecycle_status") != "PAID":
        raise DividendImportError(f"distribution is not paid: {distribution_id}")
    company_id = str(distribution.get("company_id") or "")
    company_name = str(distribution.get("company_name") or "")
    fiscal_year = int(distribution.get("fiscal_year") or 0)
    if not company_id or not company_name or fiscal_year < 1900:
        raise DividendImportError(f"invalid company/fiscal-year identity: {distribution_id}")
    try:
        amount = distribution_total(distribution)
    except Exception as error:
        raise DividendImportError(f"invalid Decimal calculation: {distribution_id}: {error}") from error
    total = distribution.get("ordinary_cash_dividend_total")
    if not isinstance(total, Mapping):
        raise DividendImportError(f"ordinary total is missing: {distribution_id}")
    currency = str(total.get("currency") or "")
    if not currency:
        raise DividendImportError(f"ordinary total currency is missing: {distribution_id}")
    primary, events = _validated_sources(
        distribution,
        company_id=company_id,
        official_annual_root=Path(official_annual_root),
    )
    reviewed_at = _review_time(distribution.get("reviewed_at"))
    source_location = str(primary.get("source_url") or primary.get("source_local_path") or "")
    raw_point = RawDataPoint(
        company_id=company_id,
        field_id=f"FY{fiscal_year}.ordinary_dividend",
        security_id=None,
        share_class=None,
        source_name=str(primary["source_name"]),
        source_document=str(primary["source_document"]),
        source_url_or_local_path=source_location,
        source_publish_date=_iso_date(
            primary["source_publish_date"], field="primary_source.source_publish_date"
        ),
        source_fetch_time=reviewed_at,
        fiscal_period=f"FY{fiscal_year}",
        currency=currency,
        unit="currency",
        value=amount,
        data_status=DataStatus.VALID,
        restatement_status="RECONCILED_FROM_OFFICIAL_ANNUAL_REPORT_V2",
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
        primary_source=primary,
        auxiliary_sources=events,
    )


def load_controlled_dividend_facts_v2(
    reconciliation_root: str | Path,
    *,
    official_annual_root: str | Path,
) -> dict[tuple[str, int], AnnualDividendFact]:
    """Validate and read the 16 safe facts from ``dividend-v2``; never write state."""

    root = Path(reconciliation_root).resolve()
    annual_root = Path(official_annual_root).resolve()
    if not annual_root.is_dir():
        raise DividendImportError(f"official annual-report root is missing: {annual_root}")
    try:
        verify_file_manifest(root)
    except Exception as error:
        raise DividendImportError(f"reconciliation manifest validation failed: {error}") from error
    manifest = _load_object(root / "manifest.json")
    report = _load_object(root / "report.json")
    _require_read_only(manifest, source="manifest.json")
    _require_read_only(report, source="report.json")
    if manifest.get("schema_version") != SCHEMA_VERSION or report.get("schema_version") != SCHEMA_VERSION:
        raise DividendImportError("unexpected dividend-v2 schema")
    scope = report.get("scope")
    if not isinstance(scope, Mapping):
        raise DividendImportError("report scope must be an object")
    expected_scope = {
        "company_count": EXPECTED_COMPANY_COUNT,
        "target_fiscal_year_slot_count": EXPECTED_TARGET_SLOT_COUNT,
        "ready_for_controlled_ledger_import_count": EXPECTED_READY_FACT_COUNT,
        "blocked_count": EXPECTED_BLOCKED_COUNT,
    }
    for field, expected in expected_scope.items():
        if int(scope.get(field) or -1) != expected:
            raise DividendImportError(f"report {field} changed: expected {expected}")
    if EXPECTED_READY_FACT_COUNT + EXPECTED_BLOCKED_COUNT != EXPECTED_TARGET_SLOT_COUNT:
        raise DividendImportError("hard-coded target accounting does not balance")
    sources = report.get("source_manifests")
    if not isinstance(sources, Mapping) or any(
        not isinstance(value, Mapping) or value.get("status") != "VALID"
        for value in sources.values()
    ):
        raise DividendImportError("source manifests are not all VALID")
    safety = report.get("safety")
    required_safety = (
        "unknown_values_are_not_zero",
        "proposed_values_are_not_importable",
        "special_dividends_are_excluded",
        "component_only_values_are_not_full_year_totals",
        "official_pdf_sha256_and_page_evidence_revalidated",
    )
    if not isinstance(safety, Mapping) or any(safety.get(field) is not True for field in required_safety):
        raise DividendImportError("report safety guarantees are incomplete")
    if safety.get("production_staging_modified") is not False:
        raise DividendImportError("report does not prove staging was untouched")

    distribution_paths: list[Path] = []
    blocked_paths: list[Path] = []
    for item in manifest.get("files") or []:
        if not isinstance(item, Mapping):
            raise DividendImportError("manifest files entries must be objects")
        relative = Path(str(item.get("path") or ""))
        if len(relative.parts) == 2 and relative.suffix == ".json":
            if relative.parts[0] == "distributions":
                distribution_paths.append(root / relative)
            elif relative.parts[0] == "blocked":
                blocked_paths.append(root / relative)
    if len(distribution_paths) != EXPECTED_READY_FACT_COUNT:
        raise DividendImportError("distribution file count does not match expected ready facts")
    if len(blocked_paths) != EXPECTED_COMPANY_COUNT:
        raise DividendImportError("blocked files must cover every company")

    facts: dict[tuple[str, int], AnnualDividendFact] = {}
    for path in sorted(distribution_paths):
        distribution = _load_object(path)
        fact = convert_ready_distribution_v2(
            distribution,
            official_annual_root=annual_root,
        )
        key = (fact.company_id, fact.fiscal_year)
        if key in facts:
            raise DividendImportError(f"duplicate company/fiscal-year fact: {key}")
        facts[key] = fact
    report_ready = report.get("ready_for_controlled_ledger_import")
    if not isinstance(report_ready, list) or len(report_ready) != EXPECTED_READY_FACT_COUNT:
        raise DividendImportError("report ready list is incomplete")
    report_ids = {str(item.get("distribution_id") or "") for item in report_ready if isinstance(item, Mapping)}
    fact_ids = {fact.distribution_id for fact in facts.values()}
    if report_ids != fact_ids:
        raise DividendImportError("report ready ids do not match distribution files")

    target_map = report.get("target_fiscal_years")
    if not isinstance(target_map, Mapping) or len(target_map) != EXPECTED_COMPANY_COUNT:
        raise DividendImportError("target fiscal-year matrix is missing")
    target_keys = {
        (str(company_id), int(year))
        for company_id, years in target_map.items()
        for year in (years if isinstance(years, list) else [])
    }
    if len(target_keys) != EXPECTED_TARGET_SLOT_COUNT:
        raise DividendImportError("target fiscal-year matrix has the wrong size")
    blocked_keys: set[tuple[str, int]] = set()
    for path in sorted(blocked_paths):
        payload = _load_object(path)
        _require_read_only(payload, source=path.name)
        company_id = str(payload.get("company_id") or "")
        for row in payload.get("blocked") or []:
            if not isinstance(row, Mapping):
                raise DividendImportError("blocked row must be an object")
            if row.get("status") != "BLOCKED" or row.get("ordinary_cash_dividend_total") is not None:
                raise DividendImportError("blocked row must not contain a dividend number")
            if row.get("unknown_is_not_zero") is not True:
                raise DividendImportError("blocked row does not preserve unknown-vs-zero semantics")
            key = (company_id, int(row.get("fiscal_year") or 0))
            if key in blocked_keys:
                raise DividendImportError(f"duplicate blocked company/fiscal-year: {key}")
            blocked_keys.add(key)
    if len(blocked_keys) != EXPECTED_BLOCKED_COUNT:
        raise DividendImportError("blocked company/fiscal-year count does not match report")
    if set(facts) & blocked_keys:
        raise DividendImportError("a company/fiscal-year is both ready and blocked")
    if set(facts) | blocked_keys != target_keys:
        raise DividendImportError("ready and blocked rows do not exactly cover target matrix")
    return facts
