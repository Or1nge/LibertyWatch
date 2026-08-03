from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from .calculations import (
    InsufficientDataError,
    decimal_median,
    sustainable_distribution_non_financial,
    to_decimal,
)
from .models import CoverageResult, CoverageStatus, FCFYear, IndustryKind
from .policy import decimal_value, integer_value


class DistributionCoverageAdapter(ABC):
    name: str

    @abstractmethod
    def calculate(self, historical_distribution: Decimal) -> CoverageResult:
        raise NotImplementedError


@dataclass(frozen=True)
class NonFinancialFCFAdapter(DistributionCoverageAdapter):
    years: Sequence[FCFYear]
    name: str = "NonFinancialFCFAdapter/v2"

    def calculate(self, historical_distribution: Decimal) -> CoverageResult:
        missing: list[str] = []
        values: list[Decimal] = []
        lease_incomplete = False
        for row in self.years[:5]:
            if row.operating_cash_flow is None:
                missing.append(f"{row.fiscal_year}.operating_cash_flow")
                continue
            if row.capital_expenditure is None:
                missing.append(f"{row.fiscal_year}.capital_expenditure")
                continue
            lease = row.lease_principal_repayment
            if lease is None:
                lease = Decimal("0")
                lease_incomplete = True
            values.append(
                to_decimal(row.operating_cash_flow)
                - to_decimal(row.capital_expenditure)
                - to_decimal(lease)
            )
        minimum_years = integer_value(
            "industry_adapters", "non_financial", "minimum_complete_fcf_years"
        )
        if missing or len(values) < minimum_years:
            return CoverageResult(
                status=CoverageStatus.INSUFFICIENT_DATA,
                adapter=self.name,
                sustainable_distribution=None,
                coverage_ratio=None,
                capacity=None,
                required_missing_fields=tuple(
                    missing or [f"at_least_{minimum_years}_complete_fcf_years"]
                ),
            )
        fcf5 = decimal_median(values)
        historical = to_decimal(historical_distribution)
        sustainable = sustainable_distribution_non_financial(historical, fcf5)
        ratio = fcf5 / historical if historical > 0 else None
        caveats = ()
        status = CoverageStatus.VALID
        if lease_incomplete:
            status = CoverageStatus.PARTIAL
            caveats = ("租赁负债本金无法可靠拆分，FCF采用经营现金流减资本开支口径。",)
        return CoverageResult(
            status=status,
            adapter=self.name,
            sustainable_distribution=sustainable,
            coverage_ratio=ratio,
            capacity=fcf5,
            caveats=caveats,
        )


@dataclass(frozen=True)
class BankCapitalInput:
    adjusted_net_income: Decimal | None
    capital_generation_capacity: Decimal | None
    cet1_ratio: Decimal | None
    cet1_regulatory_minimum: Decimal | None
    risk_weighted_asset_growth: Decimal | None
    nonperforming_loan_ratio: Decimal | None
    provision_coverage_ratio: Decimal | None
    credit_cost: Decimal | None
    net_interest_margin: Decimal | None


@dataclass(frozen=True)
class BankCapitalAdapter(DistributionCoverageAdapter):
    data: BankCapitalInput
    minimum_buffer: Decimal = field(
        default_factory=lambda: decimal_value(
            "industry_adapters", "bank", "minimum_capital_buffer"
        )
    )
    capacity_haircut: Decimal = field(
        default_factory=lambda: decimal_value(
            "industry_adapters", "bank", "capacity_haircut"
        )
    )
    name: str = "BankCapitalAdapter/v2"

    def calculate(self, historical_distribution: Decimal) -> CoverageResult:
        fields = self.data.__dataclass_fields__
        missing = tuple(name for name in fields if getattr(self.data, name) is None)
        if missing:
            return CoverageResult(
                status=CoverageStatus.INSUFFICIENT_DATA,
                adapter=self.name,
                sustainable_distribution=None,
                coverage_ratio=None,
                capacity=None,
                required_missing_fields=missing,
                caveats=("银行不得回退到普通企业FCF口径。",),
            )
        historical = to_decimal(historical_distribution)
        net_income = to_decimal(self.data.adjusted_net_income)
        capital_capacity = to_decimal(self.data.capital_generation_capacity)
        buffer = to_decimal(self.data.cet1_ratio) - to_decimal(self.data.cet1_regulatory_minimum)
        if buffer <= 0:
            capacity = Decimal("0")
        else:
            buffer_factor = min(Decimal("1"), buffer / to_decimal(self.minimum_buffer))
            capacity = max(
                Decimal("0"),
                min(net_income, capital_capacity) * to_decimal(self.capacity_haircut) * buffer_factor,
            )
        sustainable = max(Decimal("0"), min(historical, capacity))
        ratio = capacity / historical if historical > 0 else None
        return CoverageResult(
            status=CoverageStatus.VALID,
            adapter=self.name,
            sustainable_distribution=sustainable,
            coverage_ratio=ratio,
            capacity=capacity,
        )


