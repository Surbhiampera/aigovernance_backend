"""Optimization tips engine — app/services/optimization/rules/* and the
scheduled job app/workers/tasks.py::_generate_optimization_tips.

Every test seeds AiRequest/TokenUsage/RequestCost/AiResponse/DailyOrgSummary
rows for a synthetic org via the db_session fixture (rolled back on teardown,
see tests/conftest.py) and calls one rule's evaluate() directly — no HTTP
layer involved. Thresholds are patched down to small numbers so each test
only needs a handful of rows instead of the real TIP_MIN_REQUESTS=50 default.

Run:
    python -m pytest tests/test_optimization_tips.py -v
"""
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from app.models import (
    AiRequest,
    AiResponse,
    DailyOrgSummary,
    ModelDeployment,
    Organization,
    OptimizationTip,
    Project,
    RequestCost,
    TokenUsage,
)
from app.services.optimization.registry import TipRule
from app.services.optimization.rules.cache_opportunity import CacheOpportunityRule
from app.services.optimization.rules.model_substitution import ModelSubstitutionRule
from app.services.optimization.rules.oversized_prompt import OversizedPromptRule
from app.services.optimization.rules.response_length import ResponseLengthRule
from app.services.optimization.rules.response_truncated import ResponseTruncatedRule
from app.workers.tasks import _generate_optimization_tips


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_org(db, org_id: str, project_id: str) -> None:
    db.add(Organization(id=org_id, org_name=f"Org {org_id}"))
    db.add(Project(id=project_id, org_id=org_id, project_name=f"Project {project_id}"))
    db.flush()


def _seed_request(
    db, *, org_id: str, project_id: str, model_name: str,
    input_tokens: int, output_tokens: int, total_cost: str,
    created_at: datetime, sanitized_prompt_text: str = None,
    system_tokens: int = 0, tool_definition_tokens: int = 0,
    finish_reason: str = "stop",
) -> str:
    request_id = f"req-{uuid.uuid4().hex[:16]}"
    db.add(AiRequest(
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        model_name=model_name,
        sanitized_prompt_text=sanitized_prompt_text,
        request_status="success",
        created_at=created_at,
    ))
    db.add(TokenUsage(
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        model_name=model_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        system_tokens=system_tokens,
        tool_definition_tokens=tool_definition_tokens,
        created_at=created_at,
    ))
    db.add(RequestCost(
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        model_name=model_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_token_cost=Decimal(total_cost),
        total_cost=Decimal(total_cost),
        created_at=created_at,
    ))
    db.add(AiResponse(
        response_id=f"resp-{uuid.uuid4().hex[:16]}",
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        model_name=model_name,
        finish_reason=finish_reason,
        created_at=created_at,
    ))
    db.flush()
    return request_id


def _window():
    window_end = date.today()
    window_start = window_end - timedelta(days=7)
    return window_start, window_end


# ---------------------------------------------------------------------------
# Rule 1 — response_length
# ---------------------------------------------------------------------------

def test_response_length_rule_fires_above_threshold(db_session):
    org_id, project_id = "org-tip-rl1", "proj-tip-rl1"
    _seed_org(db_session, org_id, project_id)
    window_start, window_end = _window()

    db_session.add(DailyOrgSummary(
        org_id=org_id, project_id=project_id, tool_name="gpt-4o",
        date=window_end, total_events=10,
        total_prompt_tokens=1000, total_completion_tokens=5000, total_tokens=6000,
    ))
    db_session.flush()

    with patch("app.services.optimization.rules.response_length.get_tip_min_requests", return_value=5), \
         patch("app.services.optimization.rules.response_length.get_tip_min_monthly_savings", return_value=Decimal("0.01")):
        tips = ResponseLengthRule().evaluate(
            db=db_session, org_id=org_id, project_id=project_id,
            window_start=window_start, window_end=window_end,
        )

    assert len(tips) == 1
    assert tips[0]["tip_type"] == "response_length"
    assert tips[0]["model_name"] == "gpt-4o"
    assert tips[0]["evidence_json"]["rule"] == "response_length"


