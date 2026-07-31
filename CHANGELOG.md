# Changelog

All notable changes to Product Copilot are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.6.0]: https://github.com/jbkkz/product-copilot/releases/tag/v0.6.0
[0.5.0]: https://github.com/jbkkz/product-copilot/releases/tag/v0.5.0
[0.4.0]: https://github.com/jbkkz/product-copilot/releases/tag/v0.4.0
[0.3.0]: https://github.com/jbkkz/product-copilot/releases/tag/v0.3.0
[0.2.0]: https://github.com/jbkkz/product-copilot/releases/tag/v0.2.0
[0.1.0]: https://github.com/jbkkz/product-copilot/releases/tag/v0.1.0
