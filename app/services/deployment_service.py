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
import uuid
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


def provision_standard_deployments(db: Session, *, org_id: str) -> int:
    """Create one org-wide ModelDeployment row per entry in
    config.get_standard_model_deployments() for a newly created org.

    project_id is left NULL so every project under the org (including ones
    created later) resolves to these deployments without a separate row.
    Returns the number of rows created. No-op if the config list is empty.
    """
    from app.config import get_standard_model_deployments
    from app.models import ModelDeployment

    templates = get_standard_model_deployments()
    created = 0
    for tpl in templates:
        model_name = tpl.get("model_name")
        if not model_name:
            continue
        db.add(ModelDeployment(
            deployment_id=f"depl-{uuid.uuid4().hex[:20]}",
            org_id=org_id,
            project_id=None,
            provider=tpl.get("provider", "azure_openai"),
            model_name=model_name,
            deployment_name=tpl.get("deployment_name") or model_name,
            endpoint_url=tpl.get("endpoint_url"),
            api_key=tpl.get("api_key"),
            api_version=tpl.get("api_version"),
            is_default=bool(tpl.get("is_default", False)),
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ))
        created += 1
    return created


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

    Thin wrapper over get_deployments_for_org() — kept for callers that only
    need the single best match and don't care about failover candidates.
    """
    candidates = get_deployments_for_org(
        db, org_id=org_id, project_id=project_id, requested_model=requested_model,
    )
    return candidates[0] if candidates else None


def get_deployments_for_org(
    db: Session,
    *,
    org_id: str,
    project_id: Optional[str],
    requested_model: Optional[str] = None,
) -> list:
    """Return all usable ModelDeployment rows for this org/project/model,
    ranked primary-first so callers can fail over to the next entry if the
    primary is unavailable.

    Ranking: project-specific deployments before org-wide ones, then
    is_default before non-default, then earliest-registered (created_at)
    before later additions. When no DB deployment matches, falls back to a
    synthetic deployment built from env vars (see _env_fallbacks) if one
    matches the requested model.
    """
    from app.models import ModelDeployment

    q = db.query(ModelDeployment).filter(
        ModelDeployment.org_id == org_id,
        ModelDeployment.is_active == True,
    )
    if requested_model:
        q = q.filter(ModelDeployment.model_name == requested_model)

    candidates = [d for d in q.all() if _has_credentials(d)]
    if not candidates:
        env = _fallback(requested_model, org_id=org_id, project_id=project_id)
        return [env] if env else []

    def sort_key(d: ModelDeployment) -> tuple:
        project_match = 1 if d.project_id == project_id else 0
        is_default    = 1 if d.is_default else 0
        created       = d.created_at or datetime.max
        return (-project_match, -is_default, created)

    candidates.sort(key=sort_key)
    return candidates


def _fallback(requested_model, *, org_id: str, project_id):
    """Return the env-fallback deployment matching requested_model, else None."""
    envs = _env_fallbacks(org_id=org_id, project_id=project_id)
    if not requested_model:
        return envs[0] if envs else None
    for env in envs:
        if env.model_name == requested_model:
            return env
    return None


def _has_credentials(d) -> bool:
    return bool(getattr(d, "api_key", None) and getattr(d, "endpoint_url", None))


_CONTENT_TYPE_JSON = "application/json"


def _make_env_deployment(*, model_name: str, api_key: str, endpoint: str, api_version: str):
    _api_key = api_key  # class body below treats `api_key = api_key` as local-before-assignment

    class _EnvDeployment:
        provider        = "azure_openai"
        deployment_name = model_name
        endpoint_url    = endpoint
        api_key         = _api_key

    _EnvDeployment.model_name = model_name
    _EnvDeployment.api_version = api_version
    _EnvDeployment.is_default = True
    return _EnvDeployment()


def _base_endpoint(url: str) -> str:
    """Strip any path/query from a full Azure resource URL, keeping just scheme+host.

    Some Azure deployment URLs are copied from the portal including the
    `/openai/deployments/{name}/...` path — build_provider_request() re-appends
    that path itself, so we need just the bare `https://{resource}.azure.com`.
    """
    if not url:
        return url
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return url


def _env_fallbacks(*, org_id: str, project_id: Optional[str]) -> list:
    """Synthetic deployments built from env vars, for backward compat / quick setup
    without a DB row. Each set below covers one model; add more sets here as needed.
    """
    fallbacks = []

    # gpt-5-nano (and anything else routed via AZURE_OPENAI_DEPLOYMENT_NAME)
    _key, _ep, _dep = (
        os.getenv("AZURE_OPENAI_API_KEY", ""),
        os.getenv("AZURE_OPENAI_ENDPOINT", ""),
        os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", ""),
    )
    _ver = os.getenv("AZURE_OPENAI_API_VERSION") or os.getenv("OPENAI_API_VERSION") or "2024-02-01"
    if _key and _ep and _dep:
        fallbacks.append(_make_env_deployment(model_name=_dep, api_key=_key, endpoint=_ep, api_version=_ver))

    # gpt-4.1-mini (and anything else routed via OPENAI_DEPLOYMENT_NAME)
    _key2, _ep2, _dep2 = (
        os.getenv("OPENAI_API_KEY", ""),
        os.getenv("OPENAI_ENDPOINT", ""),
        os.getenv("OPENAI_DEPLOYMENT_NAME", ""),
    )
    _ver2 = os.getenv("OPENAI_API_VERSION") or "2024-02-01"
    if _key2 and _ep2 and _dep2:
        fallbacks.append(_make_env_deployment(model_name=_dep2, api_key=_key2, endpoint=_ep2, api_version=_ver2))

    # gpt-4o (and anything else routed via AZURE_DEPLOYMENT)
    _key3, _ep3, _dep3 = (
        os.getenv("AZURE_API_KEY", ""),
        _base_endpoint(os.getenv("AZURE_ENDPOINT", "")),
        os.getenv("AZURE_DEPLOYMENT", ""),
    )
    _ver3 = os.getenv("AZURE_API_VERSION") or "2024-02-01"
    if _key3 and _ep3 and _dep3:
        fallbacks.append(_make_env_deployment(model_name=_dep3, api_key=_key3, endpoint=_ep3, api_version=_ver3))

    # gpt-4o-mini-tts (and anything else routed via TTS_AZURE_OPENAI_DEPLOYMENT)
    # Registered for deployment resolution only — the proxy has no TTS/audio
    # route yet, so build_provider_request() would build a chat/completions
    # URL for it; wire up an /audio/speech route before proxying real traffic.
    _key4, _ep4, _dep4 = (
        os.getenv("TTS_AZURE_OPENAI_API_KEY", ""),
        _base_endpoint(os.getenv("TTS_AZURE_OPENAI_ENDPOINT", "")),
        os.getenv("TTS_AZURE_OPENAI_DEPLOYMENT", ""),
    )
    _ver4 = os.getenv("TTS_AZURE_OPENAI_API_VERSION") or "2024-02-01"
    if _key4 and _ep4 and _dep4:
        fallbacks.append(_make_env_deployment(model_name=_dep4, api_key=_key4, endpoint=_ep4, api_version=_ver4))

    if fallbacks:
        _log.warning(
            "No DB deployment found for org=%s project=%s — using env-var fallback for model(s): %s. "
            "Register a deployment via POST /deployments to remove this warning.",
            org_id, project_id, [f.model_name for f in fallbacks],
        )

    return fallbacks


def build_provider_request(depl, stream: bool = False) -> tuple[str, dict]:
    """Return (url, headers) for the given deployment config.

    `stream` only changes the URL for providers with a distinct streaming
    endpoint (currently Google Gemini's streamGenerateContent).
    """
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

    elif provider == "google":
        base = endpoint or "https://generativelanguage.googleapis.com/v1beta"
        method = "streamGenerateContent?alt=sse" if stream else "generateContent"
        url = f"{base}/models/{dep_name}:{method}"
        headers = {"x-goog-api-key": api_key, "Content-Type": _CONTENT_TYPE_JSON}

    else:
        url = f"{endpoint}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": _CONTENT_TYPE_JSON}

    return url, headers
