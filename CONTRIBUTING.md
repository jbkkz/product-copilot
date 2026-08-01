# Contributing to Requivo

Thanks for your interest. Requivo is an early open-source beta, solo-maintained. Contributions,
issues and real-world feedback are all welcome — the feedback we most want is *did the engine ask the
questions a good PM/BA would ask?* (see the **Real-world discovery feedback** issue template).

Before a large change, please open an issue to discuss it first — it saves everyone a wasted PR.

## Project layout in one line

Requivo is one engine behind three interfaces (CLI, Claude Code plugin, local Web). The layers form a
strict DAG: `core/` (no LLM, no I/O) → `providers/` (the only LLM callers) → `services/` (the single
validated apply path) → `render/` + `cli.py` + `web/`. The full map is in
[docs/architecture.md](docs/architecture.md), and the distribution boundary is in
[docs/open-source-strategy.md](docs/open-source-strategy.md). Read those before a structural change.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -U pip setuptools        # a fresh venv often ships pip too old for editable installs
pip install -e ".[dev]"              # deps + the `requivo`/`pc` command + pytest + ruff
```

`uv run requivo …` also works without managing a venv.

## The checks a PR must pass

```bash
.venv/bin/python -m pytest tests/ -q         # pure-logic + offline CLI units (no API calls)
.venv/bin/ruff check src tests scripts        # lint (same invocation as CI)
python -m build --wheel                        # the wheel must still build (needs `pip install build`)
```

CI runs the tests and `ruff check` on Python 3.9–3.13 and builds the wheel (then imports it and builds
every prompt from the installed package). Please run them locally first. The project lints with ruff
but does **not** enforce `ruff format` — match the surrounding style rather than reformatting.

## Conventions

- **English everywhere** — code, comments, docs, prompts, context cards, and the engine's own output
  are all in English. (Chat/issues can be in any language.)
- **Match the surrounding style** — the codebase uses deliberate alignment and compact imports; ruff
  is configured for that (`E501` off, `split-on-trailing-comma` off). Don't reformat unrelated code.
- **Python 3.9 floor** — Pydantic model fields must use `Optional[X]`, not `X | None` (that raises at
  class-definition time on 3.9). `UP045` is disabled for this reason.
- **Tests are required** for logic changes. The test suite must run with **no network / no API key**
  — reasoning-dependent code is tested through an injected fake client, never a live call.
- **Behaviour is tuned in the assets, not the Python.** Prompt and context-card changes must be
  measured through the golden harness (`scripts/golden_run.py` → `scripts/golden_diff.py`); a card
  that helps one request can quietly cost a neighbour. Commit an updated baseline only when the change
  is intended.
- **Keep the output contract in sync.** Each stage's Pydantic contract must agree with its prompt's
  "Output format" block, and slot ids must stay in `framework/model_schema.json`.
- **Session-format compatibility.** The on-disk session format is a product surface. If a change
  alters it, say so explicitly in the PR and describe the migration path — don't break existing
  saved models silently.
- **Documentation** — update the README / CLAUDE.md / relevant docs when you change behaviour, a
  command, or the architecture.

## Adding a context card or an example

- **Context card:** copy `src/requivo/assets/context/_template.md` to `…/context/<name>.md`. It is
  picked up automatically (any non-`_`-prefixed file). Cards must be **generic** — no client name, no
  real request, no confidential business rule. Company-specific cards belong in your own private
  `REQUIVO_CONTEXT_DIR`, never in the repo.
- **Example:** see [examples/README.md](examples/README.md). Public examples must be **synthetic or
  properly anonymised** — no client names, emails, identifiers, or confidential data.

## Do not commit

- **Secrets** — API keys, tokens, passwords, `.env` files. `.env` is gitignored; keep it that way.
- **Real, non-anonymised customer requests or data** — see the data boundary in
  [docs/open-source-strategy.md](docs/open-source-strategy.md#data-what-may-be-public-what-stays-private).
- **Local sessions / generated output** — `.requivo/` and `out/` are gitignored.

If you accidentally commit a secret, tell the maintainer so the credential can be **revoked** — a key
that has touched Git history must be considered compromised even after removal.

## Licensing of contributions

By submitting a contribution, you agree that your contribution will be licensed under the same
**MIT License** that covers the project.

There is no Contributor License Agreement (CLA) or Developer Certificate of Origin (DCO) sign-off in
force today. A lightweight DCO or CLA may be introduced before accepting large external contributions;
if that happens it will be documented here first. Contributing now does not assign any additional
rights beyond the MIT terms above.

## Trademark

The MIT license covers the *code*. It does not grant rights to the **Requivo name, logo, or
identity** — see [TRADEMARKS.md](TRADEMARKS.md). Forks are welcome and may say they are "based on
Requivo"; a substantially modified fork should use a distinct name.
