from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ExternalToolCost(BaseModel):
    name: str
    cost: Decimal


class PipelineStageCreate(BaseModel):
    stage_order: int = 0
    stage_name: str
    system_name: Optional[str] = None
    status: str = "success"
    stage_latency_ms: int = 0
    retry_count: int = 0
    details: dict[str, Any] = Field(default_factory=dict)


class ModelUsageItem(BaseModel):
    """Single model's usage within a unified trace."""
    model_name: str
    provider: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Optional[float] = None
    input_cost_per_1k: Optional[float] = None
    output_cost_per_1k: Optional[float] = None
    latency_ms: int = 0


class ToolUsageItemEnhanced(BaseModel):
    """Single tool's usage within a unified trace."""
    tool_name: str
    tool_type: Optional[str] = None
    invocation_count: int = 1
    execution_time_ms: int = 0
    cost: Optional[float] = None


class TelemetryEventCreate(BaseModel):
    event_id: str
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    org_id: str = "default"
    project_id: Optional[str] = None
    user_id: Optional[str] = None
    api_key_id: Optional[str] = None
    tool_name: str
    provider: Optional[str] = None
    model_name: Optional[str] = None
    component_name: Optional[str] = None
    service_type: Optional[str] = None
    execution_type: Optional[str] = None
    status: str = "success"
    latency_ms: int = 0
    input_data_size_mb: Decimal = Decimal("0")
    output_data_size_mb: Decimal = Decimal("0")
    prompt_tokens: int = 0
    completion_tokens: int = 0
    infra_cost: Decimal = Decimal("0")
    precomputed_llm_cost: Optional[Decimal] = None
    external_tools: list[ExternalToolCost] = Field(default_factory=list)
    stages: list[PipelineStageCreate] = Field(default_factory=list)
    contains_pii: bool = False
    pii_type: Optional[str] = None
    data_out_violation: bool = False
    tags: list[str] = Field(default_factory=list)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    raw_usage_json: dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    input_preview: Optional[str] = None
    output_preview: Optional[str] = None


class TelemetryEventUpdate(BaseModel):
    tool_name: Optional[str] = None
    provider: Optional[str] = None
    model_name: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    latency_ms: Optional[int] = None
    infra_cost: Optional[Decimal] = None
    input_data_size_mb: Optional[Decimal] = None
    output_data_size_mb: Optional[Decimal] = None
    pii_type: Optional[str] = None
    tags: Optional[list[str]] = None
    status: Optional[str] = None


class BatchTelemetryIngest(BaseModel):
    events: list[TelemetryEventCreate]


class CostSummary(BaseModel):
    llm_cost: Decimal = Decimal("0")
    external_cost: Decimal = Decimal("0")
    infra_cost: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")


class CostBreakdownResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cost_type: str
    component_name: Optional[str] = None
    unit_cost: Decimal
    quantity: Decimal
    total_cost: Decimal


class PipelineStageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stage_order: int
    stage_name: str
    system_name: Optional[str] = None
    status: Optional[str] = None
    stage_latency_ms: int = 0
    retry_count: int = 0
    details: Optional[dict[str, Any]] = None


class TelemetryEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    org_id: str
    project_id: Optional[str] = None
    user_id: Optional[str] = None
    api_key_id: Optional[str] = None
    tool_name: str
    provider: Optional[str] = None
    model_name: Optional[str] = None
    service_type: Optional[str] = None
    component_name: Optional[str] = None
    execution_type: Optional[str] = None
    status: Optional[str] = None
    input_data_size_mb: Decimal = Decimal("0")
    output_data_size_mb: Decimal = Decimal("0")
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    llm_cost: Decimal = Decimal("0")
    infra_cost: Decimal = Decimal("0")
    external_cost: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    risk_score: Decimal = Decimal("0")
    anomaly_score: Decimal = Decimal("0")
    misuse_detected: bool = False
    abnormal_usage_spike: bool = False
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    latency_ms: int = 0
    tags: list[str] = Field(default_factory=list)
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    raw_usage_json: Optional[dict[str, Any]] = None
    created_at: Optional[datetime] = None
    cost_breakdown: list[CostBreakdownResponse] = Field(default_factory=list)
    stages: list[PipelineStageResponse] = Field(default_factory=list)


class TraceDetailResponse(BaseModel):
    event: TelemetryEventResponse
    security: Optional["DataSecurityLogResponse"] = None


class DailySummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    org_id: str
    project_id: Optional[str] = None
    tool_name: str
    date: date
    total_events: int
    total_cost: Decimal
    llm_cost: Decimal
    infra_cost: Decimal
    external_cost: Decimal
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    avg_latency_ms: int = 0
    success_count: int = 0
    failure_count: int = 0
    anomaly_count: int = 0
    misuse_count: int = 0
    total_input_mb: Decimal = Decimal("0")
    total_output_mb: Decimal = Decimal("0")
    avg_risk_score: Decimal = Decimal("0")


class MonthlySummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    org_id: str
    project_id: Optional[str] = None
    tool_name: str
    month: date
    total_events: int
    total_cost: Decimal
    llm_cost: Decimal
    infra_cost: Decimal
    external_cost: Decimal
    total_tokens: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    avg_latency_ms: int = 0
    success_count: int = 0
    failure_count: int = 0
    anomaly_count: int = 0
    misuse_count: int = 0


class TodaySummaryResponse(BaseModel):
    total_cost: Decimal
    total_events: int
    tools: list[DailySummaryResponse]


class ToolRegistryCreate(BaseModel):
    tool_name: str
    tool_type: Optional[str] = None
    vendor: Optional[str] = None
    cost_model: Optional[str] = None
    base_cost: Decimal = Decimal("0")


class ToolRegistryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tool_name: str
    tool_type: Optional[str] = None
    vendor: Optional[str] = None
    cost_model: Optional[str] = None
    base_cost: Optional[Decimal] = None
    created_at: Optional[datetime] = None


class ModelRegistryCreate(BaseModel):
    model_name: str
    provider: Optional[str] = None
    model_type: Optional[str] = None
    cost_per_1k_tokens: Decimal = Decimal("0")


class ModelRegistryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    model_name: str
    provider: Optional[str] = None
    model_type: Optional[str] = None
    cost_per_1k_tokens: Optional[Decimal] = None
    created_at: Optional[datetime] = None


class ToolConnectorCreate(BaseModel):
    connector_name: str
    tool_name: str
    provider: Optional[str] = None
    endpoint_url: Optional[str] = None
    auth_type: Optional[str] = None
    ingestion_mode: str = "api"
    status: str = "active"
    org_id: Optional[str] = None
    project_id: Optional[str] = None
    api_key: Optional[str] = None
    sync_enabled: bool = True
    pull_interval_minutes: int = 15


class ToolConnectorUpdate(BaseModel):
    tool_name: Optional[str] = None
    provider: Optional[str] = None
    endpoint_url: Optional[str] = None
    auth_type: Optional[str] = None
    ingestion_mode: Optional[str] = None
    status: Optional[str] = None
    org_id: Optional[str] = None
    project_id: Optional[str] = None
    api_key: Optional[str] = None
    sync_enabled: Optional[bool] = None
    pull_interval_minutes: Optional[int] = None


class ToolConnectorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    connector_name: str
    tool_name: str
    provider: Optional[str] = None
    endpoint_url: Optional[str] = None
    auth_type: Optional[str] = None
    ingestion_mode: str
    status: str
    org_id: Optional[str] = None
    project_id: Optional[str] = None
    sync_enabled: bool = True
    pull_interval_minutes: int = 15
    last_sync_status: Optional[str] = None
    last_sync_error: Optional[str] = None
    total_events_pulled: int = 0
    last_ingested_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class ConnectorSyncLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    connector_id: int
    connector_name: Optional[str] = None
    sync_status: str
    events_pulled: int = 0
    error_message: Optional[str] = None
    duration_ms: int = 0
    created_at: Optional[datetime] = None


class ToolUsageResponse(BaseModel):
    tool_name: str
    vendor: Optional[str] = None
    total_events: int
    total_cost: Decimal
    total_tokens: Decimal = Decimal("0")
    total_prompt_tokens: Decimal = Decimal("0")
    total_completion_tokens: Decimal = Decimal("0")
    avg_latency_ms: Decimal = Decimal("0")
    success_rate: Decimal = Decimal("0")


class AlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: Optional[str] = None
    org_name: Optional[str] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    tool_name: Optional[str] = None
    model_name: Optional[str] = None
    rule_id: Optional[int] = None
    alert_type: Optional[str] = None
    severity: Optional[str] = None
    message: Optional[str] = None
    threshold_value: Optional[Decimal] = None
    actual_value: Optional[Decimal] = None
    status: Optional[str] = None
    telemetry_id: Optional[int] = None
    created_at: Optional[datetime] = None


class DataSecurityLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_id: Optional[str] = None
    org_id: Optional[str] = None
    project_id: Optional[str] = None
    pii_detected: Optional[bool] = None
    pii_type: Optional[str] = None
    data_out_violation: Optional[bool] = None
    misuse_pattern_detected: Optional[bool] = None
    abnormal_usage_spike: Optional[bool] = None
    masking_applied: Optional[bool] = None
    risk_score: Optional[Decimal] = None
    data_in_mb: Optional[Decimal] = None
    data_out_mb: Optional[Decimal] = None
    created_at: Optional[datetime] = None


class GovernanceRuleCreate(BaseModel):
    rule_name: str
    description: Optional[str] = None
    metric_name: str
    operator: str = ">"
    threshold_value: Decimal
    severity: str = "medium"
    scope_level: str = "organization"
    scope_reference: Optional[str] = None
    is_active: bool = True


class GovernanceRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rule_name: str
    description: Optional[str] = None
    metric_name: str
    operator: str
    threshold_value: Decimal
    severity: str
    scope_level: str
    scope_reference: Optional[str] = None
    is_active: bool
    created_at: Optional[datetime] = None


class UsageAnomalyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: str
    org_name: Optional[str] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    tool_name: str
    event_id: Optional[str] = None
    anomaly_type: str
    severity: str
    anomaly_score: Decimal
    baseline_value: Decimal
    observed_value: Decimal
    message: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None


class OrganizationCreate(BaseModel):
    id: str
    org_name: str
    plan_type: Optional[str] = None
    budget_limit: Optional[Decimal] = None


class OrganizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    org_name: str
    plan_type: Optional[str] = None
    budget_limit: Optional[Decimal] = None
    created_at: Optional[datetime] = None


class ProjectCreate(BaseModel):
    id: str
    org_id: str
    project_name: Optional[str] = None
    environment: Optional[str] = None


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: str
    project_name: Optional[str] = None
    environment: Optional[str] = None
    created_at: Optional[datetime] = None


class UserCreate(BaseModel):
    id: str
    org_id: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    created_at: Optional[datetime] = None


class ApiKeyCreate(BaseModel):
    id: str
    org_id: Optional[str] = None
    project_id: Optional[str] = None
    key_name: Optional[str] = None
    provider: Optional[str] = None


class ApiKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    org_id: Optional[str] = None
    project_id: Optional[str] = None
    key_name: Optional[str] = None
    provider: Optional[str] = None
    created_at: Optional[datetime] = None


class BudgetCreate(BaseModel):
    org_id: str
    project_id: Optional[str] = None
    budget_type: str
    limit_amount: Decimal
    alert_threshold_percent: int = 80


class BudgetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: Optional[str] = None
    project_id: Optional[str] = None
    budget_type: Optional[str] = None
    limit_amount: Optional[Decimal] = None
    alert_threshold_percent: Optional[int] = None
    created_at: Optional[datetime] = None


class GovernanceOverviewResponse(BaseModel):
    total_cost_today: Decimal
    total_events_today: int
    total_tokens_today: int
    avg_latency_today: Decimal
    success_rate_today: Decimal
    active_alerts: int
    anomalies_open: int
    connectors_active: int
    rules_active: int
    avg_risk_score: Decimal
    highest_risk_score: Decimal
    alerts_by_severity: dict[str, int]
    cost_by_type: dict[str, Decimal]
    health: dict[str, Decimal]
    tool_rollup: list[DailySummaryResponse]
    recent_alerts: list[AlertResponse]
    recent_anomalies: list[UsageAnomalyResponse]
    recent_events: list[TelemetryEventResponse]


class ModelPricingCreate(BaseModel):
    provider: str
    model_name: str
    input_cost_per_1k: Decimal
    output_cost_per_1k: Decimal
    currency: str = "USD"


class ModelPricingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: Optional[str] = None
    model_name: Optional[str] = None
    input_cost_per_1k: Optional[Decimal] = None
    output_cost_per_1k: Optional[Decimal] = None
    currency: Optional[str] = None
    effective_from: Optional[datetime] = None


TraceDetailResponse.model_rebuild()
