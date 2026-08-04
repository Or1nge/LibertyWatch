from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import MetricBasis


class BalanceSheetAdapterError(ValueError):
    pass


ALIASES = {
    "cash_and_cash_equivalents": {
        "货币资金",
        "现金及现金等价物",
        "现金及等价物",
        "cash and cash equivalents",
        "cash & cash equivalents",
    },
    "short_term_borrowings": {"短期借款", "short term borrowings", "short-term borrowings"},
    "current_portion_long_term_debt": {
        "一年内到期的非流动负债",
        "一年内到期的长期债务",
        "current portion of long term debt",
        "current portion of long-term debt",
    },
    "long_term_borrowings": {"长期借款", "long term borrowings", "long-term borrowings"},
    "bonds_payable": {"应付债券", "bonds payable"},
    "total_interest_bearing_debt": {
        "借款总额",
        "总借款",
        "有息负债合计",
        "total borrowings",
        "total debt",
    },
    "lease_liabilities": {"租赁负债", "lease liabilities", "lease liability"},
    "total_equity": {"股东权益合计", "所有者权益合计", "权益总额", "total equity"},
    "total_assets": {"资产合计", "资产总额", "total assets"},
    "total_liabilities": {"负债合计", "负债总额", "total liabilities"},
}


def _normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


NORMALIZED_ALIASES = {
    key: {_normalize(alias) for alias in aliases}
    for key, aliases in ALIASES.items()
}


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


@dataclass(frozen=True)
class BalanceSheetPrimitive:
    name: str
    value: Decimal
    provider_field_id: str
    provider_display_name: str


@dataclass(frozen=True)
class BalanceSheetAssessment:
    company_id: str
    fiscal_year: int | None
    fiscal_year_end_date: date | None
    currency: str | None
    kind: str
    value: Decimal | None
    basis: MetricBasis
    net_debt: Decimal | None
    interest_bearing_debt: Decimal | None
    cash_and_cash_equivalents: Decimal | None
    total_equity: Decimal | None
    total_assets: Decimal | None
    total_liabilities: Decimal | None
    source_field_ids: tuple[str, ...]
    primitives: tuple[BalanceSheetPrimitive, ...]
    source_fetch_time: datetime | None = None
    warnings: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.kind != "UNAVAILABLE" and self.value is not None

    def public_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": format(self.value, "f") if self.value is not None else None,
            "basis": self.basis.value,
            "fiscal_year": self.fiscal_year,
            "fiscal_year_end_date": (
                self.fiscal_year_end_date.isoformat() if self.fiscal_year_end_date else None
            ),
            "currency": self.currency,
            "net_debt": format(self.net_debt, "f") if self.net_debt is not None else None,
            "interest_bearing_debt": (
                format(self.interest_bearing_debt, "f")
                if self.interest_bearing_debt is not None
                else None
            ),
            "cash_and_cash_equivalents": (
                format(self.cash_and_cash_equivalents, "f")
                if self.cash_and_cash_equivalents is not None
                else None
            ),
            "source_field_ids": list(self.source_field_ids),
            "warnings": list(self.warnings),
        }


def _unavailable(company_id: str, warning: str) -> BalanceSheetAssessment:
    return BalanceSheetAssessment(
        company_id=company_id,
        fiscal_year=None,
        fiscal_year_end_date=None,
        currency=None,
        kind="UNAVAILABLE",
        value=None,
        basis=MetricBasis.UNAVAILABLE,
        net_debt=None,
        interest_bearing_debt=None,
        cash_and_cash_equivalents=None,
        total_equity=None,
        total_assets=None,
        total_liabilities=None,
        source_field_ids=(),
        primitives=(),
        warnings=(warning,),
        source_fetch_time=None,
    )


def _select_primitives(items: Iterable[Mapping[str, Any]]) -> dict[str, BalanceSheetPrimitive]:
    selected: dict[str, BalanceSheetPrimitive] = {}
    for item in items:
        label = _normalize(item.get("display_name"))
        value = _decimal(item.get("data"))
        if value is None:
            continue
        for name, aliases in NORMALIZED_ALIASES.items():
            if label in aliases and name not in selected:
                selected[name] = BalanceSheetPrimitive(
                    name=name,
                    value=value,
                    provider_field_id=str(item.get("field_id") or ""),
                    provider_display_name=str(item.get("display_name") or ""),
                )
    return selected


