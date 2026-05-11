"""IngestionNormalizer — orchestrates vendor-payload → Tracing Module pipeline.

Resolves the right adapter for a connector's provider, normalises raw payloads
into TelemetryEventCreate instances, and forwards them to _ingest_event().
All data ultimately lands in telemetry_events via the existing ingest pipeline
(cost engine, security engine, alert engine, daily summary upsert).
"""
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.services.ingestion.registry import adapter_registry

if TYPE_CHECKING:
    from app.models import ToolConnector

logger = logging.getLogger(__name__)


class IngestionNormalizer:
    def __init__(self, db: Session) -> None:
        self.db = db

    def ingest_payload(
        self,
        connector: "ToolConnector",
        payload: dict | list,
    ) -> tuple[int, list[str]]:
        """Normalise a raw vendor payload and forward all events to the Tracing Module.

        Returns (events_ingested, event_ids).
        """
        from app.routers.telemetry import _ingest_event

        adapter = adapter_registry.resolve(connector.provider or "generic")
        if adapter is None:
            logger.error("No adapter for connector '%s' (provider=%s)", connector.connector_name, connector.provider)
            return 0, []

        raws = payload if isinstance(payload, list) else [payload]
        ingested, event_ids = 0, []

        for raw in raws:
            try:
                events = adapter.normalize(raw, connector)
            except Exception as exc:
                logger.warning("Normalisation error for connector '%s': %s", connector.connector_name, exc)
                continue

            for event_data in events:
                try:
                    _ingest_event(self.db, event_data)
                    event_ids.append(event_data.event_id)
                    ingested += 1
                except HTTPException as exc:
                    self.db.rollback()
                    if exc.status_code == 409:
                        logger.debug("Skipping duplicate event_id '%s' for connector '%s'", event_data.event_id, connector.connector_name)
                    else:
                        logger.warning("Ingest HTTP error for connector '%s' event '%s': %s", connector.connector_name, event_data.event_id, exc.detail)
                except Exception as exc:
                    self.db.rollback()
                    logger.warning("Ingest error for connector '%s' event '%s': %s", connector.connector_name, event_data.event_id, exc)

        if ingested:
            self.db.commit()
            connector.last_ingested_at = datetime.now(timezone.utc)
            self.db.commit()

        return ingested, event_ids

    def pull_from_connector(self, connector: "ToolConnector") -> tuple[int, list[str]]:
        """Pull from vendor API and ingest all returned records."""
        adapter = adapter_registry.resolve(connector.provider or "generic")
        if adapter is None:
            return 0, []

        payloads = adapter.pull(connector)
        if not payloads:
            return 0, []

        return self.ingest_payload(connector, payloads)
