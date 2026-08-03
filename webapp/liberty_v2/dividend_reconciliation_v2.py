from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .dividend_reconciliation import DividendReconciliationError


SCHEMA_VERSION = "dividend-reconciliation-v2.0"
CONFIG_SCHEMA_VERSION = "dividend-reconciliation-review-v2.0"
TARGET_FISCAL_YEAR_COUNT = 5
AMOUNT_METHODS = {"OFFICIAL_TOTAL", "PER_SHARE_TIMES_ENTITLED_SHARES"}
CALCULATION_METHODS = {
    "DIRECT_OFFICIAL_IMPLEMENTED_TOTAL",
    "SUM_OF_VERIFIED_ORDINARY_COMPONENTS",
}
BLOCKER_MESSAGES_ZH = {
    "HK_TOTAL_CURRENCY_OR_LIFECYCLE_NOT_RECONCILED": (
        "年报中尚未同时核清全年普通股息、币种及实际支付状态，暂不填金额。"
    ),
    "NO_OFFICIAL_COMPLETE_TOTAL_LOCATED": (
        "现有年报候选中没有找到可证明为该财年全年普通股息的完整已实施总额。"
    ),
    "PROPOSAL_WITHOUT_VERIFIED_IMPLEMENTED_TOTAL": (
        "目前只定位到拟议分红，尚未由后续年报确认最终实际派发总额。"
    ),
    "ORDINARY_AND_SPECIAL_NOT_SEPARATED": (
        "相关披露同时出现普通股息和特别股息，但现有证据尚不能安全拆开全年普通股息。"
    ),
    "COMPLETE_COMPONENT_SET_NOT_RECONCILED": (
        "年报中存在相关分红数字，但尚未证明所有中期、末期等组成项已经找全且没有重复。"
    ),
}


@dataclass(frozen=True)
class CompanyFiscalYearTargets:
    company_id: str
    company_name: str
    security_id: str
    market: str
    fiscal_years: tuple[int, ...]


def _load_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DividendReconciliationError(f"cannot load JSON {path}: {error}") from error
    if not isinstance(payload, dict):
        raise DividendReconciliationError(f"JSON root must be an object: {path}")
    return payload


def decimal_value(value: Any, *, field: str, positive: bool = True) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise DividendReconciliationError(f"{field} must be a Decimal string") from error
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        requirement = "positive" if positive else "non-negative"
        raise DividendReconciliationError(f"{field} must be finite and {requirement}")
    return result


