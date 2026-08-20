"""Demo-tenant generator: a portfolio of realistically-named projects whose
traffic shapes each trip a *different* subset of the optimization-tips rules.

Unlike scripts/generate_mock_optimization_data.py (one flat "mock" project that
just proves every rule can fire), this seeds something you can put on a screen:
six products under one synthetic org, with per-user attribution, four providers,
failed/blocked requests, budgets and governance rules. Only the ORG is
synthetic — every project name is a plausible product, so the frontend can hide
the single org id and show nothing that reads as test data.

Run this ONLY against a disposable clone of the database (point DATABASE_URL at
the clone before running) — never production. Two reasons:
  * `--purge` deletes rows for this org.
  * the daily/monthly aggregation helpers it calls (the same ones the hourly
    scheduler uses) delete and recompute EVERY org's summary rows for the
    calendar dates touched.

It reuses the app's own code wherever the numbers have to agree with production:
costs come from app.services.cost_lookup.calculate_cost (the same lookup the
proxy bills with and the same one model_substitution recosts candidates with),
and the aggregates come from app.workers.tasks. It never touches rule or
threshold logic.

It does NOT write optimization_tips itself — it only prints what the rules
*would* produce. Generate them for real via:

    curl -X POST "http://localhost:8000/optimization-tips/admin/rebuild"

Usage:
    python3 -m scripts.generate_demo_org_data --purge
    python3 -m scripts.generate_demo_org_data --purge --seed 7 --days 30
"""
from __future__ import annotations

import argparse
import random
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, text

from app.database import DATABASE_URL, SessionLocal
from app.models import (
    AiRequest,
    AiResponse,
    AuditLog,
    Budget,
    GovernanceRule,
    ModelDeployment,
    Organization,
    Project,
    RequestCost,
    TokenUsage,
)
from app.services.cost_lookup import calculate_cost
from app.workers.tasks import (
    _detect_daily_anomalies,
    _rebuild_daily_summary,
    _rebuild_daily_user_summary,
)

ORG_ID = "org-demo-enterprise"
ORG_NAME = "Northwind Digital (Demo)"

DEFAULT_SEED = 20260813
DEFAULT_DAYS = 30  # must be >= TIP_WINDOW_DAYS or the rules see a partial window


# ---------------------------------------------------------------------------
# Deployment registry
#
# This list IS the model_substitution candidate pool: get_deployments_for_org()
# ignores project_id for membership, so anything registered here becomes a
# substitution candidate for EVERY project in the org. Adding a model cheaper
# than gpt-4o-mini would make the deliberately-quiet projects start firing.
# ---------------------------------------------------------------------------
DEPLOYMENTS: list[tuple[str, str, bool]] = [
    # (model_name, provider, is_default)
    ("gpt-4o", "openai", True),
    ("gpt-4o-mini", "openai", False),
    ("gpt-4", "openai", False),                    # legacy, deliberately expensive
    ("claude-sonnet-4-5", "anthropic", False),
    ("claude-haiku-4-5", "anthropic", False),
    ("gemini-2.5-pro", "google", False),
    ("gemini-2.5-flash", "google", False),         # price-tied with gpt-4o-mini
    ("codestral-2501", "mistral", False),          # only `code`-category model
    ("text-embedding-3-small", "openai", False),   # only `embedding`-category model
]

_PROVIDER_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "mistral": "https://api.mistral.ai/v1",
}


@dataclass
class Batch:
    """One traffic shape within a project."""

    model: str
    provider: str
    count: int
    input_tokens: tuple[int, int]           # uniform band (lo, hi)
    output_tokens: tuple[int, int]
    prompt_template: str = "Request {uid}"  # {uid} makes each prompt hash unique
    fixed_prompt: Optional[str] = None      # set => byte-identical rows (cache rule)
    system_share: float = 0.0               # fraction of input_tokens that is system prompt
    tool_def_share: float = 0.0
    truncation_rate: float = 0.0            # fraction with finish_reason="length"
    tool_name: Optional[str] = None


@dataclass
class ProjectSpec:
    project_id: str
    project_name: str
    environment: str
    batches: list[Batch]
    users: list[tuple[str, str, str, int]]  # (user_id, email, role, weight)
    expected_tips: set[str]
    budget_utilization: float               # target fraction of the monthly limit
    failure_rate: float = 0.03


