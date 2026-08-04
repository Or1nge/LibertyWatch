from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .capital_structure import CapitalStructureAuthorization
from .market_observation import MarketObservation
from .models import Freshness, MetricBasis


@dataclass(frozen=True)
class MarketValueResolution:
    value: Decimal | None
    basis: MetricBasis
    status: str
    selected_security_id: str
    implied_equivalent_shares: Decimal | None
    official_equivalent_shares: Decimal | None
    relative_difference: Decimal | None
    source_field_ids: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.value is not None and not self.blockers

    @property
    def estimated(self) -> bool:
        return "VENDOR_EQUIVALENT_SHARES_DEVIATION_2_TO_5PCT" in self.warnings

    def public_dict(self) -> dict[str, Any]:
        return {
            "value": format(self.value, "f") if self.value is not None else None,
            "basis": self.basis.value,
            "status": self.status,
            "selected_security_id": self.selected_security_id,
            "implied_equivalent_shares": (
                format(self.implied_equivalent_shares, "f")
                if self.implied_equivalent_shares is not None
                else None
            ),
            "official_equivalent_shares": (
                format(self.official_equivalent_shares, "f")
                if self.official_equivalent_shares is not None
                else None
            ),
            "relative_difference": (
                format(self.relative_difference, "f")
                if self.relative_difference is not None
                else None
            ),
            "source_field_ids": list(self.source_field_ids),
            "warnings": list(self.warnings),
            "blockers": list(self.blockers),
        }


def _unavailable(
    authorization: CapitalStructureAuthorization,
    blocker: str,
    *,
    warnings: tuple[str, ...] = (),
) -> MarketValueResolution:
    return MarketValueResolution(
        value=None,
        basis=MetricBasis.UNAVAILABLE,
        status="UNAVAILABLE",
        selected_security_id=authorization.selected_security_id,
        implied_equivalent_shares=None,
        official_equivalent_shares=authorization.official_equivalent_shares,
        relative_difference=None,
        source_field_ids=authorization.authorization_source_ids,
        warnings=warnings,
        blockers=(blocker,),
    )


def resolve_selected_security_equivalent_value(
    authorization: CapitalStructureAuthorization,
    observation: MarketObservation | None,
) -> MarketValueResolution:
    if observation is None:
        return _unavailable(authorization, "MARKET_OBSERVATION_MISSING")
    if observation.security_id != authorization.selected_security_id:
        return _unavailable(authorization, "SELECTED_SECURITY_IDENTITY_CONFLICT")
    if observation.price is None or observation.fx_to_base is None:
        return _unavailable(authorization, "PRICE_OR_FX_MISSING")
    if observation.price <= 0 or observation.fx_to_base <= 0:
        return _unavailable(authorization, "PRICE_OR_FX_INVALID")
    warnings: list[str] = []
    if observation.freshness is Freshness.STALE_LAST_GOOD:
        warnings.append("STALE_LAST_GOOD")
    official = authorization.official_equivalent_shares
    if authorization.vendor_value_authorized and observation.total_market_value is not None:
        implied = observation.total_market_value / observation.price
        if official is None or official <= 0:
            return _unavailable(authorization, "OFFICIAL_EQUIVALENT_SHARES_MISSING")
        relative = abs(implied - official) / official
        if relative > Decimal("0.05"):
            return MarketValueResolution(
                value=None,
                basis=MetricBasis.UNAVAILABLE,
                status="UNAVAILABLE",
                selected_security_id=authorization.selected_security_id,
                implied_equivalent_shares=implied,
                official_equivalent_shares=official,
                relative_difference=relative,
                source_field_ids=(
                    *authorization.authorization_source_ids,
                    f"MARKET.{observation.security_id}.total_market_value",
                    f"MARKET.{observation.security_id}.price",
                    f"MARKET.{observation.security_id}.fx_to_base",
                ),
                warnings=tuple(warnings),
                blockers=("VENDOR_EQUIVALENT_SHARES_DEVIATION_GT_5PCT",),
            )
        if relative > Decimal("0.02"):
            warnings.append("VENDOR_EQUIVALENT_SHARES_DEVIATION_2_TO_5PCT")
        return MarketValueResolution(
            value=observation.total_market_value * observation.fx_to_base,
            basis=MetricBasis.VENDOR_AUTHORIZED,
            status="AVAILABLE",
            selected_security_id=authorization.selected_security_id,
            implied_equivalent_shares=implied,
            official_equivalent_shares=official,
            relative_difference=relative,
            source_field_ids=(
                *authorization.authorization_source_ids,
                f"MARKET.{observation.security_id}.total_market_value",
                f"MARKET.{observation.security_id}.price",
                f"MARKET.{observation.security_id}.fx_to_base",
            ),
            warnings=tuple(warnings),
        )
    if authorization.direct_equivalent_shares_authorized and official is not None and official > 0:
        return MarketValueResolution(
            value=observation.price * official * observation.fx_to_base,
            basis=MetricBasis.DERIVED,
            status="AVAILABLE",
            selected_security_id=authorization.selected_security_id,
            implied_equivalent_shares=None,
            official_equivalent_shares=official,
            relative_difference=None,
            source_field_ids=(
                *authorization.authorization_source_ids,
                f"MARKET.{observation.security_id}.price",
                f"MARKET.{observation.security_id}.fx_to_base",
            ),
            warnings=tuple(warnings),
        )
    return _unavailable(authorization, "SEEV_NOT_AUTHORIZED", warnings=tuple(warnings))