@dataclass(frozen=True)
class InsuranceSurplusInput:
    free_surplus_generation: Decimal | None
    distributable_surplus_capacity: Decimal | None
    comprehensive_solvency_ratio: Decimal | None
    comprehensive_solvency_minimum: Decimal | None
    core_solvency_ratio: Decimal | None
    core_solvency_minimum: Decimal | None
    investment_asset_quality_score: Decimal | None
    interest_rate_sensitivity_score: Decimal | None
    new_business_value: Decimal | None


@dataclass(frozen=True)
class InsuranceSurplusAdapter(DistributionCoverageAdapter):
    data: InsuranceSurplusInput
    minimum_buffer: Decimal = field(
        default_factory=lambda: decimal_value(
            "industry_adapters", "insurance", "minimum_solvency_buffer"
        )
    )
    capacity_haircut: Decimal = field(
        default_factory=lambda: decimal_value(
            "industry_adapters", "insurance", "capacity_haircut"
        )
    )
    name: str = "InsuranceSurplusAdapter/v2"

    def calculate(self, historical_distribution: Decimal) -> CoverageResult:
        fields = self.data.__dataclass_fields__
        missing = tuple(name for name in fields if getattr(self.data, name) is None)
        if missing:
            return CoverageResult(
                status=CoverageStatus.INSUFFICIENT_DATA,
                adapter=self.name,
                sustainable_distribution=None,
                coverage_ratio=None,
                capacity=None,
                required_missing_fields=missing,
                caveats=("保险公司缺少自由盈余或偿付能力数据时不得回退到FCF。",),
            )
        historical = to_decimal(historical_distribution)
        comprehensive_buffer = (
            to_decimal(self.data.comprehensive_solvency_ratio)
            - to_decimal(self.data.comprehensive_solvency_minimum)
        )
        core_buffer = to_decimal(self.data.core_solvency_ratio) - to_decimal(self.data.core_solvency_minimum)
        binding_buffer = min(comprehensive_buffer, core_buffer)
        buffer_factor = (
            Decimal("0")
            if binding_buffer <= 0
            else min(Decimal("1"), binding_buffer / to_decimal(self.minimum_buffer))
        )
        capacity = max(
            Decimal("0"),
            min(
                to_decimal(self.data.free_surplus_generation),
                to_decimal(self.data.distributable_surplus_capacity),
            )
            * to_decimal(self.capacity_haircut)
            * buffer_factor,
        )
        sustainable = max(Decimal("0"), min(historical, capacity))
        ratio = capacity / historical if historical > 0 else None
        return CoverageResult(
            status=CoverageStatus.VALID,
            adapter=self.name,
            sustainable_distribution=sustainable,
            coverage_ratio=ratio,
            capacity=capacity,
        )


@dataclass(frozen=True)
class UnsupportedAdapter(DistributionCoverageAdapter):
    industry: IndustryKind
    name: str = "UnsupportedAdapter/v2"

    def calculate(self, historical_distribution: Decimal) -> CoverageResult:
        del historical_distribution
        return CoverageResult(
            status=CoverageStatus.INSUFFICIENT_DATA,
            adapter=f"{self.name}:{self.industry.value}",
            sustainable_distribution=None,
            coverage_ratio=None,
            capacity=None,
            caveats=("行业覆盖口径尚不完整，自动推荐受限。",),
            required_missing_fields=("industry_specific_coverage_adapter",),
        )


def ensure_non_financial_adapter(industry: IndustryKind, adapter: DistributionCoverageAdapter) -> None:
    if industry in {
        IndustryKind.BANK,
        IndustryKind.INSURANCE,
        IndustryKind.SECURITIES,
        IndustryKind.OTHER_FINANCIAL,
    } and isinstance(adapter, NonFinancialFCFAdapter):
        raise InsufficientDataError(f"{industry.value} may not use the non-financial FCF adapter")
