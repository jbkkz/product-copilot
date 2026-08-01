# Changelog

All notable changes to Requivo are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0] - 2026-08-01

**Requivo Web — a third interface.** A local, single-user, self-hostable browser UI over the same Core,
services and session format as the CLI and Claude Code. It exists for people less comfortable in a
terminal; it is deliberately *not* Requivo Cloud (no accounts, auth, database, remote storage, or
telemetry — see `docs/web.md`).

### Added
- **`requivo web`** — launches a local FastAPI + Jinja2 + HTMX interface (the optional `[web]` extra:
  `uv tool install "requivo[web]"`). Binds to `127.0.0.1` by default, opens a browser, prints the URL,
  and warns if bound to a non-local host. The Anthropic key is read from the server environment (never
  the browser) and only needed for provider actions. Options: `--host --port --workspace --no-open
  --reload`.
- **The web interface** (`requivo.web`): home + session list, new discovery (run now or *create session
  only*), a session screen (understanding split with a *partial* coverage marker, readiness + blockers,
  priority questions with a single answers form, persisted decisions/challenges/opportunities,
  artifacts), an answers turn (optimistic-locked, HTMX status refresh reporting changed slots /
  unseated reasoning / stale artifacts), and generation of the **solution assessment** and **PRD**
  (saved with source revision, marked *Draft* when blocking unknowns remain, viewable + downloadable).
  Templates, CSS and a vendored HTMX ship in the wheel — no CDN, works offline.
- **UI aligned to the Requivo landing** — indigo accent, warm off-white, soft-shadow cards, monospace
  meta labels, dot-coded understanding rows (FACT / ASSUM / UNKWN) and a segmented readiness bar. A
  visible loading signal on every action (a top progress bar + an in-button spinner) covering both HTMX
  swaps and full-page submits, so it is always clear that something is happening. Degrades without JS.
- **`DiscoveryService`** (`services/discovery.py`) — the provider-backed orchestration (start / answer /
  generate) extracted so the CLI and Web share exactly one pipeline; neither re-orchestrates "call the
  provider, then apply". `brief_markdown()` renders the assessment as a saveable/downloadable artifact.
- **Optional `web` extra** and web package-data (templates + static) in the wheel; a CI job installs the
  wheel with `[web]`, verifies the assets ship, and hits `/health`.

### Security
- Local by default: localhost bind, structured `RequivoError`s rendered as clean pages (never a
  traceback), every slug validated in Core (no path traversal), only the package `static/` served
  (never the workspace / `.requivo` / `.env` / `.git`), API key never in HTML or logs, all content
  HTML-escaped, bounded input sizes, and conservative headers (`X-Content-Type-Options`,
  `Referrer-Policy`, a same-origin `Content-Security-Policy`).

### Docs
- **Editorial pass: README as orientation, `docs/` as depth.** The README is rewritten as an
  activation guide (434 → 223 lines) — hero, why, a three-interface table, three quickstarts, core
  concepts, a docs index — with the depth moved to ten specialized documents under `docs/`
  (`getting-started`, `cli`, `architecture`, `requirements-model`, `session-format`, `providers`,
  `context-cards`, `evaluations`, `roadmap`, plus an index). Fixed stale/incorrect references (a
  non-existent `discover --provider` flag in the plugin README, "two interfaces" → three, `out/` →
  `.requivo/`) and added a local-Web exposure note to `SECURITY.md`. No behaviour change.

## [0.8.2] - 2026-08-01

Pre-Cloud correctness at the session boundary — the layer requivo-cloud will sit on. From the same
external review's "before you connect Cloud sessions" list.

### Added
- **`SessionRepository` storage seam.** `SessionService` and `ArtifactService` no longer touch the
  filesystem directly — storage is injected as a `SessionRepository` (in `services/repository.py`),
  with `FileSessionRepository` (the default) delegating to `core.persistence`. The canonical-vs-legacy
  `out/` handling now lives inside the file repository, where it belongs. requivo-cloud can supply a
  `PostgresSessionRepository` with the same protocol and reuse the service orchestration verbatim,
  instead of bypassing the service or faking a filesystem. Proven by an in-memory repository the full
  service cycle (create → apply → stale-flag → status → provenance → locking) runs against with zero
  filesystem.
- **Optimistic locking.** `SessionService.update_model` / `save_revision` take an optional
  `expected_revision`; a write whose expectation is stale raises `RevisionConflictError`
  (`revision_conflict`, with `expected`/`actual`) instead of silently landing on top of a concurrent
  update. The single-user CLI omits it; `requivo model apply --expected-revision N` exposes it. Harmless
  locally, required for a concurrent Web service.