# ---------------------------------------------------------------------------
# The portfolio. Every number below is chosen to clear (or miss) a specific
# rule gate — see the per-project `expected_tips` and the notes on each batch.
# Thresholds live in app/config.py (TIP_* env vars); defaults assumed here are
# 50 requests, $5/month savings, 3.0 output:input, 3.0 p95:median, 0.15
# truncation, 5 duplicate hits, 30-day window.
# ---------------------------------------------------------------------------
PROJECTS: list[ProjectSpec] = [
    ProjectSpec(
        project_id="proj-support-copilot",
        project_name="Customer Support Copilot",
        environment="production",
        batches=[
            # 18% of answers run into max_tokens -> response_truncated.
            # Short answers vs prompt (ratio 0.4) keep response_length quiet;
            # gpt-4o-mini is price-tied with gemini-2.5-flash so substitution
            # finds no cheaper candidate.
            Batch(
                model="gpt-4o-mini", provider="openai", count=820,
                input_tokens=(600, 1300), output_tokens=(300, 460),
                prompt_template="Customer asked about their order status. Ticket {uid}.",
                truncation_rate=0.18, tool_name="support_answer",
            ),
            # Escalations deliberately sit under the 50-request gate.
            Batch(
                model="gpt-4o", provider="openai", count=45,
                input_tokens=(600, 1300), output_tokens=(300, 460),
                prompt_template="Escalated ticket {uid} — summarise history and draft a reply.",
                tool_name="support_escalation",
            ),
        ],
        users=[
            ("u-priya-nair", "priya.nair@northwind.example", "support_agent", 9),
            ("u-daniel-oyelaran", "daniel.oyelaran@northwind.example", "support_agent", 8),
            ("u-mei-chen", "mei.chen@northwind.example", "support_agent", 4),
            ("u-tomas-vega", "tomas.vega@northwind.example", "support_agent", 3),
            ("u-hannah-ross", "hannah.ross@northwind.example", "analyst", 2),
            ("u-svc-support-bot", "support-bot@northwind.example", "service_account", 2),
        ],
        expected_tips={"response_truncated"},
        budget_utilization=0.35,
    ),
    ProjectSpec(
        project_id="proj-contract-review",
        project_name="Contract Review Assistant",
        environment="production",
        batches=[
            Batch(
                model="claude-sonnet-4-5", provider="anthropic", count=190,
                input_tokens=(3500, 4500), output_tokens=(800, 1000),
                prompt_template="Review the attached vendor agreement (matter {uid}) for unusual liability terms.",
                system_share=0.15, tool_name="contract_review",
            ),
            # 60 byte-identical 62k-token prompts: the duplicate hash drives
            # cache_opportunity, the size gap drives oversized_prompt, and the
            # 58% system share makes its cause "system_prompt".
            Batch(
                model="claude-sonnet-4-5", provider="anthropic", count=60,
                input_tokens=(62_000, 62_000), output_tokens=(650, 750),
                fixed_prompt=(
                    "STANDARD NDA CLAUSE CHECK (playbook v7, do not edit): "
                    + ("Legal policy corpus section. " * 120)
                ),
                system_share=0.58, tool_name="nda_clause_check",
            ),
        ],
        users=[
            ("u-alicia-fernandez", "alicia.fernandez@northwind.example", "legal_counsel", 9),
            ("u-rajesh-menon", "rajesh.menon@northwind.example", "legal_counsel", 7),
            ("u-clara-boateng", "clara.boateng@northwind.example", "analyst", 3),
            ("u-svc-contract-batch", "contract-batch@northwind.example", "service_account", 5),
            ("u-yusuf-demir", "yusuf.demir@northwind.example", "legal_counsel", 2),
        ],
        expected_tips={"oversized_prompt", "cache_opportunity", "model_substitution"},
        budget_utilization=0.85,
    ),
    ProjectSpec(
        project_id="proj-sales-outreach",
        project_name="Sales Email Generator",
        environment="production",
        batches=[
            # Tiny prompt, very long completion: ratio ~13 (>= 2x threshold, so
            # response_length lands at severity "high"), on an expensive model.
            Batch(
                model="gpt-4o", provider="openai", count=420,
                input_tokens=(300, 400), output_tokens=(4200, 5000),
                prompt_template="Write a cold outreach sequence for prospect {uid}.",
                tool_name="email_sequence",
            ),
        ],
        users=[
            ("u-jordan-whitfield", "jordan.whitfield@northwind.example", "sales_rep", 10),
            ("u-anika-shah", "anika.shah@northwind.example", "sales_rep", 8),
            ("u-marco-bellini", "marco.bellini@northwind.example", "sales_rep", 4),
            ("u-elena-petrova", "elena.petrova@northwind.example", "sales_rep", 3),
            ("u-owen-mcgrath", "owen.mcgrath@northwind.example", "marketer", 2),
        ],
        expected_tips={"response_length", "model_substitution"},
        budget_utilization=0.62,
    ),
    ProjectSpec(
        project_id="proj-knowledge-search",
        project_name="Internal Knowledge Search",
        environment="production",
        batches=[
            Batch(
                model="gemini-2.5-flash", provider="google", count=420,
                input_tokens=(1500, 2500), output_tokens=(280, 420),
                prompt_template="Answer from the handbook: question {uid}.",
                system_share=0.08, tool_name="rag_answer",
            ),
            # 70 identical nightly digests over a 120k-token context. System and
            # tool-definition shares are both under 0.3 on purpose, so
            # oversized_prompt attributes the bloat to conversation_history —
            # a deliberate contrast with contract-review's system_prompt cause.
            Batch(
                model="gemini-2.5-pro", provider="google", count=70,
                input_tokens=(120_000, 120_000), output_tokens=(1100, 1300),
                fixed_prompt=(
                    "NIGHTLY INDEX DIGEST (job kb-digest, deterministic prompt): "
                    + ("Knowledge base shard manifest entry. " * 100)
                ),
                system_share=0.067, tool_def_share=0.017, tool_name="kb_digest",
            ),
            # Only embedding-category deployment in the org, so substitution has
            # no candidate to compare it against.
            Batch(
                model="text-embedding-3-small", provider="openai", count=260,
                input_tokens=(600, 1000), output_tokens=(0, 0),
                prompt_template="embed: document chunk {uid}",
                tool_name="kb_index",
            ),
        ],
        users=[
            ("u-svc-kb-indexer", "kb-indexer@northwind.example", "service_account", 10),
            ("u-lena-hartmann", "lena.hartmann@northwind.example", "engineer", 6),
            ("u-david-okonkwo", "david.okonkwo@northwind.example", "analyst", 5),
            ("u-sofia-ramirez", "sofia.ramirez@northwind.example", "analyst", 4),
            ("u-kenji-watanabe", "kenji.watanabe@northwind.example", "engineer", 3),
            ("u-nadia-belkacem", "nadia.belkacem@northwind.example", "engineer", 2),
        ],
        expected_tips={"cache_opportunity", "oversized_prompt", "model_substitution"},
        budget_utilization=0.45,
    ),
    ProjectSpec(
        project_id="proj-code-review-bot",
        project_name="Code Review Bot",
        environment="production",
        # Deliberately clean: nothing fires. Ratio 0.3, narrow prompt band, no
        # duplicates, no truncation, and codestral is the only `code`-category
        # deployment so substitution has no candidate. This is the control case
        # showing the engine isn't just noise.
        batches=[
            Batch(
                model="codestral-2501", provider="mistral", count=330,
                input_tokens=(3000, 4200), output_tokens=(950, 1250),
                prompt_template="Review the diff on PR {uid} and flag correctness risks.",
                system_share=0.10, tool_name="pr_review",
            ),
        ],
        users=[
            ("u-svc-ci-reviewer", "ci-reviewer@northwind.example", "service_account", 10),
            ("u-arjun-krishnan", "arjun.krishnan@northwind.example", "engineer", 6),
            ("u-freya-lindqvist", "freya.lindqvist@northwind.example", "engineer", 5),
            ("u-paulo-cardoso", "paulo.cardoso@northwind.example", "engineer", 3),
            ("u-grace-mbeki", "grace.mbeki@northwind.example", "engineer", 2),
        ],
        expected_tips=set(),
        budget_utilization=0.30,
    ),
    ProjectSpec(
        project_id="proj-marketing-studio",
        project_name="Marketing Content Studio",
        environment="production",
        batches=[
            # Still on legacy gpt-4 at $30/$60 per 1M. Healthy prompt/response
            # shape, so the ONLY thing wrong is the model choice — and it is the
            # largest single saving in the dataset.
            Batch(
                model="gpt-4", provider="openai", count=300,
                input_tokens=(1800, 2200), output_tokens=(700, 900),
                prompt_template="Draft launch copy for campaign {uid}.",
                system_share=0.12, tool_name="campaign_copy",
            ),
        ],
        users=[
            ("u-isabelle-moreau", "isabelle.moreau@northwind.example", "marketer", 9),
            ("u-tunde-adeyemi", "tunde.adeyemi@northwind.example", "marketer", 7),
            ("u-chloe-kim", "chloe.kim@northwind.example", "marketer", 4),
            ("u-ravi-suresh", "ravi.suresh@northwind.example", "analyst", 2),
        ],
        expected_tips={"model_substitution"},
        budget_utilization=1.12,  # deliberately over budget
    ),
]

