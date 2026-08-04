from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping


class CapitalStructureError(ValueError):
    pass


class StructureKind(str, Enum):
    SINGLE_CLASS = "SINGLE_CLASS"
    A_H = "A_H"
    ADS = "ADS"
    DUAL_COUNTER = "DUAL_COUNTER"
    MULTI_CLASS = "MULTI_CLASS"


VENDOR_SEMANTICS = {
    "VENDOR_COMPANY_MARKET_VALUE",
    "VENDOR_SELECTED_SECURITY_EQUIVALENT_VALUE",
    "UNRESOLVED",
}


def _decimal(value: Any, *, field: str, required: bool = False) -> Decimal | None:
    if value is None or value == "":
        if required:
            raise CapitalStructureError(f"{field} is required")
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise CapitalStructureError(f"{field} must be a finite Decimal") from error
    if not parsed.is_finite():
        raise CapitalStructureError(f"{field} must be finite")
    return parsed


@dataclass(frozen=True)
class CapitalStructureAuthorization:
    company_id: str
    selected_security_id: str
    structure_kind: StructureKind
    material_share_classes: tuple[str, ...]
    distribution_rights_equal: bool | None
    selected_security_rights_factor: Decimal
    vendor_total_market_value_semantics: str
    vendor_value_authorized: bool
    direct_equivalent_shares_authorized: bool
    authorization_source_ids: tuple[str, ...]
    as_of_date: date | None
    official_equivalent_shares: Decimal | None = None
    authorization_quote_observed_at: str | None = None
    observed_implied_equivalent_shares: Decimal | None = None
    observed_relative_difference: Decimal | None = None
    ads_ordinary_shares_per_security: Decimal | None = None
    notes: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CapitalStructureAuthorization":
        company_id = str(raw.get("company_id") or "").strip()
        security_id = str(raw.get("selected_security_id") or "").strip()
        if not company_id or not security_id:
            raise CapitalStructureError("company_id and selected_security_id are required")
        try:
            structure = StructureKind(str(raw.get("structure_kind") or ""))
        except ValueError as error:
            raise CapitalStructureError(f"unsupported structure_kind for {company_id}") from error
        classes = tuple(str(item).strip() for item in raw.get("material_share_classes", []))
        if not classes or any(not item for item in classes) or len(classes) != len(set(classes)):
            raise CapitalStructureError(f"material_share_classes are invalid for {company_id}")
        rights_equal = raw.get("distribution_rights_equal")
        if rights_equal is not None and not isinstance(rights_equal, bool):
            raise CapitalStructureError(f"distribution_rights_equal must be boolean or null: {company_id}")
        rights_factor = _decimal(
            raw.get("selected_security_rights_factor"),
            field="selected_security_rights_factor",
            required=True,
        )
        assert rights_factor is not None
        if rights_factor <= 0:
            raise CapitalStructureError(f"selected security rights factor must be positive: {company_id}")
        semantics = str(raw.get("vendor_total_market_value_semantics") or "UNRESOLVED")
        if semantics not in VENDOR_SEMANTICS:
            raise CapitalStructureError(f"unsupported vendor market-value semantics: {company_id}")
        authorized = raw.get("vendor_value_authorized")
        if not isinstance(authorized, bool):
            raise CapitalStructureError(f"vendor_value_authorized must be boolean: {company_id}")
        direct_authorized = raw.get("direct_equivalent_shares_authorized", False)
        if not isinstance(direct_authorized, bool):
            raise CapitalStructureError(
                f"direct_equivalent_shares_authorized must be boolean: {company_id}"
            )
        source_ids = tuple(str(item).strip() for item in raw.get("authorization_source_ids", []))
        if any(not item for item in source_ids) or len(source_ids) != len(set(source_ids)):
            raise CapitalStructureError(f"authorization_source_ids are invalid: {company_id}")
        raw_date = raw.get("as_of_date")
        try:
            as_of = date.fromisoformat(str(raw_date)) if raw_date else None
        except ValueError as error:
            raise CapitalStructureError(f"invalid as_of_date: {company_id}") from error
        official = _decimal(raw.get("official_equivalent_shares"), field="official_equivalent_shares")
        implied = _decimal(
            raw.get("observed_implied_equivalent_shares"),
            field="observed_implied_equivalent_shares",
        )
        difference = _decimal(raw.get("observed_relative_difference"), field="observed_relative_difference")
        ads_ratio = _decimal(
            raw.get("ads_ordinary_shares_per_security"),
            field="ads_ordinary_shares_per_security",
        )
        if authorized:
            if semantics == "UNRESOLVED" or not source_ids or as_of is None:
                raise CapitalStructureError(f"authorized vendor value lacks evidence: {company_id}")
            if official is None or official <= 0:
                raise CapitalStructureError(f"authorized vendor value lacks official shares: {company_id}")
            if difference is None or not Decimal("0") <= difference <= Decimal("0.05"):
                raise CapitalStructureError(f"authorized vendor value failed the 5% check: {company_id}")
            if structure is StructureKind.A_H and rights_equal is not True:
                raise CapitalStructureError(f"A/H authorization requires equal distribution rights: {company_id}")
            if structure is StructureKind.ADS and (ads_ratio is None or ads_ratio <= 0):
                raise CapitalStructureError(f"ADS authorization requires a depositary ratio: {company_id}")
        if direct_authorized:
            if official is None or official <= 0 or not source_ids or as_of is None:
                raise CapitalStructureError(f"direct equivalent shares lack evidence: {company_id}")
            if structure is StructureKind.A_H and rights_equal is not True:
                raise CapitalStructureError(
                    f"direct A/H equivalent shares require equal distribution rights: {company_id}"
                )
            if structure is StructureKind.ADS and (ads_ratio is None or ads_ratio <= 0):
                raise CapitalStructureError(f"direct ADS shares require a depositary ratio: {company_id}")
        return cls(
            company_id=company_id,
            selected_security_id=security_id,
            structure_kind=structure,
            material_share_classes=classes,
            distribution_rights_equal=rights_equal,
            selected_security_rights_factor=rights_factor,
            vendor_total_market_value_semantics=semantics,
            vendor_value_authorized=authorized,
            direct_equivalent_shares_authorized=direct_authorized,
            authorization_source_ids=source_ids,
            as_of_date=as_of,
            official_equivalent_shares=official,
            authorization_quote_observed_at=(
                str(raw["authorization_quote_observed_at"])
                if raw.get("authorization_quote_observed_at")
                else None
            ),
            observed_implied_equivalent_shares=implied,
            observed_relative_difference=difference,
            ads_ordinary_shares_per_security=ads_ratio,
            notes=str(raw["notes"]) if raw.get("notes") else None,
        )


