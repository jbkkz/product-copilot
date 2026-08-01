"""Shared Jinja2 environment + template/static locations.

Paths resolve from inside the package, so they work identically from a source checkout and from an
installed wheel (the templates and static files ship as package data). Autoescaping is on for HTML, so
every rendered value is escaped by default — `| safe` is used only for content the app itself produced.

`csrf_token` is a global rather than a per-route context value: every form must carry it, and a route
that forgets to pass it would render a page whose buttons silently 403.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from requivo.web.security import CSRF_FIELD, csrf_token

_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["csrf_field"] = CSRF_FIELD
templates.env.globals["csrf_token"] = csrf_token()
