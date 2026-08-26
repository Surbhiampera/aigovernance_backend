"""Shared day-count / period parsing for cost & proxy stats endpoints.

Every such endpoint accepts a legacy `days` integer and/or the newer
`period` keyword (`7d` / `14d` / `30d` / `90d` / `all`). `period` takes
precedence over `days` when both are given. `all` resolves to no lower
bound at all rather than an approximated "day 1" date — the query then
naturally returns everything from the first row up to now, so there's no
extra cutoff to compute or keep in sync with actual data.
"""
from datetime import date, datetime, time, timedelta
from typing import Optional

from fastapi import HTTPException

PERIOD_DAYS = {"7d": 7, "14d": 14, "30d": 30, "90d": 90}
ALLOWED_PERIODS = (*PERIOD_DAYS, "all")


def resolve_days(days: Optional[int] = None, period: Optional[str] = None) -> Optional[int]:
    """Return the effective lookback window in days, or None for all-time.

    Raises a 422 on an unrecognized `period` instead of silently falling
    back to some default — a typo should error, not quietly change scope.
    """
    if period is not None:
        key = period.lower()
        if key not in ALLOWED_PERIODS:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid period '{period}'. Expected one of: {', '.join(ALLOWED_PERIODS)}.",
            )
        return None if key == "all" else PERIOD_DAYS[key]
    return days


def cutoff_datetime(days: Optional[int]) -> Optional[datetime]:
    """Midnight `days` ago (today counts as day 1), or None for no lower bound."""
    if not days:
        return None
    return datetime.combine(date.today() - timedelta(days=days - 1), time.min)


def cutoff_date(days: Optional[int]) -> Optional[date]:
    if not days:
        return None
    return date.today() - timedelta(days=days - 1)