- **Per-revision provenance.** Each applied revision now records who produced it — `RevisionRecord`
  (revision, created_at, previous_revision, provider, model_name, surface, model_hash) appended to a
  `revisions` log in `session.json`. Provenance belongs to the revision, not just session creation,
  because a model is moved by more than one surface (Anthropic provider, Claude Code, CLI, later Web)
  over its life. `discover` / `answer` / `model apply` each stamp their surface.
- **Richer `status --json`.** The payload now carries the full picture — `understanding` (per-slot,
  grouped by state, with pillar/completeness/impact and a `thin` flag), priority `questions` (labelled),
  `summary`, `remaining_gaps`, and `context_cards` — so Claude Code and a future Web client render it
  without rebuilding the presentation logic. Built from one shared `model_status` projection used by
  both the CLI and `SessionService.status` (no second implementation).

### Fixed
- **Reasoning references are validated.** `DesignDecision.derived_from` and `Challenge.contests` could
  name a slot the schema doesn't define, letting the dependency graph look rigorous while pointing at
  nothing. The `EngineOutput` contract now rejects unknown slot references, same as the model and the
  questions.
- **First apply no longer invalidates its own reasoning.** A first model carrying decisions/challenges
  reported them all as invalidated on apply (the impact was computed over the *new* model when there
  was no prior). Invalidation is now computed strictly against the prior established reasoning; on a
  first apply nothing is invalidated.
- **Third copy of the artifact-staleness bug.** `cli._status_payload` (what `status --json` actually
  used) still carried the `revision != current` invalidation the 0.8.1 fix removed from
  `ArtifactService.list` and `SessionService.status`. Unifying the two status paths onto `model_status`
  eliminated it.

## [0.8.1] - 2026-08-01

Correctness pass at the surface boundaries, from a full external review of the 0.8.0 snapshot. No
model-format change; the fixes are about *when* an artifact is stale, *when* a session is ready,
*where* a session can be written, and keeping the docs honest. All six pre-release findings from the
review are addressed.

### Fixed
- **Artifact freshness now respects the dependency graph.** `ArtifactService.list` (and `status`)
  flagged *every* artifact stale on *any* revision bump (`revision != current_revision`), defeating the
  selective blast-radius calculation. Freshness is now the explicit `stale` flag, set by
  `update_model`/`mark_stale` for exactly the artifacts a change reaches — an unrelated or
  completeness-only change leaves an artifact fresh. Revision is provenance, not an invalidation rule.
- **Readiness gates on coverage, not just provenance.** A high-impact slot could read as confirmed on
  `confidence == explicit` alone, even at completeness 5. `_readiness_blockers` now requires both
  `explicit` **and** completeness at/above the soft boundary, so a stated-but-thin high-impact
  dimension still blocks.
- **Session slugs are validated in Core (directory-traversal guard).** An explicit `--slug` was joined
  onto the session root unchecked, so `--slug ../../escaped` could write outside `.requivo/sessions/`.
  `validate_slug()` now enforces a strict kebab-case token (and confirms the resolved path stays under
  the root) at the two path constructors, so every surface — CLI, provider, a future web service —
  inherits the guard. New error: `invalid_slug`.
- **CI wheel-install job no longer imports a dead module.** It imported `requivo.core.llm`
  (`build_prompt`, `available_cards`), which the refactor moved to `requivo.core.context`.

### Changed
- **Install-free launcher moved to `scripts/requivo_cli.py`.** A root-level `requivo.py` shadowed the
  `requivo` package on `import requivo` from a checkout (a footgun for editable installs). The root
  `requivo.py` and `pc.py` launchers are removed; use `uv run requivo`, the installed `requivo`, or
  `python scripts/requivo_cli.py` from a bare clone.
- **Documentation reconciled with the shipped 0.8 surface.** README/`CLAUDE.md`/`SECURITY.md` now name
  the canonical `.requivo/sessions/<slug>/` store (not `out/`), the `/requivo-*` plugin commands (not
  `/pc-*`), and the `pip install '.[anthropic]'` / `uv run --extra anthropic` path that discovery
  actually needs. The stale `.claude/commands/pc-*` command files (which called the removed `pc.py`)
  are deleted in favour of the `plugins/claude-code/` plugin.

## [0.8.0] - 2026-08-01

