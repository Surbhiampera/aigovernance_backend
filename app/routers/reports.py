"""Downloadable per-project governance report/invoice.

GET /reports/projects/{project_id}           JSON preview of the report payload
GET /reports/projects/{project_id}/export     the same data rendered as a
                                               PDF, Word (.docx) or Excel (.xlsx)
                                               file, generated in-memory.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.services.report_exporters import export_docx, export_excel, export_pdf
from app.services.report_service import build_project_report

router = APIRouter(prefix="/reports", tags=["reports"])

_EXPORTERS = {
    "pdf": (export_pdf, "application/pdf", "pdf"),
    "xlsx": (
        export_excel,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
    ),
    "docx": (
        export_docx,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    ),
}


def _get_report(db: Session, project_id: str, start: Optional[date], end: Optional[date]) -> dict:
    report = build_project_report(db, project_id=project_id, start=start, end=end)
    if not report:
        raise HTTPException(status_code=404, detail="Project not found")
    return report


@router.get("/projects/{project_id}")
def get_project_report(
    *,
    project_id: str,
    start: Optional[date] = Query(None, description="Report period start (inclusive)"),
    end: Optional[date] = Query(None, description="Report period end (inclusive)"),
    db: Session = Depends(get_db),
) -> dict:
    """JSON preview of the report — same data the export formats render."""
    return _get_report(db, project_id, start, end)


@router.get("/projects/{project_id}/export")
def export_project_report(
    *,
    project_id: str,
    format: str = Query("pdf", pattern="^(pdf|xlsx|docx)$", description="pdf | xlsx | docx"),
    start: Optional[date] = Query(None, description="Report period start (inclusive)"),
    end: Optional[date] = Query(None, description="Report period end (inclusive)"),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Download the project report as a PDF, Excel, or Word file."""
    report = _get_report(db, project_id, start, end)

    exporter, media_type, extension = _EXPORTERS[format]
    file_bytes = exporter(report)

    safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in report["project"]["name"])
    filename = f"governance-report-{safe_name}-{date.today().isoformat()}.{extension}"

    from io import BytesIO
    return StreamingResponse(
        BytesIO(file_bytes),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