def test_response_length_rule_silent_below_threshold(db_session):
    org_id, project_id = "org-tip-rl2", "proj-tip-rl2"
    _seed_org(db_session, org_id, project_id)
    window_start, window_end = _window()

    # ratio 1.5x — well under the 3.0x default threshold
    db_session.add(DailyOrgSummary(
        org_id=org_id, project_id=project_id, tool_name="gpt-4o",
        date=window_end, total_events=10,
        total_prompt_tokens=1000, total_completion_tokens=1500, total_tokens=2500,
    ))
    db_session.flush()

    with patch("app.services.optimization.rules.response_length.get_tip_min_requests", return_value=5):
        tips = ResponseLengthRule().evaluate(
            db=db_session, org_id=org_id, project_id=project_id,
            window_start=window_start, window_end=window_end,
        )

    assert tips == []


# ---------------------------------------------------------------------------
# Rule 2 — model_substitution
# ---------------------------------------------------------------------------

def test_model_substitution_rule_fires_for_cheaper_deployed_model(db_session):
    org_id, project_id = "org-tip-ms1", "proj-tip-ms1"
    _seed_org(db_session, org_id, project_id)
    window_start, window_end = _window()
    now = datetime.utcnow()

    # gpt-4 is expensive ($30/$60 per 1M); gpt-4o-mini is deployed and much
    # cheaper ($0.15/$0.60 per 1M), same "chat" category, ample context window.
    _seed_request(
        db_session, org_id=org_id, project_id=project_id, model_name="gpt-4",
        input_tokens=100_000, output_tokens=20_000, total_cost="4.20",
        created_at=now,
    )
    db_session.add(ModelDeployment(
        deployment_id=f"dep-{uuid.uuid4().hex[:12]}",
        org_id=org_id, project_id=project_id,
        provider="openai", model_name="gpt-4o-mini", deployment_name="gpt-4o-mini",
        endpoint_url="https://example.com", api_key="test-key", is_active=True,
    ))
    db_session.flush()

    with patch("app.services.optimization.rules.model_substitution.get_tip_min_monthly_savings", return_value=Decimal("0.01")):
        tips = ModelSubstitutionRule().evaluate(
            db=db_session, org_id=org_id, project_id=project_id,
            window_start=window_start, window_end=window_end,
        )

    assert len(tips) == 1
    tip = tips[0]
    assert tip["tip_type"] == "model_substitution"
    assert tip["model_name"] == "gpt-4"
    assert tip["evidence_json"]["params"]["to_model"] == "gpt-4o-mini"
    assert tip["estimated_monthly_savings"] > 0


def test_model_substitution_rule_silent_with_no_cheaper_deployment(db_session):
    org_id, project_id = "org-tip-ms2", "proj-tip-ms2"
    _seed_org(db_session, org_id, project_id)
    window_start, window_end = _window()
    now = datetime.utcnow()

    _seed_request(
        db_session, org_id=org_id, project_id=project_id, model_name="gpt-4",
        input_tokens=100_000, output_tokens=20_000, total_cost="4.20",
        created_at=now,
    )
    db_session.flush()

    # No deployed candidates at all (patched directly rather than relying on
    # an empty ModelDeployment table, since get_deployments_for_org() falls
    # back to whatever this environment's .env deployment vars resolve to).
    with patch("app.services.optimization.rules.model_substitution.get_deployments_for_org", return_value=[]):
        tips = ModelSubstitutionRule().evaluate(
            db=db_session, org_id=org_id, project_id=project_id,
            window_start=window_start, window_end=window_end,
        )

    assert tips == []


# ---------------------------------------------------------------------------
# Rule 3 — oversized_prompt
# ---------------------------------------------------------------------------

