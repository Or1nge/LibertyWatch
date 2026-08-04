from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .models import Freshness


class MarketObservationError(ValueError):
    pass


METRIC_FIELDS = {
    "pe": ("pe", "multiple"),
    "pe_ttm": ("peTtm", "multiple"),
    "pb": ("pb", "multiple"),
    "dividend_yield_ttm_pct": ("dividendYieldTtmPct", "percent"),
    "total_market_value": ("totalMarketValue", "currency"),
    "earnings_per_share": ("earningsPerShare", "currency_per_share"),
    "book_value_per_share": ("bookValuePerShare", "currency_per_share"),
}


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _decimal(value: Any, *, positive: bool = False) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or (positive and parsed <= 0):
        return None
    return parsed


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _business_days_between(start: datetime, end: datetime) -> int:
    if start.date() >= end.date():
        return 0
    cursor = start.date()
    count = 0
    while cursor < end.date():
        cursor = cursor.fromordinal(cursor.toordinal() + 1)
        if cursor.weekday() < 5:
            count += 1
    return count


def _is_expected_open(now: datetime, market: str) -> bool:
    current = now.astimezone(timezone.utc)
    if current.weekday() >= 5:
        return False
    clock = current.time().replace(tzinfo=None)
    if market == "HK":
        return time(1, 30) <= clock <= time(4, 0) or time(5, 0) <= clock <= time(8, 0)
    if market == "CN":
        return time(1, 30) <= clock <= time(3, 30) or time(5, 0) <= clock <= time(7, 0)
    return False


def determine_freshness(
    *,
    quote_timestamp: datetime | None,
    snapshot_collected_at: datetime | None,
    market_state: str,
    market: str,
    now: datetime,
) -> Freshness:
    if quote_timestamp is None or snapshot_collected_at is None:
        return Freshness.STALE_LAST_GOOD
    current = now.astimezone(timezone.utc)
    quote = quote_timestamp.astimezone(timezone.utc)
    collected = snapshot_collected_at.astimezone(timezone.utc)
    if quote > current or collected > current:
        return Freshness.STALE_LAST_GOOD
    age = current - quote
    state = market_state.strip().lower()
    expected_open = _is_expected_open(current, market)
    if state in {"open", "trading", "morning", "afternoon"} or expected_open:
        return Freshness.CURRENT if age.total_seconds() <= 600 else Freshness.STALE_LAST_GOOD
    business_days = _business_days_between(quote, current)
    if age.total_seconds() <= 4 * 24 * 3600 and business_days <= 1:
        return Freshness.MARKET_CLOSED_CURRENT
    return Freshness.STALE_LAST_GOOD


@dataclass(frozen=True)
class MarketObservation:
    company_id: str
    security_id: str
    market: str
    currency: str
    price: Decimal | None
    quote_timestamp: datetime | None
    market_state: str
    total_market_value: Decimal | None
    pe: Decimal | None
    pe_ttm: Decimal | None
    pb: Decimal | None
    dividend_yield_ttm_pct: Decimal | None
    earnings_per_share: Decimal | None
    book_value_per_share: Decimal | None
    fx_to_base: Decimal | None
    provider: str
    snapshot_collected_at: datetime | None
    snapshot_sha256: str
    freshness: Freshness
    source_path: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "security_id": self.security_id,
            "price": format(self.price, "f") if self.price is not None else None,
            "quote_timestamp": self.quote_timestamp.isoformat() if self.quote_timestamp else None,
            "market_state": self.market_state,
            "total_market_value": (
                format(self.total_market_value, "f")
                if self.total_market_value is not None
                else None
            ),
            "pe": format(self.pe, "f") if self.pe is not None else None,
            "pe_ttm": format(self.pe_ttm, "f") if self.pe_ttm is not None else None,
            "pb": format(self.pb, "f") if self.pb is not None else None,
            "earnings_per_share": (
                format(self.earnings_per_share, "f")
                if self.earnings_per_share is not None
                else None
            ),
            "book_value_per_share": (
                format(self.book_value_per_share, "f")
                if self.book_value_per_share is not None
                else None
            ),
            "fx_to_base": format(self.fx_to_base, "f") if self.fx_to_base is not None else None,
            "currency": self.currency,
            "provider": self.provider,
            "snapshot_collected_at": (
                self.snapshot_collected_at.isoformat() if self.snapshot_collected_at else None
            ),
            "snapshot_sha256": self.snapshot_sha256,
            "freshness": self.freshness.value,
        }


