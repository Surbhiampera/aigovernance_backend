from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)

from app.database import Base


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = {"extend_existing": True}

    id = Column(String(100), primary_key=True)
    org_name = Column(String(150), nullable=False)
    plan_type = Column(String(50), nullable=True)
    budget_limit = Column(Numeric(14, 6), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = {"extend_existing": True}

    id = Column(String(100), primary_key=True)
    org_id = Column(String(100), ForeignKey("organizations.id"), nullable=False)
    project_name = Column(String(150), nullable=True)
    environment = Column(String(50), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"extend_existing": True}

    id = Column(String(100), primary_key=True)
    org_id = Column(String(100), ForeignKey("organizations.id"), nullable=True)
    email = Column(String(150), nullable=True)
    role = Column(String(50), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = {"extend_existing": True}

    id = Column(String(120), primary_key=True)
    org_id = Column(String(100), ForeignKey("organizations.id"), nullable=True)
    project_id = Column(String(100), ForeignKey("projects.id"), nullable=True)
    key_name = Column(String(100), nullable=True)
    provider = Column(String(100), nullable=True)
    hashed_key = Column(String(64), nullable=True)
    raw_key_hint = Column(String(30), nullable=True)
    is_proxy_key = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    role = Column(String(50), nullable=True)
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class Provider(Base):
    __tablename__ = "providers"
    __table_args__ = {"extend_existing": True}

    id = Column(String(100), primary_key=True)
    provider_name = Column(String(150), nullable=True)


class ToolRegistry(Base):
    __tablename__ = "tool_registry"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    tool_name = Column(String(150), unique=True, nullable=False)
    tool_type = Column(String(50), nullable=True)
    vendor = Column(String(100), nullable=True)
    cost_model = Column(String(50), nullable=True)
    base_cost = Column(Numeric(12, 6), default=0)
    project_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class ModelRegistry(Base):
    __tablename__ = "model_registry"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    model_name = Column(String(120), unique=True, nullable=False)
    provider = Column(String(100), nullable=True)
    model_type = Column(String(50), nullable=True)
    cost_per_1k_tokens = Column(Numeric(12, 6), default=0)
    created_at = Column(DateTime, server_default=func.now())


class ModelPricing(Base):
    __tablename__ = "model_pricing"
    __table_args__ = (
        UniqueConstraint("provider", "model_name"),
        {"extend_existing": True},
    )

    id = Column(BigInteger, primary_key=True)
    provider = Column(String(100), nullable=True)
    model_name = Column(String(120), nullable=True)
    input_cost_per_1k = Column(Numeric(12, 6), default=0)
    output_cost_per_1k = Column(Numeric(12, 6), default=0)
    currency = Column(String(10), default="USD")
    effective_from = Column(DateTime, server_default=func.now())


class ToolConnector(Base):
    __tablename__ = "tool_connectors"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    connector_name = Column(String(150), unique=True, nullable=False)
    tool_name = Column(String(150), nullable=False)
    provider = Column(String(100), nullable=True)
    endpoint_url = Column(String(255), nullable=True)
    auth_type = Column(String(50), nullable=True)
    ingestion_mode = Column(String(50), nullable=False, default="api")
    status = Column(String(30), nullable=False, default="active")
    org_id = Column(String(100), nullable=True)
    project_id = Column(String(100), nullable=True)
    api_key = Column(String(500), nullable=True)
    last_ingested_at = Column(DateTime, nullable=True)
    sync_enabled = Column(Boolean, default=True)
    pull_interval_minutes = Column(Integer, default=15)
    last_sync_status = Column(String(30), nullable=True)
    last_sync_error = Column(Text, nullable=True)
    total_events_pulled = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())


class ConnectorSyncLog(Base):
    __tablename__ = "connector_sync_logs"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    connector_id = Column(BigInteger, ForeignKey("tool_connectors.id"), nullable=False)
    connector_name = Column(String(150), nullable=True)
    sync_status = Column(String(30), nullable=False, default="success")
    events_pulled = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    duration_ms = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())


