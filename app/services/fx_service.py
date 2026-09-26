"""USD->INR exchange rates for cost reporting.

Costs are priced and stored in USD. Every request_cost row also stores the INR
amount and the rate used, taken when the row is written, so a request keeps its
own day's rate and its INR cost never moves when the rate does.

Rates live in exchange_rates (one row per pair per day), filled by the daily
fx_rates scheduler job (app/scheduler.py) or entered by an admin via
PUT /exchange-rates. Lookups never call the external API — they only read the
table, so the proxy's cost write never waits on a network call.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.config import get_fx_rate_api_url, get_fx_usd_inr_fallback_rate
from app.models import ExchangeRate

_log = logging.getLogger(__name__)

USD = "USD"
INR = "INR"
MANUAL_SOURCE = "manual"

_RATE_QUANT = Decimal("0.000001")   # exchange_rates.rate / request_cost.exchange_rate: NUMERIC(12, 6)
_INR_QUANT = Decimal("0.00000001")  # request_cost.*_inr: NUMERIC(16, 8)

# Per-process cache of on_date -> rate, so the proxy's cost write doesn't hit
# the DB for the rate on every request. Short TTL because other uvicorn
# workers can't see this process's invalidation after an admin edit.
_CACHE_TTL_SECONDS = 600
_cache: dict[date, tuple[Optional[Decimal], float]] = {}
_cache_lock = threading.Lock()


def clear_rate_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _lookup_usd_inr_rate(db: Session, on_date: date) -> Optional[Decimal]:
    base = db.query(ExchangeRate.rate).filter(
        ExchangeRate.base_currency == USD,
        ExchangeRate.quote_currency == INR,
    )
    row = (
        base.filter(ExchangeRate.effective_date <= on_date)
        .order_by(ExchangeRate.effective_date.desc())
        .first()
    )
    if row is None:
        # Nothing on or before on_date (e.g. a date older than the first stored
        # rate) — use the most recent stored rate rather than no rate at all.
        row = base.order_by(ExchangeRate.effective_date.desc()).first()
    if row is not None:
        return Decimal(row.rate)
    return get_fx_usd_inr_fallback_rate()


def get_usd_inr_rate(db: Session, on_date: Optional[date] = None) -> Optional[Decimal]:
    """Latest USD->INR rate effective on or before *on_date* (default: today, UTC).

    Falls back to the most recent stored rate, then FX_USD_INR_FALLBACK_RATE,
    then None. A missed daily fetch therefore just means yesterday's rate keeps
    being used.
    """
    on_date = on_date or datetime.utcnow().date()
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(on_date)
        if hit and hit[1] > now:
            return hit[0]
    rate = _lookup_usd_inr_rate(db, on_date)
    with _cache_lock:
        _cache[on_date] = (rate, now + _CACHE_TTL_SECONDS)
    return rate


def to_inr(amount_usd, rate: Optional[Decimal]) -> Optional[Decimal]:
    if rate is None or amount_usd is None:
        return None
    return (Decimal(str(amount_usd)) * rate).quantize(_INR_QUANT)


def inr_cost_fields(
    db: Session,
    *,
    on_date: date,
    total_cost,
    input_cost,
    output_cost,
) -> dict:
    """RequestCost column values for the INR snapshot. Never raises — a failed
    rate lookup must not lose the (USD) cost row, so it yields NULL INR instead."""
    try:
        # no_autoflush: the caller's pending AiResponse/TokenUsage rows must not
        # be flushed (and fail) inside this lookup and be mistaken for a rate error.
        with db.no_autoflush:
            rate = get_usd_inr_rate(db, on_date)
    except Exception:
        _log.warning("USD->INR rate lookup failed for %s; storing cost without INR", on_date, exc_info=True)
        rate = None
    return {
        "exchange_rate": rate,
        "total_cost_inr": to_inr(total_cost, rate),
        "input_cost_inr": to_inr(input_cost, rate),
        "output_cost_inr": to_inr(output_cost, rate),
    }


def upsert_rate(
    db: Session,
    *,
    rate: Decimal,
    effective_date: date,
    source: str,
    base_currency: str = USD,
    quote_currency: str = INR,
    overwrite_manual: bool = False,
) -> Optional[ExchangeRate]:
    """Insert or update the rate for (pair, effective_date). An admin's manual
    rate is kept unless *overwrite_manual* — returns None when it was kept.
    Caller commits."""
    rate = Decimal(str(rate)).quantize(_RATE_QUANT)
    if rate <= 0:
        raise ValueError("rate must be positive")
    row = (
        db.query(ExchangeRate)
        .filter(
            ExchangeRate.base_currency == base_currency,
            ExchangeRate.quote_currency == quote_currency,
            ExchangeRate.effective_date == effective_date,
        )
        .with_for_update()
        .first()
    )
    if row is None:
        row = ExchangeRate(
            base_currency=base_currency,
            quote_currency=quote_currency,
            rate=rate,
            effective_date=effective_date,
            source=source,
        )
        db.add(row)
    elif row.source == MANUAL_SOURCE and not overwrite_manual:
        return None
    else:
        row.rate = rate
        row.source = source
    db.flush()
    clear_rate_cache()
    return row


def fetch_usd_inr_rate(timeout: float = 30.0) -> tuple[Decimal, date, str]:
    """Fetch today's USD->INR rate from FX_RATE_API_URL.

    Accepts any JSON body with rates.INR (Frankfurter, openexchangerates,
    exchangerate.host all use this shape). The effective date is the body's
    "date" when given (Frankfurter returns the last ECB publishing day, so a
    Saturday fetch stores Friday's rate), else today (UTC).
    Returns (rate, effective_date, source); raises on any failure.
    """
    url = get_fx_rate_api_url()
    resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    body = resp.json()
    raw_rate = (body.get("rates") or {}).get(INR)
    if raw_rate is None:
        raise ValueError(f"FX response from {httpx.URL(url).host} has no rates.INR")
    rate = Decimal(str(raw_rate))
    if rate <= 0:
        raise ValueError(f"FX response returned non-positive rate {rate}")
    try:
        effective = date.fromisoformat(body["date"]) if body.get("date") else datetime.utcnow().date()
    except ValueError:
        effective = datetime.utcnow().date()
    return rate, effective, httpx.URL(url).host or "api"
