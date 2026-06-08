"""Deployment lookup — resolves the Azure deployment for a given org/project + model.

When requested_model is provided (always the case for proxy requests), only
deployments matching that exact model_name are considered. If none is found the
caller raises 404 — the client must request a model that an admin has registered.

When requested_model is omitted, the best active deployment for the org/project
is returned (used by internal tooling only).

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
    """Return the best ModelDeployment row for this org/project/model.

    When requested_model is given, only exact model_name matches are considered.
    Returns None if no matching deployment is found — caller raises 404.
    """
    from app.models import ModelDeployment

    q = db.query(ModelDeployment).filter(
        ModelDeployment.org_id == org_id,
        ModelDeployment.is_active == True,
    )
    if requested_model:
        q = q.filter(ModelDeployment.model_name == requested_model)

    candidates = q.all()
    if not candidates:
        return _fallback(requested_model, org_id=org_id, project_id=project_id)

    def score(d: ModelDeployment) -> tuple:
        project_match = 1 if d.project_id == project_id else 0
        is_default    = 1 if d.is_default else 0
        return (project_match, is_default)

    best = max(candidates, key=score)
    if _has_credentials(best):
        return best
    return _fallback(requested_model, org_id=org_id, project_id=project_id)


def _fallback(requested_model, *, org_id: str, project_id):
    """Return env-fallback deployment if it matches requested_model, else None."""
    env = _env_fallback(org_id=org_id, project_id=project_id)
    if not requested_model:
        return env
    return env if (env and env.model_name == requested_model) else None


def _has_credentials(d) -> bool:
    return bool(getattr(d, "api_key", None) and getattr(d, "endpoint_url", None))


_CONTENT_TYPE_JSON = "application/json"


def _env_fallback(*, org_id: str, project_id: Optional[str]):
    """Synthetic deployment from AZURE_OPENAI_* env vars for backward compat."""
    _key    = os.getenv("AZURE_OPENAI_API_KEY", "")
    _ep     = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    _dep    = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "")
    _ver    = os.getenv("AZURE_OPENAI_API_VERSION") or os.getenv("OPENAI_API_VERSION") or "2024-02-01"

    if not (_key and _ep and _dep):
        return None

    _log.warning(
        "No DB deployment found for org=%s project=%s — using AZURE_OPENAI_* env vars. "
        "Register a deployment via POST /deployments to remove this warning.",
        org_id, project_id,
    )

    class _EnvDeployment:
        provider        = "azure_openai"
        model_name      = _dep
        deployment_name = _dep
        endpoint_url    = _ep
        api_key         = _key
        api_version     = _ver
        is_default      = True

    return _EnvDeployment()


def build_provider_request(depl) -> tuple[str, dict]:
    """Return (url, headers) for the given deployment config."""
    provider  = (depl.provider or "").lower().replace("-", "_").replace(" ", "_")
    api_key   = depl.api_key or ""
    endpoint  = (depl.endpoint_url or "").rstrip("/")
    api_ver   = (
        getattr(depl, "api_version", None)
        or os.getenv("AZURE_OPENAI_API_VERSION")
        or os.getenv("OPENAI_API_VERSION")
        or "2024-02-01"
    )
    dep_name  = depl.deployment_name or depl.model_name

    if provider in ("azure_openai", "azure"):
        url = (
            f"{endpoint}/openai/deployments/{dep_name}"
            f"/chat/completions?api-version={api_ver}"
        )
        headers = {"api-key": api_key, "Content-Type": _CONTENT_TYPE_JSON}

    elif provider == "openai":
        base = endpoint or "https://api.openai.com"
        url = f"{base}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": _CONTENT_TYPE_JSON}

    elif provider == "anthropic":
        base = endpoint or "https://api.anthropic.com"
        url = f"{base}/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": _CONTENT_TYPE_JSON,
        }

    else:
        url = f"{endpoint}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": _CONTENT_TYPE_JSON}

    return url, headers