class TelemetryEvent(Base):
    __tablename__ = "telemetry_events"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    event_id = Column(String(120), unique=True, nullable=False)
    request_id = Column(String(120), nullable=True)
    trace_id = Column(String(120), nullable=True)
    org_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    user_id = Column(String(100), nullable=True)
    api_key_id = Column(String(120), nullable=True)
    tool_name = Column(String(150), nullable=True)
    provider = Column(String(100), nullable=True)
    model_name = Column(String(100), nullable=True)
    service_type = Column(String(50), nullable=True)
    component_name = Column(String(150), nullable=True)
    execution_type = Column(String(50), nullable=True)
    function_name = Column(String(255), nullable=True)
    module_path = Column(String(500), nullable=True)
    decorator_type = Column(String(50), nullable=True)
    execution_env = Column(String(50), nullable=True)
    sdk_version = Column(String(20), nullable=True)
    tool_version = Column(String(50), nullable=True)
    status = Column(String(30), nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    input_token_cost = Column(Numeric(14, 6), default=0)
    output_token_cost = Column(Numeric(14, 6), default=0)
    total_token_cost = Column(Numeric(14, 6), default=0)
    input_data_size_mb = Column(Numeric(12, 4), default=0)
    output_data_size_mb = Column(Numeric(12, 4), default=0)
    input_preview = Column(Text, nullable=True)
    output_preview = Column(Text, nullable=True)
    llm_cost = Column(Numeric(14, 6), default=0)
    infra_cost = Column(Numeric(14, 6), default=0)
    external_cost = Column(Numeric(14, 6), default=0)
    total_cost = Column(Numeric(14, 6), default=0)
    risk_score = Column(Numeric(8, 2), default=0)
    anomaly_score = Column(Numeric(8, 2), default=0)
    misuse_detected = Column(Boolean, default=False)
    abnormal_usage_spike = Column(Boolean, default=False)
    latency_ms = Column(Integer, default=0)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    tags = Column(JSON, nullable=True)
    metadata_json = Column(JSON, nullable=True)
    raw_usage_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class CostBreakdown(Base):
    __tablename__ = "cost_breakdown"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    event_id = Column(String(120), ForeignKey("telemetry_events.event_id", ondelete="CASCADE"), nullable=False)
    cost_type = Column(String(50), nullable=False)
    component_name = Column(String(150), nullable=True)
    unit_cost = Column(Numeric(12, 6), default=0)
    quantity = Column(Numeric(12, 6), default=0)
    total_cost = Column(Numeric(12, 6), default=0)
    created_at = Column(DateTime, server_default=func.now())


class ExecutionPipeline(Base):
    __tablename__ = "execution_pipeline"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    event_id = Column(String(120), ForeignKey("telemetry_events.event_id", ondelete="CASCADE"), nullable=False)
    stage_order = Column(Integer, default=0)
    stage_name = Column(String(150), nullable=False)
    system_name = Column(String(150), nullable=True)
    status = Column(String(30), nullable=True)
    stage_latency_ms = Column(Integer, default=0)
    retry_count = Column(Integer, default=0)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class TraceModelUsage(Base):
    __tablename__ = "trace_model_usage"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    event_id = Column(String(120), ForeignKey("telemetry_events.event_id", ondelete="CASCADE"), nullable=False)
    trace_id = Column(String(120), nullable=True)
    org_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    model_name = Column(String(120), nullable=False)
    provider = Column(String(100), nullable=True)
    function_name = Column(String(255), nullable=True)
    call_sequence = Column(Integer, default=0)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    input_token_cost = Column(Numeric(14, 6), default=0)
    output_token_cost = Column(Numeric(14, 6), default=0)
    total_token_cost = Column(Numeric(14, 6), default=0)
    llm_cost = Column(Numeric(14, 6), default=0)
    latency_ms = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())


