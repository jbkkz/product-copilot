# Architecture

> How Requivo is put together. For the model itself, see
> [requirements-model.md](requirements-model.md); for storage, [session-format.md](session-format.md).

Requivo is a provider-independent **Core** with interchangeable interfaces on top. The reasoning is a
single LLM call per turn, and that call lives in a **provider**, never in the Core.

```text
   Claude Code        Web        CLI / API
         \             |             /
                  Requivo Core
          validated, versioned model
```

## Layers

The code is the `requivo` package under `src/`. The layers form a strict DAG:

- **`core/`** — the deterministic engine. No LLM, no provider, no argv/stdout (enforced by
  `tests/test_boundaries.py`). It validates, versions and reasons over the model; it never *produces*
  one. Holds the Pydantic contracts, validation, readiness, the dependency graph, persistence and the
  structured error hierarchy.
- **`providers/`** — the only place an LLM is called. `anthropic.py` (behind the optional
  `requivo[anthropic]` extra) turns a request into a model and a model into an artifact.
- **`services/`** — the application seam shared by every interface. `SessionService.update_model` is
  the single validated apply path (validate → diff → propagate → revision → stale-flag).
  `DiscoveryService` is the provider-backed orchestration (start / answer / generate) the CLI and Web
  both call, so there is one pipeline, not two. Storage is injected as a `SessionRepository`
  (`FileSessionRepository` today; Postgres-swappable for a future service).
- **`render/`** turns data into strings; **`cli.py` + `deterministic.py`** are the only layers that
  touch argv/stdout/TTY. **`web/`** is a thin FastAPI + Jinja2 + HTMX layer over the same services.

Every interface — the terminal CLI, the Claude Code plugin, the local Web app — is a thin layer over
the same Core. There is no second implementation of the apply path.

## The three interfaces

- **CLI** — provider verbs (`discover`, `answer`, generators) plus offline deterministic verbs.
- **Claude Code** — Claude reasons in your session and writes a proposal; the deterministic CLI
  validates and applies it. No API key. Lives in `plugins/claude-code/` (not shipped in the wheel).
- **Web** — `requivo web`, a local single-user UI over the services. See [web.md](web.md). It is not
  Requivo Cloud.

## Bundled assets

Prompts, the framework schema, context cards and the demo payload live inside the package at
`src/requivo/assets/`, so they ship in the wheel and a `pip install` works outside a clone. The Web
interface's templates and static files ship the same way.

## Tuning behaviour

Behaviour is tuned by editing the Markdown/JSON assets (prompts, context cards, schema), not the
Python. Because the engine is non-deterministic, changes are measured with the golden harness — see
[evaluations.md](evaluations.md).
