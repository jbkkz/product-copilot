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

from requivo.web.config import MAX_ANSWERS_CHARS, MAX_REQUEST_CHARS
from requivo.web.security import CSRF_FIELD, csrf_token
from requivo.web.viewmodels.labels import EXAMPLE_BADGE, UNREADABLE_BADGE, human_time

_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["csrf_field"] = CSRF_FIELD
templates.env.globals["csrf_token"] = csrf_token()
# Registered here, decided in `labels.py`. A filter is a template mechanism; "3 days ago" is
# user-facing wording, and the whole point of that module is that a term living in six templates
# drifts in six directions (#237).
templates.env.filters["human_time"] = human_time
# The input ceilings, as globals for the same reason `csrf_token` is one: every field that has a
# ceiling must declare it, and a route that forgot to pass it would render a field whose counter
# silently never appears — an affordance that is off looking exactly like one that has nothing to say
# (#239). Rendered rather than typed into the templates so the number the reader is shown and the
# number `require_input_within_bounds` refuses on cannot drift; pinned by
# `test_the_limit_the_page_shows_is_the_limit_the_server_refuses_on`.
templates.env.globals["max_request_chars"] = MAX_REQUEST_CHARS
templates.env.globals["max_answers_chars"] = MAX_ANSWERS_CHARS
# The word a bundled example wears, on the home row and on its own page (#226). A global for the
# same reason the two above are: the caption is decided in `labels.py` and read in more than one
# template, and a route that forgot to pass it would render a sample session wearing no mark of
# being one -- an affordance that is off looking exactly like one with nothing to say.
templates.env.globals["example_badge"] = EXAMPLE_BADGE
# And the word for the third state (#240). The home row gets it through the view model, as
# `status_label`; the session page for an unreadable session has no view model to get it from, and
# typed the literal into the template instead -- which is the drift `labels.py`'s own comment on
# this constant warns about, in the file the same change wrote it into. Two surfaces, one constant.
templates.env.globals["unreadable_badge"] = UNREADABLE_BADGE
