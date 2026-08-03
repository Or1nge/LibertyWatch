from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import re
from typing import Any, Mapping, Sequence

from ..calculations import to_decimal
from ..policy import decimal_value, integer_value


URGENT_CODES = {
    "ORDINARY_DIVIDEND_DROPPED_OR_SUSPENDED",
    "AUDIT_GOVERNANCE_ALERT",
    "BUYBACK_WITHOUT_DILUTED_REDUCTION",
    "DISTRIBUTION_OVER_FCF_AND_DEBT_RISING",
    "FINANCIAL_CAPITAL_BUFFER_NEAR_MINIMUM",
    "CORE_ASSET_EXPIRES_WITHIN_10Y",
    "MAJOR_COMMITTED_CAPEX_OR_MA",
    "KEY_SOURCE_VALIDATION_FAILED",
    "MAJOR_REGULATORY_PENALTY",
}
MATERIAL_EVENT_TYPES = {
    "ANNUAL_REPORT",
    "INTERIM_REPORT",
    "PROFIT_WARNING",
    "DIVIDEND_PROPOSAL",
    "BUYBACK_RESULT",
    "SHARE_CAPITAL_CHANGE",
    "MAJOR_MA",
    "ASSET_SALE",
    "REGULATORY_EVENT",
    "INDUSTRY_POLICY",
}


@dataclass(frozen=True)
class TriggerDecision:
    should_trigger: bool
    analysis_mode: str | None
    trigger_type: str | None
    summary: str
    state: dict[str, Any]


def is_analysis_eligible(snapshot: Mapping[str, Any]) -> bool:
    """Allow only fully VALID inputs or the narrow qualitative-score bootstrap."""

    if snapshot.get("data_status") == "VALID":
        return True
    eligibility = snapshot.get("analysis_eligibility")
    if snapshot.get("data_status") != "PARTIAL" or not isinstance(eligibility, Mapping):
        return False
    missing = eligibility.get("missing_qualitative_scores")
    return bool(
        eligibility.get("eligible") is True
        and eligibility.get("status") == "CORE_VALID_QUALITATIVE_OVERLAY_PENDING"
        and isinstance(missing, list)
        and missing
        and set(map(str, missing)).issubset(
            {"business_durability", "governance_capital_allocation"}
        )
    )


def _metric(snapshot: Mapping[str, Any] | None, metric_id: str) -> Decimal | None:
    if not snapshot:
        return None
    metrics = snapshot.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    raw = metrics.get(metric_id)
    if not isinstance(raw, Mapping) or raw.get("value") is None:
        return None
    return to_decimal(raw["value"])


def _score(snapshot: Mapping[str, Any] | None, score_id: str) -> Decimal | None:
    if not snapshot:
        return None
    scores = snapshot.get("scores")
    if not isinstance(scores, Mapping):
        return None
    raw = scores.get(score_id)
    if not isinstance(raw, Mapping) or raw.get("value") is None:
        return None
    return to_decimal(raw["value"])


