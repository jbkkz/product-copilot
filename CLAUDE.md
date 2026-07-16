# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Product Copilot — Requirements Engine.** Turns a vague client request into a *structured solution
model* ready for dev. It is **not a chatbot**: the chat is only the interface. The product is the
**model** (a set of typed slots) and the **engine** that progressively fills it until it is precise
enough to build from. The whole repo — code, comments, docs, prompts, context, and the engine's own
output — is in English.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -U pip setuptools   # a fresh venv often ships pip < 21.3, too old for editable installs
pip install -e ".[dev]"         # deps + the `pc` command + pytest
cp .env.example .env      # then set ANTHROPIC_API_KEY (MODEL defaults to claude-sonnet-5)

pc discover "We'd like to set up a leave approval system."   # discovery → out/<slug>/model.json
pc status  out/<slug>/model.json                              # understanding checklist + readiness
pc prd     out/<slug>/model.json                              # regenerate any artifact from a saved model
```

`pc` is the modern subcommand CLI: `discover`, `status`, `impact`, `brief`, `prd`, `stories`,
`estimate`, `criteria`, `epic` (`--json/--github/--gitlab`), `release`. `pc impact <model> [slots…]`
is a pure offline query over the dependency DAG (no API call): the blast radius of a change (the
decisions to re-validate + artifacts that go stale), or the full map with no slots. Without an
install, `python pc.py <cmd>`
(a repo-root launcher that puts `src/` on the path) is equivalent — this is what the Claude Code
`/pc-*` commands call. The **legacy flag CLI is preserved**: `python src/engine.py "…" [--once]
[--prd] [--stories] …` and `python src/engine.py --from out/<slug>/model.json --prd` still work
identically (`src/engine.py` is now a backward-compat shim).

Run the tests with `.venv/bin/python -m pytest tests/ -q` (pure-logic + offline CLI units via an
injected fake client, no API calls); there's no build step. A complete worked example lives in
`examples/leave-approval/` (request → model.json → brief → PRD).

## Architecture

The engine is a **single Anthropic call** (per turn) whose intelligence lives entirely in assembled
prompt data, not in Python. The code is the `product_copilot` package (under `src/`); the historical
`src/engine.py` is now a backward-compat shim re-exporting it. The layers form a DAG — **`core/`
never prints and never reads argv; `render/` turns data into strings; `cli.py` is the only layer that
touches argv/stdout/TTY** — so every interface (terminal `pc`, the Claude Code `/pc-*` wrappers in
`.claude/commands/`, and later an API or MCP) is a thin layer over the same core, never a second
implementation:

```
product_copilot/
  paths.py         ROOT — single source of truth for asset/output resolution
  core/            the engine (presentation-free)
    contracts.py     Pydantic models + enums          llm.py         prompt assembly + _complete
    analysis.py      Python-authoritative model logic  discovery.py   run() (the engine turn)
    persistence.py   save/load_model, write_artifact   adapters.py    epic_export + GitHub/GitLab
    generators.py    advise, derive_stories, estimate, generate_*
    dependencies.py  the dependency DAG: propagate / diff_models / stale_on_disk (impact propagation)
  render/            views (data → str/stdout, no side effects)
    markdown.py      *_markdown                         terminal.py    render_*
  cli.py           argparse `pc` subcommands (app()) + the legacy flag main()