class TraceToolUsage(Base):
    __tablename__ = "trace_tool_usage"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    event_id = Column(String(120), ForeignKey("telemetry_events.event_id", ondelete="CASCADE"), nullable=False)
    trace_id = Column(String(120), nullable=True)
    org_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    tool_name = Column(String(150), nullable=False)
    tool_type = Column(String(50), nullable=True)
    invocation_count = Column(Integer, default=1)
    execution_time_ms = Column(Integer, default=0)
    cost = Column(Numeric(14, 6), default=0)
    created_at = Column(DateTime, server_default=func.now())


class DataSecurityLog(Base):
    __tablename__ = "data_security_logs"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    event_id = Column(String(120), ForeignKey("telemetry_events.event_id"), nullable=False)
    org_id = Column(String(100), nullable=True)
    project_id = Column(String(100), nullable=True)
    pii_detected = Column(Boolean, default=False)
    pii_type = Column(String(100), nullable=True)
    data_out_violation = Column(Boolean, default=False)
    misuse_pattern_detected = Column(Boolean, default=False)
    abnormal_usage_spike = Column(Boolean, default=False)
    masking_applied = Column(Boolean, default=False)
    risk_score = Column(Numeric(8, 2), default=0)
    data_in_mb = Column(Numeric(12, 4), default=0)
    data_out_mb = Column(Numeric(12, 4), default=0)
    created_at = Column(DateTime, server_default=func.now())


class UsageAnomaly(Base):
    __tablename__ = "usage_anomalies"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    org_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    tool_name = Column(String(150), nullable=False)
    event_id = Column(String(120), nullable=True)
    anomaly_type = Column(String(60), nullable=False)
    severity = Column(String(20), nullable=False, default="medium")
    anomaly_score = Column(Numeric(8, 2), default=0)
    baseline_value = Column(Numeric(14, 6), default=0)
    observed_value = Column(Numeric(14, 6), default=0)
    message = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="open")
    created_at = Column(DateTime, server_default=func.now())


