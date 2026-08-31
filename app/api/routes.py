from __future__ import annotations

from fastapi import APIRouter

from app import __version__

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict[str, object]:
    """Unauthenticated liveness probe, used by the service units."""
    return {"ok": True, "app": "mesh-spy", "version": __version__}