```

The runner is a thin dispatch:

1. `build_prompt(name)` loads a prompt file (`engine.md`, `stories.md`, `estimate.md`, `brief.md`)
   and substitutes two placeholders:
   - `{{SCHEMA}}` ← `framework/model_schema.json` (the slot definitions + the driver rule)
   - `{{CONTEXT}}` ← `load_context()`, which concatenates every `context/*.md` **except** files
     whose name starts with `_` (so `_template.md` is skipped).
2. Every model reply must be **JSON only**. `_complete()` is the shared call: `_first_text()` skips
   non-text blocks, `_extract_json()` strips a ```json fence or slices `{ … }`, and the result is
   validated against a Pydantic contract. On malformed/non-conformant JSON it retries (default 2×)
   with a corrective nudge in a *local* message copy, so the caller's clean history is never
   polluted. `run()`/`derive_stories()`/`estimate()`/`advise()` are thin wrappers over it.
3. Rendering is split: `render_turn()` is the lightweight per-turn view (a ✅/🟡/⚪ Understanding
   checklist + priority questions); `render_brief()` is the deliverable — the **SOLUTION ASSESSMENT**,
   a two-tier document in PM language (the function/contract/prompt keep the `brief` name; only the
   printed title and the product term are "solution assessment"). It's a *judgment*, not a recap: an
   Executive Summary (Problem / Solution / Challenge / Risks / Next) a PM reads in seconds, then the
   full analysis (Understanding checklist, Design decisions, **Challenges**, Complexity + why, Main
   risks, ranked Opportunities, Next steps, Ready-for-implementation with a single blocker). The
   checklist, discovery-complete %, decision states and readiness are computed **in Python**; the
   advisory `Brief` (problem, solution, introduces, `challenges` [premise/alternative/consequence/
   recommendation], complexity + reasons, risks, opportunities ranked by `Leverage` (each naming the `modules` it
   reaches, grounded in model + context), next steps,
   `decisions` as `DesignDecision` [decision + optional why/alternative/tradeoff], open_decisions) is
   LLM-generated. The `challenges` block **contests the premise** (grounded in model + context, never
   generic); it's the core differentiator. Both layers must avoid exposing internals (slot ids,
   completeness numbers, confidence labels) in user-facing text — `prompts/brief.md` has an explicit
   Voice rule enforcing this for the LLM prose.

**Consequence for changes:** behavior is tuned by editing the Markdown/JSON assets, not the Python.

## The output contract (keep in sync)

Each stage has a Pydantic contract that must agree with its prompt's "Output format" block:
`EngineOutput` (`model`/`questions`/`summary`) ↔ `engine.md`, `Stories` ↔ `stories.md`,
`EstimateDraft` ↔ `estimate.md`, `Brief` (`introduces`/`complexity`/`cost_driver`/`risks`/
`opportunities`) ↔ `brief.md`. Slot ids live in `framework/model_schema.json` (which also carries
each slot's `pillar` and `label`, read back by the renderer via `_slot_meta()`).

Pydantic validates at the boundary, so a rename that breaks a contract fails **loudly in `_complete()`**
instead of silently mis-rendering. The field is literally named `model` (Pydantic
`protected_namespaces=()` allows it). Per-slot keys: `completeness` (0-100), `confidence`
(explicit|inferred|empty), `impact` (low|medium|high), `value`, `evidence`.

## The two core concepts

- **Slots (the atomic unit).** Every requirement lives in a slot (see keys above). Slots are grouped
  into 4 navigation pillars (Why / What / How / Validate) defined in `framework/elicitation.md`. Every
  output is a render of the same filled model: the status bars are its per-pillar completeness, the
  questions are its *gaps*, the brief is a consultant's read of it (`inferred` slots feed the
  "Assumptions to confirm" section).

- **The driver: `information_value = uncertainty × impact`.** The engine does **not** ask because a
  slot is empty — it asks where information value is high. Empty-but-low-impact slots are left alone;
  filled-but-risky slots get probed. **Impact is estimated from the product context** — so the engine
  is only as sharp as the `context/*.md` cards it's given. This is the central design idea; preserve
  it when editing prompts.

## Multi-turn refinement

Interactive mode (`converse()`) loops: `render_turn` → ask → collect answers → feed back → repeat, up
to `MAX_TURNS` (8). The previous turn's validated output IS the state being refined — it is carried in
the conversation history (re-serialized via `model_dump_json()`), not rebuilt from scratch. The
engine flips `inferred → explicit` and raises `completeness` as answers come in. **Stop signal:** the
model returns `questions: []` when nothing is both uncertain and high-impact. `converse()` returns the
final model; `main()` handles finalization (brief + save) so the interactive and `--from` paths share
it. `--once` (or no TTY) does a single pass (status + questions, no brief).

## The model is the product; artifacts are views

Discovery persists the model to `out/<slug>/model.json` (`save_model()`) — the durable product.
Everything else is a **generator**: a pure function `model → artifact`. `--from out/<slug>/model.json`
reloads a saved model and regenerates any artifact without redoing discovery (`load_model()`).

Because artifacts are views of the model, they can go **stale** when the model moves — and the model
knows what rests on what. `core/dependencies.py` makes the dependency DAG explicit: a `DesignDecision`
records the slot ids it was `derived_from` (filled by `advise()`); a static `ARTIFACT_SLOTS` map records
which slots each buildable artifact consumes (the assessment/brief is deliberately excluded — it is the
live analysis layer, not a downstream deliverable). `propagate(model, slots)` returns the blast radius
(decisions to re-validate + artifacts to regenerate); `diff_models(old, new)` is the material change
between two versions (value/confidence/impact — not completeness noise); `stale_on_disk()` intersects
that with the files actually present in `out/<slug>/`. `pc impact` surfaces the forward view on demand;
`pc answer` runs the diff automatically each turn and warns which generated files no longer match.

Each generator is the same shape — **prompt + Pydantic contract + generator fn + writer**:
`brief.md`/`Brief`/`advise()`, `stories.md`/`Stories`/`derive_stories()`,
`estimate.md`/`EstimateDraft`/`estimate()`, `prd.md`/`PRD`/`generate_prd()` (writes `out/<slug>/prd.md`
via `prd_markdown()` + `write_artifact()`), `criteria.md`/`AcceptanceCriteria`/`generate_criteria()`
(Given/When/Then recette checklist → `out/<slug>/acceptance-criteria.md` via `criteria_markdown()`),
`epic.md`/`Epic`/`generate_epic()` (delivery epic — work broken into trackable issues with labels +
`depends_on` → `out/<slug>/epic.md` via `epic_markdown()`),
`release.md`/`ReleaseNotes`/`generate_release()` (client-facing release notes → `out/<slug>/release-notes.md`
via `release_markdown()`; `generate_release()` takes an optional `version` the CLI stamps from
`--release [version]`). Adding one (test plan, more exports) = those four pieces (in `core/generators.py`
+ `render/markdown.py`), plus wiring in `cli.py`: a `pc` subcommand in `_build_parser()` and a legacy
`--flag` in `main()`. Any generator whose text is user-facing must carry the **Voice** rule (no slot
ids / percentages / confidence labels in prose). `pc` subcommands: `discover`, `status`, `impact`,
`brief`, `prd`, `stories`, `estimate`, `criteria`, `epic`, `release`; legacy flags: `--stories`,
`--estimate`, `--prd`, `--criteria`, `--epic`, `--release`. (`impact` is a pure query, not a generator,
so it is a `pc`-only verb — no artifact, no legacy flag.)

A generator can also have **more than one writer** on the same contract — a second *view* of the same
LLM output, no extra model call. `Epic` has several: `epic_markdown()` (human) and `epic_export_json()`
(a tool-neutral, versioned envelope — `format`/`version`/`epic`/`issues[]` with labels, shared
`milestone`, and `depends_on` refs — importable into GitHub/GitLab or consumable by an n8n flow,
written to `out/<slug>/epic.json` behind `--epic-json`). `main()` calls `generate_epic()` once and
renders whichever views were requested, so `--epic --epic-json --epic-github` is a single API call.

**Tracker adapters** are pure transforms over the *neutral export* (not the internal `Epic`), which
keeps the core tool-agnostic: `to_github(export, slug)` maps `epic_export()` output → a GitHub
issue-creation plan (`out/<slug>/epic.github.json` behind `--epic-github`, via `to_github_json()`).
GitHub has no native epic or dependency, so it degrades honestly (tracking issue + task list;
`depends_on` stated in issue bodies) and stamps a `pc-epic:<slug>` idempotency label on every issue.
The authenticated push (tokens, retries) is deliberately *not* in-repo — an n8n flow consumes the
plan. `to_gitlab(export, slug)` (`--epic-gitlab` → `epic.gitlab.json`) is the same shape but maps
`depends_on` to a structured `links` array (`blocks`) instead of body text — GitLab has native issue
links. Adding Jira = another pure `to_<tracker>()` adapter + `--flag`.

## Extending

- **New client/product context:** copy `context/_template.md` to `context/<name>.md` and fill it. It
  is picked up automatically (non-`_` prefix). Better context cards → better impact estimates → better
  questions.
- **`config_vs_custom` slot** is `optional: true` — the platform edge (hardcoded / configurable /
  per-client / reusable-for-all). On for configurable multi-client platforms, off for one-shot apps.
- `framework/elicitation.md` is the human-readable spec of the framework; `model_schema.json` is the
  machine version fed to the model. Keep them consistent when adding or renaming slots.