def load_capital_structure_registry(
    path: Path,
    *,
    expected_company_ids: Iterable[str] | None = None,
) -> dict[str, CapitalStructureAuthorization]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CapitalStructureError(f"cannot read capital-structure registry: {path}") from error
    if payload.get("schema_version") != "issuer-capital-structure-v1":
        raise CapitalStructureError("capital-structure schema_version mismatch")
    rows = payload.get("companies")
    if not isinstance(rows, list) or not rows:
        raise CapitalStructureError("capital-structure companies must be a non-empty array")
    registry: dict[str, CapitalStructureAuthorization] = {}
    securities: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise CapitalStructureError("capital-structure row must be an object")
        item = CapitalStructureAuthorization.from_mapping(raw)
        if item.company_id in registry:
            raise CapitalStructureError(f"duplicate company_id: {item.company_id}")
        if item.selected_security_id in securities:
            raise CapitalStructureError(f"duplicate selected_security_id: {item.selected_security_id}")
        registry[item.company_id] = item
        securities.add(item.selected_security_id)
    if expected_company_ids is not None:
        expected = set(expected_company_ids)
        actual = set(registry)
        if expected != actual:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise CapitalStructureError(f"capital-structure coverage mismatch: missing={missing}, extra={extra}")
    return registry