Architectural refactor into **three surfaces over one engine** — Core, CLI, and Claude Code — in
preparation for a future Web UI, plus the formalized **open-source strategy** (the Community / Cloud /
Lab boundary). The model format is unchanged and the license stays MIT; the refactor itself changed no
behaviour, but this release also ships a robustness fix and a first round of discovery-quality tuning
from the first end-to-end usage test (below).

### Added
- **Requivo for Claude Code** — a plugin (`plugins/claude-code/`) with six skills (`/requivo-discover`,
  `/requivo-answer`, `/requivo-status`, `/requivo-brief`, `/requivo-prd`, `/requivo-impact`). Claude Code
  does the reasoning with your existing session; the deterministic CLI validates and applies. **No
  Anthropic API key required.**
- **Deterministic CLI surface** (no LLM, no key): `requivo doctor`, `requivo schema`, `requivo context`,
  `requivo session init|list|show|migrate|export|import`, `requivo model show|validate|apply|diff`,
  `requivo artifact save|list|show`, plus `--json` on the machine-readable verbs and a `--workspace`
  global. `status` and `impact` now accept a session slug as well as a model path.
- **Versioned session format** at `.requivo/sessions/<slug>/` — `session.json` (metadata + provenance +
  artifact status), `model.json`, `revisions/NNNN-model.json` (history), `request.md`, and `artifacts/`.
  Writes are atomic; a `migrate_session()` version frontier guards forward compatibility.
- **Structured error hierarchy** (`RequivoError` + `code`/`message`/`path`/`details`), serialized as a
  JSON envelope on `--json` failures so Claude Code and the future Web can act on the `code`.
- **Application services** (`SessionService`, `ArtifactService`) — the single validated apply path shared
  by the CLI, the Anthropic provider, and Claude Code. A proposal from any source flows through the same
  validate → diff → propagate → revision → stale-flag pipeline.
- **Reasoning invalidation.** `propagate()` now also reports the **challenges** whose premise a changed
  slot contests (via `contests`), symmetrically to decisions (`derived_from`). When a change unseats a
  decision or premise the saved **assessment** rests on, that assessment is flagged stale — `model apply`
  reports `invalidated_decisions`/`invalidated_challenges`, and `impact` shows *Premises to re-examine*.
- **Open-source governance & distribution boundary.** `docs/open-source-strategy.md` (the Core / CLI /
  Claude Code / Community Web / Cloud / Lab surface map, and the public-vs-private data boundary),
  `CONTRIBUTING.md`, `TRADEMARKS.md`, `GOVERNANCE.md`, and `examples/README.md`. New GitHub templates
  (feature request, pull request, issue-template `config.yml` routing security reports to private
  advisories) and a Gitleaks secret-scan workflow. The README gains **Open source** and **Data and
  privacy** sections; `.gitignore` and `.env.example` are hardened. The generator prompts
  (stories/estimate/prd/criteria/epic/release) now carry the same untrusted-data framing already used
  in discovery and the assessment. The license stays **MIT**.
- **Discovery-quality tuning** (from the first end-to-end usage test): the engine now ranks
  primary-object *lifecycle* questions first (where an object is created / owned / updated / completed
  / sent), asks the stakeholder to confirm expected **behaviour** rather than choose a technical
  mechanism, and no longer asserts unsourced industry consensus ("many teams do X") in the assessment.
  The assessment is titled **Draft Solution Assessment** while a blocking decision remains.

### Fixed
- **Discovery truncation on rich requests.** The per-call output ceiling was 8k tokens; the discovery
  JSON for a messy multi-feature request exceeded it and the whole reply was discarded as truncated.
  Raised to 16k — the non-streaming-safe ceiling — which fits a rich run with headroom and never
  changes an output that already fit.

### Changed
- **`requivo.core` is now provider-free** (guarded by a test): the Anthropic client, the single-call
  loop, the usage ledger, and all discovery/generation moved to `requivo.providers.anthropic`.
- **`anthropic` is now an optional extra.** Core, the deterministic CLI, and the Claude Code plugin
  install and run without the SDK; `pip install 'requivo[anthropic]'` adds the API-powered mode.
- The modern `requivo`/`pc` subcommand CLI (discover, answer, generators) now writes the canonical
  `.requivo/sessions/` store through the services; legacy `out/<slug>/` sessions are read-only and
  migrated on first change (or in bulk via `requivo session migrate`).

### Compatibility
- The legacy flag CLI (`python src/engine.py "…" --prd`) and the `src.engine` re-export shim are
  preserved. The `pc` alias is unchanged. `model.json` format is unchanged.

