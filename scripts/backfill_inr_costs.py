"""Backfill the INR snapshot (exchange_rate, total_cost_inr, input_cost_inr,
output_cost_inr) on request_cost rows written before INR was recorded.

Apply schema_clean.sql first — it adds the columns and the exchange_rates table.
Then pick one rate source:

  --fetch-history   Fetch daily historical USD->INR rates (Frankfurter / ECB
                    reference rates by default) covering every date that has
                    cost rows, store them in exchange_rates, and give each row
                    the rate for its own created_at date.
  --fixed-rate R    Give every un-backfilled row the single rate R (INR per USD).

With neither flag, rows are matched against whatever exchange_rates already
holds. Either way each row gets the latest rate on or before its created_at
date, or the earliest stored rate if the row is older than all of them.

Only rows whose total_cost_inr IS NULL are touched, so it is safe to re-run.
Updates run in small committed batches (short row locks, never a long lock on
the whole table), so it can run while the backend is serving traffic.

Needs the same DATABASE_URL as the server (read from .env).

Examples:

    python scripts/backfill_inr_costs.py --fetch-history
    python scripts/backfill_inr_costs.py --fixed-rate 83.50 --dry-run
"""
import argparse
import os
import sys
from datetime import date
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.services.fx_service import INR, USD, upsert_rate  # noqa: E402

DEFAULT_HISTORY_URL = "https://api.frankfurter.dev/v1/{start}..{end}?base=USD&symbols=INR"

_PENDING = "rc.total_cost_inr IS NULL AND rc.total_cost IS NOT NULL"

# Rate for a row: latest on or before its date, else the earliest stored rate.
_STORED_RATE_SQL = """
    COALESCE(
        (SELECT er.rate FROM exchange_rates er
          WHERE er.base_currency = 'USD' AND er.quote_currency = 'INR'
            AND er.effective_date <= rc.created_at::date
          ORDER BY er.effective_date DESC LIMIT 1),
        (SELECT er.rate FROM exchange_rates er
          WHERE er.base_currency = 'USD' AND er.quote_currency = 'INR'
          ORDER BY er.effective_date ASC LIMIT 1)
    )
"""


def _fetch_history(db, url_template: str) -> int:
    first, last = db.execute(text(
        f"SELECT MIN(rc.created_at)::date, MAX(rc.created_at)::date FROM request_cost rc WHERE {_PENDING}"
    )).one()
    if first is None:
        return 0
    url = url_template.format(start=first.isoformat(), end=max(last, date.today()).isoformat())
    print(f"Fetching USD->INR history {first} .. {last} from {httpx.URL(url).host}")
    resp = httpx.get(url, timeout=120, follow_redirects=True)
    resp.raise_for_status()
    rates = resp.json().get("rates") or {}
    stored = 0
    for day, by_ccy in sorted(rates.items()):
        rate = (by_ccy or {}).get(INR)
        if rate is None:
            continue
        # Never replaces an admin's manual rate for the same day.
        if upsert_rate(db, rate=Decimal(str(rate)), effective_date=date.fromisoformat(day),
                       source=f"{httpx.URL(url).host}-history") is not None:
            stored += 1
    db.commit()
    return stored


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--fetch-history", action="store_true", help="Fetch and store historical daily rates first")
    source.add_argument("--fixed-rate", help="Use one rate (INR per USD) for every row")
    parser.add_argument("--history-url", default=DEFAULT_HISTORY_URL,
                        help="Time-series URL with {start}/{end} placeholders, returning rates.<date>.INR")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--dry-run", action="store_true", help="Report what would change, write nothing")
    args = parser.parse_args()

    fixed = None
    if args.fixed_rate is not None:
        try:
            fixed = Decimal(args.fixed_rate)
        except InvalidOperation:
            parser.error("--fixed-rate must be a number")
        if fixed <= 0:
            parser.error("--fixed-rate must be positive")

    db = SessionLocal()
    try:
        # Fail loudly rather than queue forever behind another session's lock.
        db.execute(text("SET lock_timeout = '5s'"))
        db.execute(text("SET statement_timeout = '120s'"))

        pending = db.execute(text(f"SELECT COUNT(*) FROM request_cost rc WHERE {_PENDING}")).scalar()
        print(f"{pending} request_cost rows need an INR amount")
        if not pending:
            return

        if args.fetch_history and not args.dry_run:
            print(f"Stored {_fetch_history(db, args.history_url)} daily rates")

        if fixed is None:
            have_rates = db.execute(text(
                "SELECT EXISTS (SELECT 1 FROM exchange_rates WHERE base_currency = :b AND quote_currency = :q)"
            ), {"b": USD, "q": INR}).scalar()
            if not have_rates:
                sys.exit("exchange_rates has no USD->INR rates: pass --fetch-history or --fixed-rate")

        if args.dry_run:
            print("Dry run: nothing written")
            return

        rate_expr = ":fixed_rate" if fixed is not None else _STORED_RATE_SQL
        params = {"fixed_rate": fixed, "batch": args.batch_size}
        done = 0
        while True:
            updated = db.execute(text(f"""
                UPDATE request_cost AS t
                   SET exchange_rate   = x.rate,
                       total_cost_inr  = ROUND(t.total_cost * x.rate, 8),
                       input_cost_inr  = ROUND(COALESCE(t.input_token_cost, 0) * x.rate, 8),
                       output_cost_inr = ROUND(COALESCE(t.output_token_cost, 0) * x.rate, 8)
                  FROM (
                        SELECT rc.id, CAST({rate_expr} AS NUMERIC(12, 6)) AS rate
                          FROM request_cost rc
                         WHERE {_PENDING}
                         ORDER BY rc.id
                         LIMIT :batch
                       ) x
                 WHERE t.id = x.id AND x.rate IS NOT NULL
            """), params).rowcount
            db.commit()
            if not updated:
                break
            done += updated
            print(f"  backfilled {done}/{pending}")
        print(f"Done: {done} rows backfilled")
    finally:
        db.close()


if __name__ == "__main__":
    main()
