"""Cross-site request protection for Requivo Web.

Binding to `127.0.0.1` is not a security boundary. Any page the user has open in the same browser can
POST to a known local port, and a plain HTML form post is not subject to a CORS preflight — the browser
sends it and simply refuses to let the attacker *read* the reply. For this app, writing is the damage:
a hostile page could create sessions, submit answers, and burn the server's Anthropic key, and the
attacker never needs to see a single response to do it.

Four checks close that, in the order they run:

  * **Host allowlist** — the `Host` header must name a loopback address (or one the operator opted into
    via `REQUIVO_WEB_ALLOWED_HOSTS`). This is the DNS-rebinding guard, and it is the only check that
    applies to reads as well: a rebound `evil.com` that resolves to 127.0.0.1 is same-origin from the
    browser's point of view, so it would sail through every other check *and* be able to read the token.
  * **`Sec-Fetch-Site`** — the browser's own account of where the request came from. Free, unspoofable
    from script, and rejects `cross-site` / `same-site` outright.
  * **`Origin` / `Referer`** — when present, it must name the same trust domain as the host being
    addressed. The three loopback spellings are one machine and are interchangeable here; an
    operator-listed host is not, and must match exactly (`_same_trust_domain`). The opaque origin
    `null` is refused outright, and `_enforce` says why that differs from sending no origin at all.
  * **A synchronizer token** — minted once per process, rendered into every form, required on every
    unsafe method. This is the load-bearing check; the three above are cheap filters in front of it.

There is no login and no session cookie here, so a process-lifetime token is the whole ceremony: it
only ever appears in HTML that the checks above keep cross-origin readers away from, and a page left
open across a server restart just needs a reload. Nothing here makes Requivo Web safe to expose on an
untrusted network — it is still single-user and unauthenticated. It makes it safe to leave *running*
while the user browses the rest of the web, which is the actual threat model of a local tool.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import Response

from requivo.core.errors import InputTooLargeError, RequivoError

CSRF_FIELD = "csrf_token"          # the hidden form input every template renders
CSRF_HEADER = "x-csrf-token"       # the equivalent for a scripted client
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Where the server may legitimately be addressed. A non-loopback bind is a deliberate act (`requivo web
# --host`), so it is an explicit opt-in here too rather than a hole left open by default.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
ALLOWED_HOSTS_ENV = "REQUIVO_WEB_ALLOWED_HOSTS"

# Bounded before the body is read, let alone parsed: an unauthenticated local endpoint should not be
# willing to buffer an arbitrary upload just to discover it has no valid token.
MAX_BODY_BYTES = 1_000_000

_TOKEN = secrets.token_urlsafe(32)


class CrossSiteRequestError(RequivoError):
    """A state-changing request did not prove it came from this app's own pages."""

    code = "cross_site_request"


def csrf_token() -> str:
    """The token for this server process — rendered into every form, required back on every write."""
    return _TOKEN


def allowed_hosts() -> frozenset[str]:
    """Hostnames this server accepts in a `Host` header: loopback, plus any the operator listed in
    `REQUIVO_WEB_ALLOWED_HOSTS` (comma-separated) when deliberately binding elsewhere."""
    extra = os.getenv(ALLOWED_HOSTS_ENV, "")
    return frozenset(_LOOPBACK_HOSTS | {h.strip().lower() for h in extra.split(",") if h.strip()})


def _hostname(value: str) -> str:
    """The bare hostname from a `Host` header (`[::1]:8765`) or an origin URL (`http://evil.com`),
    lowercased, port and IPv6 brackets removed — so the two are comparable."""
    raw = value.strip()
    if not raw:
        return ""
    try:
        return (urlsplit(raw if "//" in raw else "//" + raw).hostname or "").lower()
    except ValueError:
        return ""


# The Origin header's opaque value, sent verbatim and never as part of a URL: a browser saying "a
# context I decline to attribute". Matched on the raw header rather than on `_hostname()`, which parses
# it into the plausible-looking hostname `"null"`.
OPAQUE_ORIGIN = "null"


def _same_trust_domain(origin_host: str, host: str) -> bool:
    """Is a page served from `origin_host` the same trust domain as the server answering to `host`?

    The same string always is. Beyond that, only the loopback set: `localhost`, `127.0.0.1` and `::1`
    are three spellings of one interface on one machine, the host check above already accepts any of
    them interchangeably, and a page at `http://localhost:8765` can only have been served by *this*
    process — nothing else is listening there. Comparing the two spellings as strings refused a post
    that used both at once, which is a false positive rather than a boundary (#43).

    The hosts an operator listed in `REQUIVO_WEB_ALLOWED_HOSTS` deliberately do **not** join that
    equivalence class, so this is not a membership test over `allowed_hosts()`. Those are real
    hostnames pointing at a deliberate non-loopback bind, and two of them may well be meant as two
    distinct origins — that is the operator's call to make, and inferring it from co-membership in one
    comma-separated list would make it for them, silently, in the widening direction.
    """
    if origin_host == host:
        return True
    return origin_host in _LOOPBACK_HOSTS and host in _LOOPBACK_HOSTS


