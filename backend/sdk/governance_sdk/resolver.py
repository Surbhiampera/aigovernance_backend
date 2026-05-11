"""Resolve human-readable org/project names to their database IDs.

Called once at GovernanceSDK.__init__ time so the rest of the SDK always
works with stable IDs — no runtime lookups on the hot path.
"""
from __future__ import annotations

import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)


def resolve_org_project(
    endpoint: str,
    headers: dict,
    org_name: Optional[str] = None,
    project_name: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """
    Resolve (org_name, project_name) → (org_id, project_id).

    - If org_name is None, returns (None, None) immediately.
    - If org_name is provided but not found, logs a warning and returns (None, None).
    - project_name resolution is skipped if org_id could not be resolved.
    - Any network error is caught; the SDK continues without IDs.
    """
    org_id = _resolve_org(endpoint, headers, org_name)
    if org_id is None:
        return None, None

    project_id = _resolve_project(endpoint, headers, org_id, project_name)
    return org_id, project_id


def _resolve_org(endpoint: str, headers: dict, org_name: Optional[str]) -> Optional[str]:
    if not org_name:
        return None
    try:
        r = requests.get(f"{endpoint}/organizations/", headers=headers, timeout=5)
        r.raise_for_status()
        for org in r.json():
            # API returns `org_name` field (matches OrganizationResponse schema)
            name = org.get("org_name") or org.get("name") or ""
            if name.lower() == org_name.lower():
                return org.get("id")
        log.warning("GovernanceSDK: org '%s' not found in registry", org_name)
    except Exception as exc:
        log.warning("GovernanceSDK: org resolution failed (%s)", exc)
    return None


def _resolve_project(
    endpoint: str,
    headers: dict,
    org_id: str,
    project_name: Optional[str],
) -> Optional[str]:
    if not project_name:
        return None
    try:
        r = requests.get(
            f"{endpoint}/projects/",
            headers=headers,
            params={"org_id": org_id},
            timeout=5,
        )
        r.raise_for_status()
        for proj in r.json():
            # API returns `project_name` field (matches ProjectResponse schema)
            name = proj.get("project_name") or proj.get("name") or ""
            if name.lower() == project_name.lower():
                return proj.get("id")
        log.warning("GovernanceSDK: project '%s' not found for org %s", project_name, org_id)
    except Exception as exc:
        log.warning("GovernanceSDK: project resolution failed (%s)", exc)
    return None