# (failure_code, request_status, http-ish reason) — mirrors what the proxy's
# _mark_request_failed / _store_blocked_request paths actually write.
FAILURE_MODES = [
    ("pii_blocked", "blocked", "Request blocked: PII policy (AADHAAR) set to block."),
    ("rate_limited", "blocked", "Rate limit exceeded for governance key (60 req/min)."),
    ("budget_exceeded", "blocked", "Monthly project budget exhausted."),
    ("upstream_error", "failed", "Upstream provider returned 502 after 3 attempts."),
    ("upstream_timeout", "failed", "Upstream provider timed out after 60s."),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_rate_cache: dict[tuple[str, str], tuple[Decimal, Decimal, str, dict]] = {}


def _rate_card(db, model: str, provider: str) -> tuple[Decimal, Decimal, str, dict]:
    """Per-1M input/output rates, derived from calculate_cost itself.

    Calling calculate_cost per row would mean ~3k pairs of pricing queries, so
    we call it once per (model, provider) with 1M/1M tokens and reuse the
    resulting rates. Cost is linear in token count in every branch of that
    function, so this is the same answer, memoised.
    """
    key = (model, provider)
    if key not in _rate_cache:
        in_cost, out_cost, _, _, snapshot, version = calculate_cost(
            db=db, model=model, provider=provider,
            input_tokens=1_000_000, output_tokens=1_000_000,
        )
        _rate_cache[key] = (in_cost, out_cost, version, snapshot)
    return _rate_cache[key]


def _cost_for(db, model: str, provider: str, input_tokens: int, output_tokens: int):
    in_rate, out_rate, version, snapshot = _rate_card(db, model, provider)
    input_cost = (Decimal(input_tokens) / Decimal(1_000_000) * in_rate).quantize(Decimal("0.00000001"))
    output_cost = (Decimal(output_tokens) / Decimal(1_000_000) * out_rate).quantize(Decimal("0.00000001"))
    return input_cost, output_cost, input_cost + output_cost, version, snapshot


def _random_timestamp(rng: random.Random, days: int) -> datetime:
    """A moment on one of the trailing `days` full days (never today).

    Business hours are weighted so the hourly charts aren't flat.
    """
    day_offset = rng.randint(1, days)
    base = datetime.utcnow() - timedelta(days=day_offset)
    hour = rng.choice([8, 9, 9, 10, 10, 11, 11, 12, 13, 14, 14, 15, 15, 16, 17, 19, 22, 3])
    return base.replace(
        hour=hour, minute=rng.randint(0, 59), second=rng.randint(0, 59), microsecond=0
    )


_user_pool: dict[str, tuple[list, list]] = {}


def _pick_user(rng: random.Random, spec: ProjectSpec) -> tuple[str, str, str]:
    """Weighted pick — a couple of power users carry most of a project's traffic,
    because a flat distribution makes /costs/by-user look obviously generated."""
    if spec.project_id not in _user_pool:
        _user_pool[spec.project_id] = (
            [u[:3] for u in spec.users], [u[3] for u in spec.users]
        )
    users, weights = _user_pool[spec.project_id]
    return rng.choices(users, weights=weights, k=1)[0]


def _seed_org(db) -> None:
    if not db.query(Organization).filter(Organization.id == ORG_ID).first():
        db.add(Organization(id=ORG_ID, org_name=ORG_NAME, plan_type="enterprise"))
    for spec in PROJECTS:
        if not db.query(Project).filter(Project.id == spec.project_id).first():
            db.add(Project(
                id=spec.project_id, org_id=ORG_ID,
                project_name=spec.project_name, environment=spec.environment,
            ))
    db.flush()  # org/project rows must exist before model_deployments FK-references them

    for model_name, provider, is_default in DEPLOYMENTS:
        deployment_id = f"dep-{ORG_ID}-{model_name}"
        if db.query(ModelDeployment).filter(ModelDeployment.deployment_id == deployment_id).first():
            continue
        db.add(ModelDeployment(
            deployment_id=deployment_id, org_id=ORG_ID, project_id=None,
            provider=provider, model_name=model_name, deployment_name=model_name,
            endpoint_url=_PROVIDER_ENDPOINTS[provider],
            # get_deployments_for_org() drops rows without BOTH an api_key and an
            # endpoint_url, and model_substitution's candidate pool comes from it.
            api_key=f"demo-not-a-real-key-{model_name}",
            api_version="2025-01-01-preview" if provider == "openai" else None,
            is_default=is_default, is_active=True,
        ))
    db.flush()


def _seed_governance_rules(db) -> None:
    """Two org-scoped rules, for the governance dashboard.

    Neither model named here is in DEPLOYMENTS on purpose: a blocked_model is
    stripped out of model_substitution's candidate pool, so blocking a deployed
    model would silently change which tips fire.

    governance_rules.rule_name is globally unique across all orgs, hence the
    org-id prefix.
    """
    rules = [
        dict(
            rule_name=f"{ORG_ID}:block-gpt-35-turbo", metric_name="blocked_model",
            operator="==", threshold_value=Decimal("0"), severity="high",
            scope_level="organization", scope_reference="gpt-3.5-turbo",
            description="Retired model — routes must move to gpt-4o-mini.",
        ),
        dict(
            rule_name=f"{ORG_ID}:max-input-tokens", metric_name="max_input_tokens",
            operator=">", threshold_value=Decimal("150000"), severity="medium",
            scope_level="organization", scope_reference=None,
            description="Reject prompts above 150k input tokens.",
        ),
    ]
    for r in rules:
        if db.query(GovernanceRule).filter(GovernanceRule.rule_name == r["rule_name"]).first():
            continue
        db.add(GovernanceRule(org_id=ORG_ID, project_id=None, is_active=True, **r))
    db.flush()


def _seed_batch(db, rng: random.Random, spec: ProjectSpec, batch: Batch, days: int,
                touched: set[date]) -> int:
    """Write AiRequest + TokenUsage + RequestCost + AiResponse for one shape."""
    truncated_target = int(round(batch.count * batch.truncation_rate))
    truncated_flags = [True] * truncated_target + [False] * (batch.count - truncated_target)
    rng.shuffle(truncated_flags)

    for i in range(batch.count):
        uid = uuid.uuid4().hex[:10]
        input_tokens = rng.randint(*batch.input_tokens)
        is_truncated = truncated_flags[i]
        output_tokens = (
            batch.output_tokens[1] if is_truncated else rng.randint(*batch.output_tokens)
        )
        system_tokens = int(input_tokens * batch.system_share)
        tool_def_tokens = int(input_tokens * batch.tool_def_share)

        prompt = batch.fixed_prompt or batch.prompt_template.format(uid=uid)
        created_at = _random_timestamp(rng, days)
        touched.add(created_at.date())
        latency_ms = rng.randint(400, 2600) + output_tokens // 4
        completed_at = created_at + timedelta(milliseconds=latency_ms)

        user_id, user_email, user_role = _pick_user(rng, spec)
        request_id = f"req-{uuid.uuid4().hex[:20]}"
        response_id = f"resp-{uuid.uuid4().hex[:20]}"
        input_cost, output_cost, total_cost, pricing_version, pricing_snapshot = _cost_for(
            db, batch.model, batch.provider, input_tokens, output_tokens
        )

        db.add(AiRequest(
            request_id=request_id, org_id=ORG_ID, project_id=spec.project_id,
            request_type="embedding" if output_tokens == 0 else "chat_completion",
            request_status="success",
            user_id=user_id, user_email=user_email, user_role=user_role,
            provider=batch.provider, model_name=batch.model,
            requested_model=batch.model, routed_model=batch.model,
            deployment_name=batch.model, tool_name=batch.tool_name,
            sanitized_prompt_text=prompt, prompt_char_count=len(prompt),
            input_token_estimate=input_tokens, num_messages=rng.randint(1, 6),
            has_system_prompt=system_tokens > 0, has_tool_definitions=tool_def_tokens > 0,
            trace_id=f"trace-{uuid.uuid4().hex[:16]}",
            entry_point="/proxy/chat/completions",
            created_at=created_at, received_at=created_at, completed_at=completed_at,
        ))
        db.add(AiResponse(
            response_id=response_id, request_id=request_id, org_id=ORG_ID,
            project_id=spec.project_id, provider=batch.provider, model_name=batch.model,
            response_status="success",
            finish_reason="length" if is_truncated else "stop",
            output_char_count=output_tokens * 4, latency_ms=latency_ms,
            response_started_at=created_at, response_completed_at=completed_at,
            created_at=created_at,
        ))
        db.add(TokenUsage(
            token_usage_id=f"tu-{uuid.uuid4().hex[:20]}",
            request_id=request_id, response_id=response_id, org_id=ORG_ID,
            project_id=spec.project_id, provider=batch.provider, model_name=batch.model,
            prompt_tokens=input_tokens, completion_tokens=output_tokens,
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            uncached_tokens=input_tokens,
            system_tokens=system_tokens, tool_definition_tokens=tool_def_tokens,
            input_token_source="provider", output_token_source="provider",
            created_at=created_at,
        ))
        db.add(RequestCost(
            cost_id=f"cu-{uuid.uuid4().hex[:20]}",
            request_id=request_id, response_id=response_id, org_id=ORG_ID,
            project_id=spec.project_id, provider=batch.provider, model_name=batch.model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            input_token_cost=input_cost, output_token_cost=output_cost,
            llm_cost=total_cost, total_cost=total_cost, adjusted_total_cost=total_cost,
            currency="USD", pricing_version=pricing_version, pricing_snapshot=pricing_snapshot,
            cost_model_type="token", created_at=created_at,
        ))
    db.flush()
    return batch.count


def _seed_failures(db, rng: random.Random, spec: ProjectSpec, days: int,
                   touched: set[date]) -> int:
    """Blocked/failed requests: an AiRequest row and an audit row, nothing else.

    Matches the proxy's _mark_request_failed semantics — no tokens were
    consumed, so no TokenUsage and no RequestCost. The model_name is always one
    that also has successful traffic in this project: _rebuild_daily_summary
    builds its rows from RequestCost, so an (org, project, model) tuple with
    only failures would produce no summary row at all and vanish.
    """
    total_success = sum(b.count for b in spec.batches)
    n = int(round(total_success * spec.failure_rate))
    models = [(b.model, b.provider) for b in spec.batches]

    for _ in range(n):
        failure_code, status, reason = rng.choice(FAILURE_MODES)
        model, provider = rng.choice(models)
        created_at = _random_timestamp(rng, days)
        touched.add(created_at.date())
        user_id, user_email, user_role = _pick_user(rng, spec)
        request_id = f"req-{uuid.uuid4().hex[:20]}"

        db.add(AiRequest(
            request_id=request_id, org_id=ORG_ID, project_id=spec.project_id,
            request_type="chat_completion", request_status=status,
            failure_code=failure_code, failure_reason=reason,
            user_id=user_id, user_email=user_email, user_role=user_role,
            provider=provider, model_name=model, requested_model=model,
            pii_detected=failure_code == "pii_blocked",
            pii_action_taken="block" if failure_code == "pii_blocked" else None,
            pii_types=["AADHAAR"] if failure_code == "pii_blocked" else None,
            trace_id=f"trace-{uuid.uuid4().hex[:16]}",
            entry_point="/proxy/chat/completions",
            created_at=created_at, received_at=created_at,
            completed_at=created_at + timedelta(milliseconds=rng.randint(10, 400)),
        ))
        # Built directly rather than via audit_service.log_event(): that helper
        # stamps occurred_at=utcnow() with no override, which would pile every
        # synthetic audit row onto today instead of the day it belongs to.
        db.add(AuditLog(
            audit_id=f"aud-{uuid.uuid4().hex[:20]}",
            org_id=ORG_ID, project_id=spec.project_id,
            actor_type="system", actor_id=user_id, actor_email=user_email,
            audit_category="security" if failure_code == "pii_blocked" else "governance",
            audit_action="blocked" if status == "blocked" else "request_failed",
            audit_status="blocked" if status == "blocked" else "failure",
            entity_type="ai_request", entity_id=request_id, request_id=request_id,
            policy_triggered=status == "blocked",
            compliance_relevant=failure_code == "pii_blocked",
            change_summary=reason,
            audit_metadata={"failure_code": failure_code, "model": model},
            occurred_at=created_at, created_at=created_at,
        ))
    db.flush()
    return n


def _seed_budgets(db) -> list[tuple[str, Decimal, Decimal, float]]:
    """Size monthly limits off actual current-calendar-month spend.

    budget_service measures the calendar month, not the rolling window — and a
    30-day lookback from today straddles the month boundary, so only part of the
    seeded spend counts. Deriving the limits from a live query is the only way
    the utilisation percentages come out where we want them.
    """
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = []
    org_total = Decimal("0")

    for spec in PROJECTS:
        spend = db.query(
            func.coalesce(func.sum(RequestCost.total_cost), 0)
        ).filter(
            RequestCost.org_id == ORG_ID,
            RequestCost.project_id == spec.project_id,
            RequestCost.created_at >= month_start,
        ).scalar() or Decimal("0")
        spend = Decimal(str(spend))
        org_total += spend

        limit = (spend / Decimal(str(spec.budget_utilization))).quantize(Decimal("0.01")) \
            if spend > 0 else Decimal("10.00")
        budget = (
            db.query(Budget)
            .filter(Budget.org_id == ORG_ID, Budget.project_id == spec.project_id)
            .first()
        )
        if budget:
            budget.limit_amount = limit
            budget.alert_threshold_percent = 80
        else:
            db.add(Budget(
                org_id=ORG_ID, project_id=spec.project_id, budget_type="monthly",
                limit_amount=limit, alert_threshold_percent=80,
            ))
        pct = float(spend / limit * 100) if limit > 0 else 0.0
        rows.append((spec.project_name, spend, limit, pct))

    org_limit = (org_total / Decimal("0.58")).quantize(Decimal("0.01")) if org_total > 0 \
        else Decimal("100.00")
    org_budget = (
        db.query(Budget).filter(Budget.org_id == ORG_ID, Budget.project_id.is_(None)).first()
    )
    if org_budget:
        org_budget.limit_amount = org_limit
        org_budget.alert_threshold_percent = 80
    else:
        db.add(Budget(
            org_id=ORG_ID, project_id=None, budget_type="monthly",
            limit_amount=org_limit, alert_threshold_percent=80,
        ))
    db.flush()
    rows.append(("(org total)", org_total, org_limit,
                 float(org_total / org_limit * 100) if org_limit > 0 else 0.0))
    return rows


def _purge(db) -> None:
    """Drop this org's traffic so a re-run is not additive.

    Required, unlike the older mock script: re-running would double every
    duplicate-prompt hit count and every cost total, moving the cache and
    substitution numbers. Org/project/deployment/budget/rule rows are kept and
    upserted.
    """
    project_ids = [s.project_id for s in PROJECTS]
    params = {"org_id": ORG_ID}
    # request_cost / token_usage / ai_responses cascade off ai_requests, but
    # delete explicitly so the script works even if FKs are relaxed.
    for table in ("request_cost", "token_usage", "ai_responses", "ai_requests",
                  "audit_logs", "optimization_tips", "alerts", "usage_anomalies"):
        db.execute(text(f"DELETE FROM {table} WHERE org_id = :org_id"), params)
    db.execute(
        text("DELETE FROM daily_user_usage WHERE org_id = :org_id"), params
    )
    db.execute(
        text("DELETE FROM daily_org_summary WHERE org_id = :org_id"), params
    )
    db.execute(
        text("DELETE FROM monthly_org_summary WHERE org_id = :org_id"), params
    )
    db.flush()
    print(f"Purged prior demo traffic for {ORG_ID} ({len(project_ids)} projects).")


def _report_expected_vs_actual(db, window_start: date, window_end: date) -> bool:
    """Run the real rules read-only and diff against each project's expectation.

    The numbers in this file are calculated, not measured — token jitter can
    push a borderline case across a threshold. This is the acceptance test.
    """
    from app.services.optimization import rules  # noqa: F401  (registers the rules)
    from app.services.optimization.registry import tip_registry

    print("\nExpected vs actual tips (rules run read-only against the seeded rows):")
    print(f"{'project':<32} {'expected':<44} {'actual':<44} ok")
    all_ok = True
    for spec in PROJECTS:
        actual: dict[str, Decimal] = {}
        for rule in tip_registry.all():
            for tip in rule.evaluate(
                db=db, org_id=ORG_ID, project_id=spec.project_id,
                window_start=window_start, window_end=window_end,
            ):
                actual[tip["tip_type"]] = actual.get(tip["tip_type"], Decimal("0")) + Decimal(
                    str(tip.get("estimated_monthly_savings") or 0)
                )
        ok = set(actual) == spec.expected_tips
        all_ok = all_ok and ok
        exp = ", ".join(sorted(spec.expected_tips)) or "(none)"
        act = ", ".join(f"{k} ${v:.2f}" for k, v in sorted(actual.items())) or "(none)"
        print(f"{spec.project_name:<32} {exp:<44} {act:<44} {'OK' if ok else 'MISMATCH'}")
    return all_ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--purge", action="store_true",
                        help="delete this org's existing traffic first (recommended; "
                             "the script is not idempotent without it)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help=f"RNG seed (default {DEFAULT_SEED})")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"spread traffic across the trailing N days (default {DEFAULT_DAYS})")
    args = parser.parse_args()

    safe_target = DATABASE_URL.rsplit("@", 1)[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"Target database: {safe_target}")
    print(f"Org: {ORG_ID} ({ORG_NAME})  seed={args.seed}  days={args.days}")

    rng = random.Random(args.seed)
    db = SessionLocal()
    try:
        if args.purge:
            _purge(db)

        _seed_org(db)
        _seed_governance_rules(db)

        touched: set[date] = set()
        total_success = 0
        total_failed = 0
        for spec in PROJECTS:
            n = sum(_seed_batch(db, rng, spec, b, args.days, touched) for b in spec.batches)
            f = _seed_failures(db, rng, spec, args.days, touched)
            total_success += n
            total_failed += f
            print(f"  {spec.project_name:<32} {n:>5} success  {f:>3} failed/blocked")

        print(f"\nInserted {total_success} successful and {total_failed} failed/blocked "
              f"requests across {len(touched)} distinct days.")

        for d in sorted(touched):
            _rebuild_daily_summary(db=db, summary_date=d)
            _rebuild_daily_user_summary(db=db, summary_date=d)
            _detect_daily_anomalies(db=db, summary_date=d)
        print(f"Rebuilt daily_org_summary / daily_user_usage / usage_anomalies for "
              f"{len(touched)} dates (recomputes ALL orgs' rows for those calendar "
              f"dates — idempotent, matches the hourly scheduler).")

        budget_rows = _seed_budgets(db)
        print("\nBudgets (current calendar month):")
        for name, spend, limit, pct in budget_rows:
            print(f"  {name:<32} ${float(spend):>9.2f} / ${float(limit):>9.2f}  {pct:>6.1f}%")

        # Mirror _generate_optimization_tips exactly: it always uses
        # TIP_WINDOW_DAYS, regardless of how far back this script spread traffic.
        from app.config import get_tip_window_days

        window_end = date.today()
        window_start = window_end - timedelta(days=get_tip_window_days())
        ok = _report_expected_vs_actual(db, window_start, window_end)

        db.commit()
        print("\nCommitted." if ok else "\nCommitted, but the tip set does not match "
                                        "expectations — retune the batch shapes above.")
        print("Next: POST /optimization-tips/admin/rebuild against this DB to persist tips.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
