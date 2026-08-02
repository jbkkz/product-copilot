# Changelog

All notable changes to Requivo are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.8] - 2026-08-02

The clean-up release. The pre-store architecture is gone, sessions can be checked against themselves,
and the artifact contracts hold their own references. Nothing here changes what Requivo does — it
removes the ways it could be wrong about what it has.

### Removed
- **The legacy flag CLI** (`python src/engine.py "…" --prd`) and its `src/engine.py` shim, deprecated
  since 0.9.2. It wrote the pre-versioned `out/<slug>/` layout: no revisions, no provenance, no
  staleness — everything the subcommand CLI exists to provide.
- **The `pc` command alias**, deprecated since the 0.7.0 rename. `requivo` is the command.
- **The implicit `out/<slug>/` fallback.** Every read of every session used to fall back to that
  layout, and a mutation migrated one in place — so old sessions kept working without the user
  knowing, which is also what was wrong with it: the fallback ran on every read for a layout nothing
  had written since 0.8.0, and "where does this session live?" had two answers throughout the code.
  Migration is explicit (`requivo session migrate`, unchanged, still copying rather than moving) and
  all that remains is detection: a session found only in `out/` is reported as missing *with that
  command named in the error*. The three removals were scheduled for 1.1.0, which would have meant
  carrying them through 1.0 and the Cloud beta.
- The legacy write path in `persistence` (`save_model`, `save_session`, `write_artifact`, …) and
  `stale_on_disk`, which answered "which files in `out/<slug>/` are stale?" — a question the session
  store answers from `artifact_status`. Nothing but the flag CLI had called them.

### Added
- **`requivo session verify <slug>`** and `core/integrity.py` behind it. A session is several files
  that have to agree — the revision count, one file per revision, a current model that *is* the last
  of them, artifacts pointing back at revisions that exist — and every one of those claims can be
  false while each individual file is valid JSON. Validating shapes cannot see any of it. The checker
  reports every broken relationship with a stable code rather than raising on the first, so `verify`
  can list them, `import` can refuse an archive, and `doctor` can name the sessions worth looking at.

### Fixed
- **`session import` accepted archives that did not add up.** It checked that `session.json` parsed,
  that its slug agreed and that a claimed revision had a `model.json` — shape, not truth. An archive
  announcing revision 2 with no `revisions/` at all imported cleanly, and so did one whose `model.json`
  had been swapped for a different model. Import now runs the same integrity check as `session verify`.
- **A structurally invalid `session.json`, a corrupt zip and an I/O failure reached the user as
  tracebacks.** Every way a supplied archive can be wrong now arrives as a Requivo error.
- **`import --force` deleted before it replaced.** `rmtree` then rename leaves nothing at all if the
  rename fails: the archive refused *and* the session the user already had gone. The existing session
  now steps aside and is deleted only once the new one is in place, with a rollback if it is not.
- **`session export` read the session without its lock**, so an archive could combine an old
  `session.json` with a newer `model.json` — internally inconsistent, and only discovered on import.
  It now reads under the same lock every writer takes, writes to a temp archive renamed into place,
  and excludes `.lock`, which is this machine's coordination and meant nothing in an archive.
- **Artifact contracts held references that pointed at nothing.** A story could be traced to a slot
  the schema does not define; an epic issue could `depends_on` an id the epic did not contain (and be
  exported as a real tracker link); an estimate could run 5 to 1 days, dragging the project's low
  bound above its high bound; a PRD requirement could be an empty numbered row; ids could repeat, which
  downstream reads as one item rather than two. Each of these is a pointer something follows, so each
  is now checked — structural coherence only, never a judgment about content. The generator prompts
  state the same rules, so the model self-corrects rather than burning retries.

### Changed
- Dependencies carry upper bounds on the majors (`pydantic>=2,<3`, `anthropic>=0.40,<1`, …). A
  dependency's next major is by definition allowed to break us, and without a ceiling it does so on a
  fresh install of an *unchanged* Requivo — the one failure a user cannot correlate with anything.