def _required_text(value: Any, *, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise DividendReconciliationError(f"{field} is required")
    return result


def component_amount(component: Mapping[str, Any], *, field: str) -> Decimal:
    """Calculate one component with Decimal and require auditable inputs.

    No current accepted row needs a per-share derivation.  The derivation path is
    nevertheless implemented here so a later reviewer cannot multiply a bare
    per-share value by an undocumented share count.
    """

    method = str(component.get("amount_method") or "")
    if method not in AMOUNT_METHODS:
        raise DividendReconciliationError(f"{field}.amount_method is invalid")
    expected = decimal_value(component.get("value"), field=f"{field}.value")
    if method == "OFFICIAL_TOTAL":
        return expected

    per_share = decimal_value(
        component.get("per_share_value"), field=f"{field}.per_share_value"
    )
    share_basis = decimal_value(
        component.get("share_basis"), field=f"{field}.share_basis"
    )
    entitled_shares = decimal_value(
        component.get("entitled_shares"), field=f"{field}.entitled_shares"
    )
    source_ids = [str(item) for item in component.get("derivation_source_ids") or []]
    if len(source_ids) < 2 or len(source_ids) != len(set(source_ids)):
        raise DividendReconciliationError(
            f"{field} per-share derivation requires distinct per-share and entitled-share sources"
        )
    calculated = per_share * entitled_shares / share_basis
    if calculated != expected:
        raise DividendReconciliationError(
            f"{field} Decimal derivation mismatch: {calculated} != {expected}"
        )
    return calculated


def distribution_total(distribution: Mapping[str, Any]) -> Decimal:
    components = distribution.get("ordinary_components")
    if not isinstance(components, list) or not components:
        raise DividendReconciliationError("ordinary_components must be a non-empty array")
    calculation_method = str(distribution.get("calculation_method") or "")
    if calculation_method not in CALCULATION_METHODS:
        raise DividendReconciliationError("calculation_method is invalid")
    if calculation_method == "DIRECT_OFFICIAL_IMPLEMENTED_TOTAL" and len(components) != 1:
        raise DividendReconciliationError(
            "direct official total must contain exactly one ordinary component"
        )
    if calculation_method == "SUM_OF_VERIFIED_ORDINARY_COMPONENTS" and len(components) < 2:
        raise DividendReconciliationError(
            "component-sum calculation requires at least two ordinary components"
        )
    currencies = {_required_text(item.get("currency"), field="component.currency") for item in components}
    if len(currencies) != 1:
        raise DividendReconciliationError("ordinary components must use one currency")
    calculated = sum(
        (component_amount(item, field=f"ordinary_components[{index}]") for index, item in enumerate(components)),
        Decimal("0"),
    )
    total = distribution.get("ordinary_cash_dividend_total")
    if not isinstance(total, Mapping):
        raise DividendReconciliationError("ordinary_cash_dividend_total must be an object")
    expected = decimal_value(total.get("value"), field="ordinary_cash_dividend_total.value")
    if str(total.get("unit") or "") != "currency":
        raise DividendReconciliationError("ordinary_cash_dividend_total.unit must be currency")
    if str(total.get("currency") or "") not in currencies:
        raise DividendReconciliationError("total currency differs from component currency")
    if calculated != expected:
        raise DividendReconciliationError(
            f"ordinary component sum does not equal total: {calculated} != {expected}"
        )
    return calculated


def load_recent_fiscal_year_targets(
    annual_root: Path,
    company_ids: Iterable[str],
    *,
    maximum_years: int = TARGET_FISCAL_YEAR_COUNT,
) -> dict[str, CompanyFiscalYearTargets]:
    if maximum_years <= 0:
        raise DividendReconciliationError("maximum_years must be positive")
    requested = {str(company_id) for company_id in company_ids}
    manifests: dict[str, dict[str, Any]] = {}
    for path in sorted(annual_root.glob("companies/*/manifest.json")):
        manifest = _load_object(path)
        company_id = str(manifest.get("company_id") or "")
        if company_id in requested:
            if company_id in manifests:
                raise DividendReconciliationError(f"duplicate company manifest: {company_id}")
            manifests[company_id] = manifest
    missing = sorted(requested - set(manifests))
    if missing:
        raise DividendReconciliationError(f"annual-report manifests are missing: {missing}")

    result: dict[str, CompanyFiscalYearTargets] = {}
    for company_id in sorted(requested):
        manifest = manifests[company_id]
        documents = [
            item
            for item in manifest.get("documents") or []
            if isinstance(item, Mapping)
            and item.get("selection_status") == "SELECTED_CURRENT"
            and item.get("data_status") == "VERIFIED"
        ]
        years = sorted({int(item.get("fiscal_year") or 0) for item in documents}, reverse=True)
        years = [year for year in years if year >= 1900][:maximum_years]
        if not years:
            raise DividendReconciliationError(f"no verified annual report for {company_id}")
        result[company_id] = CompanyFiscalYearTargets(
            company_id=company_id,
            company_name=_required_text(manifest.get("company_name"), field="company_name"),
            security_id=_required_text(manifest.get("security_id"), field="security_id"),
            market=_required_text(documents[0].get("market"), field="market"),
            fiscal_years=tuple(years),
        )
    return result


def validate_v2_review_config(
    config: Mapping[str, Any],
    inventory: Mapping[str, Mapping[str, Any]],
    targets: Mapping[str, CompanyFiscalYearTargets],
) -> None:
    if config.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise DividendReconciliationError("unexpected v2 review config schema")
    if int(config.get("expected_company_count") or -1) != len(targets):
        raise DividendReconciliationError("configured company count does not match target matrix")
    slot_count = sum(len(item.fiscal_years) for item in targets.values())
    if int(config.get("expected_target_slot_count") or -1) != slot_count:
        raise DividendReconciliationError("configured target slot count does not match target matrix")

    expected_security_ids = dict(config.get("expected_security_ids") or {})
    if set(expected_security_ids) != set(targets):
        raise DividendReconciliationError("expected_security_ids must cover every target company")
    for company_id, target in targets.items():
        if str(expected_security_ids[company_id]) != target.security_id:
            raise DividendReconciliationError(f"security mapping mismatch: {company_id}")

    carry_ids = [str(item) for item in config.get("carry_forward_distribution_ids") or []]
    if len(carry_ids) != len(set(carry_ids)):
        raise DividendReconciliationError("carry-forward distribution ids must be unique")
    distributions = config.get("new_distributions")
    if not isinstance(distributions, list):
        raise DividendReconciliationError("new_distributions must be an array")
    distribution_ids: set[str] = set(carry_ids)
    company_years: set[tuple[str, int]] = set()
    used_candidate_ids: set[str] = set()
    for index, distribution in enumerate(distributions):
        if not isinstance(distribution, Mapping):
            raise DividendReconciliationError("new_distributions entries must be objects")
        distribution_id = _required_text(
            distribution.get("distribution_id"), field=f"new_distributions[{index}].distribution_id"
        )
        if distribution_id in distribution_ids:
            raise DividendReconciliationError(f"duplicate distribution id: {distribution_id}")
        distribution_ids.add(distribution_id)
        company_id = _required_text(distribution.get("company_id"), field="company_id")
        fiscal_year = int(distribution.get("fiscal_year") or 0)
        if company_id not in targets or fiscal_year not in targets[company_id].fiscal_years:
            raise DividendReconciliationError(
                f"distribution is outside recent complete fiscal-year targets: {company_id} FY{fiscal_year}"
            )
        key = (company_id, fiscal_year)
        if key in company_years:
            raise DividendReconciliationError(f"duplicate company/fiscal-year: {key}")
        company_years.add(key)
        if distribution.get("dividend_kind") != "ORDINARY":
            raise DividendReconciliationError(f"only ordinary dividends are accepted: {distribution_id}")
        if distribution.get("lifecycle_status") != "PAID":
            raise DividendReconciliationError(f"only paid dividends are accepted: {distribution_id}")
        if distribution.get("import_scope") != "FISCAL_YEAR_TOTAL":
            raise DividendReconciliationError(f"complete fiscal-year scope is required: {distribution_id}")
        if distribution.get("ready_for_controlled_ledger_import") is not True:
            raise DividendReconciliationError(f"new distribution must be import-ready: {distribution_id}")
        distribution_total(distribution)
        for component_index, component in enumerate(distribution.get("ordinary_components") or []):
            if not isinstance(component, Mapping):
                raise DividendReconciliationError("ordinary component must be an object")
            candidate_id = _required_text(
                component.get("source_candidate_id"),
                field=f"{distribution_id}.ordinary_components[{component_index}].source_candidate_id",
            )
            if candidate_id in used_candidate_ids:
                raise DividendReconciliationError(
                    f"candidate assigned to more than one component: {candidate_id}"
                )
            used_candidate_ids.add(candidate_id)
            candidate = inventory.get(candidate_id)
            if candidate is None:
                raise DividendReconciliationError(f"candidate is missing: {candidate_id}")
            if str(candidate.get("company_id") or "") != company_id:
                raise DividendReconciliationError(f"candidate company mismatch: {candidate_id}")
            if str(candidate.get("amount_kind") or "") != "TOTAL":
                raise DividendReconciliationError(f"candidate is not a total amount: {candidate_id}")
            if str(candidate.get("dividend_kind") or "") != "ORDINARY":
                raise DividendReconciliationError(f"candidate is not ordinary: {candidate_id}")
            candidate_expected = decimal_value(
                component.get("candidate_original_value"),
                field=f"{candidate_id}.candidate_original_value",
            )
            candidate_actual = decimal_value(candidate.get("value"), field=f"{candidate_id}.value")
            if candidate_expected != candidate_actual:
                raise DividendReconciliationError(f"candidate value drifted: {candidate_id}")
            markers = component.get("official_page_markers")
            if not isinstance(markers, list) or len(markers) < 2 or any(
                not str(marker).strip() for marker in markers
            ):
                raise DividendReconciliationError(
                    f"at least two official page markers are required: {candidate_id}"
                )
            _required_text(component.get("candidate_disposition"), field="candidate_disposition")
            event = component.get("futu_event")
            if not isinstance(event, Mapping):
                raise DividendReconciliationError(f"implementation event is required: {candidate_id}")
            _required_text(event.get("event_key"), field="futu_event.event_key")
            _required_text(event.get("payload_hash"), field="futu_event.payload_hash")

    expected_new = int(config.get("expected_new_ready_count") or -1)
    expected_total = int(config.get("expected_ready_count") or -1)
    if len(distributions) != expected_new:
        raise DividendReconciliationError("configured new ready count does not match distributions")
    if len(distribution_ids) != expected_total:
        raise DividendReconciliationError("configured total ready count does not include carry-forward rows")
    expected_blocked = int(config.get("expected_blocked_count") or -1)
    if slot_count - len(distribution_ids) != expected_blocked:
        raise DividendReconciliationError("configured blocked count does not balance target slots")


def blocker_for_candidates(
    company_id: str,
    fiscal_year: int,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a conservative, plain-language blocker without inferring a number."""

    related = [
        item
        for item in candidates
        if int(item.get("associated_fiscal_year") or 0) == fiscal_year
        or int(item.get("report_fiscal_year") or 0) in {fiscal_year, fiscal_year + 1}
    ]
    totals = [item for item in related if item.get("amount_kind") == "TOTAL"]
    ordinary = [item for item in totals if item.get("dividend_kind") == "ORDINARY"]
    special = [item for item in totals if item.get("dividend_kind") == "SPECIAL"]
    if company_id.startswith("HK"):
        code = "HK_TOTAL_CURRENCY_OR_LIFECYCLE_NOT_RECONCILED"
    elif not ordinary:
        code = "NO_OFFICIAL_COMPLETE_TOTAL_LOCATED"
    elif special:
        code = "ORDINARY_AND_SPECIAL_NOT_SEPARATED"
    elif all(item.get("lifecycle_status") == "PROPOSED" for item in ordinary):
        code = "PROPOSAL_WITHOUT_VERIFIED_IMPLEMENTED_TOTAL"
    else:
        code = "COMPLETE_COMPONENT_SET_NOT_RECONCILED"
    return {
        "company_id": company_id,
        "fiscal_year": fiscal_year,
        "status": "BLOCKED",
        "ordinary_cash_dividend_total": None,
        "reason_code": code,
        "reason_zh": BLOCKER_MESSAGES_ZH[code],
        "candidate_diagnostics": {
            "related_candidate_count": len(related),
            "ordinary_total_candidate_count": len(ordinary),
            "special_total_candidate_count": len(special),
            "proposal_candidate_count": sum(
                item.get("lifecycle_status") == "PROPOSED" for item in ordinary
            ),
            "diagnostic_only_not_a_ledger_value": True,
        },
        "unknown_is_not_zero": True,
        "writes_production": False,
    }
