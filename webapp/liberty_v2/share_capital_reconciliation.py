from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .import_cancellations import load_confirmed_cancellation_points


SCHEMA_VERSION = "share-capital-reconciliation-v1.0"
CANDIDATE_SCHEMA_VERSION = "equity-bridge-candidates-v1.0"
ALLOWED_DECISIONS = {"ACCEPT", "REVIEW", "REJECT"}


class ShareCapitalReconciliationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ShareCapitalReconciliationError(f"cannot read JSON object: {path}") from error
    if not isinstance(value, dict):
        raise ShareCapitalReconciliationError(f"JSON value must be an object: {path}")
    return value


def compact_text(value: str) -> str:
    return (
        re.sub(r"\s+", "", value)
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace("（", "(")
        .replace("）", ")")
        .replace("％", "%")
    )


def pdf_page_count(path: Path, *, timeout_seconds: int = 30) -> int:
    completed = subprocess.run(
        ["pdfinfo", str(path)],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise ShareCapitalReconciliationError(
            f"pdfinfo failed for {path}: {completed.stderr.strip()[:500]}"
        )
    match = re.search(r"^Pages:\s*(\d+)\s*$", completed.stdout, flags=re.MULTILINE)
    if match is None:
        raise ShareCapitalReconciliationError(f"pdfinfo returned no page count: {path}")
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
        raise ShareCapitalReconciliationError(
            f"pdftotext failed for {path} pages {first_page}-{last}: "
            f"{completed.stderr.strip()[:500]}"
        )
    return completed.stdout


def positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or value in (None, ""):
        raise ShareCapitalReconciliationError(f"{field} must be a positive integer")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ShareCapitalReconciliationError(f"{field} must be a positive integer") from error
    if not decimal.is_finite() or decimal <= 0 or decimal != decimal.to_integral_value():
        raise ShareCapitalReconciliationError(f"{field} must be a positive integer")
    return int(decimal)


def verify_candidate_manifest(
    candidate_root: Path,
    *,
    expected_manifest_sha256: str,
) -> dict[str, Path]:
    root = candidate_root.resolve()
    manifest_path = root / "manifest.json"
    if sha256_file(manifest_path) != expected_manifest_sha256:
        raise ShareCapitalReconciliationError("candidate manifest SHA-256 changed")
    manifest = load_json_object(manifest_path)
    if manifest.get("schema_version") != "equity-bridge-candidate-manifest-v1":
        raise ShareCapitalReconciliationError("unsupported equity candidate manifest")
    listed: dict[str, Path] = {}
    for raw in manifest.get("files", []):
        if not isinstance(raw, Mapping):
            raise ShareCapitalReconciliationError("candidate manifest files must be objects")
        relative = str(raw.get("path") or "")
        if not relative or Path(relative).is_absolute() or relative in listed:
            raise ShareCapitalReconciliationError(f"unsafe or duplicate candidate path: {relative}")
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise ShareCapitalReconciliationError(f"missing or unsafe candidate file: {relative}")
        if path.stat().st_size != int(raw.get("size_bytes") or -1):
            raise ShareCapitalReconciliationError(f"candidate size mismatch: {relative}")
        if sha256_file(path) != str(raw.get("sha256") or ""):
            raise ShareCapitalReconciliationError(f"candidate SHA-256 mismatch: {relative}")
        listed[relative] = path
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json" and not path.name.startswith(".")
    }
    if set(listed) != actual or len(listed) != int(manifest.get("file_count") or -1):
        raise ShareCapitalReconciliationError("candidate manifest does not exactly cover bundle")
    return listed


