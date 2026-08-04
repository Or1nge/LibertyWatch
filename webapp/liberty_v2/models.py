from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class DataStatus(str, Enum):
    VALID = "VALID"
    KNOWN_ZERO = "KNOWN_ZERO"
    MISSING = "MISSING"
    NOT_DISCLOSED = "NOT_DISCLOSED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    CONFLICT = "CONFLICT"
    STALE = "STALE"
    CALCULATION_FAILED = "CALCULATION_FAILED"


class PublicationStatus(str, Enum):
    VALID = "VALID"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"
    STALE = "STALE"


class ReleaseValidity(str, Enum):
    VALID_RELEASE = "VALID_RELEASE"
    REJECTED_RELEASE = "REJECTED_RELEASE"


class CompanyDataTier(str, Enum):
    BLOCKED = "BLOCKED"
    ESTIMATED = "ESTIMATED"
    CALCULABLE = "CALCULABLE"
    VERIFIED = "VERIFIED"


class MetricBasis(str, Enum):
    DIRECT = "DIRECT"
    DERIVED = "DERIVED"
    VENDOR_AUTHORIZED = "VENDOR_AUTHORIZED"
    PROXY = "PROXY"
    CONSERVATIVE_DEFAULT = "CONSERVATIVE_DEFAULT"
    UNAVAILABLE = "UNAVAILABLE"


class Freshness(str, Enum):
    CURRENT = "CURRENT"
    MARKET_CLOSED_CURRENT = "MARKET_CLOSED_CURRENT"
    STALE_LAST_GOOD = "STALE_LAST_GOOD"


class CoverageStatus(str, Enum):
    VALID = "VALID"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class IndustryKind(str, Enum):
    NON_FINANCIAL = "NON_FINANCIAL"
    BANK = "BANK"
    INSURANCE = "INSURANCE"
    SECURITIES = "SECURITIES"
    OTHER_FINANCIAL = "OTHER_FINANCIAL"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class RawDataPoint:
    company_id: str
    field_id: str
    security_id: str | None
    share_class: str | None
    source_name: str
    source_document: str
    source_url_or_local_path: str
    source_publish_date: date
    source_fetch_time: datetime
    fiscal_period: str
    currency: str | None
    unit: str
    value: Decimal | None
    data_status: DataStatus
    restatement_status: str

    def __post_init__(self) -> None:
        required_text = (
            self.company_id,
            self.field_id,
            self.source_name,
            self.source_document,
            self.source_url_or_local_path,
            self.fiscal_period,
            self.unit,
            self.restatement_status,
        )
        if any(not value.strip() for value in required_text):
            raise ValueError("raw data provenance fields must not be blank")
        if self.value is None and self.data_status in {
            DataStatus.VALID,
            DataStatus.KNOWN_ZERO,
        }:
            raise ValueError("valid raw values must carry a Decimal value")
        if self.value is not None and self.data_status not in {
            DataStatus.VALID,
            DataStatus.KNOWN_ZERO,
        }:
            raise ValueError("unavailable raw values must not carry a number")
        if self.value == 0 and self.data_status is not DataStatus.KNOWN_ZERO:
            raise ValueError("a real zero must be explicitly KNOWN_ZERO")
        if self.value is not None and not self.currency and self.unit in {
            "currency",
            "currency_per_share",
            "yuan",
            "hundred_million_yuan",
        }:
            raise ValueError("amount data must include currency")


@dataclass(frozen=True)
class AnnualDistribution:
    fiscal_year: int
    ordinary_dividend: Decimal | None
    special_dividend: Decimal | None
    gross_cancelled_buyback: Decimal | None
    cancelled_shares: Decimal | None
    diluted_net_share_reduction: Decimal | None


@dataclass(frozen=True)
class FCFYear:
    fiscal_year: int
    operating_cash_flow: Decimal | None
    capital_expenditure: Decimal | None
    lease_principal_repayment: Decimal | None


@dataclass(frozen=True)
class SecurityClassInput:
    security_id: str
    share_class: str
    price: Decimal | None
    issued_shares: Decimal | None
    currency: str
    fx_to_base: Decimal | None
    price_timestamp: datetime | None
    quote_status: DataStatus
    rights_verified: bool = False
    economic_rights_factor: Decimal | None = None
    material: bool = True


@dataclass(frozen=True)
class CoverageResult:
    status: CoverageStatus
    adapter: str
    sustainable_distribution: Decimal | None
    coverage_ratio: Decimal | None
    capacity: Decimal | None
    caveats: tuple[str, ...] = ()
    required_missing_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class StructuredConfigScore:
    value: Decimal | None
    source: str
    as_of_date: date | None
    expires_at: date | None
    reason: str

    def is_current(self, on_date: date) -> bool:
        return bool(
            self.value is not None
            and self.source.strip()
            and self.reason.strip()
            and self.as_of_date is not None
            and self.expires_at is not None
            and self.as_of_date <= on_date <= self.expires_at
        )


@dataclass(frozen=True)
class VetoFlag:
    code: str
    severity: str
    triggered: bool
    evidence_fields: tuple[str, ...]
    message_zh: str
    source: str | None = None
    as_of_date: date | None = None


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    severity: str
    field: str
    message_zh: str


@dataclass(frozen=True)
class ValidationResult:
    status: PublicationStatus
    issues: tuple[ValidationIssue, ...] = ()


@dataclass(frozen=True)
class MetricRecord:
    value: Decimal | None
    status: str
    display: str
    reason: str | None = None
    unit: str | None = None
    basis: MetricBasis | str | None = None
    warning: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "value": format(self.value, "f") if self.value is not None else None,
            "status": self.status,
            "display": self.display,
            "reason": self.reason,
            "unit": self.unit,
            "basis": self.basis.value if isinstance(self.basis, MetricBasis) else self.basis,
            "warning": self.warning,
        }


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [jsonable(item) for item in value]
    return value