- The Claude Code `discover` skill passes the request on stdin instead of interpolating it into a
  shell command. A client request is untrusted text; quotes, newlines and `$(…)` are ordinary in prose.
- `ARTIFACT_FILENAMES` moved to Core, where the service that saves, the CLI's `--type` choices and the
  integrity checker read one vocabulary instead of two.

## [0.9.7] - 2026-08-02

The 0.9.6 review: the two seams where a provider call meets a session that can move under it, and the
service layer becoming the integrity boundary it has to be before Cloud calls it directly.

### Fixed
- **A first discovery could still overwrite a refined model.** 0.9.6 gave `run_discovery` the revision
  it read as an optimistic-lock precondition, which is the wrong instrument for this: discovery reasons
  from the request *alone* — it never sees the current model — so on a session at revision 2 it reads
  2, writes against 2, satisfies the precondition perfectly, and replaces two turns of refinement with
  a naive first analysis. `POST /sessions/{slug}/discover` reaches it directly; the Web only offers
  the button at revision 0, but a business rule enforced by a hidden button is not enforced. The
  revision itself is now the rule (`_require_revision_zero`), shared by every entry point, and checked
  *before* the provider call rather than after — a repeat `discover` used to buy a discovery turn, and
  an assessment when finalizing, purely to throw both away.
- **A generation's revision and its model were two separate reads.** A write landing between them gave
  revision N with the model of N+1: the artifact was generated from the newer model and filed against
  the older revision. Nothing downstream could catch it, because the recorded number is perfectly
  plausible — it just describes a different model than the document was written from, which is exactly
  the claim traceability cannot get wrong. `SessionService.snapshot()` returns revision, model, request
  and cards from one read under the session lock, and every provider-backed operation takes one.
- **An unestablishable freshness was reported as "fresh".** `_stale_since` swallowed a `RequivoError`
  from an unreadable history and returned `False`, on the reasoning that an unanswerable question must
  not manufacture a stale flag. But `False` is not the absence of an answer — it is the claim that the
  artifact is up to date, made about a session whose history could not be read at all. The save is now
  refused: the provenance it would record cannot be verified.
- **The service trusted its context cards.** The CLI and the Web both resolve them first, which made
  the service look safe; it is not a boundary until it holds the rule itself. An unknown card recorded
  on a session is read back by every later turn, and an empty resolved selection means *every* card, so
  a bad name silently widened the context instead of narrowing it. `create_session` resolves.
- **`DiscoveryService` could split its storage.** The artifact service defaulted to the process
  repository rather than the session service's, so `DiscoveryService(sessions=SessionService(postgres))`
  — the shape requivo-cloud constructs — wrote sessions to Postgres and artifacts to the local
  filesystem, with every call succeeding. It now follows the session service, and takes a `repo=`
  argument that configures both at once.

## [0.9.6] - 2026-08-02

The 0.9.5 review, and the last release before 1.0. Two correctness bugs sat where the product's own
promise lives — one erased the reasoning layer during an ordinary turn, the other replaced a whole
model with a fragment of it — plus the preconditions and the session identity that a second writer
makes matter.

### Fixed
- **A refinement turn silently erased every decision, challenge and opportunity.** `engine.md` asks a
  turn for `model`/`questions`/`summary` and nothing else — a refinement answers a question, it does
  not re-derive the brief. That reply was read as a complete `EngineOutput`, whose reasoning fields
  default to empty lists, and the apply path stored it verbatim: one ordinary `requivo answer` after a
  brief deleted the entire reasoning layer. It was silent in both directions, because
  `diff_reasoning` deliberately absorbed the populated → empty case to stop exactly this turn from
  marking everything stale — so the apply reported no reasoning change and left the PRD marked fresh
  over a model whose decisions no longer existed. The two defects had been hiding each other. A
  proposal is now its own contract (`ModelProposal`) in which the three collections are tri-state:
  absent keeps what is established, `[]` deletes it, a list replaces it. `resolve()` collapses them
  against the model being refined, once, for every surface — and with omission resolved before the
  diff, the diff is symmetric again, so a real deletion is reported and does invalidate what rested
  on it.
