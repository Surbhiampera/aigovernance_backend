"""Authentication router."""

from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me")
async def me():
    """Return current user info."""
    return {"email": "", "name": "", "roles": []}


@router.post("/logout")
async def logout():
    return {"status": "logged_out"}
