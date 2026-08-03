from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .models import DataStatus, RawDataPoint


RECONCILIATION_SCHEMA = "share-capital-reconciliation-v1.0"
EXPECTED_COMPANY_COUNT = 56
EXPECTED_MATERIAL_CLASS_COUNT = 60
EXPECTED_ACCEPTED_FACT_COUNT = 21
EXPECTED_RIGHTS_VERIFIED_COUNT = 17
PROHIBITED_NUMERIC_KEYS = {
    "gross_buyback",
    "gross_cancelled_buyback",
    "net_reduction_factor",
    "b_eligible",
}


class ShareCapitalImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConfirmedShareCapitalFact:
    point: RawDataPoint
    legal_share_class_id: str
    rights_verified: bool
    economic_rights_factor: Decimal | None
    company_market_value_denominator_authorized: bool = False


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
        raise ShareCapitalImportError(f"cannot read reconciliation JSON: {path}") from error
    if not isinstance(value, dict):
        raise ShareCapitalImportError(f"reconciliation JSON must be an object: {path}")
    return value


def _safe_child(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise ShareCapitalImportError(f"unsafe manifest path: {relative!r}")
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ShareCapitalImportError(f"manifest path escapes reconciliation root: {relative}")
    return path


def _verify_bundle_manifest(root: Path) -> dict[str, Path]:
    manifest = _load_object(root / "manifest.json")
    if manifest.get("schema_version") != "share-capital-reconciliation-manifest-v1":
        raise ShareCapitalImportError("unsupported share-capital reconciliation manifest")
    listed: dict[str, Path] = {}
    for raw in manifest.get("files", []):
        if not isinstance(raw, Mapping):
            raise ShareCapitalImportError("manifest files must be objects")
        relative = str(raw.get("path") or "")
        if relative in listed:
            raise ShareCapitalImportError(f"duplicate manifest path: {relative}")
        path = _safe_child(root, relative)
        if not path.is_file():
            raise ShareCapitalImportError(f"manifest file is missing: {relative}")
        if path.stat().st_size != int(raw.get("size_bytes") or -1):
            raise ShareCapitalImportError(f"manifest size mismatch: {relative}")
        if _sha256(path) != str(raw.get("sha256") or ""):
            raise ShareCapitalImportError(f"manifest SHA-256 mismatch: {relative}")
        listed[relative] = path
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json" and not path.name.startswith(".")
    }
    if set(listed) != actual:
        raise ShareCapitalImportError("manifest does not exactly cover reconciliation bundle")
    if len(listed) != int(manifest.get("file_count") or -1):
        raise ShareCapitalImportError("manifest file_count mismatch")
    required = {"report.json", "review_basis.json", "review_cases.json"}
    if not required.issubset(listed):
        raise ShareCapitalImportError("reconciliation report, basis or review cases are missing")
    return listed