class GovernanceRule(Base):
    __tablename__ = "governance_rules"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    rule_name = Column(String(150), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    metric_name = Column(String(100), nullable=False)
    operator = Column(String(20), nullable=False, default=">")
    threshold_value = Column(Numeric(14, 6), nullable=False, default=0)
    severity = Column(String(20), nullable=False, default="medium")
    scope_level = Column(String(30), nullable=False, default="organization")
    scope_reference = Column(String(150), nullable=True)
    is_active = Column(Boolean, default=True)
    org_id = Column(String(100), nullable=True)
    project_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    org_id = Column(String(100), nullable=True)
    project_id = Column(String(100), nullable=True)
    rule_id = Column(BigInteger, ForeignKey("governance_rules.id"), nullable=True)
    alert_type = Column(String(100), nullable=True)
    severity = Column(String(50), nullable=True)
    message = Column(Text, nullable=True)
    threshold_value = Column(Numeric(10, 2), nullable=True)
    actual_value = Column(Numeric(10, 2), nullable=True)
    status = Column(String(50), default="active")
    telemetry_id = Column(BigInteger, ForeignKey("telemetry_events.id"), nullable=True)
    tool_name = Column(String(150), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    org_id = Column(String(100), ForeignKey("organizations.id"), nullable=True)
    project_id = Column(String(100), ForeignKey("projects.id"), nullable=True)
    budget_type = Column(String(50), nullable=True)
    limit_amount = Column(Numeric(14, 6), nullable=True)
    alert_threshold_percent = Column(Integer, default=80)
    created_at = Column(DateTime, server_default=func.now())


class RateLimit(Base):
    __tablename__ = "rate_limits"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    org_id = Column(String(100), nullable=True)
    tool_name = Column(String(150), nullable=True)
    max_requests_per_min = Column(Integer, nullable=True)
    max_tokens_per_day = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class DailyOrgSummary(Base):
    __tablename__ = "daily_org_summary"
    __table_args__ = (
        UniqueConstraint("org_id", "project_id", "tool_name", "date"),
        {"extend_existing": True},
    )

    id = Column(BigInteger, primary_key=True)
    org_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    tool_name = Column(String(150), nullable=False)
    date = Column(Date, nullable=False)
    total_events = Column(Integer, default=0)
    total_cost = Column(Numeric(14, 6), default=0)
    llm_cost = Column(Numeric(14, 6), default=0)
    infra_cost = Column(Numeric(14, 6), default=0)
    external_cost = Column(Numeric(14, 6), default=0)
    total_prompt_tokens = Column(Integer, default=0)
    total_completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    input_token_cost = Column(Numeric(14, 6), default=0)
    output_token_cost = Column(Numeric(14, 6), default=0)
    total_token_cost = Column(Numeric(14, 6), default=0)
    avg_latency_ms = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    anomaly_count = Column(Integer, default=0)
    misuse_count = Column(Integer, default=0)
    total_input_mb = Column(Numeric(12, 4), default=0)
    total_output_mb = Column(Numeric(12, 4), default=0)
    avg_risk_score = Column(Numeric(8, 2), default=0)
    created_at = Column(DateTime, server_default=func.now())


class MonthlyOrgSummary(Base):
    __tablename__ = "monthly_org_summary"
    __table_args__ = (
        UniqueConstraint("org_id", "project_id", "tool_name", "month"),
        {"extend_existing": True},
    )

    id = Column(BigInteger, primary_key=True)
    org_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    tool_name = Column(String(150), nullable=False)
    month = Column(Date, nullable=False)
    total_events = Column(Integer, default=0)
    total_cost = Column(Numeric(14, 6), default=0)
    llm_cost = Column(Numeric(14, 6), default=0)
    infra_cost = Column(Numeric(14, 6), default=0)
    external_cost = Column(Numeric(14, 6), default=0)
    total_tokens = Column(Integer, default=0)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    input_token_cost = Column(Numeric(14, 6), default=0)
    output_token_cost = Column(Numeric(14, 6), default=0)
    total_token_cost = Column(Numeric(14, 6), default=0)
    total_prompt_tokens = Column(Integer, default=0)
    total_completion_tokens = Column(Integer, default=0)
    avg_latency_ms = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    anomaly_count = Column(Integer, default=0)
    misuse_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())


class DecoratorRegistration(Base):
    __tablename__ = "decorator_registrations"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    org_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    tool_name = Column(String(150), nullable=False)
    function_name = Column(String(255), nullable=False)
    module_path = Column(String(500), nullable=True)
    decorator_type = Column(String(50), nullable=False, default="trace")
    sdk_version = Column(String(20), nullable=True)
    python_version = Column(String(20), nullable=True)
    execution_env = Column(String(50), nullable=True, default="production")
    first_seen = Column(DateTime, server_default=func.now())
    last_seen = Column(DateTime, server_default=func.now())
    call_count = Column(BigInteger, default=0)


class ProjectModelUsage(Base):
    __tablename__ = "project_model_usage"
    __table_args__ = (
        UniqueConstraint("org_id", "project_id", "model_name", "date"),
        {"extend_existing": True},
    )

    id = Column(BigInteger, primary_key=True)
    org_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    model_name = Column(String(120), nullable=False)
    provider = Column(String(100), nullable=True)
    date = Column(Date, nullable=False)
    call_count = Column(Integer, default=0)
    total_prompt_tokens = Column(Integer, default=0)
    total_completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    input_token_cost = Column(Numeric(14, 6), default=0)
    output_token_cost = Column(Numeric(14, 6), default=0)
    total_token_cost = Column(Numeric(14, 6), default=0)
    total_cost = Column(Numeric(14, 6), default=0)
    avg_latency_ms = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    error_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())