def test_oversized_prompt_rule_fires_and_attributes_tool_defs(db_session):
    org_id, project_id = "org-tip-op1", "proj-tip-op1"
    _seed_org(db_session, org_id, project_id)
    window_start, window_end = _window()
    now = datetime.utcnow()

    for _ in range(9):
        _seed_request(
            db_session, org_id=org_id, project_id=project_id, model_name="gpt-4o",
            input_tokens=100, output_tokens=20, total_cost="0.001", created_at=now,
        )
    # One outlier dominated by tool-definition tokens.
    _seed_request(
        db_session, org_id=org_id, project_id=project_id, model_name="gpt-4o",
        input_tokens=2000, output_tokens=50, total_cost="0.01", created_at=now,
        tool_definition_tokens=1600,
    )
    db_session.flush()

    with patch("app.services.optimization.rules.oversized_prompt.get_tip_min_requests", return_value=5):
        tips = OversizedPromptRule().evaluate(
            db=db_session, org_id=org_id, project_id=project_id,
            window_start=window_start, window_end=window_end,
        )

    assert len(tips) == 1
    assert tips[0]["tip_type"] == "oversized_prompt"
    assert tips[0]["evidence_json"]["params"]["cause"] == "tool_definitions"


def test_oversized_prompt_rule_silent_when_uniform(db_session):
    org_id, project_id = "org-tip-op2", "proj-tip-op2"
    _seed_org(db_session, org_id, project_id)
    window_start, window_end = _window()
    now = datetime.utcnow()

    for _ in range(10):
        _seed_request(
            db_session, org_id=org_id, project_id=project_id, model_name="gpt-4o",
            input_tokens=100, output_tokens=20, total_cost="0.001", created_at=now,
        )
    db_session.flush()

    with patch("app.services.optimization.rules.oversized_prompt.get_tip_min_requests", return_value=5):
        tips = OversizedPromptRule().evaluate(
            db=db_session, org_id=org_id, project_id=project_id,
            window_start=window_start, window_end=window_end,
        )

    assert tips == []


# ---------------------------------------------------------------------------
# Rule 4 — response_truncated
# ---------------------------------------------------------------------------

def test_response_truncated_rule_fires_above_threshold(db_session):
    org_id, project_id = "org-tip-rt1", "proj-tip-rt1"
    _seed_org(db_session, org_id, project_id)
    window_start, window_end = _window()
    now = datetime.utcnow()

    for _ in range(3):
        _seed_request(
            db_session, org_id=org_id, project_id=project_id, model_name="gpt-4o",
            input_tokens=100, output_tokens=500, total_cost="0.01", created_at=now,
            finish_reason="length",
        )
    for _ in range(2):
        _seed_request(
            db_session, org_id=org_id, project_id=project_id, model_name="gpt-4o",
            input_tokens=100, output_tokens=50, total_cost="0.001", created_at=now,
            finish_reason="stop",
        )
    db_session.flush()

    with patch("app.services.optimization.rules.response_truncated.get_tip_min_requests", return_value=5):
        tips = ResponseTruncatedRule().evaluate(
            db=db_session, org_id=org_id, project_id=project_id,
            window_start=window_start, window_end=window_end,
        )

    assert len(tips) == 1
    assert tips[0]["tip_type"] == "response_truncated"
    assert tips[0]["evidence_json"]["params"]["truncated_count"] == 3


def test_response_truncated_rule_silent_below_threshold(db_session):
    org_id, project_id = "org-tip-rt2", "proj-tip-rt2"
    _seed_org(db_session, org_id, project_id)
    window_start, window_end = _window()
    now = datetime.utcnow()

    for _ in range(5):
        _seed_request(
            db_session, org_id=org_id, project_id=project_id, model_name="gpt-4o",
            input_tokens=100, output_tokens=50, total_cost="0.001", created_at=now,
            finish_reason="stop",
        )
    db_session.flush()

    with patch("app.services.optimization.rules.response_truncated.get_tip_min_requests", return_value=5):
        tips = ResponseTruncatedRule().evaluate(
            db=db_session, org_id=org_id, project_id=project_id,
            window_start=window_start, window_end=window_end,
        )

    assert tips == []


# ---------------------------------------------------------------------------
# Rule 5 — cache_opportunity
# ---------------------------------------------------------------------------

