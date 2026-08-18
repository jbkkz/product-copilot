# CLAUDE.md

Guidance for Claude Code working in this repository. Read the invariants section before changing
anything in `core/` or `services/` — several of them look like details and are not.

## What this is

**Requivo — a requirements engine.** It turns a vague client request into a *structured solution
model* ready for dev. It is **not a chatbot**: the chat is only an interface. The product is the
**model** (a set of typed slots) and the engine that progressively fills it until it is precise enough
to build from. Everything in the repo — code, comments, docs, prompts, context cards, and the engine's
own output — is in English.

## Run and test

```bash
cp .env.example .env                    # ANTHROPIC_API_KEY; MODEL defaults to claude-sonnet-5
uv run requivo demo                     # replays a saved run — no key, no network, no arguments
uv run requivo discover "We'd like a leave approval system."   # → .requivo/sessions/<slug>/
uv run requivo status <slug>            # understanding checklist + readiness (offline)
uv run requivo prd <slug>               # regenerate any artifact from the saved model
```

Classic install (equivalent; drop `uv run` once the venv is active):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -U pip setuptools           # a fresh venv often ships pip < 21.3, too old for editable installs
pip install -e ".[dev]"                 # deps + the `requivo` command + pytest
.venv/bin/python -m pytest tests/ -q    # 324 tests, no API calls, no build step
.venv/bin/ruff check src tests          # lint (CI runs the same)
```

`requivo` is the command. Verbs: `discover`,
`answer`, `demo`, `status`, `impact`, `brief`, `prd`, `stories`, `estimate`, `criteria`,
`epic` (`--json/--github/--gitlab`), `release`, `web`, plus the offline ones in `deterministic.py`
(`doctor`, `schema`, `context`, `session` incl. `verify`, `model`, `artifact`). `impact` is a pure query over the
dependency DAG — no API call. Without an install, `python scripts/requivo_cli.py <cmd>` is equivalent
(the launcher lives under `scripts/`, not at the repo root, where it would shadow the package).

Two worked examples live under `examples/`: `leave-approval/` (a one-line request → model → brief →
PRD) and `event-checkin-reconciliation/` (a messy multi-feature client email → an assessment that
refuses its conflation → epic + criteria). `requivo demo` replays the second from disk.

## Architecture

Reasoning is a **single LLM call per turn** whose intelligence lives in assembled prompt data, not in
Python — and that call lives in a **provider**, never in the core. The layers form a strict DAG:

- **`core/`** — the deterministic engine. Never prints, never reads argv, **never calls an LLM, never
  imports a provider** (guarded by `tests/test_boundaries.py`). It validates, versions, and reasons
  over the model; it never *produces* one.
- **`providers/`** — the only place an LLM is called. `base.py` is the `ReasoningProvider` protocol;
  `anthropic.py` (behind the optional `requivo[anthropic]` extra) implements it. The Claude Code
  surface is a *second* provider that lives outside Python: Claude reasons, the deterministic CLI
  applies.
- **`services/`** — the application seam, and the only place the two meet. `SessionService.update_model`
  is the single validated apply path (validate → diff → propagate → revision → stale-flag);
  `DiscoveryService` is the single provider-backed orchestration (reason → apply → save).
  Both storage (`SessionRepository`) and reasoning (`ReasoningProvider`) are injected, so the
  orchestration is backing-agnostic — a Postgres repository reuses it verbatim.
- **`render/`** turns data into strings; **`cli.py`**, **`deterministic.py`** and **`web/`** are the
  only layers that touch argv/stdout/HTTP.

Every interface — the terminal CLI, the Claude Code plugin, Requivo Web — is a thin layer over the same
services. There is never a second implementation of an apply, a generation, or a staleness rule.

They are equal in capability and **not** equal in weight. **Web is the product experience, Claude Code
is an integration, the CLI is infrastructure.** That ordering is a product decision, held in the
README, `docs/`, and the relative prominence of each surface's documentation — never in the code, where
all three reach the same services.

```
requivo/
  paths.py         ASSETS (read-only) + workspace_root()/session_root() + output_root() (retired out/)
  assets/          bundled data shipped in the wheel: prompts/ framework/ context/ demo/
  core/            the deterministic engine — no LLM, no provider, no argv/stdout
    contracts.py     Pydantic contracts (StrictModel base) + stable ids + slot vocabulary
    analysis.py      readiness / soft slots / blockers    context.py   card + prompt assembly (no LLM)
    persistence.py   session store: .requivo layout, revisions, migrate_legacy, atomic writes
    validation.py    validate_proposal → structured errors  errors.py  RequivoError (+ .to_dict())
    dependencies.py  the dependency DAG: propagate / diff_models / diff_reasoning
    integrity.py     does a session directory tell the truth about itself? evidence is the
                     directory and only the directory: nothing outside becomes a verdict (a lost
                     context card is an environment finding), and nothing inside aims a filesystem
                     call outside (a recorded artifact filename is untrusted input)
    adapters.py      epic_export + GitHub/GitLab tracker plans
  providers/       the only LLM callers
    base.py          ReasoningProvider protocol
    anthropic.py     client + _complete + generators + AnthropicProvider + usage ledger
  services/        the shared seam
    sessions.py      SessionService (create / update_model / diff / status)
    artifacts.py     ArtifactService (save with source revision / list / mark_stale)
    repository.py    SessionRepository protocol + FileSessionRepository (Postgres-swappable)
    discovery.py     DiscoveryService — reason → apply → save, shared by CLI + Web
  render/          views (data → str/stdout, no side effects)
  cli.py           the `requivo` CLI: provider verbs (discover/answer/generators/web)
  deterministic.py the no-LLM verbs: doctor / schema / context / session / model / artifact
  web/             Requivo Web — FastAPI + Jinja2 + HTMX over the services (the `[web]` extra)
    app.py           create_app()   security.py  cross-site guard   routes/  viewmodels/  templates/
    viewmodels/labels.py  the user-facing vocabulary, in one table (see "Two vocabularies" below)