class ToolApiInventory(Base):
    __tablename__ = "tool_api_inventory"
    __table_args__ = (
        UniqueConstraint("org_id", "project_id", "tool_name", "function_name"),
        {"extend_existing": True},
    )

    id = Column(BigInteger, primary_key=True)
    org_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    tool_name = Column(String(150), nullable=False)
    function_name = Column(String(255), nullable=False)
    module_path = Column(String(500), nullable=True)
    decorator_type = Column(String(50), nullable=True)
    description = Column(Text, nullable=True)
    first_seen = Column(DateTime, server_default=func.now())
    last_seen = Column(DateTime, server_default=func.now())
    total_calls = Column(BigInteger, default=0)
    success_calls = Column(BigInteger, default=0)
    error_calls = Column(BigInteger, default=0)
    avg_latency_ms = Column(Integer, default=0)


class RequestResponseLog(Base):
    __tablename__ = "request_response_logs"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    event_id = Column(String(120), ForeignKey("telemetry_events.event_id", ondelete="CASCADE"), nullable=True)
    trace_id = Column(String(120), nullable=True)
    user_id = Column(String(100), nullable=True)
    function_name = Column(String(255), nullable=True)
    route = Column(String(255), nullable=True)
    model_name = Column(String(120), nullable=True)
    provider = Column(String(100), nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    estimated_cost_usd = Column(Numeric(14, 8), nullable=True)
    input_preview = Column(Text, nullable=True)
    output_preview = Column(Text, nullable=True)
    input_size_bytes = Column(Integer, default=0)
    output_size_bytes = Column(Integer, default=0)
    input_keys = Column(String(500), nullable=True)
    output_keys = Column(String(500), nullable=True)
    pii_detected = Column(Boolean, default=False)
    pii_fields = Column(String(500), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

# =============================================================================
# Proxy-layer models (migration 009 + 011)
# =============================================================================

class AiRoute(Base):
    __tablename__ = "ai_routes"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    route_id = Column(String(120), unique=True, nullable=False)
    org_id = Column(String(100), ForeignKey("organizations.id"), nullable=False)
    project_id = Column(String(100), ForeignKey("projects.id"), nullable=True)
    project_ref_id = Column(String(100), nullable=True)
    route_name = Column(String(255), nullable=False)
    route_path = Column(String(500), nullable=True)
    route_type = Column(String(50), nullable=True)
    http_method = Column(String(10), default="POST")
    upstream_url = Column(String(500), nullable=True)
    default_provider = Column(String(100), nullable=True)
    default_model = Column(String(120), nullable=True)
    allowed_models = Column(JSON, nullable=True)
    model_selection_strategy = Column(String(50), nullable=True)
    description = Column(Text, nullable=True)
    tags = Column(JSON, nullable=True)
    is_active = Column(Boolean, default=True)
    requires_auth = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now())


class AiRequest(Base):
    __tablename__ = "ai_requests"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    request_id = Column(String(120), unique=True, nullable=False)
    org_id = Column(String(100), ForeignKey("organizations.id"), nullable=False)
    project_id = Column(String(100), ForeignKey("projects.id"), nullable=True)
    project_ref_id = Column(String(100), nullable=True)
    trace_id = Column(String(120), nullable=True)
    span_id = Column(String(120), nullable=True)
    parent_span_id = Column(String(120), nullable=True)
    session_id = Column(String(120), nullable=True)
    conversation_id = Column(String(120), nullable=True)
    route_id = Column(String(120), ForeignKey("ai_routes.route_id", ondelete="SET NULL"), nullable=True)
    request_type = Column(String(50), default="chat_completion")
    request_status = Column(String(30), default="pending")
    user_id = Column(String(100), nullable=True)
    user_email = Column(String(150), nullable=True)
    user_role = Column(String(50), nullable=True)
    api_key_id = Column(String(120), nullable=True)
    client_ip = Column(String(50), nullable=True)
    user_agent = Column(String(500), nullable=True)
    provider = Column(String(100), nullable=True)
    model_name = Column(String(120), nullable=True)
    model_version = Column(String(50), nullable=True)
    requested_model = Column(String(120), nullable=True)
    routed_model = Column(String(120), nullable=True)
    function_name = Column(String(255), nullable=True)
    tool_name = Column(String(150), nullable=True)
    source_system = Column(String(255), nullable=True)
    request_payload = Column(JSON, nullable=True)
    prompt_text = Column(Text, nullable=True)
    sanitized_prompt_text = Column(Text, nullable=True)
    system_prompt = Column(Text, nullable=True)
    messages = Column(JSON, nullable=True)
    request_parameters = Column(JSON, nullable=True)
    request_headers = Column(JSON, nullable=True)
    request_metadata = Column(JSON, nullable=True)
    input_token_estimate = Column(Integer, default=0)
    prompt_char_count = Column(Integer, default=0)
    num_messages = Column(Integer, default=0)
    has_system_prompt = Column(Boolean, default=False)
    has_tool_definitions = Column(Boolean, default=False)
    has_images = Column(Boolean, default=False)
    pii_detected = Column(Boolean, default=False)
    pii_types = Column(JSON, nullable=True)
    pii_masked = Column(Boolean, default=False)
    pii_action_taken = Column(String(20), nullable=True)
    content_policy_flags = Column(JSON, nullable=True)
    # Proxy-layer provenance columns (added via _SAFE_ALTERS)
    deployment_name = Column(String(255), nullable=True)
    governance_key_id = Column(String(120), nullable=True)
    source_ip = Column(String(60), nullable=True)
    completed_at = Column(DateTime, nullable=True)
    received_at = Column(DateTime, server_default=func.now())
    processing_started_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class AiResponse(Base):
    __tablename__ = "ai_responses"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    response_id = Column(String(120), unique=True, nullable=False)
    request_id = Column(String(120), ForeignKey("ai_requests.request_id", ondelete="CASCADE"), nullable=False)
    org_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    project_ref_id = Column(String(100), nullable=True)
    provider = Column(String(100), nullable=True)
    model_name = Column(String(120), nullable=True)
    model_version = Column(String(50), nullable=True)
    response_status = Column(String(30), default="pending")
    finish_reason = Column(String(50), nullable=True)
    is_streaming = Column(Boolean, default=False)
    is_cached = Column(Boolean, default=False)
    response_payload = Column(JSON, nullable=True)
    response_text = Column(Text, nullable=True)
    tool_calls = Column(JSON, nullable=True)
    tool_call_results = Column(JSON, nullable=True)
    response_metadata = Column(JSON, nullable=True)
    output_char_count = Column(Integer, default=0)
    num_tool_calls = Column(Integer, default=0)
    error_code = Column(String(50), nullable=True)
    error_type = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)
    provider_request_id = Column(String(255), nullable=True)
    provider_response_id = Column(String(255), nullable=True)
    output_pii_detected = Column(Boolean, default=False)
    output_pii_types = Column(JSON, nullable=True)
    response_started_at = Column(DateTime, nullable=True)
    response_completed_at = Column(DateTime, nullable=True)
    first_token_at = Column(DateTime, nullable=True)
    latency_ms = Column(Integer, default=0)
    ttft_ms = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())


