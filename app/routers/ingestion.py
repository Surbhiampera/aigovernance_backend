"""Ingestion & Connector Layer — push (webhook, file) and pull endpoints.

All normalised events flow through IngestionNormalizer → _ingest_event()
so costs, security scoring, and alerts fire exactly as for direct telemetry.
"""
import csv
import io
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import TelemetryEvent, ToolConnector
from app.services.ingestion import IngestionNormalizer

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ingestion", tags=["ingestion"])


def _get_connector(name: str, db: Session) -> ToolConnector:
    connector = db.query(ToolConnector).filter(ToolConnector.connector_name == name).first()
    if not connector:
        raise HTTPException(status_code=404, detail=f"Connector '{name}' not found.")
    return connector


def _validate_webhook_token(connector: ToolConnector, request: Request) -> None:
    """Require X-Webhook-Token (or Bearer) to match connector api_key when one is set."""
    if not connector.api_key:
        return
    auth = request.headers.get("Authorization", "")
    provided = request.headers.get("X-Webhook-Token") or (
        auth[7:].strip() if auth.startswith("Bearer ") else auth.strip()
    )
    if provided != connector.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook token.")


def _excel_cell(v: Any) -> Any:
    """Convert openpyxl cell values to JSON-serializable Python primitives."""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _parse_upload(file: UploadFile) -> list[dict[str, Any]]:
    """Parse an uploaded file into a list of record dicts.

    Supports: .json, .jsonl/.ndjson, .csv, .xlsx/.xls and unknown extensions
    (tried as JSON then NDJSON).
    """
    name = (file.filename or "").lower()
    raw_bytes = file.file.read()

    if name.endswith((".xlsx", ".xls")):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                return []
            headers = [str(h) if h is not None else f"col{i}" for i, h in enumerate(rows[0])]
            return [
                {headers[i]: _excel_cell(cell) for i, cell in enumerate(row)}
                for row in rows[1:]
                if any(cell is not None for cell in row)
            ]
        except ImportError:
            raise HTTPException(
                status_code=422,
                detail="openpyxl is required for Excel uploads. Install it or convert to JSON/CSV.",
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Excel parse error: {exc}")

    text = raw_bytes.decode("utf-8", errors="replace").strip()

    if name.endswith((".jsonl", ".ndjson")):
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records

    if name.endswith(".csv"):
        return [dict(row) for row in csv.DictReader(io.StringIO(text))]

    # JSON array or single object — default for .json and unknown extensions
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        # Last-chance: try NDJSON even if the extension is unrecognised
        records = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        if records:
            return records
        raise HTTPException(
            status_code=422,
            detail="Cannot parse file. Supported: JSON, NDJSON, CSV, Excel (.xlsx/.xls).",
        )


@router.post("/webhook/{connector_name}")
async def receive_webhook(
    connector_name: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Accept a push payload from a vendor webhook."""
    connector = _get_connector(connector_name, db)
    _validate_webhook_token(connector, request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON.")
    ingested, event_ids = IngestionNormalizer(db).ingest_payload(connector, payload)
    return {"status": "ok", "ingested": ingested, "event_ids": event_ids}


@router.post("/upload/{connector_name}")
def upload_file(
    connector_name: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a log file (JSON, NDJSON, CSV, or Excel) to ingest."""
    connector = _get_connector(connector_name, db)
    records = _parse_upload(file)
    if not records:
        return {"status": "ok", "ingested": 0, "event_ids": []}
    ingested, event_ids = IngestionNormalizer(db).ingest_payload(connector, records)
    return {"status": "ok", "ingested": ingested, "event_ids": event_ids}


@router.post("/pull/{connector_name}/sync")
def pull_sync(
    connector_name: str,
    db: Session = Depends(get_db),
):
    """Manually trigger a vendor API pull for a connector."""
    connector = _get_connector(connector_name, db)
    ingested, event_ids = IngestionNormalizer(db).pull_from_connector(connector)
    return {"status": "ok", "ingested": ingested, "event_ids": event_ids}


@router.get("/status")
def ingestion_status(db: Session = Depends(get_db)):
    """Return all connectors with last-ingested timestamp and ingested event count."""
    connectors = db.query(ToolConnector).order_by(ToolConnector.created_at.desc()).all()

    try:
        connector_col = TelemetryEvent.metadata_json["connector"].astext
        counts_raw = (
            db.query(connector_col, func.count(TelemetryEvent.id))
            .filter(connector_col.isnot(None))
            .group_by(connector_col)
            .all()
        )
        counts = {row[0]: row[1] for row in counts_raw}
    except Exception:
        counts = {}

    return [
        {
            "connector_name": c.connector_name,
            "provider": c.provider,
            "ingestion_mode": c.ingestion_mode,
            "status": c.status,
            "org_id": c.org_id,
            "last_ingested_at": c.last_ingested_at.isoformat() if c.last_ingested_at else None,
            "event_count": counts.get(c.connector_name, 0),
        }
        for c in connectors
    ]