def _relative_change(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    if current is None or previous is None or previous == 0:
        return None
    return abs(current - previous) / abs(previous)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _major_prompt_changed(previous: str | None, current: str) -> bool:
    if not previous or previous == current:
        return False
    old = re.fullmatch(r"risk-review-v(\d+)\.\d+\.\d+", previous)
    new = re.fullmatch(r"risk-review-v(\d+)\.\d+\.\d+", current)
    return bool(old and new and old.group(1) != new.group(1))


def evaluate_trigger(
    current: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    *,
    state: Mapping[str, Any] | None = None,
    events: Sequence[str] = (),
    has_legal_report: bool = False,
    last_success_at: datetime | None = None,
    last_prompt_version: str | None = None,
    current_prompt_version: str,
    prompt_major_upgrade: bool = False,
    prior_baseline_invalid: bool = False,
    trade_date: date | None = None,
    now: datetime | None = None,
) -> TriggerDecision:
    state_value = dict(state or {})
    state_value.setdefault("in_observation_zone", False)
    state_value.setdefault("below_exit_days", 0)
    state_value.setdefault("last_below_trade_date", None)
    state_value.setdefault("last_price_trigger_at", None)
    if not is_analysis_eligible(current):
        return TriggerDecision(False, None, None, "核心结构化输入不具备分析资格，不创建Codex任务。", state_value)

    current_time = now or datetime.now(timezone.utc)
    current_trade_date = trade_date or current_time.date()
    ssy = _metric(current, "sustainable_shareholder_yield")
    raw_yield = _metric(current, "raw_2y_shareholder_yield")
    previous_ssy = _metric(previous, "sustainable_shareholder_yield")
    cash_anchor = decimal_value("thresholds", "cash_anchor_yield")
    exit_yield = decimal_value("thresholds", "observation_exit_yield")
    entered_now = bool(ssy is not None and ssy >= cash_anchor and (previous_ssy is None or previous_ssy < cash_anchor))
    in_zone_now = bool(
        (ssy is not None and ssy >= cash_anchor)
        or (raw_yield is not None and raw_yield >= cash_anchor)
    )
    if in_zone_now:
        state_value["in_observation_zone"] = True
        state_value["below_exit_days"] = 0
        state_value["last_below_trade_date"] = None
    elif (
        state_value["in_observation_zone"]
        and ssy is not None
        and raw_yield is not None
        and ssy < exit_yield
        and raw_yield < exit_yield
    ):
        date_text = current_trade_date.isoformat()
        if state_value.get("last_below_trade_date") != date_text:
            state_value["below_exit_days"] = int(state_value.get("below_exit_days") or 0) + 1
            state_value["last_below_trade_date"] = date_text
        if state_value["below_exit_days"] >= integer_value("thresholds", "observation_exit_trading_days"):
            state_value["in_observation_zone"] = False
            state_value["below_exit_days"] = 0
            state_value["last_below_trade_date"] = None

    veto_codes = {
        str(item.get("code"))
        for item in current.get("veto_flags", [])
        if isinstance(item, Mapping)
    }
    previous_veto_codes = {
        str(item.get("code"))
        for item in (previous or {}).get("veto_flags", [])
        if isinstance(item, Mapping)
    }
    newly_urgent = veto_codes - previous_veto_codes
    if not has_legal_report:
        newly_urgent |= veto_codes
    urgent = sorted((newly_urgent | set(events)) & URGENT_CODES)
    if urgent:
        return TriggerDecision(
            True,
            "URGENT_VETO_REVIEW",
            urgent[0],
            "重大否决或风险事件：" + "、".join(urgent),
            state_value,
        )

    company_cooldown_active = bool(
        last_success_at is not None
        and current_time - last_success_at
        < timedelta(days=integer_value("thresholds", "company_analysis_cooldown_days"))
    )

    trap_check = bool(
        raw_yield is not None
        and raw_yield >= cash_anchor
        and ssy is not None
        and ssy < cash_anchor
    )
    full_reason = None
    if entered_now:
        full_reason = "SSY首次从4%以下进入4%区间。"
    elif trap_check:
        full_reason = "原始两年收益率达到4%，但SSY低于4%，优先检查收益陷阱。"
    elif state_value["in_observation_zone"] and not has_legal_report:
        full_reason = "公司在4%观察区间内，但从未有合法Codex报告。"
    elif prompt_major_upgrade or _major_prompt_changed(last_prompt_version, current_prompt_version):
        full_reason = "风险分析Prompt发生重大版本升级。"
    elif prior_baseline_invalid:
        full_reason = "过去报告输入校验不合格，需要重建完整基线。"
    if full_reason:
        last_price_trigger = _parse_datetime(state_value.get("last_price_trigger_at"))
        cooldown_active = bool(
            last_price_trigger
            and current_time - last_price_trigger
            < timedelta(days=integer_value("thresholds", "price_trigger_cooldown_days"))
        )
        if not company_cooldown_active and (
            not cooldown_active or prompt_major_upgrade or prior_baseline_invalid
        ):
            state_value["last_price_trigger_at"] = current_time.isoformat()
            return TriggerDecision(True, "FULL_ENTRY_REVIEW", "FULL_ENTRY_REVIEW", full_reason, state_value)

    material_reasons: list[str] = []
    has_non_price_material_change = False
    previous_h = _metric(previous, "historical_conservative_distribution")
    current_h = _metric(current, "historical_conservative_distribution")
    previous_s = _metric(previous, "sustainable_distribution")
    current_s = _metric(current, "sustainable_distribution")
    previous_cr10 = _metric(previous, "conservative_return_10y")
    current_cr10 = _metric(current, "conservative_return_10y")
    previous_coverage = _metric(previous, "coverage_ratio")
    current_coverage = _metric(current, "coverage_ratio")
    if ssy is not None and previous_ssy is not None:
        if abs(ssy - previous_ssy) >= decimal_value("thresholds", "material_ssy_absolute"):
            material_reasons.append("SSY绝对变化至少0.50个百分点")
        relative = _relative_change(ssy, previous_ssy)
        if relative is not None and relative >= decimal_value("thresholds", "material_ssy_relative"):
            material_reasons.append("SSY相对变化至少15%")
    for label, current_value, previous_value, threshold_key in (
        ("H", current_h, previous_h, "material_h_relative"),
        ("S", current_s, previous_s, "material_s_relative"),
    ):
        relative = _relative_change(current_value, previous_value)
        if relative is not None and relative >= decimal_value("thresholds", threshold_key):
            material_reasons.append(f"{label}变化至少15%")
            has_non_price_material_change = True
    if current_cr10 is not None and previous_cr10 is not None and abs(current_cr10 - previous_cr10) >= decimal_value("thresholds", "material_cr10_absolute"):
        material_reasons.append("CR10变化至少0.75个百分点")
    for score_name in ("recommendation_index", "entry_risk_index"):
        current_score = _score(current, score_name)
        previous_score = _score(previous, score_name)
        if current_score is not None and previous_score is not None and abs(current_score - previous_score) >= decimal_value("thresholds", "material_score_absolute"):
            material_reasons.append(f"{score_name}变化至少10分")
            if score_name == "entry_risk_index":
                has_non_price_material_change = True
    if current_coverage is not None and previous_coverage is not None:
        if (current_coverage >= 1 > previous_coverage) or (previous_coverage >= 1 > current_coverage):
            material_reasons.append("覆盖倍数跨越1.0")
            has_non_price_material_change = True
        if abs(current_coverage - previous_coverage) >= decimal_value("thresholds", "material_coverage_absolute"):
            material_reasons.append("覆盖倍数变化至少0.25")
            has_non_price_material_change = True
    current_debt = _metric(current, "net_debt_ebitda")
    previous_debt = _metric(previous, "net_debt_ebitda")
    if (
        current_debt is not None
        and previous_debt is not None
        and current_debt - previous_debt >= decimal_value("thresholds", "material_net_debt_ebitda")
    ):
        material_reasons.append("净负债/EBITDA增加至少0.5倍")
        has_non_price_material_change = True
    material_events = sorted(set(events) & MATERIAL_EVENT_TYPES)
    material_reasons.extend(material_events)
    has_non_price_material_change = has_non_price_material_change or bool(material_events)
    material_price_cooldown = False
    if state_value["in_observation_zone"] and material_reasons:
        if not has_non_price_material_change:
            last_price_trigger = _parse_datetime(state_value.get("last_price_trigger_at"))
            material_price_cooldown = bool(
                company_cooldown_active
                or (
                    last_price_trigger
                    and current_time - last_price_trigger
                    < timedelta(days=integer_value("thresholds", "price_trigger_cooldown_days"))
                )
            )
        if not company_cooldown_active and not material_price_cooldown:
            if not has_non_price_material_change:
                state_value["last_price_trigger_at"] = current_time.isoformat()
            return TriggerDecision(
                True,
                "MATERIAL_CHANGE_REVIEW",
                material_events[0] if material_events else "MATERIAL_METRIC_CHANGE",
                "；".join(material_reasons),
                state_value,
            )

    if (
        state_value["in_observation_zone"]
        and last_success_at is not None
        and not company_cooldown_active
    ):
        eri = _score(current, "entry_risk_index")
        high_risk = bool((eri is not None and eri > 45) or veto_codes)
        due_days = integer_value(
            "thresholds",
            "high_risk_refresh_days" if high_risk else "periodic_refresh_days",
        )
        if current_time - last_success_at >= timedelta(days=due_days):
            return TriggerDecision(
                True,
                "PERIODIC_REFRESH",
                "PERIODIC_REFRESH",
                f"最近一次成功分析已超过{due_days}天。",
                state_value,
            )
    if company_cooldown_active:
        summary = "同一公司距上次成功分析不足30天，非紧急任务不调用Codex。"
    elif material_price_cooldown:
        summary = "价格驱动的同类变化仍在7天冷却期内。"
    else:
        summary = "仅结构化数据更新，无需调用Codex。"
    return TriggerDecision(False, None, None, summary, state_value)