def _submitted_token(request: Request, body: bytes) -> str:
    """The token the client sent: a header (scripted clients, tests) or the hidden field of a
    urlencoded form (every page in this app). A multipart body carries no token we will read — nothing
    here uploads files, and refusing to parse one keeps the pre-token surface as small as possible."""
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("application/x-www-form-urlencoded"):
        return ""
    try:
        fields = parse_qs(body.decode("utf-8"))
    except UnicodeDecodeError:
        return ""
    values = fields.get(CSRF_FIELD) or [""]
    return values[0]


async def _enforce(request: Request) -> None:
    """Run the checks for one request, raising the first failure. Reads run the host check only;
    anything that can change state runs all four."""
    host = _hostname(request.headers.get("host", ""))
    if host and host not in allowed_hosts():
        raise CrossSiteRequestError(
            f"this server does not answer to host {host!r}",
            details={"host": host, "hint": f"set {ALLOWED_HOSTS_ENV} to bind elsewhere on purpose"})

    if request.method in SAFE_METHODS:
        return

    fetch_site = request.headers.get("sec-fetch-site", "")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        raise CrossSiteRequestError(
            "this request came from another site", details={"sec_fetch_site": fetch_site})

    # `Origin: null` is refused on purpose, and the asymmetry with an *absent* origin below is the
    # reason rather than an oversight. A browser attaches `Origin` to every POST, so no origin at all
    # means no browser is speaking — a scripted client, which the token already gates and which is a
    # supported caller. `null` is the opposite: a browser that is speaking and declining to attribute
    # itself, and it is the one origin a browser-borne attacker can *choose* to emit, from a sandboxed
    # cross-site frame. No page this server serves ever produces it. Before #43 this arm fired only by
    # accident, because `_hostname("null")` happens to return `"null"` and fail an equality test; the
    # outcome is unchanged and the reason is now stated. It is a cheap filter either way — the token
    # remains the load-bearing check for both cases.
    # Read unstripped, so a whitespace-only `Origin` stays truthy here exactly as it did before and
    # still reaches the hostname comparison (where it resolves to `""` and is refused) rather than
    # newly falling through to `Referer`.
    origin_header = request.headers.get("origin", "")
    if origin_header.strip().lower() == OPAQUE_ORIGIN:
        raise CrossSiteRequestError(
            "this request came from an opaque origin, which this server does not accept",
            details={"origin": OPAQUE_ORIGIN, "host": host})

    origin = origin_header or request.headers.get("referer") or ""
    if origin and not _same_trust_domain(_hostname(origin), host):
        raise CrossSiteRequestError(
            "this request came from another origin",
            details={"origin": _hostname(origin), "host": host})

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise InputTooLargeError(
            f"the submitted form exceeds {MAX_BODY_BYTES:,} bytes",
            details={"limit": MAX_BODY_BYTES, "declared": int(declared)})

    # Starlette caches the body for the downstream app, so reading it here to find the token does not
    # consume it — the route still parses its own form as usual.
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise InputTooLargeError(
            f"the submitted form exceeds {MAX_BODY_BYTES:,} bytes", details={"limit": MAX_BODY_BYTES})
    if not secrets.compare_digest(_submitted_token(request, body), _TOKEN):
        raise CrossSiteRequestError(
            "this form did not carry a valid request token — reload the page and try again")


def install_cross_site_guard(app: FastAPI, render_error: Callable[..., Response]) -> None:
    """Register the guard as the innermost middleware.

    It renders its own failures rather than raising: an exception raised in a `BaseHTTPMiddleware` is
    outside the app's `ExceptionMiddleware`, so the registered `RequivoError` handler never sees it and
    the caller would get a bare 500 instead of the intended 403. `render_error` is passed in (rather
    than imported) to keep this module free of any dependency on the app factory.
    """

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        try:
            await _enforce(request)
        except InputTooLargeError as exc:
            return render_error(request, 413, exc.code, exc.message)
        except CrossSiteRequestError as exc:
            return render_error(request, 403, exc.code, exc.message)
        return await call_next(request)
