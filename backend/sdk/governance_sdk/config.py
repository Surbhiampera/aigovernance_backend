"""
Environment-based factory for GovernanceDecorator.

Reads GOV_* environment variables once and returns a module-level singleton,
so every file in a project that calls ``get_gov()`` gets the same instance.

Required environment variable (at least one)::

    GOV_ORG_ID          Organization ID
    GOV_ORG_NAME        Organization name  (used if GOV_ORG_ID is not set)

Optional environment variables::

    GOV_PROJECT_ID      Project ID
    GOV_PROJECT_NAME    Project name       (used if GOV_PROJECT_ID is not set)
    GOV_TOOL_NAME       Application name shown in dashboards  (default: "unknown")
    GOV_ENDPOINT        Governance platform base URL          (default: "http://localhost:8000")
    GOV_API_KEY         API key for authentication
    GOV_TOOL_VERSION    Semver string for this tool           (default: "1.0.0")
    GOV_ENV             Execution environment label           (default: "production")
    GOV_DETECT_PII      Enable PII detection                  (default: "true")
    GOV_REDACT_PII      Replace PII with [REDACTED] in previews (default: "true")
    GOV_CAPTURE_IO      Store input/output previews           (default: "true")
    GOV_MAX_PREVIEW     Max chars in each preview string      (default: "500")
    GOV_BATCH_SIZE      Events to buffer before sending       (default: "1")

Typical usage in a tool's main module::

    # governance_setup.py  (import this once at app startup)
    from governance_sdk.config import get_gov

    gov = get_gov()          # reads env vars, returns singleton

    # Then in any other file:
    from governance_setup import gov   # or re-import get_gov()

    @gov.trace()
    def my_function(x):
        ...

You can also use it inline without a shared module::

    from governance_sdk.config import get_gov

    @get_gov().trace()
    def my_function(x):
        ...
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .decorator import GovernanceDecorator

_instance: Optional["GovernanceDecorator"] = None


def get_gov(*, refresh: bool = False) -> "GovernanceDecorator":
    """
    Return the module-level GovernanceDecorator singleton, creating it on
    first call from environment variables.

    Pass ``refresh=True`` to rebuild the instance (e.g. after env vars change
    in a test environment).
    """
    global _instance
    if _instance is None or refresh:
        from .decorator import GovernanceDecorator
        _instance = GovernanceDecorator.from_env()
    return _instance


def reset_gov() -> None:
    """Clear the cached singleton (useful in tests)."""
    global _instance
    _instance = None


def configure(
    org_id: Optional[str] = None,
    *,
    org_name: Optional[str] = None,
    project_id: Optional[str] = None,
    project_name: Optional[str] = None,
    tool_name: str = "unknown",
    endpoint: str = "http://localhost:8000",
    api_key: Optional[str] = None,
    tool_version: str = "1.0.0",
    execution_env: str = "production",
    capture_io: bool = True,
    max_preview_chars: int = 500,
    detect_pii: bool = True,
    redact_pii: bool = True,
    batch_size: int = 1,
    flush_interval: float = 5.0,
) -> "GovernanceDecorator":
    """
    Programmatically configure and store the singleton GovernanceDecorator.
    Prefer this over ``get_gov()`` when you need explicit control (e.g. tests
    or apps that don't use environment variables).

    Usage::

        from governance_sdk.config import configure

        gov = configure(
            org_id="royal-sundaram",
            project_id="email-agent-prod",
            tool_name="email-agent",
            endpoint="https://governance.internal:8000",
            api_key="gvk-prod-xxx",
        )

        @gov.trace()
        def run():
            ...
    """
    global _instance
    from .decorator import GovernanceDecorator

    _instance = GovernanceDecorator(
        org_id=org_id,
        org_name=org_name,
        project_id=project_id,
        project_name=project_name,
        tool_name=tool_name,
        endpoint=endpoint,
        api_key=api_key,
        tool_version=tool_version,
        execution_env=execution_env,
        capture_io=capture_io,
        max_preview_chars=max_preview_chars,
        detect_pii=detect_pii,
        redact_pii=redact_pii,
        batch_size=batch_size,
        flush_interval=flush_interval,
    )
    return _instance
