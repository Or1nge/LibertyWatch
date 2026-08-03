from __future__ import annotations

from typing import Any, Mapping

from .calculations import to_decimal
from .models import VetoFlag
from .policy import decimal_value


def _flag(code: str, triggered: bool, message: str, *fields: str, severity: str = "MAJOR") -> VetoFlag:
    return VetoFlag(
        code=code,
        severity=severity,
        triggered=bool(triggered),
        evidence_fields=tuple(fields),
        message_zh=message,
    )


def evaluate_vetoes(values: Mapping[str, Any]) -> list[VetoFlag]:
    latest = values.get("ordinary_dividend_latest")
    previous = values.get("ordinary_dividend_previous")
    dividend_decline = False
    if latest is not None and previous is not None and to_decimal(previous) > 0:
        dividend_decline = (
            (to_decimal(previous) - to_decimal(latest)) / to_decimal(previous)
            > decimal_value("thresholds", "ordinary_dividend_decline_veto")
        )
    dividend_suspended = latest is not None and to_decimal(latest) == 0

    two_year_over_fcf = bool(values.get("two_year_distribution_over_fcf_125"))
    net_debt_increased = bool(values.get("net_debt_increased"))
    claimed_buyback = values.get("claimed_buyback")
    diluted_reduction = values.get("diluted_net_share_reduction")
    buyback_no_reduction = bool(
        claimed_buyback is not None
        and to_decimal(claimed_buyback) > 0
        and diluted_reduction is not None
        and to_decimal(diluted_reduction) <= 0
    )

    one_off = values.get("one_off_distribution")
    surface = values.get("surface_distribution")
    one_off_share = False
    if one_off is not None and surface is not None and to_decimal(surface) > 0:
        one_off_share = (
            to_decimal(one_off) / to_decimal(surface)
            > decimal_value("thresholds", "one_off_distribution_share_veto")
        )

    return [
        _flag(
            "ORDINARY_DIVIDEND_DROPPED_OR_SUSPENDED",
            dividend_decline or dividend_suspended,
            "最近普通分红暂停或下降超过30%。",
            "ordinary_dividend_latest",
            "ordinary_dividend_previous",
        ),
        _flag(
            "DISTRIBUTION_OVER_FCF_AND_DEBT_RISING",
            two_year_over_fcf and net_debt_increased,
            "连续两年总分配超过自由现金流125%，同时净负债增加。",
            "two_year_distribution_over_fcf_125",
            "net_debt_increased",
        ),
        _flag(
            "BUYBACK_WITHOUT_DILUTED_REDUCTION",
            buyback_no_reduction,
            "公司声称大量回购，但稀释后总股本没有下降。",
            "claimed_buyback",
            "diluted_net_share_reduction",
        ),
        _flag(
            "ONE_OFF_DISTRIBUTION_OVER_30PCT",
            one_off_share,
            "特别股息、资产出售或一次性回购占表面分配额30%以上。",
            "one_off_distribution",
            "surface_distribution",
        ),
        _flag(
            "CORE_ASSET_EXPIRES_WITHIN_10Y",
            bool(values.get("core_asset_expires_within_10y")),
            "已知特许经营、资源、牌照或核心资产将在未来十年内到期。",
            "core_asset_expires_within_10y",
        ),
        _flag(
            "MAJOR_COMMITTED_CAPEX_OR_MA",
            bool(values.get("major_committed_capex_or_ma")),
            "已承诺重大并购、扩产或改扩建，可能明显挤压分配能力。",
            "major_committed_capex_or_ma",
        ),
        _flag(
            "AUDIT_GOVERNANCE_ALERT",
            any(
                bool(values.get(key))
                for key in (
                    "qualified_audit_opinion",
                    "material_internal_control_weakness",
                    "controlling_shareholder_fund_occupation",
                    "major_related_party_alert",
                )
            ),
            "出现审计、内控、资金占用或重大关联交易警报。",
            "qualified_audit_opinion",
            "material_internal_control_weakness",
            "controlling_shareholder_fund_occupation",
            "major_related_party_alert",
        ),
        _flag(
            "MAJOR_REGULATORY_PENALTY",
            bool(values.get("major_regulatory_penalty")),
            "出现可能影响持续经营或资本分配的重大监管处罚。",
            "major_regulatory_penalty",
        ),
        _flag(
            "FINANCIAL_CAPITAL_BUFFER_NEAR_MINIMUM",
            bool(values.get("financial_capital_buffer_near_minimum")),
            "银行或保险资本缓冲接近监管约束。",
            "financial_capital_buffer_near_minimum",
        ),
        _flag(
            "KEY_SOURCE_VALIDATION_FAILED",
            bool(values.get("key_source_validation_failed")),
            "关键数据无法完成来源校验。",
            "key_source_validation_failed",
        ),
        _flag(
            "INCOMPLETE_AH_MARKET_CAP",
            bool(values.get("incomplete_ah_market_cap")),
            "A/H总市值或总股本口径不完整，禁止公司级收益率更新。",
            "incomplete_ah_market_cap",
        ),
    ]
