from app.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    print("\n-- telemetry_events --")
    rows = conn.execute(text(
        "SELECT event_id, model_name, provider, prompt_tokens, completion_tokens, "
        "total_tokens, latency_ms, llm_cost, total_cost, created_at "
        "FROM telemetry_events ORDER BY created_at DESC"
    )).fetchall()
    for r in rows:
        print(" ", dict(r._mapping))

    print("\n-- daily_org_summary --")
    rows = conn.execute(text(
        "SELECT org_id, project_id, tool_name, date, "
        "total_events, total_prompt_tokens, total_completion_tokens, total_tokens, total_cost "
        "FROM daily_org_summary ORDER BY date DESC"
    )).fetchall()
    for r in rows:
        print(" ", dict(r._mapping))

    print("\n-- project_model_usage --")
    rows = conn.execute(text(
        "SELECT org_id, project_id, model_name, date, "
        "call_count, total_prompt_tokens, total_completion_tokens, total_tokens, total_cost "
        "FROM project_model_usage ORDER BY date DESC"
    )).fetchall()
    for r in rows:
        print(" ", dict(r._mapping))
