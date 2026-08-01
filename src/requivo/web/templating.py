"""Shared Jinja2 environment + template/static locations.

Paths resolve from inside the package, so they work identically from a source checkout and from an
installed wheel (the templates and static files ship as package data). Autoescaping is on for HTML, so
every rendered value is escaped by default — `| safe` is used only for content the app itself produced.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
