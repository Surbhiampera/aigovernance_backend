"""Plug-and-play rule registry for the optimization-tips engine.

Follows the same pattern as app/services/ingestion/registry.py: rules
self-register with @tip_registry.register, and app/services/optimization/rules/
imports every rule module so registration happens on package import.
"""
from __future__ import annotations

from datetime import date
from typing import Optional, Type

from sqlalchemy.orm import Session


class TipRule:
    """Base class for an optimization-tip rule.

    evaluate() must never raise for data-shape reasons it can anticipate
    (e.g. missing pricing) — return an empty list instead. The scheduled job
    (app/workers/tasks.py::_generate_optimization_tips) catches unexpected
    exceptions per-rule so one broken rule cannot block the others.
    """

    tip_type: str

    def evaluate(
        self,
        *,
        db: Session,
        org_id: str,
        project_id: Optional[str],
        window_start: date,
        window_end: date,
    ) -> list[dict]:
        raise NotImplementedError


class TipRuleRegistry:
    def __init__(self) -> None:
        self._rules: list[Type[TipRule]] = []

    def register(self, cls: Type[TipRule]) -> Type[TipRule]:
        self._rules.append(cls)
        return cls

    def all(self) -> list[TipRule]:
        return [cls() for cls in self._rules]


tip_registry = TipRuleRegistry()
