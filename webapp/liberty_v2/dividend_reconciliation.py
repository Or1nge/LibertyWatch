from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "dividend-reconciliation-v1.0"
DECISIONS = {"ACCEPT", "REJECT", "REVIEW"}
IMPORT_SCOPES = {"FISCAL_YEAR_TOTAL", "COMPONENT_ONLY", "NONE"}
SELECTION_BASES = {
    "CURRENT_ELIGIBLE",
    "HISTORICAL_ELIGIBLE_BEFORE_LIFECYCLE_FIX",
}


class DividendReconciliationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def compact_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def is_full_annual_report_title(value: str) -> bool:
    title = compact_text(value).lower()
    if not any(token in title for token in ("年度报告", "年報", "annualreport")):
        return False
    if "摘要" in title or "abridged" in title:
        return False
    notice_markers = (
        "更正公告",
        "更正说明",
        "更正說明",
        "更正通知",
        "修订公告",
        "修訂公告",
        "修订说明",
        "修訂說明",
        "修订通知",
        "修訂通知",
        "补充公告",
        "補充公告",
    )
    return not any(marker in title for marker in notice_markers)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DividendReconciliationError(f"cannot load JSON {path}: {error}") from error
    if not isinstance(payload, dict):
        raise DividendReconciliationError(f"JSON root must be an object: {path}")
    return payload


def _validate_nonnegative_decimal(value: Any, *, field: str) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise DividendReconciliationError(f"{field} must be a decimal string") from error
    if not number.is_finite() or number < 0:
        raise DividendReconciliationError(f"{field} must be finite and non-negative")
    return format(number, "f")


def verify_file_manifest(root: Path, *, expected_manifest_sha256: str | None = None) -> dict[str, Any]:
    root = root.resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise DividendReconciliationError(f"manifest is missing: {manifest_path}")
    manifest_sha256 = sha256_file(manifest_path)
    if expected_manifest_sha256 and manifest_sha256 != expected_manifest_sha256:
        raise DividendReconciliationError(
            f"manifest SHA-256 mismatch: expected {expected_manifest_sha256}, got {manifest_sha256}"
        )
    manifest = _load_json(manifest_path)
    listed: set[str] = set()
    for raw in manifest.get("files", []):
        if not isinstance(raw, Mapping):
            raise DividendReconciliationError("manifest files must contain objects")
        relative = str(raw.get("path") or "")
        if not relative or relative in listed:
            raise DividendReconciliationError(f"invalid or duplicate manifest path: {relative}")
        path = (root / relative).resolve()
        if root not in path.parents:
            raise DividendReconciliationError(f"manifest path escapes root: {relative}")
        if not path.is_file():
            raise DividendReconciliationError(f"manifest file is missing: {relative}")
        if path.stat().st_size != int(raw.get("size_bytes") or -1):
            raise DividendReconciliationError(f"manifest size mismatch: {relative}")
        if sha256_file(path) != str(raw.get("sha256") or ""):
            raise DividendReconciliationError(f"manifest SHA-256 mismatch: {relative}")
        listed.add(relative)
    if len(listed) != int(manifest.get("file_count") or -1):
        raise DividendReconciliationError("manifest file_count mismatch")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual != listed:
        missing = sorted(listed - actual)
        extra = sorted(actual - listed)
        raise DividendReconciliationError(
            f"manifest file set mismatch; missing={missing}, extra={extra}"
        )
    return {
        "status": "VALID",
        "file_count": len(listed),
        "manifest_sha256": manifest_sha256,
    }


def _run_text_command(argv: Sequence[str], *, timeout_seconds: int) -> str:
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DividendReconciliationError(f"command failed: {argv[0]}: {error}") from error
    if completed.returncode != 0:
        raise DividendReconciliationError(
            f"command returned {completed.returncode}: {argv[0]}: {completed.stderr[:500]}"
        )
    return completed.stdout


def pdf_page_text(path: Path, page: int, *, timeout_seconds: int = 60) -> str:
    return _run_text_command(
        ["pdftotext", "-f", str(page), "-l", str(page), "-layout", str(path), "-"],
        timeout_seconds=timeout_seconds,
    )