## [0.7.0] - 2026-07-31

**First release under the name Requivo, and the first published to PyPI** (`pip install requivo`).
Versions 0.1.0–0.6.3 were developed pre-publication under the name Product Copilot.

### Changed
- Renamed **Product Copilot to Requivo**. New positioning: *turn vague requests into validated product
  decisions*. The engine, model format and business behaviour are unchanged — this is a rename only.
- Renamed the Python package from `product_copilot` to `requivo` (no compatibility shim — the project
  has no published distribution yet, so a clean rename is preferred).
- Added `requivo` as the **primary CLI command**; kept `pc` as a temporary backward-compatible alias
  (same entry point) that may be removed in a future major version.
- Renamed the environment variables `PC_OUTPUT_DIR` → `REQUIVO_OUTPUT_DIR` and `PC_CONTEXT_DIR` →
  `REQUIVO_CONTEXT_DIR`, the default user-context directory to `~/.config/requivo/context`, the tracker
  idempotency label to `requivo-epic:<slug>`, and the `session.json` provenance key to `requivo_version`.
- Updated project metadata (name, description, URLs) and documentation to the Requivo identity.

## [0.6.3] - 2026-07-31

Closes the UX gap the 0.6.2 packaging move introduced — pip-installed users can now bring their own
context, the last thing standing between the wheel and a first PyPI release.

### Added
- **User-level context directory (`REQUIVO_CONTEXT_DIR`).** A pip-installed setup can be extended without a
  source checkout: drop cards in `REQUIVO_CONTEXT_DIR` (default `~/.config/requivo/context`) and
  they merge with the bundled cards. A user card whose stem matches a built-in **overrides** it, so a
  bundled card can be tweaked without editing the package. Both feed the same `--context` selector and
  `load_context()`; with no user directory present, behaviour is byte-identical to before (so golden
  baselines are untouched).

## [0.6.2] - 2026-07-31

Packaging: the engine is now a self-contained, pip-installable wheel, and generated output no longer
lands inside the install. This closes the review's top remaining gap — that installing from a wheel
(rather than a clone) would break, because the assets lived outside the package.

### Fixed
- **Assets ship inside the wheel.** The prompts, the framework schema, the context cards and the demo
  payload moved into the package at `src/requivo/assets/` and are declared as package data, so
  a `pip install` outside the clone has everything it needs. Before, they lived at the repo root and a
  wheel install had no prompts or schema — every command that builds a prompt would fail. Git tracked
  the move as renames, so history is preserved.
- **Read-only assets vs writable output are separated.** `paths.py` now exposes `ASSETS` (resolved
  from the package location, read-only — works identically from an editable checkout or a wheel) and
  `output_root()` (`./out` under the working directory, overridable via `REQUIVO_OUTPUT_DIR`). Generated
  models/artifacts are never written inside a possibly read-only install.

### Added
- **Wheel-install CI job.** Builds the wheel, installs it into a clean venv, and drives `pc demo` plus
  a schema load and all eight prompt builds from a directory that is *not* the repo — so the packaging
  invariant (assets resolve from the installed package, not the source tree) is guarded on every push.
- **Frozen demo payload** (`assets/demo/`) so `pc demo` runs from a wheel with no clone. A test asserts
  it stays byte-identical to the browsable `examples/event-checkin-reconciliation/` copy, killing drift.

### Changed
- **README leads with the proof.** Reordered to open on a real before/after — a rambling client email
  and what the engine caught in it (two systems conflated, a disguised-employment exposure, an offline
  constraint, a buried deadline) — followed immediately by `pc demo`, before the theory and diagrams.

## [0.6.1] - 2026-07-31

A boundary-hardening pass from a second external review: durable writes, run provenance, clean
handling of API failures, and context continuity across commands. No engine-logic changes — the core
is unchanged; this hardens what happens at the edges (disk, network, untrusted input).

### Fixed
- **Atomic model/artifact writes.** Every write (`save_model`, `write_artifact`, `session.json`) now
  goes through a temp file + atomic rename, so an interruption can never leave a half-written JSON in
  place of a good one. model.json is the durable product — a truncated write would be unrecoverable.
- **API failures surface as clean messages, not tracebacks.** `client.messages.create()` was called
  outside the retry loop's `try`, so a network drop, timeout, rate limit, or provider outage escaped
  as a raw traceback. `_complete()` now translates any `anthropic.APIError` into an `EngineError` the
  CLI prints as one actionable line ("… The model on disk was not modified. Retry the command."), and
  exits non-zero. The saved model is never touched by a failed call.
