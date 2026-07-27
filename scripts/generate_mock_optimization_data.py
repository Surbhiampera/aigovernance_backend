"""One-off generator for synthetic ai_requests/token_usage/request_cost/ai_responses
rows, shaped to clear every optimization-tips rule's threshold
(app/services/optimization/rules/*, driven by app/workers/tasks.py::_generate_optimization_tips).

Run this ONLY against a disposable clone of the database (point DB_NAME, or
DATABASE_URL, at the clone before running) — never production. It reuses the
app's own ORM models and the real `_rebuild_daily_summary` aggregation function
so the generated data is internally consistent with what the live scheduler
would produce; it does not touch any rule/threshold logic.

It does NOT call `_generate_optimization_tips` itself. After running this,
trigger tip generation for real via:

    curl -X POST "http://localhost:8000/optimization-tips/admin/rebuild"

Everything is written under a dedicated synthetic org/project
(org_id="org-mock-optztips", project_id="proj-mock-optztips") so it never
mixes with real tenant data. Re-running this script adds another batch on top
(it does not delete previous runs).

Usage:
    python -m scripts.generate_mock_optimization_data
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from app.database import DATABASE_URL, SessionLocal
from app.models import (
    AiRequest,
    AiResponse,
    ModelDeployment,
    Organization,
    Project,
    RequestCost,
    TokenUsage,
)
from app.workers.tasks import _rebuild_daily_summary

ORG_ID = "org-mock-optztips"
PROJECT_ID = "proj-mock-optztips"

# Real gpt-4o catalogue rates (app/services/ai_model_pricing.py) — used so the
# model_substitution rule's recost-against-gpt-4o-mini comparison is genuine.
GPT4O_INPUT_RATE = Decimal("2.50")
GPT4O_OUTPUT_RATE = Decimal("10.00")

DAYS_BACK = 20  # spread synthetic traffic across the trailing N days

random.seed(20260725)  # deterministic dataset across re-runs


def _cost(input_tokens: int, output_tokens: int) -> tuple[Decimal, Decimal, Decimal]:
    input_cost = (Decimal(input_tokens) / Decimal(1_000_000) * GPT4O_INPUT_RATE).quantize(Decimal("0.000001"))
    output_cost = (Decimal(output_tokens) / Decimal(1_000_000) * GPT4O_OUTPUT_RATE).quantize(Decimal("0.000001"))
    return input_cost, output_cost, input_cost + output_cost


def _jitter(base: int, pct: float = 0.08) -> int:
    return max(1, int(base * random.uniform(1 - pct, 1 + pct)))


def _random_timestamp(days_back: int = DAYS_BACK) -> datetime:
    day_offset = random.randint(1, days_back)
    base = datetime.utcnow() - timedelta(days=day_offset)
    return base.replace(
        hour=random.randint(0, 23), minute=random.randint(0, 59),
        second=random.randint(0, 59), microsecond=0,
    )


def _seed_request(
    db, *, model_name: str, input_tokens: int, output_tokens: int,
    input_cost: Decimal, output_cost: Decimal, total_cost: Decimal,
    created_at: datetime, sanitized_prompt_text: str,
    system_tokens: int = 0, tool_definition_tokens: int = 0,
    finish_reason: str = "stop",
) -> str:
    request_id = f"req-mock-{uuid.uuid4().hex[:16]}"
    db.add(AiRequest(
        request_id=request_id, org_id=ORG_ID, project_id=PROJECT_ID,
        model_name=model_name, sanitized_prompt_text=sanitized_prompt_text,
        request_status="success", created_at=created_at, completed_at=created_at,
    ))
    db.add(TokenUsage(
        request_id=request_id, org_id=ORG_ID, project_id=PROJECT_ID,
        model_name=model_name, input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        system_tokens=system_tokens, tool_definition_tokens=tool_definition_tokens,
        created_at=created_at,
    ))
    db.add(RequestCost(
        request_id=request_id, org_id=ORG_ID, project_id=PROJECT_ID,
        model_name=model_name, input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_token_cost=input_cost, output_token_cost=output_cost, total_cost=total_cost,
        created_at=created_at,
    ))
    db.add(AiResponse(
        response_id=f"resp-mock-{uuid.uuid4().hex[:16]}", request_id=request_id,
        org_id=ORG_ID, project_id=PROJECT_ID, model_name=model_name,
        finish_reason=finish_reason, created_at=created_at,
    ))
    return request_id


def _seed_org_and_deployments(db) -> None:
    if not db.query(Organization).filter(Organization.id == ORG_ID).first():
        db.add(Organization(id=ORG_ID, org_name="Mock Org — Optimization Tips Demo"))
        db.add(Project(id=PROJECT_ID, org_id=ORG_ID, project_name="Mock Project — Optimization Tips Demo"))
        db.flush()  # org/project must exist before model_deployments FK-references them

    # gpt-4o-mini must be a *deployed* candidate for model_substitution to
    # consider it; gpt-4o is registered too for dashboard realism.
    for model_name, is_default in [("gpt-4o", True), ("gpt-4o-mini", False)]:
        exists = (
            db.query(ModelDeployment)
            .filter(ModelDeployment.org_id == ORG_ID, ModelDeployment.model_name == model_name)
            .first()
        )
        if not exists:
            db.add(ModelDeployment(
                deployment_id=f"dep-mock-{model_name}", org_id=ORG_ID, project_id=PROJECT_ID,
                provider="openai", model_name=model_name, deployment_name=model_name,
                endpoint_url="https://example.com", api_key="mock-key",
                is_default=is_default, is_active=True,
            ))
    db.flush()


def _seed_verbose_and_truncated(db, touched_dates: set) -> None:
    """Group 1 (80 rows, finish_reason=stop) + Group 2 (20 rows,
    finish_reason=length), both model=gpt-4o.

    Combined: 100 requests, long completions relative to prompt (~10-13x
    ratio) -> response_length. 20/100 = 20% truncation rate -> response_truncated.
    Real gpt-4o cost vs a recost at gpt-4o-mini rates -> model_substitution.
    """
    for _ in range(80):
        input_tokens = _jitter(700)
        output_tokens = _jitter(9000)
        ic, oc, tc = _cost(input_tokens, output_tokens)
        created_at = _random_timestamp()
        touched_dates.add(created_at.date())
        _seed_request(
            db, model_name="gpt-4o", input_tokens=input_tokens, output_tokens=output_tokens,
            input_cost=ic, output_cost=oc, total_cost=tc, created_at=created_at,
            sanitized_prompt_text=f"Explain the troubleshooting steps for ticket #{uuid.uuid4().hex[:8]}",
            finish_reason="stop",
        )

    for _ in range(20):
        input_tokens = _jitter(700)
        output_tokens = _jitter(9500)
        ic, oc, tc = _cost(input_tokens, output_tokens)
        created_at = _random_timestamp()
        touched_dates.add(created_at.date())
        _seed_request(
            db, model_name="gpt-4o", input_tokens=input_tokens, output_tokens=output_tokens,
            input_cost=ic, output_cost=oc, total_cost=tc, created_at=created_at,
            sanitized_prompt_text=f"Generate a full report covering ticket #{uuid.uuid4().hex[:8]}",
            finish_reason="length",
        )


def _seed_cache_and_oversized(db, touched_dates: set) -> None:
    """Group 3 (50 rows, identical sanitized_prompt_text, model=internal-doc-summarizer).

    A ~50k-token prompt (84% of it system_tokens) resent verbatim every call
    -> cache_opportunity (50 identical hits) and oversized_prompt
    (cause=system_prompt, since this project-wide rule ignores model_name and
    these rows dominate the p95 tail against the ~700-token gpt-4o traffic).

    Uses a model name outside app/services/ai_model_pricing.py's catalogue on
    purpose: model_substitution looks up pricing per RequestCost.model_name
    and skips anything with no catalogue entry, so this batch (huge prompt,
    tiny completion) can't also mis-fire a bogus "switch model" tip.
    """
    fixed_prompt = "SYSTEM CONTEXT (v3, do not modify): " + ("Company knowledge base excerpt. " * 400)
    input_cost = Decimal("0.125")
    output_cost = Decimal("0.003")
    total_cost = input_cost + output_cost
    for _ in range(50):
        input_tokens = _jitter(50_000, pct=0.02)
        output_tokens = _jitter(300, pct=0.10)
        system_tokens = int(input_tokens * 0.84)
        created_at = _random_timestamp()
        touched_dates.add(created_at.date())
        _seed_request(
            db, model_name="internal-doc-summarizer", input_tokens=input_tokens, output_tokens=output_tokens,
            input_cost=input_cost, output_cost=output_cost, total_cost=total_cost, created_at=created_at,
            sanitized_prompt_text=fixed_prompt, system_tokens=system_tokens, finish_reason="stop",
        )


def main() -> None:
    safe_target = DATABASE_URL.rsplit("@", 1)[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"Target database: {safe_target}")

    db = SessionLocal()
    try:
        _seed_org_and_deployments(db)

        touched_dates: set = set()
        _seed_verbose_and_truncated(db, touched_dates)
        _seed_cache_and_oversized(db, touched_dates)
        db.flush()

        print(f"Inserted 150 synthetic requests (org_id={ORG_ID}, project_id={PROJECT_ID}) "
              f"across {len(touched_dates)} distinct days.")

        for d in sorted(touched_dates):
            _rebuild_daily_summary(db=db, summary_date=d)
        db.commit()

        print(f"Rebuilt daily_org_summary for {len(touched_dates)} touched dates "
              "(this recomputes ALL orgs' summary rows for those calendar dates from "
              "ai_requests/request_cost — idempotent, matches the real hourly scheduler).")
        print("Done. Next: POST /optimization-tips/admin/rebuild against this DB to generate tips.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