- **`model apply --allow-partial` replaced the model with the fragment.** The name read as a patch; it
  merged nothing. It only relaxed the completeness check, and the partial model then replaced the
  complete one — applying a single slot left a one-slot model where fifteen had been, reported as
  fourteen changed slots. The flag is gone from `apply` and from `diff` (its dry run). Checking a
  projection is still `model validate --allow-partial`, which is what the flag always actually meant.
- **A first discovery had no precondition.** `run_discovery` and `finalize_discovery` reasoned from a
  session and applied without stating the revision they read, the one gap left after 0.9.4 closed
  `answer` and `generate`. A write that landed during the provider call was overwritten by a model
  reasoned from the older state. Both now carry it — and because creation is idempotent, re-running
  `discover` on a request whose session already holds a model is a `revision_conflict` naming
  `requivo answer`, rather than a naive first-turn model quietly replacing a refined one.
- **Session creation ignored its context cards, and was not atomic.** Two creations of the same request
  returned the same session even when they asked for different cards — but the cards are the provenance
  of every impact estimate the session will make, so the same request read against `b2b-platform` and
  against `event-ops` is not the same discovery; the caller got a session with cards it had not asked
  for and no way to notice. Identity is now the request *and* its card selection. Creation itself was a
  `has_meta` check followed by a write, which a dozen concurrent callers all passed: each wrote its own
  `session.json` over the last, so the session's id, provider and cards were whichever writer finished
  last. A session is now assembled in a staging directory and renamed into place — the rename is the
  claim on the slug, so exactly one caller creates it and the rest are handed what exists.
- **An empty objective was complete on one surface and not the other.** The provider's retry hook
  required `summary.objective`; the deterministic apply path required only the slots. The same model
  was therefore acceptable from Claude Code and refused from Anthropic. Both boundaries now read one
  definition (`completeness_gap`), which also keeps a session of fifteen filled slots from rendering a
  blank heading in every view.
- **The brief skill offered an enum the contract rejects** — `"leverage": "low|medium|high"`, where
  `Leverage` is `high|medium|future`. A skill's JSON template is an instruction, so the failure landed
  one step later as a schema error on an apply. Fixed, and a static test now holds every skill's enum
  placeholders to the contracts' vocabulary.

### Changed
- `plugins/claude-code/REASONING.md` states the proposal contract the skills work against: complete
  slots, a real objective, and the tri-state reasoning layer. The `answer` skill is explicit that
  leaving the three collections out is the normal case.

## [0.9.5] - 2026-08-02

The second half of the 0.9.3 review: untrusted input, output that is worth saving, and the Claude Code
surface reaching parity with the provider path.

### Fixed
- **`session import` wrote before it checked.** It called `extractall` straight into the session store
  and then reported success, so a bad archive was already unpacked by the time anything could object.
  Its traversal guard compared path *strings* (`str(target).startswith(str(root))`), which is not a
  containment test — `/…/sessions-evil` starts with `/…/sessions`. And an archive whose folder was
  named `bad slug` imported happily and then broke every later `session list`. Import is now
  inspect → extract to scratch → validate → move: exactly one session directory, its name validated as
  a slug, file-count and expanded-size ceilings, every entry decomposed into path components rather
  than string-matched, and the extracted directory confirmed to be a real session (its `session.json`
  parses, its slug agrees with the directory, a claimed revision has a `model.json`) before it is moved
  into place. A collision is refused unless `--force`. A refused import leaves nothing behind.
- **The Claude Code brief produced prose and dropped its reasoning.** The provider path absorbs the
  assessment's decisions, challenges and opportunities into `model.json` so every later generator
  inherits them; the skill only wrote Markdown. A PRD generated after a brief in Claude Code therefore
  could not build on it. The skill now folds the structured reasoning back through `model apply`
  first — no CLI change was needed, the apply path already accepted it — and saves the document
  against the revision that created.
- **Skills staged content in `/tmp`.** One shared path, so two sessions working at once overwrote each
  other; `/tmp/requivo:prd.md` is not a legal filename on Windows; and cleanup needed `rm`, which the
  plugin does not grant itself. Every command that takes a document now accepts `-` for stdin, and no
  skill writes a file at all — the `Write` grant is gone with the need for it.