def pdf_page_range_text(
    path: Path, first_page: int, last_page: int, *, timeout_seconds: int = 90
) -> str:
    return _run_text_command(
        [
            "pdftotext",
            "-f",
            str(first_page),
            "-l",
            str(last_page),
            "-layout",
            str(path),
            "-",
        ],
        timeout_seconds=timeout_seconds,
    )


def pdf_full_text(path: Path, *, timeout_seconds: int = 120) -> str:
    return _run_text_command(
        ["pdftotext", "-layout", str(path), "-"], timeout_seconds=timeout_seconds
    )


def pdf_page_count(path: Path, *, timeout_seconds: int = 30) -> int:
    output = _run_text_command(["pdfinfo", str(path)], timeout_seconds=timeout_seconds)
    match = re.search(r"^Pages:\s+(\d+)\s*$", output, flags=re.MULTILINE)
    if not match:
        raise DividendReconciliationError(f"pdfinfo did not return a page count: {path}")
    return int(match.group(1))


def _assert_markers(text: str, markers: Iterable[str], *, source: str) -> list[str]:
    compact = compact_text(text)
    checked: list[str] = []
    for marker in markers:
        marker_compact = compact_text(str(marker))
        if not marker_compact or marker_compact not in compact:
            raise DividendReconciliationError(f"evidence marker not found in {source}: {marker}")
        checked.append(str(marker))
    return checked


def validate_identity_fragments(
    text: str, fragments: Sequence[str], *, source: str
) -> list[str]:
    if not fragments:
        raise DividendReconciliationError(f"identity_fragments are missing for {source}")
    compact = compact_text(text)
    missing = [fragment for fragment in fragments if compact_text(str(fragment)) not in compact]
    if missing:
        raise DividendReconciliationError(
            f"issuer/security identity failed for {source}: {missing}"
        )
    return [str(fragment) for fragment in fragments]


@dataclass(frozen=True)
class AnnualDocument:
    company_id: str
    fiscal_year: int
    path: Path
    relative_path: str
    metadata: Mapping[str, Any]


