# Requivo Web

A **local, single-user, self-hostable** browser interface over the same Requivo Core, CLI and session
format. It exists so someone less comfortable in a terminal can run discovery, answer the priority
questions, review readiness and reasoning, and generate a solution assessment or a PRD — all against
sessions that also open in the CLI and Claude Code.

Requivo Web is deliberately small. It is **not** [Requivo Cloud](#requivo-web-is-not-requivo-cloud).

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

## What you can do

- **Home** — the product page and a list of local sessions with revision, last change, readiness, and
  artifact/stale counts. Sessions created by the CLI or Claude Code appear here too.
- **New discovery** — paste a product request, optionally name the session and pick context cards, then
  either run discovery now (Anthropic) or *create session only* to capture the request and run it later.
- **Session** — the understanding split (explicit facts / inferred assumptions / unknowns, with a
  *partial* marker for stated-but-thin topics), readiness and what blocks it, the priority questions
  with a single answers form, the persisted decisions / challenges / opportunities, and the artifacts.
- **Answer** — submit answers; the model is refined as a new revision (optimistic-locked), and the page
  shows what changed, which reasoning it unseated, and which artifacts went stale.
- **Generate** — a solution assessment or a PRD, saved with its source revision and marked *Draft* when
  blocking unknowns remain. View it in the browser or download the Markdown.

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

## Security (local by default)

Even though it is a local app:

- The server binds to `127.0.0.1` by default. Passing `--host 0.0.0.0` prints a warning: there is **no
  authentication**, so the app must not be exposed on an untrusted network.
- Every slug is validated in the Core (strict kebab-case, no path separators or dot segments), so a
  request can never escape `.requivo/sessions/`.
- Only the package's `static/` directory is served — never the workspace, `.requivo`, `.env` or `.git`.
- The Anthropic key is read from the server environment and never rendered into HTML or logged.
- All rendered content is HTML-escaped (Jinja autoescape); artifact Markdown is shown in a code block.
- Conservative headers are set: `X-Content-Type-Options`, `Referrer-Policy`, and a `Content-Security-Policy`
  that allows only same-origin assets (so the vendored HTMX and local CSS are the only scripts/styles).
- Input fields are length-bounded.

## Limits of this first version

- Generation is limited to the **solution assessment** and the **PRD**. Stories, acceptance criteria,
  estimate and epic already exist as CLI generators and can be added without new orchestration.
- Provider calls are synchronous (run in a worker thread so the event loop is not blocked); a request
  waits for the result, with an HTMX loading state. No job queue, no WebSockets.
- Artifacts are shown as escaped Markdown in a code block, not rendered to HTML.
- Readiness is binary (ready + blocking topics), as in the Core — no invented "levels".
- Single user, single workspace, no concurrent-editing UI beyond the optimistic-lock conflict message.

## Requivo Web is not Requivo Cloud

Requivo Web is intentionally bounded to: local, single-user, filesystem-backed, no authentication, no
organizations, no collaboration, no billing, no remote storage, no telemetry, no database, no SaaS
infrastructure.

The future **Requivo Cloud** is a separate, private product that may add accounts, authentication,
workspaces, PostgreSQL, collaboration, managed storage, quotas, billing, managed LLM providers, and
enterprise administration. None of that is part of Requivo Web.
