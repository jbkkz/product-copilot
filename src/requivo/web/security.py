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
    """A request did not prove it came from this app's own pages — the family, not a code to raise.

    Every arm below carries its own code, and that is #52. This one error was raised for six distinct
    facts whose `details` payloads had five different shapes between them, against the rule
    `docs/compatibility.md` states in this repository for exactly this reason (#35): **a code carries
    one fact and one `details` shape**. A consumer matching `cross_site_request` and reading
    `details["origin"]` gets a `KeyError` from the host arm, and the shape it was written against was
    never the contract.

    The counter-argument, which is real and which this rejects: nothing serializes `details` on the
    Web surface — a refusal renders as HTML — so no consumer can observe the inconsistency today, and
    an argued exception in the policy was the other defensible answer. What decides it is that the
    cost is already being paid. Both #43 and #45 had to distinguish their new arm **by message**,
    because the code could not tell them apart, and the same policy says never to match on the
    message. So the only handle a caller has for the distinction is the one it is told not to use.
    That is a present cost, not a future one, and `empty_selector_token` was split for the identical
    shape one release ago.

    The family is kept because `install_cross_site_guard` catches it and answers 403 for every arm,
    and because a caller that wants *any* cross-site refusal should not have to enumerate six names.
    Nothing raises it directly.
    """

    code = "cross_site_request"


class UndeterminedHostError(CrossSiteRequestError):
    """No host could be read from the request at all — absent, empty, or not an authority (#45, #51).

    `details`: `{host_header_present, host_header, hint}`. `host_header_present` is what separates
    *no header was sent* from *a header was sent and could not be read*; both are the same fact here
    — nobody could attribute this request — and the same shape, so they share a code.
    """

    code = "undetermined_host"


class HostNotAllowedError(CrossSiteRequestError):
    """The host was read and is not one this server answers to. `details`: `{host, hint}`."""

    code = "host_not_allowed"


class CrossSiteFetchError(CrossSiteRequestError):
    """The browser's own `Sec-Fetch-Site` says this came from elsewhere. `details`:
    `{sec_fetch_site}`."""

    code = "cross_site_fetch"


class OpaqueOriginError(CrossSiteRequestError):
    """`Origin: null` — a browser speaking and declining to attribute itself (#43). `details`:
    `{origin, host}`."""

    code = "opaque_origin"


class OriginMismatchError(CrossSiteRequestError):
    """The stated origin is not the same trust domain as the host addressed. `details`:
    `{origin, host}` — the same keys as `opaque_origin` and a different fact, which is why they are
    two codes rather than one: a shared shape is not a shared meaning."""

    code = "origin_mismatch"


class MissingRequestTokenError(CrossSiteRequestError):
    """The synchronizer token was absent or did not match — the load-bearing check.

    `details` is deliberately empty. The only fact beyond the code is the token itself, and echoing
    a token back is never a diagnostic. An empty payload is a shape; a payload carrying a secret is
    a defect.
    """

    code = "missing_request_token"


def csrf_token() -> str:
    """The token for this server process — rendered into every form, required back on every write."""
    return _TOKEN


def allowed_hosts() -> frozenset[str]:
    """Hostnames this server accepts in a `Host` header: loopback, plus any the operator listed in
    `REQUIVO_WEB_ALLOWED_HOSTS` (comma-separated) when deliberately binding elsewhere."""
    extra = os.getenv(ALLOWED_HOSTS_ENV, "")
    return frozenset(_LOOPBACK_HOSTS | {h.strip().lower() for h in extra.split(",") if h.strip()})


# What a determined host may contain once `urlsplit` has lowercased it and removed the port and any
# IPv6 brackets: the letter-digit-hyphen set of a DNS name, plus what an IPv6 literal leaves behind
# (`:` between groups, `%` before a zone id) and `_`, which is not legal in a DNS hostname but does
# occur in internal names an operator may deliberately bind to. Anything else means `urlsplit`
# handed back a string that is not a host, and this returns the third state instead of that string.
_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-_:%")


