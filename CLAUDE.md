# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Requivo — Requirements Engine.** Turns a vague client request into a *structured solution
model* ready for dev. It is **not a chatbot**: the chat is only the interface. The product is the
**model** (a set of typed slots) and the **engine** that progressively fills it until it is precise
enough to build from. The whole repo — code, comments, docs, prompts, context, and the engine's own
output — is in English.

## Run

Fastest, no venv to manage — `uv run` builds the env from `pyproject.toml` on first run (installs the
project editable, so `paths.ROOT` still resolves assets/`out/` from the repo checkout, same as a
manual editable install):

```bash
cp .env.example .env                                          # set ANTHROPIC_API_KEY (MODEL defaults to claude-sonnet-5)
uv run requivo discover "We'd like to set up a leave approval system."   # discovery → out/<slug>/model.json
uv run requivo status  out/<slug>/model.json                       # understanding checklist + readiness
uv run requivo prd     out/<slug>/model.json                       # regenerate any artifact from a saved model
```

Classic pip + venv (equivalent; drop the `uv run` prefix once the venv is active):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -U pip setuptools   # a fresh venv often ships pip < 21.3, too old for editable installs
pip install -e ".[dev]"         # deps + the `pc` command + pytest
```

`pc` is the modern subcommand CLI: `discover`, `demo`, `status`, `impact`, `brief`, `prd`, `stories`,
`estimate`, `criteria`, `epic` (`--json/--github/--gitlab`), `release`. `requivo demo` replays the
event-checkin example from its saved outputs — no API key, no arguments, no network (status rendered
live from the model, assessment read from disk) — the zero-friction way to feel the product. `pc
impact <model> [slots…]` is a pure offline query over the dependency DAG (no API call): the blast
radius of a change (the decisions to re-validate + artifacts that go stale), or the full map with no
slots. Without an
install, `python requivo.py <cmd>`
(a repo-root launcher that puts `src/` on the path) is equivalent — this is what the Claude Code
`/pc-*` commands call. The **legacy flag CLI is preserved**: `python src/engine.py "…" [--once]
[--prd] [--stories] …` and `python src/engine.py --from out/<slug>/model.json --prd` still work
identically (`src/engine.py` is now a backward-compat shim).

Run the tests with `.venv/bin/python -m pytest tests/ -q` (pure-logic + offline CLI units via an
injected fake client, no API calls); there's no build step. Two complete worked examples live under
`examples/`: `leave-approval/` (a one-line request → model.json → brief → PRD) and
`event-checkin-reconciliation/` (a messy multi-feature client email → the assessment that refuses its
conflation → epic + criteria).

## Architecture

The reasoning is a **single LLM call** (per turn) whose intelligence lives entirely in assembled prompt
data, not in Python — but that call lives in a **provider**, never in the core. The code is the `requivo`
package (under `src/`); the historical `src/engine.py` is now a backward-compat shim re-exporting it.
The layers form a strict DAG:

- **`core/`** — the deterministic engine. Never prints, never reads argv, **never calls an LLM, never
  imports a provider** (guarded by `tests/test_boundaries.py`). It validates, versions, and reasons over
  the model; it never *produces* one.
- **`providers/`** — the only place an LLM is called. `anthropic.py` (behind the optional
  `requivo[anthropic]` extra) turns a request into a model and a model into an artifact. The Claude Code
  surface is a *second* provider that lives outside Python: Claude reasons, the deterministic CLI applies.
- **`services/`** — the application seam. `SessionService.update_model` is the single validated apply
  path (validate → diff → propagate → revision → stale-flag) shared by the CLI, the provider, and Claude
  Code. There is no second apply implementation.
- **`render/`** turns data into strings; **`cli.py` + `deterministic.py`** are the only layers that touch
  argv/stdout/TTY.

So every interface (the terminal `requivo`/`pc` CLI, the Claude Code plugin under `plugins/claude-code/`,
and later an API or Web UI) is a thin layer over the same core — never a second implementation.

Bundled assets (`prompts/`, `framework/`, `context/`, plus the `requivo demo` payload) live **inside the
package** at `src/requivo/assets/`, so they ship in the wheel and a `pip install` works outside the clone.
Sessions are written to `.requivo/sessions/<slug>/` under the caller's **workspace** (cwd, or
`--workspace`/`REQUIVO_WORKSPACE`), never inside the install; the legacy `./out` root (`output_root()`,
`REQUIVO_OUTPUT_DIR`) is read-only and migrated on first mutation. `paths.py` exposes the three roots:
`ASSETS` (read-only package data), `workspace_root()`/`session_root()` (canonical writable), and
`output_root()` (legacy).

```
requivo/
  paths.py         ASSETS (read-only) + workspace_root()/session_root() (canonical) + output_root() (legacy)
  assets/          bundled data shipped in the wheel: prompts/ framework/ context/ demo/
  core/            the deterministic engine — no LLM, no provider, no argv/stdout
    contracts.py     Pydantic models + enums           context.py     card + prompt assembly (no LLM)
    analysis.py      readiness / soft slots / blockers  validation.py  validate_proposal → structured errors
    persistence.py   session store: .requivo layout, revisions, migrate_legacy, atomic writes
    errors.py        RequivoError hierarchy (+ .to_dict() JSON envelope)
    dependencies.py  the dependency DAG: propagate / diff_models / stale_on_disk (impact propagation)
    adapters.py      epic_export + GitHub/GitLab tracker plans
  providers/       the only LLM callers
    base.py          ReasoningProvider protocol         anthropic.py   client + _complete + discovery/generators + ledger
  services/        the shared apply/artifact seam
    sessions.py      SessionService (create / update_model / diff / status)   artifacts.py  ArtifactService
  render/          views (data → str/stdout, no side effects)
    markdown.py      *_markdown                         terminal.py    render_*
  cli.py           the `requivo`/`pc` CLI: provider verbs (discover/answer/generators) + legacy flag main()
  deterministic.py the no-LLM verbs: doctor / schema / context / session / model / artifact