class TokenUsage(Base):
    __tablename__ = "token_usage"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    token_usage_id = Column(String(120), unique=True, nullable=False,
                           server_default=text("concat('tu-', replace(gen_random_uuid()::text, '-', ''))"))
    request_id = Column(String(120), ForeignKey("ai_requests.request_id", ondelete="CASCADE"), nullable=False)
    response_id = Column(String(120), ForeignKey("ai_responses.response_id", ondelete="SET NULL"), nullable=True)
    org_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    project_ref_id = Column(String(100), nullable=True)
    provider = Column(String(100), nullable=True)
    model_name = Column(String(120), nullable=True)
    model_version = Column(String(50), nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    input_tokens = Column(Integer, default=0)
    cached_tokens = Column(Integer, default=0)
    uncached_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    reasoning_tokens = Column(Integer, default=0)
    tool_definition_tokens = Column(Integer, default=0)
    system_tokens = Column(Integer, default=0)
    context_window_limit = Column(Integer, nullable=True)
    context_utilization_pct = Column(Numeric(6, 3), nullable=True)
    raw_usage = Column(JSON, nullable=True)
    # Token provenance — values: "azure" (exact from provider) or "tiktoken_estimate"
    input_token_source = Column(String(30), nullable=True)
    output_token_source = Column(String(30), nullable=True)
    is_estimated = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())