- **Skills read every context card, whatever the session was created with.** A session's card selection
  is held constant across its turns because it is what the impact estimates were made against; a later
  turn reading all of them reasons from a wider context than the model was built on, which the golden
  harness has measured as a real cost. `requivo context --session <slug>` prints exactly that
  session's cards, and the skills use it.
- **`schema_version` was decorative** — recorded on every session, read by nothing. A session authored
  against a newer slot vocabulary is now refused as clearly as a newer `format_version`.
- **Two identical reasoning items collided on one id.** Ids are content-derived, so a repeated decision
  produces a duplicate key — and the id is what a diff keys on and what a user cites a decision by. It
  is now a validation error that rides the retry loop, rather than one of the pair going invisible.
- **htmx injected a `<style>` block the CSP blocked** on every page load. Nothing uses
  `.htmx-indicator`, so the styles were pure cost and the violation was pure noise — and a CSP that
  cries wolf is one nobody reads. Disabled via htmx's config meta tag.
- Docstring and comment corrections: the package no longer describes Claude Code and the Web as
  future work, and `ArtifactService` no longer claims the assessment is exempt from staleness.

### Changed
- **Artifact contracts require what makes each artifact *be* that artifact.** A PRD with an empty
  `title` or no `problem`, a Gherkin scenario with no `when` or no `then`, an epic that decomposes into
  zero issues, a nameless story — all were structurally valid and none were usable. These fields are
  now non-empty by contract, so a degraded generation fails loudly and retries instead of being saved.
  The bar is deliberately low: only what is definitionally required, never a judgment about whether an
  artifact is *good enough*. The generator prompts state the same requirements, so the model is told
  the rule rather than discovering it through a retry. `engine.md` and `brief.md` are untouched — the
  golden baselines still apply.

### Added
- `-` reads a document from stdin on `model validate`, `model apply`, `model diff`,
  `artifact save --file`, and `session init`.
- `requivo context --session <slug>` — the cards that session was created with.
- `session import --force` — replace a session of the same slug.

## [0.9.4] - 2026-08-02

Integrity: the session store is now trustworthy under concurrent writers, and freshness and forward
compatibility are guarantees rather than intentions. From an external review of 0.9.3 — everything
here is a case where the store could lose a change or report something it could not know.

### Fixed
- **Two writers could both win.** `save_revision` checked `expected_revision` and *then* performed its
  writes, with nothing holding the two together, so two processes reading the same revision both
  passed the check and the second silently overwrote the first. Worse, they also shared one scratch
  filename (`.model.json.tmp`), so the usual symptom was not a lost update but a `FileNotFoundError`
  from `Path.replace` — a conflict presented as a bug in Requivo. Every compound mutation now runs
  under a per-session OS lock (`.lock`, re-entrant per thread, released by the kernel on crash) and
  every atomic write uses a temp name private to the writer. The loser gets `revision_conflict`, which
  is a real answer. Reproduced by a twelve-thread regression test.
- **An artifact saved against an older revision was recorded fresh.** `ArtifactService.save` took the
  caller's `source_revision` and wrote `stale=False` beside it, so a PRD reasoned from revision 1 and
  saved once the session had reached 3 sat on disk marked current. Reasoning and saving are not the
  same moment — a provider call takes minutes, and Claude Code may save a document from several turns
  ago — and the answer is knowable: the source revision is now diffed against the current model and
  the artifact is recorded stale if its dependencies moved. `artifact save --json` returns the `stale`
  it recorded, rather than making the caller ask again.
