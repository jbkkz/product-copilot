"""Cross-site request protection for Requivo Web.

Binding to `127.0.0.1` is not a security boundary. Any page the user has open in the same browser can
POST to a known local port, and a plain HTML form post is not subject to a CORS preflight — the browser
sends it and simply refuses to let the attacker *read* the reply. For this app, writing is the damage:
a hostile page could create sessions, submit answers, and burn the server's Anthropic key, and the
attacker never needs to see a single response to do it.

Four checks close that, in the order they run:

  * **Host allowlist** — the `Host` header must name a loopback address (or one the operator opted into
    via `REQUIVO_WEB_ALLOWED_HOSTS`), and a request that names no host *at all* is refused rather than
    waved through — a check that cannot read its input has to say so, not treat it as nothing to check
    (#45). This is the DNS-rebinding guard, and it is the only check that applies to reads as well: a
    rebound `evil.com` that resolves to 127.0.0.1 is same-origin from the browser's point of view, so
    it would sail through every other check *and* be able to read the token.
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
    are three spellings of one interface on one machine, and the host check above already accepts any
    of them interchangeably. Comparing the two spellings as strings refused a post that used both at
    once, which is a false positive rather than a boundary (#43).

    What that accepts is the loopback **interface**, not this process. `_hostname` discards the port on
    both sides, so `http://localhost:3000` and `http://localhost:8765` arrive here as the same string:
    the accepted set is every page served by every process on any loopback port, which on a developer
    machine is a populated one. This docstring used to claim the opposite — that such a page *"can only
    have been served by this process, nothing else is listening there"* — and that was simply false. A
    rationale is what the next change gets reasoned from, so a wrong one is worse than none (#46).

    The port-blindness is deliberate, and it predates #43 rather than following from it: before that
    fix, `Origin: http://localhost:3000` against `Host: localhost:8765` already compared equal. It
    stays, because the **request token** is what gates the write and a page on another loopback port
    cannot get hold of one. The browser's own same-origin policy is (scheme, host, port), so reading a
    page this server rendered is a cross-origin read; this app sends no CORS headers, so the body never
    reaches the script. `Sec-Fetch-Site` refuses that same post one check earlier, as `same-site`, on
    every browser that sends it. Comparing ports here would add nothing those two do not already do,
    and it would reintroduce #43's exact failure shape — a default port elided in an `Origin` but
    spelled out in a `Host`, refusing a form with no way forward. Tightening it is a separate decision
    needing its own tests; `test_a_cross_port_loopback_origin_is_accepted_and_that_is_the_decision`
    pins the behaviour so that change has to argue with this paragraph rather than slip past it.

    The hosts an operator listed in `REQUIVO_WEB_ALLOWED_HOSTS` deliberately do **not** join that
    equivalence class, so this is not a membership test over `allowed_hosts()`. Those are real
    hostnames pointing at a deliberate non-loopback bind, and two of them may well be meant as two
    distinct origins — that is the operator's call to make, and inferring it from co-membership in one
    comma-separated list would make it for them, silently, in the widening direction.

    An empty string on either side is not a match, and that arm is the point rather than a special
    case. `""` is what `_hostname` returns when it could not find a hostname *at all* — an absent or
    unparseable `Host`, or an origin such as `http:///` that is a well-formed URL naming nobody. Two of
    those facing each other used to compare equal, so the one input where **neither** side was
    determined produced the same verdict as a verified match: a check that could not look, answering
    anyway. Refusing costs nothing real — no browser omits `Host`, and a request that reaches here at
    all has already stated an origin — and it keeps this function's name true for every input.

    Since #45 the `host` half of that arm is unreachable from the only caller: `_enforce` refuses an
    undetermined `Host` outright, several checks earlier, so `host` is always determined by the time it
    gets here. It is kept rather than pruned as now-dead, and deliberately. A helper that makes a claim
    by name should hold for every input it is handed, this one is called directly by its own tests, and
    narrowing a security helper on the grounds that today's single caller happens to pre-filter its
    input is how the next caller inherits a guarantee nobody re-checked.
    """
    if not origin_host or not host:
        return False
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
    # An undetermined `Host` is a **refusal**, not a skip. `_hostname` returns `""` when it could not
    # find a host at all — an absent header, an empty or whitespace-only one, or one that will not
    # parse — and the earlier `if host and host not in allowed_hosts()` read that as *no host check
    # needed*. So the one request nobody could attribute walked past the only check that also runs on
    # reads, and the guard reported nothing while it was off. Observed rather than reasoned: against
    # the 0.10.1 candidate, `GET / HTTP/1.0` with no `Host` and `GET / HTTP/1.1` with an empty one both
    # answered 200, because h11 requires `Host` on 1.1 only and passes an empty one straight through.
    #
    # This refuses a `GET`, which is a real behaviour change and the intended one. HTTP/1.1 requires a
    # `Host` and every browser, `curl`, httpx and requests sends one; nothing here documents HTTP/1.0
    # support; and a caller able to craft a hostless request can open a socket to this port directly,
    # so it gains nothing from the skip that it did not already have. The cost is a caller that does
    # not exist. What it buys is the third state stated instead of silently folded into the clean one:
    # *could not determine the host* now reads differently from *determined it and was happy*.
    raw_host = request.headers.get("host")
    host = _hostname(raw_host or "")
    if not host:
        raise CrossSiteRequestError(
            "this request did not state which host it was addressed to — send a Host header naming "
            "this server",
            details={"host_header_present": raw_host is not None, "host_header": raw_host or "",
                     "hint": "HTTP/1.1 requires a Host header; HTTP/1.0 without one is not supported"})
    if host not in allowed_hosts():
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
