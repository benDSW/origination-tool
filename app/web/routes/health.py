"""Liveness probe for Railway. Public, and deliberately touches nothing else
(no DB, no integrations) so a slow API never marks the web service unhealthy."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