def adapt_balance_sheet_payload(
    payload: Mapping[str, Any],
    *,
    expected_company_id: str,
) -> BalanceSheetAssessment:
    company = payload.get("company") if isinstance(payload.get("company"), Mapping) else {}
    company_id = str(company.get("issuer_id") or "")
    if company_id != expected_company_id:
        raise BalanceSheetAdapterError(
            f"balance-sheet identity mismatch: expected {expected_company_id}, got {company_id}"
        )
    statements = payload.get("statements") if isinstance(payload.get("statements"), Mapping) else {}
    balance = statements.get("balance_sheet") if isinstance(statements.get("balance_sheet"), Mapping) else {}
    reports = balance.get("report_list")
    if not isinstance(reports, list) or not reports:
        return _unavailable(company_id, "BALANCE_SHEET_RESPONSE_MISSING")
    valid_reports = [
        report
        for report in reports
        if isinstance(report, Mapping)
        and isinstance(report.get("item_list"), list)
        and report.get("fiscal_year") is not None
    ]
    if not valid_reports:
        return _unavailable(company_id, "BALANCE_SHEET_REPORT_MISSING")
    report = max(valid_reports, key=lambda item: int(item["fiscal_year"]))
    year = int(report["fiscal_year"])
    try:
        period_end = date.fromisoformat(str(report.get("date_time_str")))
    except ValueError:
        period_end = None
    primitives = _select_primitives(report["item_list"])
    cash = primitives.get("cash_and_cash_equivalents")
    equity = primitives.get("total_equity")
    assets = primitives.get("total_assets")
    liabilities = primitives.get("total_liabilities")
    total_debt = primitives.get("total_interest_bearing_debt")
    debt_names = (
        "short_term_borrowings",
        "current_portion_long_term_debt",
        "long_term_borrowings",
        "bonds_payable",
    )
    debt_parts = [primitives.get(name) for name in debt_names]
    warnings: list[str] = []
    interest_bearing_debt: Decimal | None
    selected_names: list[str]
    if total_debt is not None:
        interest_bearing_debt = total_debt.value
        selected_names = [total_debt.name]
    elif all(item is not None for item in debt_parts):
        interest_bearing_debt = sum((item.value for item in debt_parts if item is not None), Decimal("0"))
        selected_names = list(debt_names)
    else:
        interest_bearing_debt = None
        selected_names = []
        warnings.append("INCOMPLETE_INTEREST_BEARING_DEBT_COMPONENTS")
    net_debt = (
        interest_bearing_debt - cash.value
        if interest_bearing_debt is not None and cash is not None
        else None
    )
    if net_debt is not None and equity is not None and equity.value > 0:
        kind = "NET_CASH" if net_debt < 0 else "NET_DEBT_TO_EQUITY"
        value = abs(net_debt) if net_debt < 0 else net_debt / equity.value
        basis = MetricBasis.DIRECT
        selected_names.extend(["cash_and_cash_equivalents", "total_equity"])
    elif assets is not None and liabilities is not None and assets.value > 0:
        kind = "DEBT_TO_ASSETS_PROXY"
        value = liabilities.value / assets.value
        basis = MetricBasis.PROXY
        selected_names = ["total_liabilities", "total_assets"]
        warnings.append("BALANCE_SHEET_PROXY")
    else:
        return _unavailable(company_id, "BALANCE_SHEET_MINIMUM_FIELDS_MISSING")
    selected_primitives = tuple(primitives[name] for name in dict.fromkeys(selected_names))
    field_ids = tuple(f"FY{year}.balance_sheet.{item.name}" for item in selected_primitives)
    return BalanceSheetAssessment(
        company_id=company_id,
        fiscal_year=year,
        fiscal_year_end_date=period_end,
        currency=str(report.get("currency_code") or "") or None,
        kind=kind,
        value=value,
        basis=basis,
        net_debt=net_debt,
        interest_bearing_debt=interest_bearing_debt,
        cash_and_cash_equivalents=cash.value if cash is not None else None,
        total_equity=equity.value if equity is not None else None,
        total_assets=assets.value if assets is not None else None,
        total_liabilities=liabilities.value if liabilities is not None else None,
        source_field_ids=field_ids,
        primitives=selected_primitives,
        source_fetch_time=(
            datetime.fromisoformat(str(payload.get("fetched_at")).replace("Z", "+00:00"))
            if payload.get("fetched_at")
            else None
        ),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def load_balance_sheet_assessment(
    evidence_root: Path,
    company_id: str,
) -> BalanceSheetAssessment:
    path = evidence_root / company_id / "latest.json"
    if not path.is_file():
        return _unavailable(company_id, "BALANCE_SHEET_EVIDENCE_NOT_FOUND")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BalanceSheetAdapterError(f"cannot read balance-sheet evidence: {path}") from error
    return adapt_balance_sheet_payload(payload, expected_company_id=company_id)


def balance_sheet_source_records(
    assessment: BalanceSheetAssessment,
    *,
    source_path: Path,
    fetched_at: datetime | None = None,
) -> list[dict[str, Any]]:
    if assessment.fiscal_year is None or assessment.fiscal_year_end_date is None:
        return []
    fetched = fetched_at or assessment.source_fetch_time or datetime.now(timezone.utc)
    return [
        {
            "company_id": assessment.company_id,
            "field_id": f"FY{assessment.fiscal_year}.balance_sheet.{item.name}",
            "security_id": None,
            "share_class": None,
            "source_name": "Futu detailed financial statements",
            "source_document": source_path.name,
            "source_url_or_local_path": str(source_path.resolve()),
            "source_publish_date": assessment.fiscal_year_end_date.isoformat(),
            "source_fetch_time": fetched.isoformat(),
            "fiscal_period": f"FY{assessment.fiscal_year}",
            "currency": assessment.currency,
            "unit": "currency",
            "value": format(item.value, "f"),
            "data_status": "KNOWN_ZERO" if item.value == 0 else "VALID",
            "restatement_status": "CURRENT_PROVIDER_RESPONSE",
        }
        for item in assessment.primitives
    ]


def overlay_balance_sheet(
    raw: Mapping[str, Any],
    assessment: BalanceSheetAssessment,
    *,
    evidence_root: Path,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(raw))
    result["balance_sheet"] = assessment.public_dict()
    source_path = evidence_root / assessment.company_id / "latest.json"
    records = balance_sheet_source_records(assessment, source_path=source_path)
    retained = [
        item
        for item in result.get("raw_data_points", [])
        if isinstance(item, Mapping) and ".balance_sheet." not in str(item.get("field_id") or "")
    ]
    result["raw_data_points"] = [*retained, *records]
    return result