- **The output-token ceiling is raised from 4k to 8k.** A rich discovery output (full slot model +
  questions + summary) runs right up against 4k — a simple request already spends ~3.6k output tokens
  — so multi-feature requests were one variance spike away from silent truncation. 8k gives ~2x
  headroom; you pay only for tokens generated, not the ceiling, so smaller outputs cost the same. (A
  per-generator budget is a later refinement.)
- **Genuinely truncated replies fail cleanly instead of feeding the parser garbage.** When a reply is
  cut off at the ceiling (`stop_reason == "max_tokens"`) *and* its JSON won't parse, it's reported
  ("narrow the request, or split it into fewer features per run") rather than retried — the same
  ceiling would truncate again. A reply flagged `max_tokens` whose JSON is nonetheless complete still
  succeeds (the check is parse-first), so outputs sitting right at the boundary aren't wrongly rejected.
- **All text blocks are read, not just the first.** `_response_text()` concatenates every text block
  of a response (skipping thinking/tool_use), so a reply split across blocks isn't silently truncated
  to its opening fragment before JSON extraction.
- **The `--context` selection now persists across commands.** A discovery run with a card subset saved
  its selection nowhere, so `pc answer` and every generator (`prd`, `stories`, `brief`, …) silently
  widened back to all cards — breaking reproducibility and re-diluting the context the run had trimmed.
  The selection is recorded in `session.json` and threaded through `answer_turn()` and all generators.

### Added
- **Run provenance (`session.json`).** Each discovery now writes a sidecar next to `model.json`
  recording the engine version, the Claude model, the context cards used, a SHA-256 of the request,
  and a timestamp — so a run is reproducible and traceable, matching the "the model is a durable
  product" thesis. `model.json` stays a clean `EngineOutput`; readers tolerate the sidecar's absence
  (pre-0.6.1 models simply mean "all cards").
- **Trust boundary against prompt injection.** The engine and assessment prompts now state explicitly
  that the client request, answers, and context cards are *untrusted business data* — material to
  model, never instructions to obey. A new `SECURITY.md` documents what leaves the machine (Anthropic
  API only, no telemetry), the injection posture, and how to report a vulnerability.
- **GitHub issue templates**: a *Real-world discovery feedback* template (the field signal we most
  want — was each question useful, useless, or missing?) and a *Bug report* template.

## [0.6.0] - 2026-07-31

A robustness-and-packaging pass, closing gaps an external code review surfaced.

### Fixed
- **The model's slot set is now guaranteed, closing a readiness blind spot.** `EngineOutput` rejected
  nothing about *which* slots it carried, and readiness inspected only the slots the model returned —
  so a required slot the engine omitted became invisible and a high-impact gap could pass as "ready".
  Now the contract rejects unknown slot ids everywhere, the discovery boundary requires the full
  required set (self-healing through the existing retry loop), readiness reasons over the schema (a
  missing high-impact slot is a blocker, not invisible), and `diff_models()` walks the union of keys so
  a removed slot registers as a change.
- **Output invariants the prompt only suggested are now enforced in the contract.** `EngineOutput`
  caps `questions` at 6 (the prompt asks for 3–6) and rejects any question that targets a slot the
  schema doesn't define — both self-healing through the discovery retry loop.

### Added
- **`pc discover --context <cards>`** — load a chosen subset of `context/*.md` for a discovery instead
  of all of them, so irrelevant cards can't dilute impact estimation. Selection is per-session (held
  constant across the run's turns), so the cached system prefix survives; unknown card names are warned
  and ignored. Partially mitigates the "every card is loaded for every request" known limit.
- **Per-run API usage reporting.** Every `pc` command that hits the API now prints its footprint when
  it finishes — calls, tokens (with the cached share), latency, and an estimated cost. `_complete()`
  records each call into a session-scoped `UsageLedger`; tokens are exact, cost is a labelled estimate
  from a dated rate table (never presented as a bill). Offline verbs (`demo`, `status`, `impact`) print
  nothing.
- **`pc demo`** — a no-API-key, no-argument, no-network walkthrough that replays the event-check-in
  example from its saved outputs: the messy request, the questions the engine raised (rendered live
  from the saved model), and the solution assessment it produced. The zero-friction way to feel the
  product before installing a key.
- **README "Before you rely on it"** section: what leaves your machine, cost shape, models tested,
  known limits (non-determinism, all-cards-loaded), and an explicit no-professional-advice note. The
  quickstart now leads with `pc demo`.
