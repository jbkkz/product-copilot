"""Opening a usage ledger around a paid web request, and recording what it cost (#253).

`requivo.usage` records into whatever ledger is active on the current call stack and is an explicit
no-op when there is none. `cli.py` opened one around every command; nothing opened one for the Web,
so a provider call made through a browser was billed by Anthropic and recorded nowhere — not shown,
not logged, not recoverable after the fact. On a product whose own ordering says "Web is the product
experience, the CLI is infrastructure", cost observability existed only on the infrastructure.

Two outcomes per action, deliberately, because they answer different people:

* the **response** carries the figures where it can, for the reader who just spent the money —
  `viewmodels/usage.py` turns the ledger into that;
* the **log** carries them always, for the operator, in the terminal they started the server in.
  That is the only channel available on the two paths that answer with a redirect (creating a
  session, running a deferred discovery): a 303 has no body to put a figure in, and carrying one to
  the following GET would need cross-request state this app deliberately does not have. It is also
  the only channel left when the call *fails* — the fragment is then replaced by the app's error
  rendering, and a paid turn that failed must still leave a trace somewhere.

The log line is written from a `finally`, which is what makes the second bullet true on the failure
path. It is the same rule `record_call` states one layer down: a failed call is still billed, so the
spend is recorded before the failure surfaces.

Pinned by `test_a_failed_paid_call_still_records_what_it_spent`.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from requivo.usage import track_usage
from requivo.web.viewmodels.usage import usage_view

# The same logger `app.py` uses, reached by name rather than by import so this module stays free of
# the app factory — the argument `security.py` makes for taking `render_error` as an argument.
logger = logging.getLogger("requivo.web")


@contextmanager
def track_web_usage(surface: str):
    """Scope a ledger over one paid web action and log its footprint when the action ends.

    `surface` is the same string the route stamps on the revision's provenance, so a line in the
    terminal and a line in the session's history name the same operation.
    """
    with track_usage() as ledger:
        try:
            yield ledger
        finally:
            view = usage_view(ledger)
            if view is not None:
                cost = (f"est. ~${view['cost']}" if view["cost"] is not None
                        else view["unpriced_reason"])
                logger.info("%s spent %s tokens over %s call(s) — %s",
                            surface, format(view["tokens"], ","), view["calls"], cost)
