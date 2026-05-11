from sqlalchemy import (
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
)

from app.database import Base


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
    total_cost = Column(Numeric(14, 6), default=0)
    avg_latency_ms = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    error_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())


class ToolApiInventory(Base):
    __tablename__ = "tool_api_inventory"
    __table_args__ = (
        UniqueConstraint("org_id", "tool_name", "function_name"),
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
    function_name = Column(String(255), nullable=True)
    input_preview = Column(Text, nullable=True)
    output_preview = Column(Text, nullable=True)
    input_size_bytes = Column(Integer, default=0)
    output_size_bytes = Column(Integer, default=0)
    input_keys = Column(String(500), nullable=True)
    output_keys = Column(String(500), nullable=True)
    pii_detected = Column(Boolean, default=False)
    pii_fields = Column(String(500), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
