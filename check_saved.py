from app.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    print("=== 1. telemetry_events ===")
    rows = conn.execute(text("SELECT event_id, model_name, service_type, latency_ms FROM telemetry_events")).fetchall()
    for r in rows:
        print(f"  {r[0][:12]}... | {r[1]} | {r[2]} | {r[3]}ms")
    print(f"  Total: {len(rows)} rows\n")

    print("=== 2. cost_breakdown ===")
    rows = conn.execute(text("SELECT event_id, cost_type, component_name, total_cost FROM cost_breakdown")).fetchall()
    for r in rows:
        print(f"  {r[0][:12]}... | {r[1]} | {r[2]} | ${r[3]}")
    print(f"  Total: {len(rows)} rows\n")

    print("=== 3. data_security_logs ===")
    rows = conn.execute(text("SELECT event_id, pii_detected, risk_score, data_in_mb, data_out_mb FROM data_security_logs")).fetchall()
    for r in rows:
        print(f"  {r[0][:12]}... | pii={r[1]} | risk={r[2]} | in={r[3]}MB | out={r[4]}MB")
    print(f"  Total: {len(rows)} rows\n")

    print("=== 4. alerts ===")
    rows = conn.execute(text("SELECT alert_type, severity, status, telemetry_id FROM alerts")).fetchall()
    for r in rows:
        print(f"  {r[0]} | {r[1]} | {r[2]} | telemetry_id={r[3]}")
    print(f"  Total: {len(rows)} rows\n")

    print("=== 5. daily_org_summary ===")
    rows = conn.execute(text("SELECT org_id, tool_name, date, total_events, total_cost, llm_cost, external_cost, infra_cost FROM daily_org_summary")).fetchall()
    for r in rows:
        print(f"  {r[0]} | {r[1]} | {r[2]} | events={r[3]} | total=${r[4]} | llm=${r[5]} | ext=${r[6]} | infra=${r[7]}")
    print(f"  Total: {len(rows)} rows")