plugins/claude-code/   the Claude Code plugin (skills + manifest) — NOT shipped in the wheel
```

Assets (`prompts/`, `framework/`, `context/`, the demo payload) live **inside the package** at
`src/requivo/assets/`, so they ship in the wheel and a `pip install` works outside a clone. Sessions
are written to `.requivo/sessions/<slug>/` under the caller's **workspace** (cwd, or
`--workspace`/`REQUIVO_WORKSPACE`), never inside the install. The retired `./out` root is opened by
nothing but `requivo session migrate`.

## Invariants

These are the rules a change must not quietly break. Each one exists because breaking it produced a
bug that looked like correct behaviour.

1. **Staleness is the dependency graph, never the revision number.** An artifact is stale when
   something it rests on changed — not because the session moved past its source revision. The source
   revision is *provenance*. Report `ArtifactStatus.stale`; never infer staleness by comparing
   revisions. Two edge sets feed it: the slots an artifact consumes (`ARTIFACT_SLOTS`) and the
   reasoning layer (`REASONING_CONSUMERS` — every generator, since each is prompted with the full
   model, so `diff_reasoning` invalidates on its own). Reasoning a turn merely *omits* is not a
   removal — but that is resolved *before* the diff, by `ModelProposal.resolve`, not inside it (see
   invariant 10). By the time two models reach `diff_models`/`diff_reasoning` both are complete, so
   the diff is symmetric: an empty collection facing a populated one is a real deletion.
2. **A generation carries the revision it read.** Provider calls take seconds to minutes and the
   session can move underneath them. Capture `current_revision` before the call; pass it as
   `expected_revision` on any apply and as `source_revision` on the artifact write. Saving against an
   older revision stays legal — `ArtifactService.save` then computes freshness against the current
   model rather than assuming it. Never record `stale=False` because the caller didn't say otherwise.
3. **Refuse, don't truncate; refuse, don't filter.** Over-long input is rejected, not cut — half a
   request reads exactly like a whole one. An unknown context card is an error, not something to drop:
   dropping it leaves an empty selection, and an empty selection means *every* card.
4. **Boundary contracts are strict.** Everything an LLM fills inherits `StrictModel` (`extra="forbid"`).
   A field the model invented must fail loudly and ride the retry loop, not be silently discarded.
   *Completeness* rules (the full required slot set, a non-empty objective) live at the discovery
   boundary instead, because a partial `EngineOutput` is a legitimate internal object.
5. **Reasoning items have content-derived ids.** `DesignDecision`, `Challenge` and `Opportunity` carry
   an `id` recomputed from their own text on every validation. Never trust a supplied one.
6. **Provenance is real or absent.** Each revision records provider, model, surface and a hash of the
   exact prompt it was reasoned against. Don't add a provenance field you do not populate.
7. **Core stays provider-free, and talks to its caller rather than to the process.** It may read and
   write files — `persistence.py`, `context.py`, `contracts.py` and `analysis.py` all do, by design;
   *IO-free*, which this invariant used to say, was false as written and a guard against the literal
   wording would have to fail on correct code. What core may not do is import a provider or the
   Anthropic SDK, and may not touch **argv, the standard streams, the environment, or process exit** —
   `cli.py`/`deterministic.py` own argv and stdout, `paths.py` owns the environment. `logging` is
   fine; it is the library-correct way of *not* printing. `tests/test_boundaries.py` enforces both
   halves, walks `core/` recursively, resolves relative imports, and **fails when its scan set is
   empty** — a glob over a directory that no longer exists returns `[]`, and `assert not []` is an
   all-clear nobody earned. Each rule in that file carries the reason it is there, so the next person
   argues with a line instead of deleting the file.
8. **The session format and the `--json` outputs are public.** `.requivo/sessions/` is the interface
   between every surface, at `format_version` 1. Adding a field is free; renaming or repurposing a
   *populated* one needs a version bump and a migration in `migrate_session()`. A frozen 0.8.2
   `session.json` in `tests/test_sessions.py` pins the backward-compatibility half of that promise, and
   `docs/compatibility.md` is the written contract — update it in the same change, not later. Forward
   compatibility is the other half: persisted models are `extra="allow"`, so a field from a *newer*
   Requivo survives a round-trip through an older one. Retiring a key is explicit, in `_RETIRED_KEYS`.
9. **A precondition is held across the writes it authorises.** `save_revision` checks
   `expected_revision` and then performs five writes; without `session_lock` around both, two writers
   pass the same check and the second overwrites the first — the check reads as protection while
   providing none. Every compound mutation runs under `repo.lock(slug)`, taken by the service so the
   whole sequence is one unit. The lock is re-entrant per thread, and OS-held, so a crash releases it.
   Any new multi-step write goes inside it; any scratch file gets a unique name.
10. **A proposal is not a model, and silence is not deletion.** What a surface sends is a
    `ModelProposal`: the slots are complete (an apply *replaces*, so a partial one is refused, never
    merged), but `decisions`/`challenges`/`opportunities` are tri-state — absent means "not speaking
    to it", `[]` means "delete". `resolve(current)` collapses the three states against the model being
    refined, and it is the *only* place that happens: `validate_proposal(…, current=…)` for every
    apply, and the provider's `run(…, carry_from=…)` for a turn it reasons itself. Read as an
    `EngineOutput` instead, an ordinary refinement turn — which `engine.md` never asks to re-state the
    brief — deleted every decision the assessment had produced, silently.
11. **Creating a session is one atomic claim on its slug.** `create_session` assembles the session in a
    staging directory and renames it into place; the rename either wins the slug or raises
    `SessionExistsError`. Never decide with a preceding existence check — two concurrent creations both
    pass it, and the second overwrites the first's identity, provider and context cards. Identity is
    the request **and** its context-card selection: same request, different cards is a different
    discovery, because the cards are what the impact estimates are read against.
12. **A provider call reasons from one snapshot.** `SessionService.snapshot(slug)` reads the revision,
    the model, the request and the cards under the session lock; `run_discovery`, `answer`, `generate`
    and `reason` all take one. Reading the revision and the model separately yields revision N with the
    model of N+1 when a write lands between them — the artifact is then generated from one model and
    filed against another, undetectably, because the recorded number is plausible. The lock is released
    before the call (which takes minutes); `expected_revision` handles what happens *after*, the
    snapshot handles what the call started *from*.
13. **A first discovery only lands on revision 0.** Discovery reasons from the request alone — it never
    sees the current model — so running it on a refined session discards that work rather than
    improving it, with the optimistic lock satisfied throughout (it reads revision N and writes against
    N). `_require_revision_zero` is the gate, taken *before* the paid call, by every entry point. A
    rule that lives in an interface (the Web hides the button after revision 0) is not enforced.
14. **The service layer is the integrity boundary, not the interfaces.** Context cards are resolved in
    `create_session`, not trusted from the caller, because the CLI and the Web being careful is not a
    guarantee — an external consumer can call the service directly. For the same reason `DiscoveryService`'s
    artifact service defaults to the *session service's* repository: on files a split backing is
    invisible and every call succeeds.
15. **A listing survives its own members.** `session_list` renders every session on the home page, and
    a session at revision 0 has no model — `status()` raises for it. Letting that propagate turned one
    un-analysed session into a 404 for the *whole* list, hiding every other session behind it. Any
    aggregate view catches per-item failure and degrades that row, never the page.

## Two vocabularies, one meaning

The engine's vocabulary is precise: slots, evidence, coverage, artifacts, staleness, revisions. It is
the right one for `core/`, for `--json`, for `docs/` — and the wrong one for a first screen, because it
asks a reader to learn the model before they can use the product.

So the Web speaks a translation of it, defined once in `web/viewmodels/labels.py` and in
`viewmodels/status.py` (*what we know* / *what we are assuming* / *open question* / *needs updating* /
*are we ready?* / *decision brief*). Two rules keep this from becoming a second model:

- **Translation only, never computation.** A view model relabels and *selects* (which five questions
  lead the page); it never re-derives readiness, coverage or a blast radius. `impact_view` reshapes an
  `UpdateResult`; it does not recompute one, and it must never ask the provider — a generated list of
  documents needing an update is a plausible guess where a computed one is an answer.
- **Nothing stored changes.** `brief` is still `brief` on disk, in the CLI verb, in the contract and in
  `session.json`; only the caption reads "Decision brief". Renaming a persisted key to change a label
  would cost a format bump for a word.

The primary screen shows what a reader must act on; everything else lives behind *Traceability
details*, complete and one click away. Hiding is presentational — the counts are always stated, so it
is never possible to mistake a short list for the whole list.

## The runner

1. `build_prompt(name, only)` loads a prompt file and substitutes `{{SCHEMA}}` (the slot definitions)
   and `{{CONTEXT}}` (`load_context()`, which concatenates every `context/*.md` except `_`-prefixed
   ones). `prompt_version()` hashes exactly this string — that hash is what lands in the revision log.
2. Every reply must be **JSON only**. `_complete()` is the shared call: it concatenates the response's
   text blocks, strips a fence or slices `{ … }`, and validates against a Pydantic contract. On
   malformed or non-conformant JSON it retries (2× by default) with a corrective nudge in a *local*
   message copy, so the caller's history stays clean. An optional `validate` hook rides the same loop
   for semantic checks. Transport failures and truncated replies surface as a clean `EngineError` —
   never a traceback. The output ceiling is `MAX_OUTPUT_TOKENS` (16k; the call is non-streaming, and
   the SDK risks HTTP timeouts above that). Truncation is checked **parse-first**: a reply flagged
   `max_tokens` whose JSON is nonetheless complete still succeeds.
3. The `system` prompt is sent as a single `cache_control: ephemeral` block, so its prefix is cached
   across a session's calls (a golden capture's K runs, `converse()`'s turns, each retry). Keep it
   byte-identical per call or the cache is lost. `_complete()` records per-call usage into a
   session-scoped `UsageLedger`; `render_usage()` prints it (tokens are exact, cost is a labelled
   estimate from a dated table with expiry-aware launch pricing).

**Consequence for changes:** behaviour is tuned by editing the Markdown/JSON assets, not the Python.

## The output contract (keep in sync)

Each stage has a Pydantic contract that must agree with its prompt's "Output format" block:
`ModelProposal` ↔ `engine.md` (the reply is a *proposal*; `EngineOutput` is what it resolves into —
see invariant 10), `Brief` ↔ `brief.md`, `Stories` ↔ `stories.md`, `EstimateDraft` ↔
`estimate.md`, `PRD` ↔ `prd.md`, `AcceptanceCriteria` ↔ `criteria.md`, `Epic` ↔ `epic.md`,
`ReleaseNotes` ↔ `release.md`. Slot ids live in `framework/model_schema.json`, which also carries each
slot's `pillar` and `label` (read back by the renderer via `_slot_meta()`).

The slot vocabulary is enforced in two layers, with `schema_slot_ids()` as the single source:

- *Vocabulary* — both contracts always reject unknown slot ids: in the model, in the slot each
  `Question` targets, and in every DAG edge (`derived_from`, `contests`). `questions` is capped at 6.
- *Completeness* — `completeness_gap()` is the single definition (the full required slot set, plus a
  non-empty objective), read by both boundaries that enforce it: the discovery `validate` hook, which
  needs a `ValueError` to ride the retry loop, and `validate_proposal`, which needs a structured
  `RequivoError`. They used to state it separately, and drifted. As defence in depth, `_readiness_blockers()` reasons over the
  *schema's* required slots rather than the ones returned, and `diff_models()` walks the union of
  old/new keys so a removed slot registers as a change.

## The two core concepts

- **Slots (the atomic unit).** Every requirement lives in a slot: `completeness` (0–100), `confidence`
  (explicit|inferred|empty), `impact` (low|medium|high), `value`, `evidence`. Slots group into four
  navigation pillars (Why / What / How / Validate) defined in `framework/elicitation.md`. Every output
  is a render of the same filled model: the bars are per-pillar completeness, the questions are its
  gaps, the assessment is a consultant's read of it.
- **The driver: `information_value = uncertainty × impact`.** The engine does **not** ask because a
  slot is empty — it asks where information value is high. Empty-but-low-impact slots are left alone;
  filled-but-risky slots get probed. Impact is estimated **from the product context**, so the engine is
  only as sharp as the `context/*.md` cards it is given. This is the central design idea; preserve it
  when editing prompts.

## The model is the product; artifacts are views

Discovery persists the model to `.requivo/sessions/<slug>/model.json` — the durable product; each apply
also freezes a copy under `revisions/`. Everything else is a **generator**: a pure function
`model → artifact`, run again from the saved model without redoing discovery.

Because artifacts are views, they go **stale** when the model moves, and the model knows what rests on
what. `core/dependencies.py` holds the graph: a `DesignDecision` records the slots it was
`derived_from`; a `Challenge` records the slots it `contests`; `ARTIFACT_SLOTS` records which slots each
artifact consumes. The assessment maps to `*` — it is a judgment over the whole model, so any material
change invalidates the saved copy. `propagate()` gives the blast radius, `diff_models()` the material
change between two versions (value/confidence/impact — completeness alone is noise).

Each generator is the same shape — **prompt + contract + generator fn + writer** — and every interface
reaches them through `DiscoveryService.generate()`, which owns the revision lock, the provenance and
the artifact write. `stories` and `estimate` are deliberately terminal-only analyses with no file
(`DiscoveryService.reason()`). Adding a generator: prompt asset + contract + a function in
`providers/anthropic.py` (registered in `_GENERATORS` and `_OP_PROMPTS`) + a writer in
`render/markdown.py` (registered in `_WRITERS`) + a subcommand in `cli.py`. Any generator whose text is
user-facing carries the **Voice** rule: no slot ids, percentages or confidence labels in prose.

`brief_markdown` is deliberately half deterministic. Its *What is confirmed* and *Important
assumptions* sections are projections of the model (`_stated()` reads each topic's evidence), not
prose the provider was asked to write — a restatement of facts can drift from the model it restates,
and a projection cannot. Ask the provider for judgment; read the facts off the model.

A generator can have **more than one writer** on the same contract — a second view, no extra call.
`Epic` has `epic_markdown()` (human) and `epic_export_json()` (a tool-neutral versioned envelope).
**Tracker adapters** are pure transforms over that neutral export, not over the internal `Epic`, which
keeps the core tool-agnostic: `to_github()` degrades honestly (GitHub has no native epic or dependency
— a tracking issue plus task list, `depends_on` stated in bodies, a `requivo-epic:<slug>` idempotency
label), `to_gitlab()` maps `depends_on` to native issue links. The authenticated push is deliberately
out of repo — an n8n flow consumes the plan. Adding Jira = another pure `to_<tracker>()`.

## The golden harness (measuring a prompt or context-card change)

Behaviour is tuned by editing assets, and the engine is non-deterministic with no sampling controls on
the model family in use — so "did this edit help?" cannot be answered from one run.

```bash
python scripts/golden_run.py [<slug>…] [--brief]   # re-capture the K-run baseline (K=3, GOLDEN_K)
python scripts/golden_diff.py [<slug>…]            # what moved, above the noise floor
python scripts/golden_diff.py <slug> --questions   # the questions & challenges themselves, old vs new
```

`fixtures/golden/requests.md` is the fixed request set — one request per problem *form*. Each is
captured K times into `fixtures/golden/<slug>.runs.json`; the committed version is the baseline, the
working tree is the candidate. Workflow: edit an asset → `golden_run` → `golden_diff` → commit the new
baseline if the change was intended. Why it is built this way (`scripts/golden_lib.py`):

- **Consensus over K runs, not one capture.** A slot dimension is only a usable reference if all K runs
  agree; the per-request noise floor is printed on a fresh capture so you know how much signal it can
  carry.
- **Strong vs weak moves.** Strong = unanimous before *and* after; weak = a bare majority, which at K=3
  is one run flipping. Act on strong; watch weak only in aggregate. Without this the lens reports
  jitter as signal.
- **A capture identical to HEAD reports "not re-captured", never "no change"** — a false all-clear is
  the one failure mode a regression lens must not have.
- **The assessment lens** (`--brief`, doubles that request's calls) watches the deliverable: the
  complexity verdict and the challenges, grouped by the slots they contest. Grouping challenges by
  headline wording was tried and abandoned — the engine rephrases at the concept level, so two
  wordings of one challenge share no words and matching read that as one lost plus one gained.
- **The slot tiers are a projection; the questions and challenges are the product.** `--questions` is
  usually what settles whether a change was an improvement or merely a movement.

Cost: K calls per request, doubled under `--brief` — a full six-request cycle is 18. Re-capture the
targeted request first, the full set only before committing a baseline.

**Known limit (partially mitigated):** `load_context()` concatenates every card by default, so each new
card dilutes its neighbours. Measured once, strongly: adding `financial-reporting` cost `doc-reapproval`
its sharpest question (3/3 runs → 1/3, displaced by that card's audit-trail emphasis).
`requivo discover --context <cards>` lets a session opt into a subset, held constant across its turns
so the cached prefix survives — but there is still no *automatic* relevance routing, which a third such
instance would justify.

## Extending

- **New context card:** copy `src/requivo/assets/context/_template.md` to `…/context/<name>.md` and
  fill it; it is picked up automatically (non-`_` prefix). For a pip install with no checkout, drop
  cards in `user_context_dir()` (`REQUIVO_CONTEXT_DIR`, default `~/.config/requivo/context`) —
  `_card_paths()` in `core/context.py` merges bundled + user cards by stem, user winning on a clash.
  Better cards → better impact estimates → better questions. Measure through the golden harness: a card
  helps its target request and can quietly cost a neighbour.
- **`config_vs_custom`** is the one `optional: true` slot — the platform edge (hardcoded / configurable
  / per-client / reusable-for-all). On for configurable multi-client platforms, off for one-shot apps.
- `framework/elicitation.md` is the human-readable spec of the framework; `model_schema.json` is the
  machine version fed to the model. Keep them consistent when adding or renaming a slot.
- **Docs live in `docs/`**, one file per subject (`architecture`, `cli`, `web`, `session-format`,
  `providers`, `context-cards`, `requirements-model`, `evaluations`, `product-validation`, `roadmap`).
  The README is an orientation, not a manual — put depth in `docs/`. `product-validation.md` is the
  manual protocol for "is this better than a strong prompt?"; keep it out of the golden harness, which
  answers a narrow mechanical question and would lend a false precision to a judgment.
