from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = "cancellation-reconciliation-v1.0"
ALLOWED_DECISIONS = {"ACCEPT", "REJECT", "REVIEW"}


class CancellationReconciliationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def compact_text(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("−", "-").replace("–", "-")


def pdf_page_count(path: Path, *, timeout_seconds: int = 30) -> int:
    completed = subprocess.run(
        ["pdfinfo", str(path)],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise CancellationReconciliationError(
            f"pdfinfo failed for {path}: {completed.stderr.strip()[:500]}"
        )
    match = re.search(r"^Pages:\s*(\d+)\s*$", completed.stdout, flags=re.MULTILINE)
    if match is None:
        raise CancellationReconciliationError(f"pdfinfo did not return a page count: {path}")
    return int(match.group(1))


def pdf_page_text(
    path: Path,
    first_page: int,
    last_page: int | None = None,
    *,
    timeout_seconds: int = 60,
) -> str:
    last = first_page if last_page is None else last_page
    completed = subprocess.run(
        [
            "pdftotext",
            "-f",
            str(first_page),
            "-l",
            str(last),
            "-layout",
            str(path),
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise CancellationReconciliationError(
            f"pdftotext failed for {path} pages {first_page}-{last}: "
            f"{completed.stderr.strip()[:500]}"
        )
    return completed.stdout


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or value in (None, ""):
        raise CancellationReconciliationError(f"{field} must be an integer share count")
    try:
        result = int(str(value))
    except ValueError as error:
        raise CancellationReconciliationError(f"{field} must be an integer share count") from error
    if result < 0:
        raise CancellationReconciliationError(f"{field} cannot be negative")
    return result


def validate_bridge(review: Mapping[str, Any]) -> dict[str, Any]:
    bridge = review.get("issued_share_bridge")
    if not isinstance(bridge, Mapping):
        raise CancellationReconciliationError("issued_share_bridge is required")
    opening = _integer(bridge.get("opening_issued_shares"), "opening_issued_shares")
    additions = sum(
        _integer(item.get("shares"), "issued_additions.shares")
        for item in bridge.get("issued_additions", [])
        if isinstance(item, Mapping)
    )
    cancellations = _integer(
        bridge.get("verified_cancelled_shares"), "verified_cancelled_shares"
    )
    derived_closing = opening + additions - cancellations
    if derived_closing < 0:
        raise CancellationReconciliationError("issued-share bridge produces a negative closing balance")
    expected_derived = _integer(
        bridge.get("derived_closing_issued_shares"), "derived_closing_issued_shares"
    )
    if derived_closing != expected_derived:
        raise CancellationReconciliationError(
            f"issued-share bridge does not reconcile: calculated {derived_closing}, "
            f"configured {expected_derived}"
        )
    reported_closing = _integer(
        bridge.get("reported_closing_issued_shares"), "reported_closing_issued_shares"
    )
    status = str(bridge.get("status") or "")
    if status not in ALLOWED_DECISIONS:
        raise CancellationReconciliationError(f"invalid issued-share bridge status: {status}")
    if status == "ACCEPT" and reported_closing != derived_closing:
        raise CancellationReconciliationError(
            "an ACCEPT issued-share bridge must equal the reported closing balance"
        )
    return {
        "opening_issued_shares": str(opening),
        "issued_additions": [dict(item) for item in bridge.get("issued_additions", [])],
        "verified_cancelled_shares": str(cancellations),
        "derived_closing_issued_shares": str(derived_closing),
        "reported_closing_issued_shares": str(reported_closing),
        "reported_minus_derived_shares": str(reported_closing - derived_closing),
        "reported_net_issued_share_change": str(reported_closing - opening),
        "derived_net_issued_share_change": str(derived_closing - opening),
        "status": status,
        "reason": str(bridge.get("reason") or ""),
        "reported_unit": str(bridge.get("reported_unit") or "shares"),
        "reported_unit_multiplier": str(bridge.get("reported_unit_multiplier") or "1"),
    }


def _candidate_key(item: Mapping[str, Any]) -> tuple[str, int]:
    return str(item.get("company_id") or ""), int(item.get("fiscal_year") or 0)


def discover_cancelled_share_candidates(candidate_root: Path) -> dict[tuple[str, int], dict[str, Any]]:
    found: dict[tuple[str, int], dict[str, Any]] = {}
    for path in sorted((candidate_root / "candidates").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for raw in payload.get("reports", []):
            if not isinstance(raw, Mapping) or raw.get("eligible_for_cancelled_shares_candidate") is not True:
                continue
            item = dict(raw)
            key = _candidate_key(item)
            if key in found:
                raise CancellationReconciliationError(f"duplicate cancellation candidate: {key}")
            item["candidate_file"] = str(path.resolve())
            found[key] = item
    return found


def _selected_document(
    annual_root: Path,
    company_id: str,
    fiscal_year: int,
) -> tuple[dict[str, Any], Path, Path]:
    manifests = sorted((annual_root / "companies").glob(f"{company_id}_*/manifest.json"))
    if len(manifests) != 1:
        raise CancellationReconciliationError(
            f"expected one annual-report manifest for {company_id}, found {len(manifests)}"
        )
    manifest_path = manifests[0]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    matches = [
        dict(item)
        for item in manifest.get("documents", [])
        if isinstance(item, Mapping)
        and int(item.get("fiscal_year") or 0) == fiscal_year
        and item.get("selection_status") == "SELECTED_CURRENT"
        and item.get("data_status") == "VERIFIED"
    ]
    if len(matches) != 1:
        raise CancellationReconciliationError(
            f"expected one selected verified report for {company_id} FY{fiscal_year}, "
            f"found {len(matches)}"
        )
    document = matches[0]
    source_path = (annual_root / str(document["local_path"])).resolve()
    if annual_root.resolve() not in source_path.parents or not source_path.is_file():
        raise CancellationReconciliationError(f"unsafe or missing annual report: {source_path}")
    return document, source_path, manifest_path


def verify_review_sources(
    review: Mapping[str, Any],
    annual_root: Path,
    *,
    page_counter: Callable[[Path], int] = pdf_page_count,
    page_reader: Callable[[Path, int, int | None], str] = pdf_page_text,
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    company_id = str(review.get("company_id") or "")
    source_expectations = review.get("source_expectations")
    if not isinstance(source_expectations, Sequence) or isinstance(source_expectations, (str, bytes)):
        raise CancellationReconciliationError("source_expectations must be an array")
    sources: dict[int, dict[str, Any]] = {}
    source_summaries: list[dict[str, Any]] = []
    for expected in source_expectations:
        if not isinstance(expected, Mapping):
            raise CancellationReconciliationError("source expectation must be an object")
        fiscal_year = int(expected.get("fiscal_year") or 0)
        if fiscal_year in sources:
            raise CancellationReconciliationError(f"duplicate source expectation FY{fiscal_year}")
        document, source_path, manifest_path = _selected_document(
            annual_root, company_id, fiscal_year
        )
        expected_sha = str(expected.get("sha256") or "")
        actual_sha = sha256_file(source_path)
        if actual_sha != expected_sha or str(document.get("sha256")) != expected_sha:
            raise CancellationReconciliationError(
                f"source SHA-256 mismatch for {company_id} FY{fiscal_year}"
            )
        actual_pages = page_counter(source_path)
        expected_pages = int(expected.get("pdf_pages") or 0)
        if actual_pages != expected_pages or int(document.get("pdf_pages") or 0) != expected_pages:
            raise CancellationReconciliationError(
                f"source page-count mismatch for {company_id} FY{fiscal_year}: "
                f"expected {expected_pages}, got {actual_pages}"
            )
        if str(document.get("source_document") or "") != str(expected.get("source_document") or ""):
            raise CancellationReconciliationError(
                f"source-document title mismatch for {company_id} FY{fiscal_year}"
            )
        identity_text = compact_text(page_reader(source_path, 1, min(15, actual_pages)))
        missing_identity = [
            fragment
            for fragment in expected.get("identity_fragments", [])
            if compact_text(str(fragment)) not in identity_text
        ]
        if missing_identity:
            raise CancellationReconciliationError(
                f"issuer/title identity failed for {company_id} FY{fiscal_year}: {missing_identity}"
            )
        sources[fiscal_year] = {
            "document": document,
            "path": source_path,
            "manifest": manifest_path,
            "pdf_pages": actual_pages,
            "sha256": actual_sha,
        }
        source_summaries.append(
            {
                "fiscal_year": fiscal_year,
                "source_document": document["source_document"],
                "source_url": document["source_url"],
                "source_publish_date": document["source_publish_date"],
                "source_local_path": str(source_path),
                "source_manifest": str(manifest_path.resolve()),
                "sha256": actual_sha,
                "pdf_pages": actual_pages,
                "identity_status": "VALID",
            }
        )
    return sources, source_summaries


def reconcile_one(
    candidate: Mapping[str, Any],
    review: Mapping[str, Any],
    sources: Mapping[int, Mapping[str, Any]],
    source_summaries: Sequence[Mapping[str, Any]],
    *,
    page_reader: Callable[[Path, int, int | None], str] = pdf_page_text,
) -> dict[str, Any]:
    company_id, fiscal_year = _candidate_key(candidate)
    if (company_id, fiscal_year) != _candidate_key(review):
        raise CancellationReconciliationError("candidate and review scope do not match")
    decision = str(review.get("decision") or "")
    cancellation_decision = str(review.get("cancellation_fact_decision") or "")
    diluted_status = str(review.get("diluted_share_bridge_status") or "")
    if decision not in ALLOWED_DECISIONS or cancellation_decision not in ALLOWED_DECISIONS:
        raise CancellationReconciliationError("invalid candidate/cancellation decision")
    if diluted_status not in ALLOWED_DECISIONS:
        raise CancellationReconciliationError("invalid diluted-share bridge status")
    candidate_value = _integer(
        candidate.get("cancelled_shares_candidate"), "candidate.cancelled_shares_candidate"
    )
    expected_candidate = _integer(
        review.get("candidate_cancelled_shares"), "review.candidate_cancelled_shares"
    )
    if candidate_value != expected_candidate:
        raise CancellationReconciliationError(
            f"candidate value changed for {company_id} FY{fiscal_year}: "
            f"expected {expected_candidate}, got {candidate_value}"
        )
    primary = sources.get(fiscal_year)
    if primary is None:
        raise CancellationReconciliationError("current-fiscal-year source expectation is missing")
    if str(candidate.get("source_sha256")) != str(primary["sha256"]):
        raise CancellationReconciliationError("candidate source SHA does not match selected annual report")
    if Path(str(candidate.get("source_local_path"))).resolve() != Path(primary["path"]).resolve():
        raise CancellationReconciliationError("candidate source path does not match selected annual report")

    verified_checks: list[dict[str, Any]] = []
    for check in review.get("evidence_checks", []):
        if not isinstance(check, Mapping):
            raise CancellationReconciliationError("evidence check must be an object")
        source_year = int(check.get("source_fiscal_year") or fiscal_year)
        source = sources.get(source_year)
        if source is None:
            raise CancellationReconciliationError(
                f"evidence check references unverified source FY{source_year}"
            )
        page = int(check.get("page") or 0)
        if page < 1 or page > int(source["pdf_pages"]):
            raise CancellationReconciliationError(
                f"evidence page out of range for {company_id} FY{source_year}: {page}"
            )
        text = compact_text(page_reader(Path(source["path"]), page, page))
        fragments = [str(item) for item in check.get("required_fragments", [])]
        missing = [fragment for fragment in fragments if compact_text(fragment) not in text]
        if missing:
            raise CancellationReconciliationError(
                f"evidence fragments missing for {company_id} FY{source_year} page {page}: {missing}"
            )
        verified_checks.append(
            {
                "kind": str(check.get("kind") or "evidence"),
                "source_fiscal_year": source_year,
                "page": page,
                "required_fragments": fragments,
                "status": "VALID",
            }
        )

    bridge = validate_bridge(review)
    verified_cancelled = _integer(
        bridge["verified_cancelled_shares"], "verified_cancelled_shares"
    )
    if cancellation_decision == "ACCEPT" and verified_cancelled == 0:
        raise CancellationReconciliationError("accepted cancellation cannot be zero")
    if review.get("net_reduction_factor") is not None:
        raise CancellationReconciliationError(
            "net_reduction_factor must remain null without an accepted diluted-share bridge"
        )
    if review.get("b_eligible_authorized") is not False:
        raise CancellationReconciliationError(
            "b_eligible_authorized must be false for this isolated review"
        )
    if diluted_status == "ACCEPT":
        raise CancellationReconciliationError(
            "this review does not include an endpoint diluted-share reconciliation"
        )

    canonical_input = json.dumps(
        {"candidate": dict(candidate), "review": dict(review)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": SCHEMA_VERSION,
        "review_id": str(review.get("review_id") or f"{company_id}-FY{fiscal_year}"),
        "company_id": company_id,
        "company_name": str(candidate.get("company_name") or review.get("company_name") or ""),
        "fiscal_year": fiscal_year,
        "candidate_decision": decision,
        "candidate_decision_reason": str(review.get("candidate_decision_reason") or ""),
        "candidate_cancelled_shares": str(candidate_value),
        "cancellation_fact_decision": cancellation_decision,
        "verified_cancelled_shares": str(verified_cancelled),
        "candidate_minus_verified_cancelled_shares": str(candidate_value - verified_cancelled),
        "issued_share_bridge": bridge,
        "issued_shares_are_not_diluted_shares": True,
        "diluted_share_bridge_status": diluted_status,
        "dilution_blockers": list(review.get("dilution_blockers") or []),
        "net_reduction_factor": None,
        "b_eligible": None,
        "b_eligible_authorized": False,
        "buyback_cash_scope_note": str(review.get("buyback_cash_scope_note") or ""),
        "source_checks": list(source_summaries),
        "evidence_checks": verified_checks,
        "input_hash": hashlib.sha256(canonical_input).hexdigest(),
        "writes_production": False,
    }