class RequestCost(Base):
    __tablename__ = "request_cost"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    cost_id = Column(String(120), unique=True, nullable=False,
                    server_default=text("concat('cu-', replace(gen_random_uuid()::text, '-', ''))"))
    request_id = Column(String(120), ForeignKey("ai_requests.request_id", ondelete="CASCADE"), nullable=False)
    response_id = Column(String(120), ForeignKey("ai_responses.response_id", ondelete="SET NULL"), nullable=True)
    org_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    project_ref_id = Column(String(100), nullable=True)
    provider = Column(String(100), nullable=True)
    model_name = Column(String(120), nullable=True)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    input_token_cost = Column(Numeric(14, 8), default=0)
    cached_token_cost = Column(Numeric(14, 8), default=0)
    output_token_cost = Column(Numeric(14, 8), default=0)
    tool_cost = Column(Numeric(14, 8), default=0)
    infra_cost = Column(Numeric(14, 8), default=0)
    gateway_cost = Column(Numeric(14, 8), default=0)
    llm_cost = Column(Numeric(14, 8), default=0)
    total_cost = Column(Numeric(14, 8), default=0)
    currency = Column(String(10), default="USD")
    pricing_version = Column(String(50), nullable=True)
    pricing_snapshot = Column(JSON, nullable=True)
    cost_model_type = Column(String(50), nullable=True)
    discount_pct = Column(Numeric(6, 3), default=0)
    adjusted_total_cost = Column(Numeric(14, 8), default=0)
    created_at = Column(DateTime, server_default=func.now())


class RouteExecution(Base):
    __tablename__ = "route_executions"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    execution_id = Column(String(120), unique=True, nullable=False)
    request_id = Column(String(120), ForeignKey("ai_requests.request_id", ondelete="CASCADE"), nullable=False)
    route_id = Column(String(120), ForeignKey("ai_routes.route_id", ondelete="SET NULL"), nullable=True)
    org_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    project_ref_id = Column(String(100), nullable=True)
    execution_status = Column(String(30), default="pending")
    routing_strategy = Column(String(50), nullable=True)
    routing_reason = Column(Text, nullable=True)
    original_model = Column(String(120), nullable=True)
    selected_model = Column(String(120), nullable=True)
    selected_provider = Column(String(100), nullable=True)
    proxy_type = Column(String(50), nullable=True)
    upstream_url = Column(String(500), nullable=True)
    upstream_request_id = Column(String(255), nullable=True)
    pipeline_stages = Column(JSON, nullable=True)
    attempt_number = Column(Integer, default=1)
    retry_count = Column(Integer, default=0)
    retry_reasons = Column(JSON, nullable=True)
    last_failure_reason = Column(Text, nullable=True)
    total_latency_ms = Column(Integer, default=0)
    routing_latency_ms = Column(Integer, default=0)
    proxy_latency_ms = Column(Integer, default=0)
    upstream_latency_ms = Column(Integer, default=0)
    governance_check_ms = Column(Integer, default=0)
    quota_checked = Column(Boolean, default=False)
    quota_remaining = Column(Integer, nullable=True)
    policy_applied = Column(JSON, nullable=True)
    blocked_by_policy = Column(Boolean, default=False)
    block_reason = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    audit_id = Column(String(120), unique=True, nullable=False)
    org_id = Column(String(100), nullable=False)
    project_id = Column(String(100), nullable=True)
    project_ref_id = Column(String(100), nullable=True)
    actor_type = Column(String(50), nullable=False)
    actor_id = Column(String(100), nullable=True)
    actor_email = Column(String(150), nullable=True)
    actor_ip = Column(String(50), nullable=True)
    audit_category = Column(String(50), nullable=False)
    audit_action = Column(String(100), nullable=False)
    audit_status = Column(String(30), default="success")
    entity_type = Column(String(100), nullable=True)
    entity_id = Column(String(120), nullable=True)
    request_id = Column(String(120), nullable=True)
    trace_id = Column(String(120), nullable=True)
    old_value = Column(JSON, nullable=True)
    new_value = Column(JSON, nullable=True)
    change_summary = Column(Text, nullable=True)
    policy_triggered = Column(Boolean, default=False)
    compliance_relevant = Column(Boolean, default=False)
    requires_review = Column(Boolean, default=False)
    audit_metadata = Column(JSON, nullable=True)
    occurred_at = Column(DateTime, server_default=func.now())
    created_at = Column(DateTime, server_default=func.now())


