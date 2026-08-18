# Requivo Web

The **primary Requivo interface**: a local, single-user, self-hostable browser workspace over the same
Core, services and session format as the CLI and the Claude Code plugin. It is where the product's
workflow lives — paste a request, work through what could change the solution, and leave with a
decision brief. Sessions it creates open in the other two, and theirs open here.

"Primary" is about weight, not capability. The CLI can do everything this can and more; it is
infrastructure. This is the one to hand someone who has a request and half an hour.

Requivo Web is deliberately small — see [Scope](#scope).

## Install

```bash
uv tool install "requivo[web,anthropic]"   # web UI + the Anthropic provider (for discovery/generation)
uv tool install "requivo[web]"             # web UI only — review existing sessions, no provider
```

For development from a checkout:

```bash
uv sync --extra web --extra anthropic
uv run requivo web --no-open --port 8765
```

The web dependencies (FastAPI, Uvicorn, Jinja2, python-multipart) are an **optional extra** — they are
never imposed on CLI or Claude Code users. The templates, CSS and a vendored copy of HTMX ship inside
the wheel, so nothing loads from a CDN and the UI works offline.

## Run

```bash
requivo web \
  --workspace .        # where sessions live (default: current directory)
  --host 127.0.0.1     # default; localhost only
  --port 8765          # default
  --no-open            # do not open a browser automatically
  --reload             # auto-reload on code changes (development)
```

By default the server binds to `127.0.0.1`, prints its URL, and opens your browser. The
`ANTHROPIC_API_KEY` is read from the **server environment** — it is only needed for provider actions
(discovery, generation); reviewing existing sessions needs no key. The key is never shown in the
browser, never a form field, never logged.

## The workflow

One path leads the product, and the interface is built around it rather than around the model:

```text
paste a request
  → read what Requivo understood
  → answer the few questions that could change the solution
  → see what those answers moved
  → generate one decision brief
  → change an answer later and see what needs review
```

- **Home** — the request box *is* the home page; there is no separate "new discovery" screen. Below it,
  the requests already in progress, each showing what was asked, whether it is waiting on you, and
  whether a document needs updating. Sessions created by the CLI or Claude Code appear here too.
- **Advanced settings** — session name, product context cards, and whether to analyse now or just save
  the request. Collapsed by default: the server already knows whether a provider action can run, so it
  resolves that itself instead of asking. The API key is never a form field.
- **Session** — the request, what Requivo understood, at most five questions (each with *why it
  matters* and its likely area of impact), the answer form, *Are we ready?* in one action state with
  its reasons, and the decision brief.
- **Answer** — submit answers; the understanding is refined as a new revision (optimistic-locked), and
  the page leads with **What changed**: which parts of the solution moved, which decisions and
  assumptions need review, and which documents need updating. All of it computed from the dependency
  graph, never generated.
- **Generate** — the decision brief is the one primary action. PRD, acceptance criteria, epic and
  release notes live under *More documents*. Each is saved with its source revision and marked *Draft*
  when high-impact topics are still unresolved. Nothing is ever regenerated on your behalf.
- **Traceability details** — one disclosure holding everything the engine knows: the per-topic
  understanding, coverage, every open question, the decisions and contested premises, provenance, and
  the raw model export. The primary flow works without opening it.

## Architecture

Requivo Web is a thin layer — it owns **no business logic**:

- Routes parse the request, call an application **service**, and render a Jinja template. They never
  touch the filesystem, never read or write `model.json`, and never shell out to the CLI.
- Discovery, answers and generation all go through `DiscoveryService` — the *same* orchestration the CLI
  uses — which calls the provider and applies the result through `SessionService` (validate → diff →
  propagate → revision → stale-flag) and `ArtifactService` (save with source revision).
- Readiness, the understanding split and staleness are computed in the Core; the templates only render
  the `SessionService.status()` projection through small view models — no logic in Jinja.

```
browser ──HTTP──> routes ──> DiscoveryService / SessionService / ArtifactService ──> Core ──> .requivo/
                     │                        (the same services the CLI calls)
                     └── Jinja templates + view models (presentation only)
```

### When something goes wrong

A structured `RequivoError` becomes a clean page, never a traceback, and its HTTP status answers one
question: **is this about what you sent, or about the state of the server you sent it to?**

- **4xx — your request.** A name that does not resolve (`404`), a malformed model or selection
  (`400`), a submission over the ceiling (`413`), a write that raced another one (`409`).
- **5xx — this server, or what it depends on.** It cannot read its own context-card directory
  (`500`), it has no cards installed at all (`500`), the model would not return valid output after
  every retry (`502`), or a session lock did not clear (`503`).

That split used to leak: every code the status table did not list fell through to `400`, so a
server-side fault told the reader they had made a bad request. Every code now has an explicit
mapping and a test fails if a new one is added without deciding which side it falls on. The full
table, and what changed for anyone scripting against it, is in
[compatibility.md](compatibility.md#http-statuses-in-requivo-web).

A 5xx is also logged in the terminal you started the server in — the page a reader gets says little
by design, and an operator otherwise has no record of a condition the reader cannot act on.

## Security (local by default)

Even though it is a local app:

- The server binds to `127.0.0.1` by default. Passing `--host 0.0.0.0` prints a warning: there is **no
  authentication**, so the app must not be exposed on an untrusted network.
- **Writes are protected against cross-site requests.** Binding to loopback keeps nobody out — any page
  open in the same browser can post to a known local port without a preflight, and for this app writing
  is the damage (sessions created, provider calls billed). Four checks run in `web/security.py`: a host
  allowlist (loopback, plus anything in `REQUIVO_WEB_ALLOWED_HOSTS` — this is the DNS-rebinding guard,
  and the only one that also runs on reads), the browser's `Sec-Fetch-Site` hint, an `Origin`/`Referer`
  trust-domain match, and a per-process request token rendered into every form. A page held open across
  a server restart needs a reload to pick up the new token.
- **A request that names no host is refused, not waved through.** The host allowlist used to skip
  itself when it could not determine a `Host` — an absent header, or an empty one — so the one request
  nobody could attribute walked past the only check that also runs on reads, and nothing reported that
  it was off (#45). It is now the third state, stated: the refusal says the host could not be
  determined rather than borrowing the wording of a genuine mismatch. This does refuse an HTTP/1.0
  request that sends no `Host` at all; HTTP/1.1 requires one, every browser and ordinary client sends
  one, and HTTP/1.0 is not a supported caller here.
- **The three loopback spellings are one origin.** `localhost`, `127.0.0.1` and `::1` name one machine,
  so a page served on any of them may post to any other — the host allowlist already accepted them
  interchangeably, and comparing them as strings refused a form that used two at once, with no way
  forward from the error page (#43). A host you listed in `REQUIVO_WEB_ALLOWED_HOSTS` is **not** in that
  equivalence: two real hostnames there must match exactly, because whether they are one trust domain is
  your call and not something the app should infer from one comma-separated list. `Origin: null` — the
  opaque origin a sandboxed cross-site frame sends — is refused; no origin header at all is accepted,
  which is what lets `curl` with a valid token work, and the reasoning for the difference is in
  `web/security.py`.
- **That equivalence is the loopback interface, not this process, and the port is deliberately not
  compared.** A page on *any* loopback port passes the origin check — `http://localhost:3000` as much
  as the port Requivo is serving on — because the comparison discards the port on both sides. That
  predates the loopback-spelling change above and is kept on purpose: the request token is what gates
  the write, and a page on another port cannot read one, because the browser's own same-origin policy
  counts the port and this app sends no CORS headers. Comparing ports here would add nothing and would
  reintroduce the failure it just fixed, since a default port is elided in an `Origin` but spelled out
  in a `Host` (#46).
- Every slug is validated in the Core (strict kebab-case, no path separators or dot segments), so a
  request can never escape `.requivo/sessions/`.
- Only the package's `static/` directory is served — never the workspace, `.requivo`, `.env` or `.git`.
- The Anthropic key is read from the server environment and never rendered into HTML or logged.
- All rendered content is HTML-escaped (Jinja autoescape); artifact Markdown is shown in a code block.
- Conservative headers are set: `X-Content-Type-Options`, `Referrer-Policy`, and a `Content-Security-Policy`
  that allows only same-origin assets (so the vendored HTMX and local CSS are the only scripts/styles).
  HTMX is vendored rather than fetched from a CDN for exactly that reason, and because the app is meant
  to work offline; its version and licence are recorded in `THIRD-PARTY-NOTICES.md`.
- Input is length-bounded, and an over-long request or answer is **refused, not truncated** — half a
  request folded into the model reads exactly like a whole one. That refusal is the only bound the
  reader meets: no field carries an HTML `maxlength`, because a browser clips a paste to the remaining
  allowance silently — no event, no message, no visual difference — so an over-long request would
  arrive at exactly the ceiling and sail through the very check written to stop it (#8). A client-side
  affordance is welcome here, but it has to count and warn; it must never trim what the reader typed.
  Request bodies are capped before they are parsed. An unknown context card is an error too:
  filtering it out would leave an empty selection, which every reader downstream treats as "load
  every card".

## Limits of this first version

- Generation covers every document the shared service produces — decision brief, PRD, acceptance
  criteria, delivery epic, release notes. The buttons come from the service's own vocabulary, so a new
  generator appears here without touching the Web. The epic's tracker exports (`epic.json`,
  `epic.github.json`, `epic.gitlab.json`) remain CLI-only; `stories` and `estimate` are terminal
  analyses that produce no document at all.
- Provider calls are synchronous (run in a worker thread so the event loop is not blocked); a request
  waits for the result, with an HTMX loading state. No job queue, no WebSockets.
- Artifacts are shown as escaped Markdown in a code block, not rendered to HTML.
- Readiness is binary (ready + unresolved topics), as in the Core — no invented "levels".
- **What changed** is shown after the answer that caused it, and is not persisted: reloading the page
  loses the narrative. What *is* persisted is the consequence — each document carries its own "needs
  updating" flag on disk. Keeping a full impact history would mean a new field in `session.json`, so a
  format bump and a migration, for a display; that is a decision to take once real use asks for it.
- Single user, single workspace, no concurrent-editing UI beyond the optimistic-lock conflict message.
  Two tabs cannot corrupt a session — a generation carries the revision it read as a precondition, so a
  concurrent change surfaces as a conflict rather than being overwritten — but the second tab is not
  live-updated; it finds out when it next submits.

## Scope

Requivo Web is intentionally bounded to: local, single-user, filesystem-backed, no authentication, no
organizations, no collaboration, no billing, no remote storage, no telemetry, no database, no SaaS
infrastructure.

That is a scope decision, not a missing feature list. Everything the interface does runs against your
own filesystem, and the boundary is what keeps the security posture simple enough to state in a
paragraph — see [SECURITY.md](../SECURITY.md).