- **A change to the reasoning layer alone left every artifact fresh.** Staleness was computed from
  `diff_models`, which compares slots. But every generator is prompted with the complete model,
  reasoning included, so a rewritten design decision can change a PRD with no slot touched — and the
  apply reported `changed_slots: []` and marked nothing stale. `diff_reasoning` now covers decisions,
  challenges and opportunities, comparing content rather than only ids (an id derives from a subset of
  each item's fields, so an edited rationale kept its id and was invisible). Reasoning a turn merely
  *omits* is deliberately not a removal: a refinement turn replies without re-stating the brief, and
  reading that silence as a deletion would mark everything stale on nearly every turn.
- **An older Requivo destroyed a field a newer one had added.** `SessionMeta` used `extra="ignore"`,
  so an unknown key loaded fine and was then dropped the moment the older version wrote the file back
  — turning the documented "adding a field is compatible" into "the first mutation by an older reader
  deletes it". Persisted metadata is now `extra="allow"`, matching `RevisionRecord`, which had made
  this choice for exactly this reason. Keys Requivo has genuinely retired are dropped explicitly, in
  `_RETIRED_KEYS`, so forward compatibility does not mean carrying dead keys forever.
- **A long request produced a slug the filesystem refused.** `_slug` took the first five words with no
  length bound, so a single 300-character token became a 300-character directory name and the write
  failed deep inside with a bare `OSError`. Slugs are now capped (80 characters, enforced in
  `validate_slug` so an explicit `--slug` is bounded too) with deterministic truncation plus a content
  hash, so two different long requests cannot collapse onto one session.
- **The provider raised a bare `RuntimeError` after exhausting its retries.** Every surface catches
  `RequivoError`, so this one reached the user as a traceback. It is now `ProviderOutputError`
  (`provider_output_invalid`), carrying the contract, the attempt count and the last failure.
- **The Claude Code skills ignored the locking primitives the CLI already had.** `model apply` accepts
  `--expected-revision` and `artifact save` accepts `--revision`, and no skill passed either — so a
  Claude Code turn could overwrite a change made in the Web while it was reasoning, with no error. The
  revision contract is now stated once in `REASONING.md` and followed by every skill: read the
  revision, reason from it, hand it back on apply and on save.
- **The plugin version had drifted a release behind** because it was also written out in prose. The
  prose no longer restates it, and a test pins the manifest to the package version.

### Added
- `session init --json` reports `revision`. Init is idempotent, so it can hand back an *existing*
  session that already carries a model; a caller about to apply needs to know which of the two it got.
- `model apply --json` reports `changed_decisions`, `changed_challenges` and `changed_opportunities`
  alongside `changed_slots`. The slots say the facts moved; these say the judgment over them moved.
- `SessionRepository.lock(slug)` — the storage seam gains the one operation the service needs to make
  a compound update atomic. The file backing maps it to an OS file lock; a Postgres backing maps it to
  the row lock of the enclosing transaction.

## [0.9.3] - 2026-08-01

Pre-1.0 consolidation: the session format is declared public and pinned by a test, the deprecations
are written down, and the Web catches up with what the shared service can already do.

### Added
- **The session format is a published contract.** [`docs/compatibility.md`](docs/compatibility.md)
  states what `.requivo/sessions/` guarantees, what may change without a `format_version` bump (adding
  a field, retiring an unpopulated one), and what requires one (renaming or repurposing a populated
  field, changing the layout). The `--json` outputs and error `code` values are covered by the same
  rule. A frozen 0.8.2 `session.json` now lives in the test suite and must keep loading verbatim —
  including a key that has since been removed — and a session claiming a newer `format_version` must
  still be refused rather than half-understood.
- **A written deprecation policy**, with the current list: the legacy flag CLI (removal 1.1.0), the
  `pc` alias, legacy `out/` sessions, and the old `/requivo-<skill>` plugin names. Anything deprecated
  keeps working for at least one minor version and names its replacement; nothing is removed in a patch.
- **The Web generates every artifact the service can produce** — acceptance criteria, delivery epic and
  release notes join the solution assessment and the PRD. The buttons are built from the service's own
  `GENERATABLE` vocabulary rather than a list kept in the Web, so a generator registered once appears
  on every surface instead of each surface keeping its own list and drifting.

### Fixed
- **`discover` on a file whose name is not already a slug.** The filename stem was passed through as
  the session slug, so an ordinary input file — `Leave Approval v2.md` — died on `invalid_slug`. A
  filename is a suggestion; it is now slugified like any other.
- **`discover` on a directory path.** The file check used `exists()`, which a directory satisfies, and
  the next line called `read_text()` on it — a traceback instead of treating the argument as a request.
- **`model validate --session` was declared and read by nothing.** A flag that parses and changes
  nothing is worse than a missing one: the caller believes a check ran. Removed; `model diff` is the
  command that actually validates a proposal against a session.
- **Unexpected web errors are logged.** The handler correctly kept tracebacks away from the browser
  but sent them nowhere else, so a genuine failure left the operator with a generic page and no trace.
  Method and path are logged; the request body deliberately is not.

## [0.9.2] - 2026-08-01

The second half of the 0.9.0 review: consistency between surfaces, and the identity/provenance
decisions that have to be made before anything else writes sessions. No breaking change to the session
format — a 0.9.x session is read and written unchanged.

### Changed
- **Every interface produces the same artifact.** Generation moved behind `DiscoveryService.generate()`
  for all of `brief` / `prd` / `criteria` / `epic` / `release`, so a document asked for from the
  terminal is produced, saved, versioned and tracked exactly as the same document asked for from the
  browser or from Claude Code. The terminal used to render the solution assessment and keep it — it now
  saves it like everywhere else. `stories` and `estimate` stay deliberately terminal-only (analyses
  feeding the estimate, not deliverables with a file) via `DiscoveryService.reason()`.
- **The provider seam is real, not decorative.** `DiscoveryService` now talks to the
  `ReasoningProvider` protocol only — `analyze` / `generate` / `model_name` / `provenance` — instead of
  importing the Anthropic functions directly. Swapping the reasoning backend is a constructor argument;
  a test runs a whole discovery through a provider with no vendor behind it.
- **Provenance is populated, not just declared.** Each revision records the provider, the model, the
  surface, and `prompt_version` — a hash of the exact system prompt (prompt file + schema + the context
  cards actually selected). Behaviour here is tuned by editing assets, so that hash is half the answer
  to "what produced this revision". The never-written session-level `prompt_versions` map is gone.
- **Boundary contracts are strict.** Everything an LLM fills now inherits a `StrictModel` base
  (`extra="forbid"`): a field the model invents fails loudly and rides the retry loop instead of being
  silently dropped. Text that must say something (a question's `q`/`why`, a challenge's premise,
  alternative, consequence and recommendation) is rejected when empty, and a discovery reply must carry
  a non-empty objective.
- **Design decisions, challenges and opportunities carry stable ids** (`dec_…`, `chl_…`, `opp_…`),
  derived from their own content and recomputed on every validation — identical across revisions,
  surfaces and machines while the statement is unchanged, and impossible to forge, since a supplied id
  is always overwritten. Cloud needs to refer back to a decision; text is a poor handle.
- **The legacy flag CLI is deprecated** and moved to `requivo/legacy.py`, frozen, with a notice on use
  and removal scheduled for **1.1.0**. It writes the old `out/` layout — no revisions, no provenance,
  no staleness — and deleting that one file is now the whole removal.
- **`CLAUDE.md` rewritten** (327 → 261 lines). It described `out/<slug>/model.json` as the store, `pc`
  as the modern CLI, an 8k token ceiling, and two modules that no longer exist — and it is the file an
  agent reads before changing this repo, so its drift was a live risk of re-introducing what had just
  been removed. It now leads with the invariants that must not be broken.

### Fixed
- **The Claude Code skills were documented under names nobody could type.** Claude Code namespaces
  plugin skills as `/<plugin>:<skill>`, so `skills/requivo-discover/` in a plugin named `requivo` was
  really `/requivo:requivo-discover`. The skills are renamed to `discover`, `answer`, `status`,
  `brief`, `prd`, `impact` — invoked as `/requivo:discover` — and a test now checks the README against
  the actual namespacing.
- **`requivo discover --context` accepted an unknown card with a warning** and carried on with *all*
  cards, which is the opposite of narrowing. It now uses the same Core resolver as the deterministic
  verbs and the Web, and refuses. (0.9.1 fixed this everywhere except the main discovery path.)

### Added
- **The repository is a plugin marketplace** (`.claude-plugin/marketplace.json`), so the plugin install
  is two exact commands — `/plugin marketplace add jbkkz/requivo` then `/plugin install requivo@requivo`
  — instead of the previous "point Claude Code at this directory". The plugin version now tracks the
  Requivo release it was tested against.
- **`THIRD-PARTY-NOTICES.md`**, shipped in the wheel's `dist-info`: the vendored htmx copy, its version,
  its upstream and its licence. 0BSD requires no attribution; a redistributed file should still be
  traceable.
- **The publish workflow gates on the things that make a bad release permanent**: the tag must exist and
  agree with both `pyproject.toml` and `__version__`, the tagged commit is what gets built, and lint,
  tests, `twine check` and an outside-the-repo wheel smoke test (including the web assets) all run
  before the upload. A manual dispatch now requires a tag instead of publishing whatever is on main.

## [0.9.1] - 2026-08-01

Correctness and web-security fixes from an external review of 0.9.0. No new surface, no format change:
a 0.9.0 session is read and written identically.

### Fixed
- **Generation no longer races a concurrent write.** A provider call runs for seconds to minutes, and
  the session can move underneath it (a second browser tab, a CLI apply, a Claude Code turn). The
  revision the model was read at is now captured before the call and carried through both writes: as
  the optimistic-lock precondition on any apply — so a concurrent change surfaces as a clean
  `revision_conflict` instead of being silently overwritten — and as the artifact's recorded source, so
  a document written from revision 1 is never filed as if it came from revision 2. An artifact whose
  inputs moved while it was being generated is now born stale rather than inheriting the newer
  revision's freshness. An answers turn now defaults to the same precondition (the revision it read),
  so the CLI inherits the protection the Web already had from its form.
- **The saved solution assessment goes stale when the model does.** It was excluded from the
  artifact→slot map as "the live analysis layer"; that stopped being true when it became a saved
  artifact, and the result was an assessment still marked fresh after the problem statement under it
  had been rewritten. It now maps to every slot — it is a judgment over the whole model — so any
  material change reaches it.
- **`session show` and `artifact list` agree on freshness.** `session show` treated any artifact behind
  the current revision as stale, contradicting every other view in the same binary. The explicit stale
  flag (set from the dependency graph) is the rule; the source revision is provenance, not a verdict.
  The Claude Code skills said the same wrong thing and have been corrected.
- **Impossible artifact provenance is refused.** An artifact could be recorded against a revision that
  does not exist (or against revision 0). Every freshness answer downstream is read off that number,
  so it is now validated against the session's history at the write.
- **Sonnet 5 launch pricing.** The cost estimate billed the default model at the standard $3/$15 while
  launch pricing ($2/$10, through 2026-08-31) was live, overstating cost by a third. Rates that expire
  now carry their end date, so the estimate is right on both sides of it without another edit.

### Security
- **Cross-site request protection on Requivo Web.** Binding to `127.0.0.1` is not a boundary: any page
  open in the same browser could post to a known local port without a preflight, creating sessions and
  spending the server's Anthropic key — the attacker never needs to read a response to do that damage.
  Writes now require a per-process request token (rendered into every form), and are checked against
  the browser's `Sec-Fetch-Site` hint and an `Origin`/`Referer` host match. A host allowlist (loopback,
  plus `REQUIVO_WEB_ALLOWED_HOSTS` for a deliberate non-local bind) runs on reads too — it is the guard
  against DNS rebinding, where the attacker's page *would* be able to read the token. `requivo web
  --host` records its own bind address, so an intentional non-local run keeps working.
- **Over-long input is refused, not truncated.** A request or answers block past its ceiling was cut
  silently and reasoned over as if whole. Request bodies are also capped before being parsed.
- **An unknown context card is an error.** It used to be filtered out, which left an empty selection —
  and an empty selection means *all* cards, so a typo widened the context instead of narrowing it. The
  CLI already refused; the resolver now lives in Core and both surfaces share it.

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
