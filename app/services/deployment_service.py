"""Deployment lookup — resolves which AI deployment to use for a given org/project.

Resolution order (first match wins):
  1. Project-specific deployment matching the requested model name
  2. Project-specific default deployment (is_default=True)
  3. Any active project-specific deployment
  4. Org-level default deployment
  5. Any active org-level deployment
  6. Env-var fallback (AZURE_OPENAI_* — backward compat for single-model setups)

External teams never see credentials or provider details.
Admin registers deployments via POST /deployments.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


def get_deployment_for_org(
    db: Session,
    *,
    org_id: str,
    project_id: Optional[str],
    requested_model: Optional[str] = None,
):
    """Return the best ModelDeployment row for this org/project request.

    Returns None if nothing is configured — caller should raise 503.
    """
    from app.models import ModelDeployment

    base_q = (
        db.query(ModelDeployment)
        .filter(
            ModelDeployment.org_id == org_id,
            ModelDeployment.is_active == True,
        )
    )

    candidates = base_q.all()
    if not candidates:
        return _env_fallback(org_id=org_id, project_id=project_id)

    def score(d: ModelDeployment) -> tuple:
        # Higher score = preferred
        project_match = 1 if d.project_id == project_id else 0
        model_match   = 1 if (requested_model and d.model_name == requested_model) else 0
        is_default    = 1 if d.is_default else 0
        return (project_match, model_match, is_default)

    best = max(candidates, key=score)
    if _has_credentials(best):
        return best

    return _env_fallback(org_id=org_id, project_id=project_id)


def _has_credentials(d) -> bool:
    return bool(getattr(d, "api_key", None) and getattr(d, "endpoint_url", None))


def _env_fallback(*, org_id: str, project_id: Optional[str]):
    """Synthetic deployment from AZURE_OPENAI_* env vars for backward compat."""
    api_key    = os.getenv("AZURE_OPENAI_API_KEY", "")
    endpoint   = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")

    if not (api_key and endpoint and deployment):
        return None

    _log.warning(
        "No DB deployment found for org=%s project=%s — using AZURE_OPENAI_* env vars. "
        "Register a deployment via POST /deployments to remove this warning.",
        org_id, project_id,
    )

    class _EnvDeployment:
        provider        = "azure_openai"
        model_name      = deployment
        deployment_name = deployment
        endpoint_url    = endpoint
        api_key         = api_key
        api_version     = api_version
        is_default      = True

    return _EnvDeployment()


def build_provider_request(depl) -> tuple[str, dict]:
    """Return (url, headers) for the given deployment config.

    Supports:
      azure_openai — Azure REST format with api-key header
      openai       — OpenAI compatible with Bearer auth
      anthropic    — Anthropic Messages API (basic)
      <other>      — Generic Bearer auth at endpoint/chat/completions
    """
    provider  = (depl.provider or "").lower().replace("-", "_").replace(" ", "_")
    api_key   = depl.api_key or ""
    endpoint  = (depl.endpoint_url or "").rstrip("/")
    api_ver   = getattr(depl, "api_version", None) or "2024-02-01"
    dep_name  = depl.deployment_name or depl.model_name

    if provider in ("azure_openai", "azure"):
        url = (
            f"{endpoint}/openai/deployments/{dep_name}"
            f"/chat/completions?api-version={api_ver}"
        )
        headers = {"api-key": api_key, "Content-Type": "application/json"}

    elif provider == "openai":
        base = endpoint or "https://api.openai.com"
        url = f"{base}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    elif provider == "anthropic":
        base = endpoint or "https://api.anthropic.com"
        url = f"{base}/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    else:
        url = f"{endpoint}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    return url, headers