class AnnualReportIndex:
    def __init__(
        self,
        root: Path,
        *,
        minimum_pages: int = 60,
        identity_fragments: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.minimum_pages = minimum_pages
        self.identity_fragments = {
            str(company_id): [str(fragment) for fragment in fragments]
            for company_id, fragments in dict(identity_fragments or {}).items()
        }
        self._documents: dict[tuple[str, int], AnnualDocument] = {}
        self._full_text_cache: dict[Path, str] = {}
        self._validated_cache: dict[Path, dict[str, Any]] = {}
        for manifest_path in sorted(self.root.glob("companies/*/manifest.json")):
            manifest = _load_json(manifest_path)
            company_id = str(manifest.get("company_id") or "")
            for raw in manifest.get("documents", []):
                if not isinstance(raw, Mapping):
                    continue
                if raw.get("selection_status") != "SELECTED_CURRENT":
                    continue
                fiscal_year = int(raw.get("fiscal_year") or 0)
                key = (company_id, fiscal_year)
                if key in self._documents:
                    raise DividendReconciliationError(f"duplicate current annual report: {key}")
                relative = str(raw.get("local_path") or "")
                self._documents[key] = AnnualDocument(
                    company_id=company_id,
                    fiscal_year=fiscal_year,
                    path=(self.root / relative).resolve(),
                    relative_path=relative,
                    metadata=dict(raw),
                )

    def get(self, company_id: str, fiscal_year: int) -> AnnualDocument:
        try:
            return self._documents[(company_id, fiscal_year)]
        except KeyError as error:
            raise DividendReconciliationError(
                f"current annual report not found: {company_id} FY{fiscal_year}"
            ) from error

    def validate(self, document: AnnualDocument) -> dict[str, Any]:
        if document.path in self._validated_cache:
            return self._validated_cache[document.path]
        metadata = document.metadata
        if metadata.get("data_status") != "VERIFIED":
            raise DividendReconciliationError(f"annual report is not VERIFIED: {document.relative_path}")
        source_document = compact_text(str(metadata.get("source_document") or ""))
        if not is_full_annual_report_title(source_document):
            raise DividendReconciliationError(f"source is an abridged or corrected notice: {document.relative_path}")
        if not document.path.is_file():
            raise DividendReconciliationError(f"annual report PDF is missing: {document.relative_path}")
        expected_sha256 = str(metadata.get("sha256") or "")
        actual_sha256 = sha256_file(document.path)
        if actual_sha256 != expected_sha256:
            raise DividendReconciliationError(f"annual report SHA-256 mismatch: {document.relative_path}")
        actual_pages = pdf_page_count(document.path)
        if actual_pages != int(metadata.get("pdf_pages") or -1):
            raise DividendReconciliationError(f"annual report page count mismatch: {document.relative_path}")
        if actual_pages < self.minimum_pages:
            raise DividendReconciliationError(
                f"annual report is abnormally short ({actual_pages} pages): {document.relative_path}"
            )
        identity_pages = min(15, actual_pages)
        matched_identity = validate_identity_fragments(
            pdf_page_range_text(document.path, 1, identity_pages),
            self.identity_fragments.get(document.company_id, []),
            source=document.relative_path,
        )
        result = {
            "source_type": "OFFICIAL_ANNUAL_REPORT",
            "company_id": document.company_id,
            "fiscal_year": document.fiscal_year,
            "source_document": metadata.get("source_document"),
            "source_name": metadata.get("source_name"),
            "source_url": metadata.get("source_url"),
            "source_publish_date": metadata.get("source_publish_date"),
            "source_local_path": document.relative_path,
            "source_sha256": actual_sha256,
            "pdf_pages": actual_pages,
            "identity_fragments": matched_identity,
            "identity_pages_checked": {"first": 1, "last": identity_pages},
            "identity_status": "VALID",
            "verification_status": "FULL_ANNUAL_REPORT_SHA256_AND_PAGE_COUNT_OK",
        }
        self._validated_cache[document.path] = result
        return result

    def validate_markers(
        self,
        *,
        company_id: str,
        fiscal_year: int,
        markers: Sequence[str],
    ) -> dict[str, Any]:
        document = self.get(company_id, fiscal_year)
        result = dict(self.validate(document))
        if document.path not in self._full_text_cache:
            self._full_text_cache[document.path] = pdf_full_text(document.path)
        result["matched_markers"] = _assert_markers(
            self._full_text_cache[document.path],
            markers,
            source=document.relative_path,
        )
        return result

    def validate_candidate_page(
        self,
        *,
        company_id: str,
        fiscal_year: int,
        page: int,
        markers: Sequence[str],
    ) -> dict[str, Any]:
        document = self.get(company_id, fiscal_year)
        result = dict(self.validate(document))
        if page < 1 or page > int(result["pdf_pages"]):
            raise DividendReconciliationError(f"candidate page is outside PDF: {page}")
        result["page"] = page
        result["matched_markers"] = _assert_markers(
            pdf_page_text(document.path, page),
            markers,
            source=f"{document.relative_path}#page={page}",
        )
        return result


def load_candidate_inventory(candidate_root: Path) -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    for path in sorted((candidate_root / "candidates").glob("*.json")):
        company = _load_json(path)
        for report in company.get("reports", []):
            if not isinstance(report, Mapping):
                continue
            for candidate in report.get("candidates", []):
                if not isinstance(candidate, Mapping):
                    continue
                evidence_id = str(candidate.get("evidence_id") or "")
                if not evidence_id or evidence_id in inventory:
                    raise DividendReconciliationError(f"invalid duplicate evidence_id: {evidence_id}")
                inventory[evidence_id] = {
                    **dict(candidate),
                    "company_id": report.get("company_id"),
                    "company_name": report.get("company_name"),
                    "security_id": report.get("security_id"),
                    "market": report.get("market"),
                    "report_fiscal_year": report.get("fiscal_year"),
                }
    return inventory


def validate_futu_event(database: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    if not database.is_file():
        raise DividendReconciliationError(f"Futu event database is missing: {database}")
    event_key = str(expected.get("event_key") or "")
    uri = f"file:{database.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT event_key, issuer_id, event_type, event_date, source, source_url,
                   payload_hash, payload_json
            FROM events WHERE event_key = ?
            """,
            (event_key,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise DividendReconciliationError(f"Futu event not found: {event_key}")
    if row["event_type"] != "dividend":
        raise DividendReconciliationError(f"Futu event is not a dividend: {event_key}")
    for field in ("issuer_id", "payload_hash"):
        if str(row[field]) != str(expected.get(field) or ""):
            raise DividendReconciliationError(f"Futu event {field} mismatch: {event_key}")
    payload = json.loads(str(row["payload_json"]))
    for field, value in dict(expected.get("expected_payload") or {}).items():
        if payload.get(field) != value:
            raise DividendReconciliationError(
                f"Futu event payload mismatch for {field}: {event_key}"
            )
    return {
        "source_type": "SECONDARY_CORPORATE_ACTION_FEED",
        "event_key": row["event_key"],
        "issuer_id": row["issuer_id"],
        "event_date": row["event_date"],
        "source": row["source"],
        "source_url": row["source_url"] or None,
        "payload_hash": row["payload_hash"],
        "payload": payload,
        "verification_status": "EXACT_EVENT_AND_PAYLOAD_HASH_MATCH",
    }


def validate_review_config(
    config: Mapping[str, Any], inventory: Mapping[str, Mapping[str, Any]]
) -> None:
    decisions = list(config.get("candidate_decisions") or [])
    distributions = list(config.get("reconciled_distributions") or [])
    decision_ids = [str(item.get("evidence_id") or "") for item in decisions]
    if any(not evidence_id for evidence_id in decision_ids) or len(decision_ids) != len(
        set(decision_ids)
    ):
        raise DividendReconciliationError(
            "candidate decision evidence_ids must be unique and non-empty"
        )
    missing_inventory = sorted(set(decision_ids) - set(inventory))
    if missing_inventory:
        raise DividendReconciliationError(
            f"reviewed candidates are missing from the source inventory: {missing_inventory}"
        )
    historical_review_ids = [
        str(evidence_id)
        for evidence_id in config.get("expected_historical_review_ids") or []
    ]
    if (
        not historical_review_ids
        or any(not evidence_id for evidence_id in historical_review_ids)
        or len(historical_review_ids) != len(set(historical_review_ids))
    ):
        raise DividendReconciliationError(
            "expected_historical_review_ids must be unique, non-empty and explicitly configured"
        )
    if set(decision_ids) != set(historical_review_ids):
        raise DividendReconciliationError(
            "review decisions must preserve the complete historical review set; "
            f"missing={sorted(set(historical_review_ids)-set(decision_ids))}, "
            f"extra={sorted(set(decision_ids)-set(historical_review_ids))}"
        )
    eligible_ids = {
        evidence_id
        for evidence_id, item in inventory.items()
        if item.get("eligible_after_manual_review") is True
    }
    if not eligible_ids.issubset(set(decision_ids)):
        raise DividendReconciliationError(
            "review set must cover every currently eligible candidate; "
            f"missing={sorted(eligible_ids-set(decision_ids))}"
        )
    distribution_ids = [str(item.get("distribution_id") or "") for item in distributions]
    if len(distribution_ids) != len(set(distribution_ids)) or any(not item for item in distribution_ids):
        raise DividendReconciliationError("reconciled distribution_ids must be unique and non-empty")
    distribution_set = set(distribution_ids)
    distribution_by_id = {
        str(item.get("distribution_id") or ""): item for item in distributions
    }
    decision_by_id: dict[str, Mapping[str, Any]] = {}
    for item in decisions:
        decision = str(item.get("decision") or "")
        if decision not in DECISIONS:
            raise DividendReconciliationError(f"invalid candidate decision: {decision}")
        evidence_id = str(item.get("evidence_id") or "")
        selection_basis = str(item.get("selection_basis") or "")
        if selection_basis not in SELECTION_BASES:
            raise DividendReconciliationError(
                f"invalid candidate selection_basis: {evidence_id}: {selection_basis}"
            )
        expected_basis = (
            "CURRENT_ELIGIBLE"
            if evidence_id in eligible_ids
            else "HISTORICAL_ELIGIBLE_BEFORE_LIFECYCLE_FIX"
        )
        if selection_basis != expected_basis:
            raise DividendReconciliationError(
                f"candidate selection_basis does not match current source state: {evidence_id}"
            )
        decision_by_id[evidence_id] = item
        direct_target = item.get("distribution_id")
        replacement_target = item.get("replacement_distribution_id")
        if decision == "ACCEPT" and (
            not direct_target
            or replacement_target is not None
            or not item.get("accepted_role")
        ):
            raise DividendReconciliationError(
                f"accepted candidate must have only a direct distribution and accepted_role: {evidence_id}"
            )
        if decision == "REJECT" and (
            not replacement_target
            or direct_target is not None
            or item.get("accepted_role") is not None
        ):
            raise DividendReconciliationError(
                f"rejected candidate must have only a replacement distribution: {evidence_id}"
            )
        target = direct_target or replacement_target
        if target and str(target) not in distribution_set:
            raise DividendReconciliationError(f"candidate references unknown distribution: {target}")
        if not target:
            raise DividendReconciliationError(f"candidate decision has no reconciled target: {evidence_id}")
        if selection_basis == "HISTORICAL_ELIGIBLE_BEFORE_LIFECYCLE_FIX":
            downstream = distribution_by_id[str(target)]
            if not (
                downstream.get("support_annual_reports")
                or downstream.get("futu_events")
            ):
                raise DividendReconciliationError(
                    "historical review candidate requires independent downstream evidence: "
                    f"{evidence_id}"
                )
    candidate_owners: dict[str, str] = {}
    for item in distributions:
        distribution_id = str(item.get("distribution_id") or "")
        scope = str(item.get("import_scope") or "")
        if scope not in IMPORT_SCOPES:
            raise DividendReconciliationError(f"invalid import_scope: {scope}")
        for evidence_id in item.get("source_candidate_ids") or []:
            evidence_id = str(evidence_id)
            if evidence_id not in decision_by_id:
                raise DividendReconciliationError(
                    f"distribution references an unreviewed candidate: {evidence_id}"
                )
            if evidence_id in candidate_owners:
                raise DividendReconciliationError(
                    f"candidate is assigned to multiple distributions: {evidence_id}"
                )
            decision = decision_by_id[evidence_id]
            target = decision.get("distribution_id") or decision.get("replacement_distribution_id")
            if str(target) != distribution_id:
                raise DividendReconciliationError(
                    f"candidate decision/distribution target mismatch: {evidence_id}"
                )
            if str(inventory[evidence_id].get("company_id") or "") != str(
                item.get("company_id") or ""
            ):
                raise DividendReconciliationError(
                    f"candidate/distribution company mismatch: {evidence_id}"
                )
            candidate_owners[evidence_id] = distribution_id
        total = item.get("ordinary_cash_dividend_total")
        if total is not None:
            if not isinstance(total, Mapping):
                raise DividendReconciliationError("ordinary_cash_dividend_total must be an object or null")
            _validate_nonnegative_decimal(total.get("value"), field="ordinary_cash_dividend_total.value")
            if not total.get("currency") or total.get("unit") != "currency":
                raise DividendReconciliationError("total dividend requires currency and unit=currency")
        for position, component in enumerate(item.get("per_share_components") or []):
            _validate_nonnegative_decimal(
                component.get("value"), field=f"per_share_components[{position}].value"
            )
            if int(component.get("share_basis") or 0) <= 0:
                raise DividendReconciliationError("per-share component requires positive share_basis")
        ready = item.get("ready_for_controlled_ledger_import") is True
        if ready and (scope != "FISCAL_YEAR_TOTAL" or total is None):
            raise DividendReconciliationError(
                "controlled ledger import readiness requires a reconciled fiscal-year total"
            )
        if ready and (
            item.get("dividend_kind") != "ORDINARY"
            or item.get("lifecycle_status") not in {"PAID", "APPROVED"}
        ):
            raise DividendReconciliationError(
                "controlled ledger import readiness requires an ordinary paid/approved distribution"
            )
    if set(candidate_owners) != set(decision_ids):
        raise DividendReconciliationError("every reviewed candidate must belong to exactly one distribution")


def public_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "evidence_id",
        "company_id",
        "company_name",
        "security_id",
        "market",
        "report_fiscal_year",
        "associated_fiscal_year",
        "amount_kind",
        "component",
        "value",
        "unit",
        "currency",
        "share_basis",
        "dividend_kind",
        "lifecycle_status",
        "source_document",
        "source_publish_date",
        "source_url",
        "source_sha256",
        "page",
        "line_excerpt",
    )
    return {field: candidate.get(field) for field in fields}