class ProviderConfig(Base):
    __tablename__ = "provider_configs"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    config_id = Column(String(120), unique=True, nullable=False)
    org_id = Column(String(100), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    project_id = Column(String(100), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    provider = Column(String(100), nullable=False)
    api_key = Column(Text, nullable=False)
    api_key_hint = Column(String(20), nullable=True)
    base_url = Column(String(500), nullable=True)
    model_allowlist = Column(JSON, nullable=True)
    max_rpm = Column(Integer, nullable=True)
    max_tpm = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now())


class PiiPolicy(Base):
    __tablename__ = "pii_policies"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    policy_id = Column(String(120), unique=True, nullable=False)
    org_id = Column(String(100), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
    pii_type = Column(String(100), nullable=False)
    risk_level = Column(String(20), default="medium")
    action = Column(String(20), default="mask")
    mask_pattern = Column(String(100), default="[{pii_type}]")
    log_detection = Column(Boolean, default=True)
    priority = Column(Integer, default=0)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now())


class ModelVersion(Base):
    __tablename__ = "model_versions"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    provider = Column(String(100), nullable=False)
    model_name = Column(String(120), nullable=False)
    version_tag = Column(String(50), nullable=True)
    full_model_id = Column(String(200), nullable=True)
    context_window = Column(Integer, nullable=True)
    max_output_tokens = Column(Integer, nullable=True)
    supports_functions = Column(Boolean, default=False)
    supports_vision = Column(Boolean, default=False)
    supports_streaming = Column(Boolean, default=False)
    supports_reasoning = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    released_at = Column(DateTime, nullable=True)
    deprecated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class ModelDeployment(Base):
    __tablename__ = "model_deployments"
    __table_args__ = {"extend_existing": True}

    id = Column(BigInteger, primary_key=True)
    deployment_id = Column(String(120), unique=True, nullable=False)
    org_id = Column(String(100), ForeignKey("organizations.id"), nullable=False)
    project_id = Column(String(100), ForeignKey("projects.id"), nullable=True)
    provider = Column(String(100), nullable=False)
    model_name = Column(String(120), nullable=False)
    deployment_name = Column(String(255), nullable=True)
    endpoint_url = Column(String(500), nullable=True)
    deployment_type = Column(String(50), default="api")
    auth_type = Column(String(50), nullable=True)
    api_key_ref = Column(String(500), nullable=True)
    default_parameters = Column(JSON, nullable=True)
    rate_limit_rpm = Column(Integer, nullable=True)
    rate_limit_tpm = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now())