def load_candidate_companies(
    candidate_root: Path,
    *,
    expected_manifest_sha256: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    listed = verify_candidate_manifest(
        candidate_root, expected_manifest_sha256=expected_manifest_sha256
    )
    report = load_json_object(listed["report.json"])
    if report.get("schema_version") != CANDIDATE_SCHEMA_VERSION:
        raise ShareCapitalReconciliationError("unsupported equity candidate report")
    companies: dict[str, dict[str, Any]] = {}
    for relative, path in sorted(listed.items()):
        if not relative.startswith("candidates/"):
            continue
        payload = load_json_object(path)
        if payload.get("schema_version") != CANDIDATE_SCHEMA_VERSION:
            raise ShareCapitalReconciliationError(f"unsupported candidate schema: {relative}")
        if payload.get("candidate_only") is not True or payload.get("writes_production") is not False:
            raise ShareCapitalReconciliationError(f"candidate is not read-only: {relative}")
        company_id = str(payload.get("company_id") or "")
        if not company_id or company_id in companies:
            raise ShareCapitalReconciliationError(f"invalid or duplicate company: {company_id}")
        reports = payload.get("reports")
        if not isinstance(reports, list) or not reports:
            raise ShareCapitalReconciliationError(f"candidate has no reports: {company_id}")
        years = [int(item.get("fiscal_year") or 0) for item in reports if isinstance(item, Mapping)]
        if len(years) != len(reports) or len(years) != len(set(years)):
            raise ShareCapitalReconciliationError(f"invalid fiscal-year rows: {company_id}")
        companies[company_id] = payload
    report_ids = {
        str(item.get("company_id") or "")
        for item in report.get("companies", [])
        if isinstance(item, Mapping)
    }
    if set(companies) != report_ids:
        raise ShareCapitalReconciliationError("candidate report and company files disagree")
    return companies, report


def _selected_document(
    annual_root: Path,
    company_id: str,
    fiscal_year: int,
) -> tuple[dict[str, Any], Path, Path]:
    manifests = sorted((annual_root / "companies").glob(f"{company_id}_*/manifest.json"))
    if len(manifests) != 1:
        raise ShareCapitalReconciliationError(
            f"expected one annual-report manifest for {company_id}, found {len(manifests)}"
        )
    manifest_path = manifests[0]
    manifest = load_json_object(manifest_path)
    matches = [
        dict(item)
        for item in manifest.get("documents", [])
        if isinstance(item, Mapping)
        and int(item.get("fiscal_year") or 0) == fiscal_year
        and item.get("selection_status") == "SELECTED_CURRENT"
        and item.get("data_status") == "VERIFIED"
    ]
    if len(matches) != 1:
        raise ShareCapitalReconciliationError(
            f"expected one selected official report for {company_id} FY{fiscal_year}, "
            f"found {len(matches)}"
        )
    document = matches[0]
    source_path = (annual_root / str(document.get("local_path") or "")).resolve()
    if annual_root.resolve() not in source_path.parents or not source_path.is_file():
        raise ShareCapitalReconciliationError(f"missing or unsafe official report: {source_path}")
    return document, source_path, manifest_path


def _source_summary(
    document: Mapping[str, Any],
    source_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    return {
        "source_name": str(document.get("source_name") or ""),
        "source_document": str(document.get("source_document") or ""),
        "source_url": str(document.get("source_url") or ""),
        "source_publish_date": str(document.get("source_publish_date") or ""),
        "source_fetch_time": str(document.get("source_fetch_time") or ""),
        "restatement_status": str(document.get("restatement_status") or ""),
        "source_local_path": str(source_path),
        "source_manifest": str(manifest_path.resolve()),
        "sha256": str(document.get("sha256") or ""),
        "pdf_pages": int(document.get("pdf_pages") or 0),
        "identity_status": "VALID",
    }


def verify_candidate_source(
    row: Mapping[str, Any],
    annual_root: Path,
    *,
    hash_cache: dict[Path, str] | None = None,
) -> dict[str, Any]:
    company_id = str(row.get("company_id") or "")
    fiscal_year = int(row.get("fiscal_year") or 0)
    document, source_path, manifest_path = _selected_document(
        annual_root, company_id, fiscal_year
    )
    comparisons = {
        "source_document": document.get("source_document"),
        "source_url": document.get("source_url"),
        "source_publish_date": document.get("source_publish_date"),
        "source_fetch_time": document.get("source_fetch_time"),
        "source_sha256": document.get("sha256"),
    }
    for candidate_field, official_value in comparisons.items():
        if str(row.get(candidate_field) or "") != str(official_value or ""):
            raise ShareCapitalReconciliationError(
                f"candidate source {candidate_field} mismatch: {company_id} FY{fiscal_year}"
            )
    if Path(str(row.get("source_local_path") or "")).resolve() != source_path:
        raise ShareCapitalReconciliationError(
            f"candidate source path mismatch: {company_id} FY{fiscal_year}"
        )
    cache = {} if hash_cache is None else hash_cache
    actual_sha = cache.get(source_path)
    if actual_sha is None:
        actual_sha = sha256_file(source_path)
        cache[source_path] = actual_sha
    if actual_sha != str(document.get("sha256") or ""):
        raise ShareCapitalReconciliationError(
            f"official source SHA-256 mismatch: {company_id} FY{fiscal_year}"
        )
    return _source_summary(document, source_path, manifest_path)


def exact_issued_candidate(row: Mapping[str, Any]) -> bool:
    if (
        row.get("status") != "VALID"
        or row.get("eligible_for_issued_share_candidate") is not True
        or row.get("unit") != "shares"
    ):
        return False
    try:
        positive_integer(row.get("closing_issued_shares"), "closing_issued_shares")
    except ShareCapitalReconciliationError:
        return False
    evidence = row.get("closing_evidence")
    multiplier = row.get("reported_unit_multiplier")
    if multiplier in (None, "") and isinstance(evidence, Mapping):
        multiplier = evidence.get("reported_unit_multiplier")
    return str(multiplier or "1") == "1"


def verify_manual_sources(
    config: Mapping[str, Any],
    annual_root: Path,
    *,
    page_counter: Callable[[Path], int] = pdf_page_count,
    page_reader: Callable[[Path, int, int | None], str] = pdf_page_text,
) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for raw in config.get("source_expectations", []):
        if not isinstance(raw, Mapping):
            raise ShareCapitalReconciliationError("source expectation must be an object")
        expectation_id = str(raw.get("source_expectation_id") or "")
        if not expectation_id or expectation_id in sources:
            raise ShareCapitalReconciliationError(
                f"invalid or duplicate source expectation: {expectation_id}"
            )
        company_id = str(raw.get("company_id") or "")
        fiscal_year = int(raw.get("fiscal_year") or 0)
        document, source_path, manifest_path = _selected_document(
            annual_root, company_id, fiscal_year
        )
        expected_sha = str(raw.get("sha256") or "")
        if (
            sha256_file(source_path) != expected_sha
            or str(document.get("sha256") or "") != expected_sha
        ):
            raise ShareCapitalReconciliationError(f"manual source SHA mismatch: {expectation_id}")
        pages = page_counter(source_path)
        if pages != int(raw.get("pdf_pages") or 0) or pages != int(document.get("pdf_pages") or 0):
            raise ShareCapitalReconciliationError(
                f"manual source page-count mismatch: {expectation_id}"
            )
        if str(document.get("source_document") or "") != str(raw.get("source_document") or ""):
            raise ShareCapitalReconciliationError(
                f"manual source-document mismatch: {expectation_id}"
            )
        identity = compact_text(page_reader(source_path, 1, min(15, pages)))
        missing = [
            str(fragment)
            for fragment in raw.get("identity_fragments", [])
            if compact_text(str(fragment)) not in identity
        ]
        if missing:
            raise ShareCapitalReconciliationError(
                f"manual source identity failed {expectation_id}: {missing}"
            )
        sources[expectation_id] = {
            "company_id": company_id,
            "fiscal_year": fiscal_year,
            "document": document,
            "path": source_path,
            "manifest": manifest_path,
            "summary": _source_summary(document, source_path, manifest_path),
        }
    return sources


def verify_evidence_checks(
    item: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    page_reader: Callable[[Path, int, int | None], str] = pdf_page_text,
) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for raw in item.get("evidence_checks", []):
        if not isinstance(raw, Mapping):
            raise ShareCapitalReconciliationError("evidence check must be an object")
        page = int(raw.get("page") or 0)
        if page < 1 or page > int(source["summary"]["pdf_pages"]):
            raise ShareCapitalReconciliationError(f"evidence page out of range: {page}")
        page_text = compact_text(page_reader(Path(source["path"]), page, page))
        fragments = [str(fragment) for fragment in raw.get("required_fragments", [])]
        missing = [fragment for fragment in fragments if compact_text(fragment) not in page_text]
        if missing:
            raise ShareCapitalReconciliationError(
                f"evidence fragments missing for {item.get('company_id')} page {page}: {missing}"
            )
        verified.append(
            {"page": page, "required_fragments": fragments, "status": "VALID"}
        )
    return verified


def _validate_arithmetic_identity(item: Mapping[str, Any]) -> None:
    derivation = str(item.get("derivation") or "")
    if not derivation.startswith("OFFICIAL_ARITHMETIC_IDENTITY:"):
        return
    expression = derivation.split(":", 1)[1]
    if re.fullmatch(r"\d+(?:\+\d+)+", expression) is None:
        raise ShareCapitalReconciliationError(f"unsafe arithmetic identity: {expression}")
    calculated = sum(int(part) for part in expression.split("+"))
    expected = positive_integer(item.get("issued_shares"), "manual issued_shares")
    if calculated != expected:
        raise ShareCapitalReconciliationError(
            f"manual arithmetic identity does not reconcile: {item.get('fact_id')}"
        )


def verified_manual_facts(
    config: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    *,
    page_reader: Callable[[Path, int, int | None], str] = pdf_page_text,
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    facts: dict[tuple[str, str], dict[str, Any]] = {}
    review_cases: list[dict[str, Any]] = []
    for raw in config.get("manual_current_facts", []):
        if not isinstance(raw, Mapping) or raw.get("decision") != "ACCEPT":
            raise ShareCapitalReconciliationError("manual current fact must be ACCEPT")
        item = dict(raw)
        source_id = str(item.get("source_expectation_id") or "")
        source = sources.get(source_id)
        if source is None:
            raise ShareCapitalReconciliationError(f"missing source expectation: {source_id}")
        if (
            str(item.get("company_id") or "") != str(source["company_id"])
            or int(item.get("fiscal_year") or 0) != int(source["fiscal_year"])
        ):
            raise ShareCapitalReconciliationError(f"manual fact source scope mismatch: {item}")
        value = positive_integer(item.get("issued_shares"), "manual issued_shares")
        _validate_arithmetic_identity(item)
        rights_verified = item.get("rights_verified") is True
        factor = item.get("economic_rights_factor")
        if rights_verified and str(factor) != "1":
            raise ShareCapitalReconciliationError("verified equal rights require factor 1")
        if not rights_verified and factor is not None:
            raise ShareCapitalReconciliationError("unverified rights factor must remain null")
        key = (str(item.get("company_id") or ""), str(item.get("security_id") or ""))
        if not all(key) or key in facts:
            raise ShareCapitalReconciliationError(f"duplicate manual fact scope: {key}")
        facts[key] = {
            **item,
            "issued_shares": str(value),
            "source": dict(source["summary"]),
            "verified_evidence": verify_evidence_checks(
                item, source, page_reader=page_reader
            ),
        }
    for raw in config.get("review_only_cases", []):
        if not isinstance(raw, Mapping) or raw.get("decision") != "REVIEW":
            raise ShareCapitalReconciliationError("review-only case must be REVIEW")
        item = dict(raw)
        source = sources.get(str(item.get("source_expectation_id") or ""))
        if source is None:
            raise ShareCapitalReconciliationError("review-only source expectation is missing")
        review_cases.append(
            {
                **item,
                "source": dict(source["summary"]),
                "verified_evidence": verify_evidence_checks(
                    item, source, page_reader=page_reader
                ),
            }
        )
    return facts, review_cases


def load_cancellation_context(
    cancellation_root: Path,
    annual_root: Path,
) -> dict[tuple[str, int], dict[str, Any]]:
    points = load_confirmed_cancellation_points(cancellation_root, annual_root)
    point_values = {
        (point.company_id, int(point.fiscal_period.removeprefix("FY"))): str(point.value)
        for point in points
    }
    decisions: dict[tuple[str, int], dict[str, Any]] = {}
    for path in sorted((cancellation_root / "decisions").glob("*.json")):
        raw = load_json_object(path)
        key = (str(raw.get("company_id") or ""), int(raw.get("fiscal_year") or 0))
        if key not in point_values or str(raw.get("verified_cancelled_shares") or "") != point_values[key]:
            raise ShareCapitalReconciliationError(f"cancellation context mismatch: {key}")
        bridge = raw.get("issued_share_bridge")
        if not isinstance(bridge, Mapping):
            raise ShareCapitalReconciliationError(f"cancellation bridge is missing: {key}")
        decisions[key] = {
            "cancelled_shares": point_values[key],
            "cancellation_status": "ACCEPT",
            "issued_share_bridge_status": str(bridge.get("status") or "REVIEW"),
            "known_issued_additions": [
                {
                    "type": str(item.get("type") or ""),
                    "shares": str(item.get("shares") or ""),
                }
                for item in bridge.get("issued_additions", [])
                if isinstance(item, Mapping)
            ],
        }
    if set(decisions) != set(point_values):
        raise ShareCapitalReconciliationError("cancellation points and decisions disagree")
    return decisions


def _action_bridge(
    company_id: str,
    fiscal_year: int,
    cancellation_context: Mapping[tuple[str, int], Mapping[str, Any]],
) -> dict[str, Any]:
    cancellation = cancellation_context.get((company_id, fiscal_year))
    return {
        "cancellation": {
            "status": "ACCEPT" if cancellation is not None else "REVIEW",
            "cancelled_shares": (
                str(cancellation["cancelled_shares"]) if cancellation is not None else None
            ),
            "reason": (
                "Imported read-only from the verified cancellation-v1 bundle."
                if cancellation is not None
                else "No exact, separately reconciled cancellation fact is available."
            ),
        },
        "issuance": {
            "status": (
                str(cancellation["issued_share_bridge_status"])
                if cancellation is not None
                else "REVIEW"
            ),
            "known_components": (
                list(cancellation["known_issued_additions"])
                if cancellation is not None
                else []
            ),
            "reason": "Known components are not treated as an exhaustive diluted-share bridge.",
        },
        "share_based_compensation": {
            "status": "REVIEW",
            "reason": "Awards, options and treasury/subsidiary-held shares are not fully reconciled at both endpoints.",
        },
        "convertible_conversion": {
            "status": "REVIEW",
            "reason": "Convertible and other contingent issuance effects are not fully reconciled at both endpoints.",
        },
        "diluted_share_bridge_status": "INSUFFICIENT_DATA",
    }


def _material_classes(
    company: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> list[dict[str, Any]]:
    company_id = str(company.get("company_id") or "")
    configured = overrides.get(company_id)
    if configured is not None:
        if not isinstance(configured, Sequence) or isinstance(configured, (str, bytes)):
            raise ShareCapitalReconciliationError(f"invalid material-class override: {company_id}")
        result = [dict(item) for item in configured if isinstance(item, Mapping)]
    else:
        latest = max(company["reports"], key=lambda item: int(item["fiscal_year"]))
        share_class = str(latest.get("share_class") or "ORDINARY")
        result = [
            {
                "security_id": str(latest.get("security_id") or company_id),
                "legal_share_class_id": (
                    f"{share_class}_ORDINARY" if share_class in {"A", "H"} else "ORDINARY"
                ),
                "share_class": share_class,
                "material": True,
            }
        ]
    keys = {(str(item.get("security_id") or ""), str(item.get("legal_share_class_id") or "")) for item in result}
    if len(keys) != len(result) or any(not all(key) for key in keys):
        raise ShareCapitalReconciliationError(f"invalid material-class registry: {company_id}")
    if any(item.get("material") is not True for item in result):
        raise ShareCapitalReconciliationError(f"non-material class in material registry: {company_id}")
    return result


def build_reconciliation(
    config: Mapping[str, Any],
    candidates: Mapping[str, Mapping[str, Any]],
    candidate_report: Mapping[str, Any],
    annual_root: Path,
    cancellation_context: Mapping[tuple[str, int], Mapping[str, Any]],
    manual_facts: Mapping[tuple[str, str], Mapping[str, Any]],
    review_cases: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    policy = config.get("policy")
    if not isinstance(policy, Mapping) or policy.get("production_write") is not False:
        raise ShareCapitalReconciliationError("review config is not read-only")
    expected_companies = int(policy.get("expected_company_count") or 0)
    if len(candidates) != expected_companies:
        raise ShareCapitalReconciliationError(
            f"expected {expected_companies} companies, got {len(candidates)}"
        )
    direct_ids = {str(item) for item in config.get("direct_exact_current_company_ids", [])}
    if len(direct_ids) != len(config.get("direct_exact_current_company_ids", [])):
        raise ShareCapitalReconciliationError("duplicate direct current company id")
    overrides = config.get("material_class_overrides")
    if not isinstance(overrides, Mapping):
        raise ShareCapitalReconciliationError("material_class_overrides must be an object")
    hash_cache: dict[Path, str] = {}
    review_by_company = {
        str(item.get("company_id") or ""): dict(item) for item in review_cases
    }
    company_results: list[dict[str, Any]] = []
    history_status_counts: Counter[str] = Counter()
    exact_history_candidate_count = 0
    accepted_history_count = 0
    latest_five_count = 0

    for company_id, payload in sorted(candidates.items()):
        reports = sorted(payload["reports"], key=lambda item: int(item["fiscal_year"]))
        latest = reports[-1]
        latest_five = reports[-5:]
        latest_five_count += len(latest_five)
        classes = _material_classes(payload, overrides)
        is_multi_class = len(classes) > 1
        history: list[dict[str, Any]] = []
        for row in latest_five:
            source = verify_candidate_source(row, annual_root, hash_cache=hash_cache)
            exact = exact_issued_candidate(row)
            if exact:
                exact_history_candidate_count += 1
            accepted_reported = exact and not is_multi_class
            if accepted_reported:
                accepted_history_count += 1
            status = str(row.get("status") or "REVIEW")
            history_status_counts[status] += 1
            history.append(
                {
                    "fiscal_year": int(row["fiscal_year"]),
                    "candidate_status": status,
                    "reported_issued_shares_candidate": row.get("closing_issued_shares"),
                    "reported_unit_multiplier": str(
                        row.get("reported_unit_multiplier")
                        or (
                            row.get("closing_evidence", {}).get("reported_unit_multiplier")
                            if isinstance(row.get("closing_evidence"), Mapping)
                            else "1"
                        )
                        or "1"
                    ),
                    "exact_unit_candidate": exact,
                    "reported_issued_shares_decision": (
                        "ACCEPT" if accepted_reported else "REVIEW"
                    ),
                    "reported_issued_shares": (
                        str(row["closing_issued_shares"]) if accepted_reported else None
                    ),
                    "diluted_total_shares": None,
                    "diluted_total_shares_status": "INSUFFICIENT_DATA",
                    "diluted_net_share_reduction": None,
                    "diluted_net_share_reduction_status": "INSUFFICIENT_DATA",
                    "action_bridge": _action_bridge(
                        company_id, int(row["fiscal_year"]), cancellation_context
                    ),
                    "source": source,
                }
            )

        class_facts: list[dict[str, Any]] = []
        for class_row in classes:
            security_id = str(class_row["security_id"])
            manual = manual_facts.get((company_id, security_id))
            if manual is not None:
                fact = {
                    **class_row,
                    "fiscal_year": int(manual["fiscal_year"]),
                    "decision": "ACCEPT",
                    "issued_shares": str(manual["issued_shares"]),
                    "reported_issued_shares_candidate": str(manual["issued_shares"]),
                    "origin": "MANUAL_OFFICIAL_RECONCILIATION",
                    "derivation": str(manual["derivation"]),
                    "rights_verified": manual.get("rights_verified") is True,
                    "economic_rights_factor": manual.get("economic_rights_factor"),
                    "source": dict(manual["source"]),
                    "verified_evidence": list(manual["verified_evidence"]),
                }
            elif company_id in direct_ids:
                if is_multi_class or not exact_issued_candidate(latest):
                    raise ShareCapitalReconciliationError(
                        f"direct current fact is not exact single-class: {company_id}"
                    )
                if security_id != str(latest.get("security_id") or ""):
                    raise ShareCapitalReconciliationError(
                        f"direct current security id mismatch: {company_id}"
                    )
                source = history[-1]["source"]
                fact = {
                    **class_row,
                    "fiscal_year": int(latest["fiscal_year"]),
                    "decision": "ACCEPT",
                    "issued_shares": str(latest["closing_issued_shares"]),
                    "reported_issued_shares_candidate": str(latest["closing_issued_shares"]),
                    "origin": "PINNED_EXACT_CANDIDATE",
                    "derivation": "DIRECT_OFFICIAL_EXACT_SHARE_ROW",
                    "rights_verified": True,
                    "economic_rights_factor": "1",
                    "source": dict(source),
                    "verified_evidence": [
                        {
                            "page": int(latest.get("page") or 0),
                            "line_excerpt": str(latest.get("line_excerpt") or ""),
                            "status": "VALID",
                        }
                    ],
                }
            else:
                review_case = review_by_company.get(company_id)
                candidate_value = latest.get("closing_issued_shares")
                if review_case is not None:
                    matching = [
                        item
                        for item in review_case.get("reported_table_class_candidates", [])
                        if isinstance(item, Mapping)
                        and str(item.get("security_id") or "") == security_id
                    ]
                    if len(matching) == 1:
                        candidate_value = matching[0].get("issued_shares_candidate")
                fact = {
                    **class_row,
                    "fiscal_year": int(latest["fiscal_year"]),
                    "decision": "REVIEW",
                    "issued_shares": None,
                    "reported_issued_shares_candidate": candidate_value,
                    "origin": "UNRESOLVED_OFFICIAL_CANDIDATE",
                    "derivation": None,
                    "rights_verified": False,
                    "economic_rights_factor": None,
                    "source": (
                        dict(review_case["source"])
                        if review_case is not None
                        else dict(history[-1]["source"])
                    ),
                    "verified_evidence": (
                        list(review_case["verified_evidence"])
                        if review_case is not None
                        else []
                    ),
                    "reason": (
                        str(review_case.get("reason") or "")
                        if review_case is not None
                        else str(latest.get("reason") or "Exact class count not verified.")
                    ),
                }
            class_facts.append(fact)

        accepted = [item for item in class_facts if item["decision"] == "ACCEPT"]
        all_counts = len(accepted) == len(class_facts)
        total = sum(int(item["issued_shares"]) for item in accepted) if all_counts else None
        expected_totals = config.get("accepted_multi_class_total_expectations", {})
        expected_total = expected_totals.get(company_id) if isinstance(expected_totals, Mapping) else None
        if expected_total is not None and total != positive_integer(
            expected_total, "accepted multi-class total"
        ):
            raise ShareCapitalReconciliationError(
                f"accepted class facts do not reconcile to company total: {company_id}"
            )
        company_results.append(
            {
                "schema_version": SCHEMA_VERSION,
                "company_id": company_id,
                "company_name": str(payload.get("company_name") or ""),
                "latest_fiscal_year": int(latest["fiscal_year"]),
                "material_share_class_count": len(class_facts),
                "material_share_classes": class_facts,
                "all_material_class_counts_status": "ACCEPT" if all_counts else "REVIEW",
                "company_total_issued_shares": str(total) if total is not None else None,
                "company_total_issued_shares_status": "ACCEPT" if all_counts else "REVIEW",
                "all_material_class_rights_status": (
                    "ACCEPT"
                    if all_counts and all(item["rights_verified"] for item in class_facts)
                    else "REVIEW"
                ),
                "company_market_value_denominator_authorized": False,
                "company_market_value_denominator_blockers": [
                    "This bundle does not validate current prices, price timestamps or FX conversion.",
                    *(
                        []
                        if all_counts
                        else ["At least one material legal share-class count is unresolved."]
                    ),
                    *(
                        []
                        if all(item["rights_verified"] for item in class_facts)
                        else ["At least one material class economic-rights factor is unresolved."]
                    ),
                ],
                "latest_five_fiscal_years": history,
                "diluted_endpoint_status": "INSUFFICIENT_DATA",
                "writes_production": False,
            }
        )

    if direct_ids - set(candidates):
        raise ShareCapitalReconciliationError("direct fact config includes an unknown company")
    accepted_facts = [
        fact
        for company in company_results
        for fact in company["material_share_classes"]
        if fact["decision"] == "ACCEPT"
    ]
    material_count = sum(item["material_share_class_count"] for item in company_results)
    rights_count = sum(item["rights_verified"] is True for item in accepted_facts)
    expected_latest_five = int(policy.get("expected_latest_five_company_years") or 0)
    expected_material = int(policy.get("expected_material_class_count") or 0)
    expected_accepted = int(policy.get("expected_accepted_current_class_facts") or 0)
    expected_rights = int(policy.get("expected_rights_verified_class_facts") or 0)
    observed = (latest_five_count, material_count, len(accepted_facts), rights_count)
    expected = (expected_latest_five, expected_material, expected_accepted, expected_rights)
    if observed != expected:
        raise ShareCapitalReconciliationError(
            f"reconciliation count mismatch: observed={observed}, expected={expected}"
        )
    report = {
        "schema_version": SCHEMA_VERSION,
        "candidate_schema_version": str(candidate_report.get("schema_version") or ""),
        "company_count": len(company_results),
        "latest_five_company_year_count": latest_five_count,
        "latest_five_candidate_status_counts": dict(sorted(history_status_counts.items())),
        "latest_five_exact_issued_candidate_count": exact_history_candidate_count,
        "latest_five_accepted_reported_issued_count": accepted_history_count,
        "material_share_class_count": material_count,
        "accepted_current_class_fact_count": len(accepted_facts),
        "accepted_current_company_count": sum(
            item["all_material_class_counts_status"] == "ACCEPT" for item in company_results
        ),
        "rights_verified_class_fact_count": rights_count,
        "rights_unverified_accepted_class_fact_count": len(accepted_facts) - rights_count,
        "company_denominator_authorized_count": 0,
        "diluted_total_shares_non_null_count": 0,
        "diluted_net_share_reduction_non_null_count": 0,
        "review_only_case_count": len(review_cases),
        "writes_production": False,
        "summary": [
            {
                "company_id": item["company_id"],
                "company_name": item["company_name"],
                "material_share_class_count": item["material_share_class_count"],
                "accepted_current_class_fact_count": sum(
                    fact["decision"] == "ACCEPT" for fact in item["material_share_classes"]
                ),
                "all_material_class_counts_status": item["all_material_class_counts_status"],
                "all_material_class_rights_status": item["all_material_class_rights_status"],
                "company_total_issued_shares": item["company_total_issued_shares"],
                "company_market_value_denominator_authorized": False,
                "diluted_endpoint_status": "INSUFFICIENT_DATA",
            }
            for item in company_results
        ],
        "blocker_counts": {
            "NO_DILUTED_ENDPOINT_BRIDGE": len(company_results),
            "CURRENT_CLASS_COUNT_UNRESOLVED": sum(
                item["all_material_class_counts_status"] != "ACCEPT"
                for item in company_results
            ),
            "ACCEPTED_CLASS_RIGHTS_UNRESOLVED": len(accepted_facts) - rights_count,
            "PRICE_FX_NOT_IN_SCOPE": len(company_results),
        },
        "safety": [
            "No missing share count is converted to zero.",
            "No company-level denominator is authorized by this bundle.",
            "A/H and other multi-class issuers use explicit legal-class rows.",
            "Issued shares are never promoted to diluted total shares.",
            "No buyback cash or eligible-buyback value is derived.",
        ],
    }
    return company_results, report