- **Continuous integration** (`.github/workflows/ci.yml`): `ruff` lint plus the test suite across
  Python 3.9–3.13 on every push and pull request.
- **Ruff configuration** and richer packaging metadata (keywords, classifiers, `dev` extra now includes
  `ruff`) in `pyproject.toml`.

### Changed
- **Discovery no longer silently overwrites a colliding slug.** The five-word `out/<slug>/` folder is
  kept when it's free or belongs to the same request (a re-run), but a *different* request that maps to
  the same slug now gets a short deterministic hash suffix (`leave-approval-a3f82c`) instead of
  clobbering the first.
- **Markdown tables escape cell content.** The PRD requirements table now escapes `|` and flattens
  newlines in its cells, so a requirement containing a pipe no longer breaks the table.

## [0.5.0] - 2026-07-31

A hardening-and-proof milestone: the reasoning was validated end to end on real, messy input, the
robustness holes that real input exposes were closed, and the regression lens and docs were finished.

### Added
- A second worked example, `examples/event-checkin-reconciliation/` — a rambling, multi-feature client
  email taken end to end (request → model → assessment → epic → acceptance criteria). The assessment
  refuses the "tie this together" conflation, catches a disguised-employment (*salariat déguisé*)
  exposure the request never mentions, and sequences the two builds against a fixed deadline.
- `golden_diff.py --questions` now prints each challenge's `alternative` and `recommendation`, not just
  the headline — the half of a challenge that separates an architect's pushback from a bare observation.
- Complete `--brief` assessment baselines across all six golden request forms (previously only two), so
  the challenge block can be measured on every problem shape before it is tuned.

### Fixed
- `pc discover` no longer crashes on a real-length request. `Path.exists()` raises above the OS filename
  limit, so any request longer than a tidy sentence — i.e. any real client email — died before reaching
  the engine.
- `pc discover ""` (empty or whitespace request) now fails fast with a usage message instead of crashing
  on `Path("")` resolving to the current directory.

### Changed
- The engine's `system` prompt (prompt + schema + all context cards) is sent as a single
  `cache_control: ephemeral` block, so its prefix is cached across the calls of a session — the K runs of
  a golden capture, the up-to-eight turns of `converse()`, and each JSON retry — cutting repeated-call
  input cost to roughly a tenth.

## [0.4.0] - impact calibration + the dependency DAG

### Added
- `core/dependencies.py` — the dependency DAG made explicit: `propagate()` (blast radius of a change),
  `diff_models()` (material change between two model versions), and `stale_on_disk()`.
- `pc impact` — an offline query for the decisions to re-validate and artifacts to regenerate when a slot
  changes; `pc answer` now runs the diff each turn and warns which generated files no longer match.
- A release-notes generator (`pc release`).

### Changed
- Impact calibration: `impact_default` is a baseline, not a ceiling — a compliance/audit/traceability
  need named anywhere in the request escalates the relevant slots to high impact.

## [0.3.0]

- Repository cleanup: removed the demo GIF cluster and the redundant `requirements.txt` in favour of the
  `pyproject.toml` single source of truth.

## [0.2.0]

- README polish and structure per review feedback.

## [0.1.0]

- Initial public release: the requirements engine and discovery loop, the solution assessment (the
  differentiator — a judgment that contests the request's premises, not a recap), the artifact
  generators (PRD, user stories, estimate, acceptance criteria, delivery epic with GitHub/GitLab
  exports), and the MIT license.

[Unreleased]: https://github.com/jbkkz/requivo/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/jbkkz/requivo/releases/tag/v0.8.0
[0.7.0]: https://github.com/jbkkz/requivo/releases/tag/v0.7.0
[0.6.3]: https://github.com/jbkkz/requivo/releases/tag/v0.6.3
[0.6.2]: https://github.com/jbkkz/requivo/releases/tag/v0.6.2
[0.6.1]: https://github.com/jbkkz/requivo/releases/tag/v0.6.1
[0.6.0]: https://github.com/jbkkz/requivo/releases/tag/v0.6.0
[0.5.0]: https://github.com/jbkkz/requivo/releases/tag/v0.5.0
[0.4.0]: https://github.com/jbkkz/requivo/releases/tag/v0.4.0
[0.3.0]: https://github.com/jbkkz/requivo/releases/tag/v0.3.0
[0.2.0]: https://github.com/jbkkz/requivo/releases/tag/v0.2.0
[0.1.0]: https://github.com/jbkkz/requivo/releases/tag/v0.1.0