def load_market_observations(
    path: Path,
    *,
    now: datetime,
) -> dict[str, MarketObservation]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MarketObservationError(f"cannot read quote snapshot: {path}") from error
    securities = payload.get("securities")
    if not isinstance(securities, list):
        raise MarketObservationError("quote snapshot has no securities array")
    market_data = payload.get("marketData") if isinstance(payload.get("marketData"), Mapping) else {}
    collected_at = _datetime(market_data.get("collectedAt") or payload.get("snapshotGeneratedAt"))
    provider = str(market_data.get("provider") or "futu-opend")
    fx_rates = market_data.get("fxRates") if isinstance(market_data.get("fxRates"), Mapping) else {}
    hkd = fx_rates.get("HKD_CNY") if isinstance(fx_rates.get("HKD_CNY"), Mapping) else {}
    hkd_rate = _decimal(hkd.get("rate"), positive=True)
    digest = _canonical_sha256(payload)
    observations: dict[str, MarketObservation] = {}
    security_ids: set[str] = set()
    for row in securities:
        if not isinstance(row, Mapping):
            raise MarketObservationError("quote security row must be an object")
        company_id = str(row.get("issuerId") or "").strip()
        security_id = str(row.get("id") or "").strip()
        if not company_id or not security_id or company_id in observations or security_id in security_ids:
            raise MarketObservationError("quote snapshot contains missing or duplicate identities")
        quote = row.get("quote") if isinstance(row.get("quote"), Mapping) else {}
        metrics = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else {}
        price = _decimal(quote.get("currentPrice"), positive=True)
        if quote.get("status") != "available":
            price = None
        quote_time = _datetime(quote.get("lastUpdatedAt")) if price is not None else None
        market = str(row.get("market") or "")
        currency = str(row.get("currency") or "")
        fx = Decimal("1") if currency == "CNY" else hkd_rate if currency == "HKD" else None
        market_state = str(quote.get("marketState") or "unknown")
        freshness = determine_freshness(
            quote_timestamp=quote_time,
            snapshot_collected_at=collected_at,
            market_state=market_state,
            market=market,
            now=now,
        )
        observations[company_id] = MarketObservation(
            company_id=company_id,
            security_id=security_id,
            market=market,
            currency=currency,
            price=price,
            quote_timestamp=quote_time,
            market_state=market_state,
            total_market_value=_decimal(metrics.get("totalMarketValue"), positive=True),
            pe=_decimal(metrics.get("pe")),
            pe_ttm=_decimal(metrics.get("peTtm")),
            pb=_decimal(metrics.get("pb")),
            dividend_yield_ttm_pct=_decimal(metrics.get("dividendYieldTtmPct")),
            earnings_per_share=_decimal(metrics.get("earningsPerShare")),
            book_value_per_share=_decimal(metrics.get("bookValuePerShare")),
            fx_to_base=fx,
            provider=provider,
            snapshot_collected_at=collected_at,
            snapshot_sha256=digest,
            freshness=freshness,
            source_path=str(path.resolve()),
        )
        security_ids.add(security_id)
    return observations


def market_source_records(observation: MarketObservation) -> list[dict[str, Any]]:
    published = observation.snapshot_collected_at or observation.quote_timestamp
    if published is None:
        return []
    common = {
        "company_id": observation.company_id,
        "security_id": observation.security_id,
        "share_class": None,
        "source_name": observation.provider,
        "source_document": f"latest_snapshot.json#{observation.snapshot_sha256[:12]}",
        "source_url_or_local_path": observation.source_path,
        "source_publish_date": published.date().isoformat(),
        "source_fetch_time": published.isoformat(),
        "fiscal_period": f"MARKET_AS_OF_{published.date().isoformat()}",
        "restatement_status": "CURRENT_MARKET_OVERLAY",
    }
    values: list[tuple[str, Decimal | None, str, str | None]] = [
        ("price", observation.price, "currency_per_share", observation.currency),
        ("fx_to_base", observation.fx_to_base, "ratio", None),
        ("total_market_value", observation.total_market_value, "currency", observation.currency),
    ]
    for attr, (source, unit) in METRIC_FIELDS.items():
        if source == "totalMarketValue":
            continue
        values.append((attr, getattr(observation, attr), unit, observation.currency if "currency" in unit else None))
    return [
        {
            **common,
            "field_id": f"MARKET.{observation.security_id}.{field}",
            "currency": currency,
            "unit": unit,
            "value": format(value, "f") if value is not None else None,
            "data_status": (
                "KNOWN_ZERO" if value == 0 else "VALID" if value is not None else "MISSING"
            ),
        }
        for field, value, unit, currency in values
    ]


def overlay_market_observation(
    raw: Mapping[str, Any],
    observation: MarketObservation | None,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(raw))
    if observation is None or observation.company_id != str(result.get("company_id") or ""):
        return result
    result["market_observation"] = observation.public_dict()
    valuation = result.get("valuation") if isinstance(result.get("valuation"), dict) else {}
    valuation["current_market_metrics"] = {
        "pe": format(observation.pe, "f") if observation.pe is not None else None,
        "pe_ttm": format(observation.pe_ttm, "f") if observation.pe_ttm is not None else None,
        "pb": format(observation.pb, "f") if observation.pb is not None else None,
        "earnings_per_share": (
            format(observation.earnings_per_share, "f")
            if observation.earnings_per_share is not None
            else None
        ),
        "book_value_per_share": (
            format(observation.book_value_per_share, "f")
            if observation.book_value_per_share is not None
            else None
        ),
        "basis": "VENDOR_AUTHORIZED",
    }
    result["valuation"] = valuation
    for row in result.get("share_classes", []):
        if isinstance(row, dict) and str(row.get("security_id") or "") == observation.security_id:
            row["price"] = format(observation.price, "f") if observation.price is not None else None
            row["fx_to_base"] = (
                format(observation.fx_to_base, "f") if observation.fx_to_base is not None else None
            )
            row["price_timestamp"] = (
                observation.quote_timestamp.isoformat() if observation.quote_timestamp else None
            )
            row["quote_status"] = "VALID" if observation.price is not None else "MISSING"
    retained = [
        item
        for item in result.get("raw_data_points", [])
        if isinstance(item, Mapping) and not str(item.get("field_id") or "").startswith("MARKET.")
    ]
    result["raw_data_points"] = [*retained, *market_source_records(observation)]
    return result
