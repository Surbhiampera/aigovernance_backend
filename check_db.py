from app.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    tables = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).fetchall()
    print("Tables found:", [t[0] for t in tables])

    required = ["telemetry_events", "cost_breakdown", "tool_registry", "alerts", "data_security_logs", "daily_org_summary"]
    for t in required:
        if t in [row[0] for row in tables]:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            print(f"  OK {t}: {count} rows")
        else:
            print(f"  MISSING {t}!")
