"""
Decorator Framework Package — backend-side components.

Folder layout
─────────────
decorator/
├── __init__.py          ← this file; re-exports ORM models + router
├── models.py            ← 4 new SQLAlchemy ORM tables
├── router.py            ← REST endpoints for decorator monitoring & SDK upsert
└── migrations/
    ├── 003_decorator_framework.sql   ← incremental migration (existing DB)
    └── 004_consolidated_schema.sql   ← full schema for fresh installs

Integration checklist
─────────────────────
1. Run 003_decorator_framework.sql against your existing DB  (or let startup
   _SAFE_ALTERS handle the column additions automatically).
2. The four ORM classes below are imported into backend/app/models.py so that
   Base.metadata.create_all() picks them up at startup.
3. The FastAPI router is registered in backend/app/main.py.
"""

from .models import (
    DecoratorRegistration,
    ProjectModelUsage,
    RequestResponseLog,
    ToolApiInventory,
)
from .router import router as decorator_router

__all__ = [
    "DecoratorRegistration",
    "ProjectModelUsage",
    "ToolApiInventory",
    "RequestResponseLog",
    "decorator_router",
]
