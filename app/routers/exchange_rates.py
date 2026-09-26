"""USD->INR exchange rates used to snapshot INR costs on request_cost rows.

Rates are normally fetched daily by the fx_rates scheduler job. An admin can
set a day's rate by hand (air-gapped deployments, or to correct a bad fetch);
a manual rate is never overwritten by the job. Changing a rate only affects
cost rows written afterwards — existing rows keep the rate they were written with.
"""
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.deps import get_db
from app.models import ExchangeRate
from app.schemas import CurrentExchangeRateResponse, ExchangeRateResponse, ExchangeRateUpsert
from app.services.fx_service import INR, MANUAL_SOURCE, USD, get_usd_inr_rate, upsert_rate

router = APIRouter(prefix="/exchange-rates", tags=["exchange-rates"])


@router.get("/", response_model=list[ExchangeRateResponse])
def list_exchange_rates(limit: int = Query(30, ge=1, le=366), db: Session = Depends(get_db)):
    return (
        db.query(ExchangeRate)
        .filter(ExchangeRate.base_currency == USD, ExchangeRate.quote_currency == INR)
        .order_by(ExchangeRate.effective_date.desc())
        .limit(limit)
        .all()
    )


@router.get("/current", response_model=CurrentExchangeRateResponse)
def current_exchange_rate(on_date: Optional[date] = None, db: Session = Depends(get_db)):
    """The USD->INR rate new cost rows written on *on_date* (default today, UTC) would use."""
    on_date = on_date or datetime.utcnow().date()
    return {
        "base_currency": USD,
        "quote_currency": INR,
        "on_date": on_date,
        "rate": get_usd_inr_rate(db, on_date),
    }


@router.put("/", response_model=ExchangeRateResponse)
def set_exchange_rate(
    data: ExchangeRateUpsert,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    try:
        row = upsert_rate(
            db, rate=data.rate, effective_date=data.effective_date,
            source=MANUAL_SOURCE, overwrite_manual=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    db.commit()
    db.refresh(row)
    return row
