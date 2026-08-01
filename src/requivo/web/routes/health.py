"""Liveness endpoint — a cheap 200 for the CI smoke test and any local health check."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from requivo import __version__

router = APIRouter()


@router.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "requivo-web", "version": __version__})
