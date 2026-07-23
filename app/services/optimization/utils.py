"""Small shared helpers for optimization-tip rules."""
from __future__ import annotations

from datetime import date, timedelta


def project_eq(column, project_id):
    """NULL-safe equality for an optional project scope.

    project_id is frequently None (org-wide requests), and SQL '= NULL' never
    matches — every rule needs this, so it lives in one place.
    """
    return column.is_(None) if project_id is None else column == project_id


def before_window_end(column, window_end: date):
    """Upper-bound filter for a DateTime column against an inclusive *date*
    window_end.

    window_end is a bare date (e.g. today); casting it directly to a
    timestamp for '<=' comparisons means midnight — every row from today
    would be silently excluded. Compare '<' the *next* day's midnight instead
    so all of window_end's day is included.
    """
    return column < (window_end + timedelta(days=1))
