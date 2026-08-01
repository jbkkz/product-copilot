"""Home — the product page and the list of local sessions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from requivo.services.sessions import SessionService
from requivo.web.config import provider_status
from requivo.web.dependencies import get_sessions
from requivo.web.templating import templates
from requivo.web.viewmodels.sessions import session_list

router = APIRouter()


@router.get("/")
def home(request: Request, sessions: SessionService = Depends(get_sessions)):
    return templates.TemplateResponse(request, "home.html", {
        "sessions": session_list(sessions),
        "provider": provider_status(),
    })
