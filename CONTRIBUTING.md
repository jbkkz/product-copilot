# Contributing to Requivo

Thanks for your interest. Requivo is a solo-maintained open-source project. Contributions,
issues and real-world feedback are all welcome — the feedback we most want is *did the engine ask the
questions a good PM/BA would ask?* (see the **Real-world discovery feedback** issue template).

Before a large change, please open an issue to discuss it first — it saves everyone a wasted PR.

## Project layout in one line

Requivo is one engine behind three interfaces (CLI, Claude Code plugin, local Web). The layers form a
strict DAG: `core/` (no LLM, no provider, no argv/stdout — reading and writing files *is* core's
job) → `providers/` (the only LLM callers) → `services/` (the single validated apply path) →
`render/` + `cli.py` + `web/`. The full map is in
[docs/architecture.md](docs/architecture.md), and the distribution boundary is in
[docs/open-source-strategy.md](docs/open-source-strategy.md). Read those before a structural change.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -U pip setuptools        # a fresh venv often ships pip too old for editable installs
pip install -e ".[dev]"              # deps + the `requivo` command + pytest + ruff
```

`uv run requivo …` also works without managing a venv.

## The `.claude/` directory is maintainer tooling — you need none of it

This repository tracks a `.claude/` directory: an empty `settings.json`, and a `jit-context/` rule
layer beneath it. One of the rules
(`jit-context/tools/01-oss/supertool-required.md`) declares `mode: block` over `Read`, `Edit`,
`Write`, `Glob` and `Grep`, with a `match:` of everything, and names a `supertool` command as the
replacement. Read cold, that looks like a repository which refuses to let you open a file unless you
have a tool you have never heard of.

It is not, and the reason is mechanical. A jit-context rule is **data**. The only thing that reads it
is a `PreToolUse` hook, and that hook ships inside the `claude-jit-context` plugin, registered from
that plugin's own manifest. This repository registers no hooks of its own, and it goes further than
that: `.claude/settings.json` is tracked as an empty JSON object. No plugin enablement, no key naming
a command, and no hook script tracked anywhere under `.claude/`. Without that plugin installed there
is no hook, nothing reads the layer, and every file operation behaves exactly as it normally does.
`tests/test_agent_layer.py` is the guard that keeps that true, so it cannot quietly stop being true.

The file is empty rather than merely hook-free because it did stop being true once. A `statusLine`
command pointing at a maintainer script *outside* `.claude/` sat in the tracked settings from #186
until #215, executing on the machine of anyone who cloned `main` in between, while the guard — which
read the `hooks` key and only the `hooks` key — stayed green. No tagged release carries it; the
plugin enablement beside it shipped in every release from 0.10.0 to 1.2.0. Both live in
`.claude/settings.local.json` now, which `.gitignore` excludes. If a key ever returns to the tracked
file it has to be added to the allowlist in `tests/test_agent_layer.py` and described here, in the
same change; the test fails otherwise.

Three more tracked files belong to the same maintainer loop and are equally inert for a
contributor: `.oss.json` and `.oss/` configure the `oss` plugin that runs this repository's
maintenance (see [.oss/README.md](.oss/README.md)), and `.supertool.json` configures its shell
tooling. Nothing in the test suite, the build or the product reads any of them.

So: **contributing needs Python, git and the setup above — nothing else.** If the directory bothers
you, delete it in your working copy; just do not commit the deletion. A fresh git worktree takes its
rule layer from git, and this repository's maintenance loop cuts one worktree per issue, which is why
the layer is tracked here rather than kept in a personal config.

## The checks a PR must pass

```bash
.venv/bin/python -m pytest tests/ -q         # pure-logic + offline CLI units (no API calls)
.venv/bin/ruff check src tests scripts        # lint (same invocation as CI)
python -m build --wheel                        # the wheel must still build (needs `pip install build`)
```

CI runs the tests and `ruff check` on Python 3.9–3.13 and builds the wheel (then imports it and builds
every prompt from the installed package). Please run them locally first. The project lints with ruff
but does **not** enforce `ruff format` — match the surrounding style rather than reformatting.

### What the changelog gate does not cover

The changelog gate (`.github/workflows/oss-changelog.yml`, which requires a `changelog.d/` fragment)
triggers on **`pull_request` only**. Direct pushes to `main` are never checked by it.

That matters here because a direct push to `main` is not impossible, only rare. Every change now
lands as a squash-merged pull request, with one deliberate exception: the `chore(release)` commit
that cuts a version goes straight to `main`. So the uncovered class is small and known rather than
hypothetical — and a release commit is precisely the one whose changelog entry a reader is most
likely to go looking for. (A count is deliberately not quoted here — it changes with the next
release, and a stale number is its own small version of this same problem.)

So **a green board means the changelog gate passed on the commits it was shown**, not that every
change in the release carries a fragment. Those are different claims, and nothing on the board
distinguishes them.

This limit is stated rather than closed, deliberately. A `push:` trigger on `main` would go red
*after* the fact — a fragment cannot be added retroactively to a commit already pushed — installing a
permanently red default branch, which is a worse lie than the one it fixes. If you push directly to
`main`, add the fragment in that same commit; nothing will remind you.

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
- **Local sessions / generated output** — `.requivo/`, `out/` and `demo-out/` are gitignored.

If you accidentally commit a secret, tell the maintainer so the credential can be **revoked** — a key
that has touched Git history must be considered compromised even after removal.

## Licensing of contributions

By submitting a contribution, you agree that your contribution will be licensed under the same
**Apache License 2.0** that covers the project.

There is no Contributor License Agreement (CLA) or Developer Certificate of Origin (DCO) sign-off in
force today. A lightweight DCO or CLA may be introduced before accepting large external contributions;
if that happens it will be documented here first. Contributing now does not assign any additional
rights beyond the Apache-2.0 terms above.

## Trademark

The Apache-2.0 license covers the *code*. Section 6 of that license is explicit that it grants no
trademark rights, and this project relies on that rather than adding a term of its own: it does not
grant rights to the **Requivo name, logo, or identity** — see [TRADEMARKS.md](TRADEMARKS.md). Forks
are welcome and may say they are "based on Requivo"; a substantially modified fork should use a
distinct name.