def _hostname(value: str) -> str:
    """The bare hostname from a `Host` header (`[::1]:8765`) or an origin URL (`http://evil.com`),
    lowercased, port and IPv6 brackets removed — so the two are comparable. `""` when this could not
    determine a host at all, which every caller reads as a refusal.

    **Userinfo is refused rather than stripped, and that is #51.** `urlsplit` is a URL parser and
    correctly discards the `user@` part of an authority, so `Host: evil.com@127.0.0.1` resolved to
    `127.0.0.1`, passed `allowed_hosts()`, and `Origin: http://evil.com@127.0.0.1` came out
    same-trust-domain. Not reachable from a browser — none of `Host`, `Origin` or `Referer` is ever
    serialized with userinfo, and RFC 7231 requires a `Referer` to have it removed — so this closes
    a hole with no attacker who benefits.

    It is fixed for the class, not the instance. This is the third time this module's parser answered
    *confidently* about an input it should have refused: #43 was the opaque origin parsing to the
    plausible hostname `"null"`, #45 was an undetermined host read as *no host check needed*, and this
    is the same shape again. The first two were closed with checks at the caller. **This one is closed
    in the parser**, because a caller-side check is a guarantee the next caller inherits without
    re-checking — and `_hostname` has two callers already, on two different headers.

    The charset test is the general form of the same rule and is why `Host: 127.0.0.1 evil.com` now
    refuses too: it previously came back as that whole string, which is not a hostname, and was
    refused only by happening to miss the allowlist. A parser that returns a non-host and relies on a
    later equality test to reject it is answering where it should be declining.

    Known residue, stated rather than implied: an **unbracketed** IPv6 literal (`Host: fe80::1`) still
    parses to `fe80` with the rest read as a port. It is malformed as an authority, no browser emits
    it, and it fails the allowlist — but the parser does answer, so this docstring does not claim the
    class is empty.
    """
    raw = value.strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw if "//" in raw else "//" + raw)
        host = (parts.hostname or "").lower()
        if parts.username is not None:      # any userinfo at all, including an empty `@127.0.0.1`
            return ""
    except ValueError:
        return ""
    if not host or set(host) - _HOST_CHARS:
        return ""
    return host


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
    #
    # Since #51 this arm also covers a header that *was* sent and is not an authority — userinfo, or
    # a character no hostname carries. The wording says "could not read" rather than "did not state"
    # so it is true of both; `host_header_present` in `details` is what tells them apart, which is
    # why they are one code and one shape rather than two (#52).
    raw_host = request.headers.get("host")
    host = _hostname(raw_host or "")
    if not host:
        raise UndeterminedHostError(
            "this request did not name a host this server could read — send a Host header naming "
            "this server",
            details={"host_header_present": raw_host is not None, "host_header": raw_host or "",
                     "hint": "HTTP/1.1 requires a Host header; HTTP/1.0 without one is not supported"})
    if host not in allowed_hosts():
        raise HostNotAllowedError(
            f"this server does not answer to host {host!r}",
            details={"host": host, "hint": f"set {ALLOWED_HOSTS_ENV} to bind elsewhere on purpose"})

    if request.method in SAFE_METHODS:
        return

    fetch_site = request.headers.get("sec-fetch-site", "")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        raise CrossSiteFetchError(
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
        raise OpaqueOriginError(
            "this request came from an opaque origin, which this server does not accept",
            details={"origin": OPAQUE_ORIGIN, "host": host})

    origin = origin_header or request.headers.get("referer") or ""
    if origin and not _same_trust_domain(_hostname(origin), host):
        raise OriginMismatchError(
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
    # **Compared as bytes, because a token this check cannot read is a wrong token — not a crash**
    # (#212). `secrets.compare_digest` on two `str` arguments raises `TypeError` unless both are
    # ASCII-only, and neither side of `_submitted_token` is guaranteed to be: a form field is decoded
    # from the body as UTF-8, and a header is decoded by Starlette as latin-1. So one accented
    # character in a mangled token took the module's own stated rule — a check that cannot read its
    # input has to refuse, not treat it as nothing to check — and broke it in the loudest available
    # way. The `TypeError` escaped `_guard`'s two `except` arms, so it never became the 403 this line
    # is written to raise; it propagated *past* `security_headers` and landed on the outermost 500
    # handler, making the one crash path in the security module also the one response class served
    # with no CSP, no nosniff and no Referrer-Policy.
    #
    # `surrogatepass` rather than a bare `.encode()` for the same reason the line is here at all: a
    # lone surrogate would raise `UnicodeEncodeError` and reintroduce the identical shape one codec
    # along. Neither path above can produce one today — that is precisely the kind of "today's
    # callers happen to pre-filter it" argument this module declines to narrow a check on. Every
    # `str` now has an encoding, so every input reaches a verdict.
    #
    # Pinned by `test_a_token_this_server_cannot_compare_is_refused_rather_than_crashing` and
    # `test_a_latin1_token_header_reaches_the_refusal_rather_than_the_comparison`.
    submitted = _submitted_token(request, body).encode("utf-8", errors="surrogatepass")
    if not secrets.compare_digest(submitted, _TOKEN.encode("ascii")):
        raise MissingRequestTokenError(
            "this form did not carry a valid request token — reload the page and try again",
            details={})


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