def _assert_no_prohibited_numeric_fields(value: Any, *, path: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in PROHIBITED_NUMERIC_KEYS and item is not None:
                raise ShareCapitalImportError(f"prohibited numeric field at {path}.{key}")
            _assert_no_prohibited_numeric_fields(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_prohibited_numeric_fields(item, path=f"{path}[{index}]")


def _positive_shares(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ShareCapitalImportError(f"invalid share count in {field}") from error
    if not result.is_finite() or result <= 0 or result != result.to_integral_value():
        raise ShareCapitalImportError(f"share count must be a positive integer in {field}")
    return result


def _company_files(files: Mapping[str, Path]) -> dict[str, dict[str, Any]]:
    companies: dict[str, dict[str, Any]] = {}
    for relative, path in sorted(files.items()):
        if not relative.startswith("companies/") or not relative.endswith(".json"):
            continue
        payload = _load_object(path)
        if payload.get("schema_version") != RECONCILIATION_SCHEMA:
            raise ShareCapitalImportError(f"unsupported company decision schema: {relative}")
        company_id = str(payload.get("company_id") or "")
        if not company_id or company_id in companies:
            raise ShareCapitalImportError(f"invalid or duplicate company decision: {company_id}")
        companies[company_id] = payload
    if len(companies) != EXPECTED_COMPANY_COUNT:
        raise ShareCapitalImportError(
            f"expected {EXPECTED_COMPANY_COUNT} company decisions, got {len(companies)}"
        )
    return companies


def _validate_report_and_basis(
    report: Mapping[str, Any],
    basis: Mapping[str, Any],
    companies: Mapping[str, Mapping[str, Any]],
) -> None:
    if report.get("schema_version") != RECONCILIATION_SCHEMA:
        raise ShareCapitalImportError("unsupported share-capital report")
    if report.get("writes_production") is not False:
        raise ShareCapitalImportError("share-capital report is not read-only")
    expected_counts = {
        "company_count": EXPECTED_COMPANY_COUNT,
        "material_share_class_count": EXPECTED_MATERIAL_CLASS_COUNT,
        "accepted_current_class_fact_count": EXPECTED_ACCEPTED_FACT_COUNT,
        "rights_verified_class_fact_count": EXPECTED_RIGHTS_VERIFIED_COUNT,
        "company_denominator_authorized_count": 0,
        "diluted_total_shares_non_null_count": 0,
        "diluted_net_share_reduction_non_null_count": 0,
    }
    for field, expected in expected_counts.items():
        if int(report.get(field) or 0) != expected:
            raise ShareCapitalImportError(f"report {field} mismatch")
    for field in ("candidate_manifest_sha256", "cancellation_manifest_sha256", "review_config_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", str(report.get(field) or "")) is None:
            raise ShareCapitalImportError(f"report {field} is invalid")
    if basis.get("schema_version") != "share-capital-reconciliation-review-v1.0":
        raise ShareCapitalImportError("unsupported share-capital review basis")
    policy = basis.get("policy")
    if not isinstance(policy, Mapping) or policy.get("production_write") is not False:
        raise ShareCapitalImportError("review basis is not read-only")
    policy_counts = {
        "expected_company_count": EXPECTED_COMPANY_COUNT,
        "expected_material_class_count": EXPECTED_MATERIAL_CLASS_COUNT,
        "expected_accepted_current_class_facts": EXPECTED_ACCEPTED_FACT_COUNT,
        "expected_rights_verified_class_facts": EXPECTED_RIGHTS_VERIFIED_COUNT,
    }
    for field, expected in policy_counts.items():
        if int(policy.get(field) or 0) != expected:
            raise ShareCapitalImportError(f"review policy {field} mismatch")
    summaries = {
        str(item.get("company_id") or ""): dict(item)
        for item in report.get("summary", [])
        if isinstance(item, Mapping)
    }
    if set(summaries) != set(companies):
        raise ShareCapitalImportError("report summary and company decision scopes disagree")
    for company_id, company in companies.items():
        summary = summaries[company_id]
        if int(summary.get("material_share_class_count") or 0) != int(
            company.get("material_share_class_count") or 0
        ):
            raise ShareCapitalImportError(f"material class summary mismatch: {company_id}")
        if summary.get("company_market_value_denominator_authorized") is not False:
            raise ShareCapitalImportError(f"summary authorizes company denominator: {company_id}")


def _official_source(
    company_id: str,
    fiscal_year: int,
    source: Mapping[str, Any],
    annual_root: Path,
    hash_cache: dict[Path, str],
) -> dict[str, Any]:
    manifest_paths = sorted((annual_root / "companies").glob(f"{company_id}_*/manifest.json"))
    if len(manifest_paths) != 1:
        raise ShareCapitalImportError(f"one official annual-report manifest is required: {company_id}")
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
        raise ShareCapitalImportError(
            f"one selected official annual report is required: {company_id} FY{fiscal_year}"
        )
    document = documents[0]
    comparisons = {
        "source_name": document.get("source_name"),
        "source_document": document.get("source_document"),
        "source_url": document.get("source_url"),
        "source_publish_date": document.get("source_publish_date"),
        "source_fetch_time": document.get("source_fetch_time"),
        "restatement_status": document.get("restatement_status"),
        "sha256": document.get("sha256"),
        "pdf_pages": document.get("pdf_pages"),
    }
    for field, expected in comparisons.items():
        if str(source.get(field) or "") != str(expected or ""):
            raise ShareCapitalImportError(
                f"official source {field} mismatch: {company_id} FY{fiscal_year}"
            )
    source_path = (annual_root / str(document.get("local_path") or "")).resolve()
    if annual_root not in source_path.parents or not source_path.is_file():
        raise ShareCapitalImportError(
            f"official annual report is missing or unsafe: {company_id} FY{fiscal_year}"
        )
    if str(source.get("source_local_path") or "") != str(source_path):
        raise ShareCapitalImportError(
            f"official source path mismatch: {company_id} FY{fiscal_year}"
        )
    actual_sha = hash_cache.get(source_path)
    if actual_sha is None:
        actual_sha = _sha256(source_path)
        hash_cache[source_path] = actual_sha
    if actual_sha != str(document.get("sha256") or ""):
        raise ShareCapitalImportError(
            f"official source file SHA mismatch: {company_id} FY{fiscal_year}"
        )
    return document


def _build_fact(
    company: Mapping[str, Any],
    class_fact: Mapping[str, Any],
    annual_root: Path,
    hash_cache: dict[Path, str],
) -> ConfirmedShareCapitalFact:
    company_id = str(company.get("company_id") or "")
    fiscal_year = int(class_fact.get("fiscal_year") or 0)
    security_id = str(class_fact.get("security_id") or "")
    share_class = str(class_fact.get("share_class") or "")
    legal_class = str(class_fact.get("legal_share_class_id") or "")
    if not all((company_id, security_id, share_class, legal_class)) or fiscal_year <= 0:
        raise ShareCapitalImportError(f"accepted class fact scope is incomplete: {company_id}")
    value = _positive_shares(class_fact.get("issued_shares"), "issued_shares")
    if str(class_fact.get("reported_issued_shares_candidate") or "") != format(value, "f"):
        raise ShareCapitalImportError(f"accepted class candidate and value disagree: {company_id}")
    rights_verified = class_fact.get("rights_verified") is True
    factor_raw = class_fact.get("economic_rights_factor")
    factor = Decimal(str(factor_raw)) if factor_raw is not None else None
    if rights_verified and factor != Decimal("1"):
        raise ShareCapitalImportError(f"verified class rights require factor 1: {company_id}")
    if not rights_verified and factor is not None:
        raise ShareCapitalImportError(f"unverified class rights factor must be null: {company_id}")
    source = class_fact.get("source")
    if not isinstance(source, Mapping) or source.get("identity_status") != "VALID":
        raise ShareCapitalImportError(f"accepted class source is invalid: {company_id}")
    document = _official_source(company_id, fiscal_year, source, annual_root, hash_cache)
    try:
        publish_date = date.fromisoformat(str(document["source_publish_date"]))
        fetch_time = datetime.fromisoformat(
            str(document["source_fetch_time"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError) as error:
        raise ShareCapitalImportError(f"official source date metadata is invalid: {company_id}") from error
    if fetch_time.tzinfo is None:
        fetch_time = fetch_time.replace(tzinfo=timezone.utc)
    point = RawDataPoint(
        company_id=company_id,
        field_id=f"SECURITY.{security_id}.issued_shares",
        security_id=security_id,
        share_class=share_class,
        source_name=str(document["source_name"]),
        source_document=str(document["source_document"]),
        source_url_or_local_path=str(document["source_url"]),
        source_publish_date=publish_date,
        source_fetch_time=fetch_time,
        fiscal_period=f"FY{fiscal_year}",
        currency=None,
        unit="shares",
        value=value,
        data_status=DataStatus.VALID,
        restatement_status=str(document["restatement_status"]),
    )
    return ConfirmedShareCapitalFact(
        point=point,
        legal_share_class_id=legal_class,
        rights_verified=rights_verified,
        economic_rights_factor=factor,
        company_market_value_denominator_authorized=False,
    )


def load_confirmed_share_capital_facts(
    reconciliation_root: Path,
    annual_report_root: Path,
) -> tuple[ConfirmedShareCapitalFact, ...]:
    """Read accepted current class facts without mutating any data store.

    The importer returns only exact issued-share facts.  It never returns a
    company market-value denominator, a diluted-share value or buyback cash.
    """

    root = reconciliation_root.resolve()
    annual_root = annual_report_root.resolve()
    files = _verify_bundle_manifest(root)
    report = _load_object(files["report.json"])
    basis = _load_object(files["review_basis.json"])
    review_cases = _load_object(files["review_cases.json"])
    companies = _company_files(files)
    _assert_no_prohibited_numeric_fields(report)
    _assert_no_prohibited_numeric_fields(review_cases)
    for company in companies.values():
        _assert_no_prohibited_numeric_fields(company)
    _validate_report_and_basis(report, basis, companies)
    if review_cases.get("schema_version") != "share-capital-review-cases-v1.0":
        raise ShareCapitalImportError("unsupported review-case schema")
    if int(review_cases.get("review_case_count") or 0) != len(
        review_cases.get("review_cases", [])
    ):
        raise ShareCapitalImportError("review-case count mismatch")

    facts: list[ConfirmedShareCapitalFact] = []
    material_count = 0
    hash_cache: dict[Path, str] = {}
    for company_id, company in sorted(companies.items()):
        if company.get("writes_production") is not False:
            raise ShareCapitalImportError(f"company decision is not read-only: {company_id}")
        if company.get("company_market_value_denominator_authorized") is not False:
            raise ShareCapitalImportError(f"company denominator is authorized: {company_id}")
        if company.get("diluted_endpoint_status") != "INSUFFICIENT_DATA":
            raise ShareCapitalImportError(f"company diluted endpoint is not blocked: {company_id}")
        classes = company.get("material_share_classes")
        if not isinstance(classes, list) or len(classes) != int(
            company.get("material_share_class_count") or 0
        ):
            raise ShareCapitalImportError(f"material share classes are invalid: {company_id}")
        material_count += len(classes)
        class_keys: set[tuple[str, str]] = set()
        accepted_values: list[Decimal] = []
        for class_fact in classes:
            if not isinstance(class_fact, Mapping):
                raise ShareCapitalImportError(f"class fact must be an object: {company_id}")
            key = (
                str(class_fact.get("security_id") or ""),
                str(class_fact.get("legal_share_class_id") or ""),
            )
            if not all(key) or key in class_keys or class_fact.get("material") is not True:
                raise ShareCapitalImportError(f"invalid or duplicate material class: {company_id}")
            class_keys.add(key)
            decision = str(class_fact.get("decision") or "")
            if decision == "ACCEPT":
                fact = _build_fact(company, class_fact, annual_root, hash_cache)
                facts.append(fact)
                accepted_values.append(fact.point.value or Decimal("0"))
            elif decision == "REVIEW":
                if class_fact.get("issued_shares") is not None:
                    raise ShareCapitalImportError(
                        f"unaccepted class carries issued_shares: {company_id} {key[0]}"
                    )
                if class_fact.get("rights_verified") is not False:
                    raise ShareCapitalImportError(
                        f"unaccepted class claims verified rights: {company_id} {key[0]}"
                    )
            else:
                raise ShareCapitalImportError(f"unsupported class decision: {company_id} {key[0]}")
        all_accepted = len(accepted_values) == len(classes)
        expected_status = "ACCEPT" if all_accepted else "REVIEW"
        if company.get("all_material_class_counts_status") != expected_status:
            raise ShareCapitalImportError(f"company count status mismatch: {company_id}")
        total = company.get("company_total_issued_shares")
        if all_accepted:
            if _positive_shares(total, "company_total_issued_shares") != sum(accepted_values):
                raise ShareCapitalImportError(f"company class totals do not reconcile: {company_id}")
        elif total is not None:
            raise ShareCapitalImportError(f"incomplete company carries a total: {company_id}")
        history = company.get("latest_five_fiscal_years")
        if not isinstance(history, list) or not history:
            raise ShareCapitalImportError(f"company history is missing: {company_id}")
        for row in history:
            if not isinstance(row, Mapping):
                raise ShareCapitalImportError(f"history row must be an object: {company_id}")
            if row.get("diluted_total_shares") is not None:
                raise ShareCapitalImportError(f"history contains diluted total shares: {company_id}")
            if row.get("diluted_net_share_reduction") is not None:
                raise ShareCapitalImportError(f"history contains diluted net reduction: {company_id}")
            if row.get("diluted_total_shares_status") != "INSUFFICIENT_DATA":
                raise ShareCapitalImportError(f"history diluted status is invalid: {company_id}")

    if material_count != EXPECTED_MATERIAL_CLASS_COUNT:
        raise ShareCapitalImportError("material share-class count mismatch")
    if len(facts) != EXPECTED_ACCEPTED_FACT_COUNT:
        raise ShareCapitalImportError("accepted current class-fact count mismatch")
    rights_count = sum(fact.rights_verified for fact in facts)
    if rights_count != EXPECTED_RIGHTS_VERIFIED_COUNT:
        raise ShareCapitalImportError("rights-verified fact count mismatch")
    field_keys = {(fact.point.company_id, fact.point.field_id) for fact in facts}
    if len(field_keys) != len(facts):
        raise ShareCapitalImportError("controlled import produced duplicate issued-share facts")
    if any(
        not fact.point.field_id.startswith("SECURITY.")
        or not fact.point.field_id.endswith(".issued_shares")
        for fact in facts
    ):
        raise ShareCapitalImportError("controlled import produced a forbidden field")
    return tuple(facts)


def load_confirmed_issued_share_points(
    reconciliation_root: Path,
    annual_report_root: Path,
) -> tuple[RawDataPoint, ...]:
    return tuple(
        fact.point
        for fact in load_confirmed_share_capital_facts(
            reconciliation_root, annual_report_root
        )
    )