plugins/claude-code/   the Claude Code plugin (skills + manifest) — NOT shipped in the wheel
```

The runner is a thin dispatch:

1. `build_prompt(name)` loads a prompt file (`engine.md`, `stories.md`, `estimate.md`, `brief.md`)
   and substitutes two placeholders:
   - `{{SCHEMA}}` ← `framework/model_schema.json` (the slot definitions + the driver rule)
   - `{{CONTEXT}}` ← `load_context()`, which concatenates every `context/*.md` **except** files
     whose name starts with `_` (so `_template.md` is skipped).
2. Every model reply must be **JSON only**. `_complete()` is the shared call: `_response_text()`
   concatenates the response's text blocks (skipping thinking/tool_use), `_extract_json()` strips a
   ```json fence or slices `{ … }`, and the result is validated against a Pydantic contract. On
   malformed/non-conformant JSON it retries (default 2×) with a corrective nudge in a *local* message
   copy, so the caller's clean history is never polluted. Transport failures (`anthropic.APIError`)
   and truncated replies (`stop_reason == max_tokens` with unparseable JSON) are raised as a clean
   `EngineError` the CLI turns into a one-line message + non-zero exit — never a traceback; the ceiling
   is `MAX_OUTPUT_TOKENS` (8k). Truncation is checked **parse-first**: a reply flagged `max_tokens`
   whose JSON is nonetheless complete still succeeds. `run()`/`derive_stories()`/`estimate()`/`advise()` are thin wrappers over it. The
   `system` prompt (prompt + schema + all context cards) is passed as a single `cache_control:
   ephemeral` block, so its prefix is cached across the calls of a session (the K runs of a golden
   capture, the up-to-8 turns of `converse()`, each JSON retry) — keep it byte-identical per call, or
   the cache is lost. `_complete()` also records each call's usage (tokens, cache read/write, latency,
   attempts) into a session-scoped `UsageLedger` when one is active — `cli.py`'s `app()` opens
   `track_usage()` around a command and `render_usage()` prints the footprint (tokens are exact; cost
   is a labelled *estimate* from a dated `_PRICE_PER_MTOK` table — never treated as authoritative).
3. Rendering is split: `render_turn()` is the lightweight per-turn view (a ✅/🟡/⚪ Understanding
   checklist + priority questions); `render_brief()` is the deliverable — the **SOLUTION ASSESSMENT**,
   a two-tier document in PM language (the function/contract/prompt keep the `brief` name; only the
   printed title and the product term are "solution assessment"). It's a *judgment*, not a recap: an
   Executive Summary (Problem / Solution / Challenge / Risks / Next) a PM reads in seconds, then the
   full analysis (Understanding checklist, Design decisions, **Challenges**, Complexity + why, Main
   risks, ranked Opportunities, Next steps, Ready-for-implementation with a single blocker). The
   checklist, discovery-complete %, decision states and readiness are computed **in Python**; the
   advisory `Brief` (problem, solution, introduces, `challenges` [premise/alternative/consequence/
   recommendation + `contests`, the slot ids the challenge calls into question — the DAG edge a
   challenge carries, mirroring `derived_from` on a decision],
   complexity + reasons, risks, opportunities ranked by `Leverage` (each naming the `modules` it
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

**The slot vocabulary is enforced, in two layers** (`schema_slot_ids()` is the single source, read
from `model_schema.json`):
- *Vocabulary* — `EngineOutput` rejects **unknown** slot ids always — both in the model and in the
  slot each `Question` targets (a typo/hallucination can never sit unseen by the schema-driven views,
  nor point a question at a slot that doesn't exist). Completeness is *not* checked here, so internal
  partial projections (diff/propagate) stay constructable. `questions` is also capped at **6**
  (`Field(max_length=6)`) — the prompt says 3–6 and the stop signal is `[]`; the cap makes it an
  invariant, not a suggestion.
- *Completeness* — the discovery boundary (`run()`, via a `validate` hook on `_complete()`) requires
  the **full required slot set**. It rides the same retry loop, so a model that omits a slot is nudged
  to re-emit it rather than failing — safe on a non-deterministic model. This closes the north-star
  hole: a required slot the model dropped can no longer become invisible to readiness. As
  defence-in-depth, `_readiness_blockers()` reasons over the *schema's* required slots (not just the
  ones returned), treating a missing high-impact slot as a blocker; and `diff_models()` walks the
  **union** of old/new keys so a removed slot registers as a change.

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
that with the files actually present in `out/<slug>/`. `requivo impact` surfaces the forward view on demand;
`requivo answer` runs the diff automatically each turn and warns which generated files no longer match.

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
`depends_on` stated in issue bodies) and stamps a `requivo-epic:<slug>` idempotency label on every issue.
The authenticated push (tokens, retries) is deliberately *not* in-repo — an n8n flow consumes the
plan. `to_gitlab(export, slug)` (`--epic-gitlab` → `epic.gitlab.json`) is the same shape but maps
`depends_on` to a structured `links` array (`blocks`) instead of body text — GitLab has native issue
links. Adding Jira = another pure `to_<tracker>()` adapter + `--flag`.

## The golden harness (measuring a prompt or context-card change)

Behavior is tuned by editing assets, and the engine is non-deterministic with no sampling controls
available on the model family in use — so "did this edit help?" cannot be answered by looking at one
run. `scripts/` holds a regression lens built around that constraint:

```bash
python scripts/golden_run.py [<slug>…] [--brief]   # re-capture the K-run baseline (K=3, GOLDEN_K)
python scripts/golden_diff.py [<slug>…]            # what moved, above the noise floor
python scripts/golden_diff.py <slug> --questions   # the questions & challenges themselves, old vs new
```

`fixtures/golden/requests.md` is the fixed request set — one request per problem *form*. Each is
captured K times into `fixtures/golden/<slug>.runs.json`; the committed version is the baseline and
the working tree is the candidate. Workflow: edit an asset → `golden_run` → `golden_diff` → commit the
new baseline if the change is intended.

What the lens reports, and why it is built this way (`scripts/golden_lib.py`):

- **Consensus over K runs, not a single capture.** A slot dimension is only a usable reference if all
  K runs agree on it; the per-request *noise floor* (how many slots are unanimous) is printed on a
  fresh capture so you know how much signal that request can carry.
- **Strong vs weak moves.** Strong = unanimous before *and* after; weak = a bare majority, which at
  K=3 is one run flipping. Act on strong, watch weak only in aggregate. This distinction matters:
  without it the lens reports jitter as signal.
- **A capture identical to HEAD is reported as "not re-captured", never as "no change"** — a false
  all-clear is the one failure mode a regression lens must not have.
- **The assessment lens** (`--brief`, opt-in, doubles that request's calls) watches the deliverable
  rather than the discovery state: the complexity verdict (graded like a slot) and the **challenges**,
  grouped by the slot ids they contest (`Challenge.contests`, the same role `derived_from` plays for a
  decision) — so a challenge theme is a contested slot, exactly as a question theme is a questioned
  slot. A challenge a majority of runs used to raise and no longer do is a strong signal on its own.
  Grouping challenges by headline wording was tried and abandoned: the engine rephrases at the concept
  level, so "Visibility of the superseded signed copy" and "Published-document blast radius ignored"
  are one challenge with no word in common, and word matching read that as one lost plus one gained.
- **The slot tiers are a projection; the questions and challenges are the product.** `--questions` is
  usually what settles whether a change was an improvement or merely a movement.

Its own logic is unit-tested in `tests/test_golden_lib.py` (no API calls). Cost: K calls per request,
doubled under `--brief` — a full six-request cycle is 18, so re-capture the targeted request first
and the full set only before committing a baseline.

**Known limit (partially mitigated):** `load_context()` concatenates every card by default, so each
new context card dilutes its neighbours. Measured once, strongly: adding `financial-reporting` cost
`doc-reapproval` its sharpest question (supersession, 3/3 runs → 1/3, displaced by that card's
audit-trail emphasis). `requivo discover --context <cards>` now lets a session opt into a subset
(`load_context(only=…)`, threaded through `run()`), so a user can drop irrelevant cards manually —
but there is still no *automatic* relevance routing, which a third such instance would justify. The
selection is per-session (held constant across a run's turns) so the cached system prefix survives.

## Extending

- **New client/product context:** copy `src/requivo/assets/context/_template.md` to
  `…/context/<name>.md` and fill it. It is picked up automatically (non-`_` prefix). For a
  pip-installed setup with no checkout, drop cards in `user_context_dir()` (`REQUIVO_CONTEXT_DIR`, default
  `~/.config/requivo/context`) instead — `_card_paths()` in `core/llm.py` merges bundled +
  user cards by stem, user winning on a clash. Both feed the same `available_cards()`/`load_context()`. Better context cards → better impact estimates → better
  questions. Measure the change through the golden harness above — a card helps its target request and
  can quietly cost a neighbour.
- **`config_vs_custom` slot** is `optional: true` — the platform edge (hardcoded / configurable /
  per-client / reusable-for-all). On for configurable multi-client platforms, off for one-shot apps.
- `framework/elicitation.md` is the human-readable spec of the framework; `model_schema.json` is the
  machine version fed to the model. Keep them consistent when adding or renaming slots.