def test_cache_opportunity_rule_fires_for_repeated_prompt(db_session):
    org_id, project_id = "org-tip-co1", "proj-tip-co1"
    _seed_org(db_session, org_id, project_id)
    window_start, window_end = _window()
    now = datetime.utcnow()

    for _ in range(4):
        _seed_request(
            db_session, org_id=org_id, project_id=project_id, model_name="gpt-4o",
            input_tokens=200, output_tokens=50, total_cost="0.05", created_at=now,
            sanitized_prompt_text="Summarize this exact same document please.",
        )
    db_session.flush()

    with patch("app.services.optimization.rules.cache_opportunity.get_tip_duplicate_min_hits", return_value=3), \
         patch("app.services.optimization.rules.cache_opportunity.get_tip_min_monthly_savings", return_value=Decimal("0.001")):
        tips = CacheOpportunityRule().evaluate(
            db=db_session, org_id=org_id, project_id=project_id,
            window_start=window_start, window_end=window_end,
        )

    assert len(tips) == 1
    tip = tips[0]
    assert tip["tip_type"] == "cache_opportunity"
    assert tip["evidence_json"]["observed"]["value"] == 4
    # No raw prompt text anywhere in the evidence payload — only a hash prefix.
    assert "Summarize" not in str(tip["evidence_json"])
    assert len(tip["evidence_json"]["params"]["prompt_hash_prefix"]) == 16


def test_cache_opportunity_rule_silent_below_min_hits(db_session):
    org_id, project_id = "org-tip-co2", "proj-tip-co2"
    _seed_org(db_session, org_id, project_id)
    window_start, window_end = _window()
    now = datetime.utcnow()

    for _ in range(2):
        _seed_request(
            db_session, org_id=org_id, project_id=project_id, model_name="gpt-4o",
            input_tokens=200, output_tokens=50, total_cost="0.05", created_at=now,
            sanitized_prompt_text="Summarize this exact same document please.",
        )
    db_session.flush()

    with patch("app.services.optimization.rules.cache_opportunity.get_tip_duplicate_min_hits", return_value=3):
        tips = CacheOpportunityRule().evaluate(
            db=db_session, org_id=org_id, project_id=project_id,
            window_start=window_start, window_end=window_end,
        )

    assert tips == []


# ---------------------------------------------------------------------------
# Scheduled job — dedup + cooldown
# ---------------------------------------------------------------------------

class _FakeRule(TipRule):
    """Deterministic stand-in so the dedup test doesn't depend on real
    rule thresholds or seeded volume — it only exercises _generate_optimization_tips'
    own dedup/cooldown bookkeeping."""

    tip_type = "response_truncated"

    def evaluate(self, *, db, org_id, project_id, window_start, window_end):
        return [{
            "org_id": org_id,
            "project_id": project_id,
            "model_name": "gpt-4o",
            "tip_type": self.tip_type,
            "severity": "medium",
            "title": "fake tip",
            "message": "fake tip for dedup test",
            "estimated_monthly_savings": Decimal("1.00"),
            "confidence": "high",
            "evidence_json": {"rule": self.tip_type},
            "period_start": window_start,
            "period_end": window_end,
        }]


def test_generate_optimization_tips_dedups_and_respects_cooldown(db_session):
    org_id, project_id = "org-tip-job1", "proj-tip-job1"
    _seed_org(db_session, org_id, project_id)
    window_end = date.today()

    # _generate_optimization_tips only considers (org, project) pairs that
    # appear in DailyOrgSummary within the lookback window.
    db_session.add(DailyOrgSummary(
        org_id=org_id, project_id=project_id, tool_name="gpt-4o",
        date=window_end, total_events=1,
    ))
    db_session.flush()

    with patch("app.services.optimization.tip_registry.all", return_value=[_FakeRule()]):
        inserted_first = _generate_optimization_tips(db=db_session, window_end=window_end)
        db_session.flush()
        inserted_second = _generate_optimization_tips(db=db_session, window_end=window_end)

    assert inserted_first == 1
    assert inserted_second == 0

    tip = (
        db_session.query(OptimizationTip)
        .filter(OptimizationTip.org_id == org_id, OptimizationTip.tip_type == "response_truncated")
        .one()
    )
    tip.status = "dismissed"
    db_session.flush()

    with patch("app.services.optimization.tip_registry.all", return_value=[_FakeRule()]):
        inserted_third = _generate_optimization_tips(db=db_session, window_end=window_end)

    assert inserted_third == 0
    db_session.refresh(tip)
    assert tip.status == "dismissed"
