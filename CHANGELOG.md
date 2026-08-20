# Changelog

All notable changes to Requivo are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-08-20

### Added

- **Both spellings of the context-card selector now work on every verb that takes one** (#85). The
  same selector — same comma-separated grammar, same `resolve_cards` validator — was spelled
  `--context` on `discover` and `session init` and `--cards` on `context`. Each verb now accepts both.
- **`--context` is the documented primary; `--cards` is a permanent alias.** They are two option
  strings on one argparse action rather than two arguments, so they cannot drift apart and neither can
  silently discard the other when both appear on a command line. The destination is unchanged on each
  verb, so no handler moved and nothing persisted changed.
- One cost worth naming: on these three verbs the single-letter abbreviation `--c` now reports
  `ambiguous option` where argparse used to resolve it. `--co` and `--ca` still resolve, as do both
  full spellings; argparse prefix abbreviation is not a documented part of this CLI.
- Compatibility: compatible - additive, with the single exception named in the bullet above: `--c`
  on those three verbs now reports `ambiguous option` where argparse used to resolve it. Every other
  command line that worked before works unchanged and produces the same output, and the new behaviour
  is that the previously-rejected spelling is now accepted. Adding the aliases **in** 1.0.0 converts
  what would have been a breaking removal into a documentation choice that can be made at any time.

### Changed

- `invalid_session` is now a **family that nothing raises directly** (#82), and each of its nine
  conditions carries a code that names the fact: `unsupported_format_version`, `unsupported_schema_version`,
  `session_unreadable`, `artifact_revision_out_of_range`, `unstated_source_revision`,
  `unreadable_source_revision`, `inconsistent_archive`, `unreadable_archive` and `import_move_failed`.
  It had carried seven facts across eight raise sites with four `details` shapes, and no key was
  present on all eight -- `details["slug"]` raised `KeyError` on three of them. Unlike the
  `cross_site_request` split, this one was never inert: `cli.py` serializes `to_dict()` on every
  `--json` verb, so a consumer could observe the inconsistency, and `docs/compatibility.md` promised a
  condition by code (`invalid_session`, "upgrade requivo") that the code could not tell from a corrupt
  zip -- while the same page says to assert on the code and never on the message.
- `except InvalidSessionError` is unchanged: the base is kept as the family, so nothing that catches
  the class has to enumerate nine names.
- Six of the nine conditions change HTTP status in Requivo Web. The two version frontiers answer
  **409** and the four store-state arms answer **500**, where everything under `invalid_session`
  previously answered 400 -- the misattribution #34 fixed for `context_unreadable`, one condition
  along. **Both counts are history and stay as written**: #101, in this same release, adds a tenth
  arm (`invalid_archive`) and a fourth 400 arm, and restating #82 against the family as it now stands
  would claim something #82 did not do. `docs/compatibility.md` carries the reconciling table.
  The three that keep 400 are the ones genuinely about the request: both archive arms and
  `unstated_source_revision`.
- `unsupported_format_version` carries `{format_version, supported_format_version}`; the second key is
  new, because *newer than what* is half the fact and a reader had no other way to learn which build
  they were holding.
- The arms deliberately do **not** share one `details` shape. Three of them identify no session at
  all -- none has been identified when a zip will not open -- and a `slug: null` there would state a
  fact nobody measured. Branch on the code, then read the shape documented for it.
- Compatibility: breaking - moving a condition from one error code to another, and changing an HTTP
  status, are both listed as breaking in `docs/compatibility.md`. Taken **in** 1.0.0 deliberately:
  this is the release that draws the boundary, so the move costs nothing beyond the tag itself. After
  it, the same move costs a major version, or a promise on that page nobody can keep.

- **`requivo epic --json` is now `requivo epic --export-json`** (#83). On every other verb `--json`
  means *emit the payload on stdout*; on `epic` it meant *also write a second file into `artifacts/`*,
  and nothing reached stdout. It now sits alongside `--github` and `--gitlab` as three flags of one
  kind, each writing an export file. `epic` deliberately gains no stdout `--json`.
- **The rename fixes a second thing the name was hiding: the error channel.** `cli.app()` reads
  `getattr(args, "json", False)` generically and uses it to switch failures from a prose message on
  stderr to a structured JSON envelope on stdout. Because `epic`'s file-writing flag was spelled
  `--json`, passing it silently changed how a failure was reported — the same provider outage printed
  prose under `--github` and a JSON envelope under `--json`. With no `json` attribute on `epic`, that
  `getattr` falls through and all three export flags report a failure identically. Nothing documented
  the old behaviour and nothing asked for it.
- Compatibility: breaking - `requivo epic <slug> --json` is no longer accepted and exits 2 with an
  argparse usage error; use `--export-json`, which writes the same `epic.json`. Breaking on both
  halves named in `docs/compatibility.md`: a flag is removed, and the meaning of passing it changes.
  The failure is loud rather than silent — `--json` is not a prefix of `--export-json`, so argparse
  rejects it outright instead of quietly doing something new. Callers parsing `epic`'s failure output
  as JSON must now read the prose on stderr, as `--github` and `--gitlab` callers already did.

- `requivo session import --json` now prints `{"slug": ..., "path": ..., "replaced": ...}`. It was
  `{"imported": ..., "into": ..., "replaced": ...}` — the one session verb that spelled the session
  and its location differently from all of its siblings, so a consumer looping over the session verbs
  and reading `row["slug"]` got a `KeyError` from the verb that had just put the session there (#84).
- `path` is the **session directory**, not the session root. `into` carried the root; `session init
  --json` has always meant the session directory by `path`, and `session import`'s own human-readable
  line already printed it. Renaming the key over the old value would have given `path` two meanings
  across two verbs of one noun, which is the defect this change closes, back under the harmonised
  name (#84).
- Compatibility: breaking - two keys are removed from a populated public `--json` output and the
  value under the new location key changes. `imported` is `slug` and `into` is `path`, with `path`
  now naming the session directory rather than the directory that holds it. They are not kept as
  duplicates: removing a `--json` key is breaking, so the rename ships **in** the 1.0 tag or never
  (#84).

- `requivo session verify` now exits **4** when it could not read a session's product context at all.
  It answers three different things — the session is inconsistent, its product context was read and
  does not resolve, its product context could not be read — and had two exit codes, so *checked and
  broken* and *could not check* were spelled the same way by the one verb whose whole job is to say
  whether a session is sound. The third state already had a rendering of its own; only the exit code
  collapsed (#86).
- Where both happen at once the **firm negative wins**: a session that is inconsistent *and* whose
  cards were unreadable exits 1. A script gating on *is this usable* wants the definite answer, and
  there is one (#86).
- Exit code 4 is now general. It was `EXIT_DEGRADED_LISTING` and is `EXIT_DEGRADED`: it describes a
  shape of answer — the work was done and part of the answer was unreachable — not a verb. Minting a
  code per verb would rebuild the collapse 4 was introduced to undo. The exit-code table in
  `docs/cli.md` states the general sentence and lists the two commands that reach it (#86).
- `requivo doctor` does **not** move and still exits 0 whatever it finds. `verify` is a gate whose
  exit code is a decision; `doctor` is a report, and a report that exits non-zero is concluding — the
  same directory can be a half-extracted archive or a leftover lock and nothing in it says which. The
  reason is now written down in `docs/cli.md` beside the table, so harmonising the two is a decision
  somebody has to argue with rather than a tidy-up (#86).
- Compatibility: breaking - one condition moves to a different exit code. A session whose product
  context could not be read answered 1 from `session verify` and now answers 4, which is a change a
  script can observe and was never announced. Narrow in practice:
  nothing that exited 0 now exits non-zero and nothing that exited non-zero now exits 0, so
  `verify && deploy` is unaffected — only a script that discriminates on the number sees it. The
  module constant `requivo.deterministic.EXIT_DEGRADED_LISTING` is renamed to `EXIT_DEGRADED` in the
  same change (#86).

- `requivo session list --json` now prints an **object**, not a bare array:
  `{"sessions": [...], "degraded": n, "session_root": "..."}`. The rows are unchanged — the wrap is
  the whole difference — and `degraded` is the count of rows that could not be read, the same
  condition exit code 4 already signals (#87).
- Compatibility: breaking - the top-level type of a populated public `--json` output changes from
  array to object. A `jq '.[] | .slug'` one-liner becomes `jq '.sessions[] | .slug'`; nothing else
  moves, and no row field is renamed or removed. This is deliberately not shimmed: it was the only
  array among the CLI's JSON payloads and an array has no top level, so no field could ever be added
  to it. It ships **in** the 1.0 tag, which is the boundary itself; the next break of this class is
  a 2.0 (#87).
- `degraded` recovers no fact that was missing. Every row has carried `readable` and `error` since
  #62, so the count was always derivable. What it buys is that exit 4 is readable on stdout rather
  than only signalled — the same argument that makes a degraded row name its session instead of
  disappearing (#87).

- `requivo doctor --json` spells the strict-handler stream state `will_crash` rather than
  `will-crash` in `output.streams[].state`. It was the only hyphenated value in any `--json` enum —
  `context.status`, `NonSessionEntry.kind` and `UpdateResult.status` are each one word or
  underscore-joined — so a consumer mapping a state onto an identifier had exactly one value to
  special-case, and one that a naive split on `-` would cut in half (#88).
- The human `doctor` report is unchanged: the state is a wire value, never a printed word.
- Compatibility: breaking - `will-crash` -> `will_crash` renames a value in an enum that v0.11.0
  published to PyPI, so a consumer matching on the old string stops matching and must be updated.
  It ships in 1.0.0, the release that draws the compatibility boundary, so this is the last window
  in which the rename is cheap; after it the hyphen would stand until a 2.0 (#88).

- `docs/compatibility.md` now bounds four surfaces that were in neither column -- neither promised nor
  disclaimed (#89). A 1.0 is only as good as its boundary, and a surface listed nowhere is a promise
  nobody made and everybody may assume.
- **The epic export envelope is stable and versioned.** It carries its own `format` (`requivo-epic`)
  and `version` (1) and exists to be validated outside this repo, so calling it unstable would have
  contradicted the code declaring it stable. The `--github` / `--gitlab` tracker plans are stable in
  the same way, with the asymmetry stated: a change we make is breaking, a change forced by GitHub or
  GitLab moving was never a promise we could make.
- **Environment variables are stable**, under the same rule as a CLI flag: `REQUIVO_WORKSPACE`,
  `REQUIVO_CONTEXT_DIR` and `REQUIVO_WEB_ALLOWED_HOSTS`.
- **Requivo Web's ten routes are stable in path, method and status; their response bodies are not.**
  The bodies are HTML rendered for a browser, HTMX fragments included. `GET /health` and
  `GET /sessions/{slug}/export` are the two that return data and are stable.
- **Artifact filenames are stable and part of the session format**, so renaming one needs a
  `format_version` bump and a migration. The map is written out, which also corrects a detail: `brief`
  is stored as `solution-assessment.md`, not `brief.md`.
- **The `code` on Requivo Web's error banner is presentational and not stable.** Four of its values
  are bare string literals outside the `RequivoError` vocabulary, so the guard that walks that
  vocabulary cannot see them; a caller scripting the Web branches on the HTTP status.
- Compatibility: compatible - nothing in the product changed. This states what the existing behaviour
  promises, in both directions, so a reader can tell which column a surface is in.

- `docs/compatibility.md` now records three breaking `--json` changes that reached no line on the
  contract page (#100): `session list --json` becoming an object and the `session import --json` key
  rename, which both land in 1.0.0, and the `will-crash` -> `will_crash` respelling, which shipped in
  0.11.0 and is corrected here. Each was
  declared breaking in its own changelog fragment and named in `docs/cli.md`; none reached the page
  that says what will not be taken away. `grep -cE '#84|#87|#88|session_root'` returned 0 before this
  change.
- The migrations are stated where a reader needs them: `jq '.sessions[]'` for the first, and the note
  that `path` is the session directory rather than a rename of `into`, which was the sessions root.
- Compatibility: compatible - this records changes already shipped. Nothing in the product moved.

- `session import` refuses a malformed archive as an **archive** (#101). Seven shape conditions --
  no entries, more than `MAX_ARCHIVE_FILES`, expanding past `MAX_ARCHIVE_BYTES`, an entry carrying a
  Windows separator, an entry that is absolute or holds a `.`/`..` segment, an entry not inside a
  session directory, and more than one session directory -- moved from
  `invalid_model` to a new `invalid_archive`. `invalid_model` is documented as *"a proposed model is
  structurally or semantically invalid"* and nobody proposes a model when they hand `session import`
  a zip.
- **An occupied slug is `session_exists` / 409**, not `invalid_model` / 400. The vocabulary already
  had the right code and the right status; the import path was the one caller that did not use them.
- The composition is why this shipped now rather than after 1.0. #82 split `invalid_session` into a
  nine-arm family in the release just gone, on the principle that a code must name its fact, and gave
  `unreadable_archive` and `inconsistent_archive` codes of their own. Those two arms sit *either
  side* of these eight conditions, in the same function, on the same code path. `cli.py` serializes
  `to_dict()` on every `--json` verb, so a consumer scripting `session import --json` read one handle
  for *my zip is too big*, *that slug is taken* and *your proposal is malformed* -- three remedies,
  one code, on the page that tells them to assert on the code and never on the message.
- **One code for the seven, and a discriminator rather than an excuse.** They share a remedy -- give
  me a different archive -- so seven codes would send a reader to one place seven ways, which #82's
  own rule refuses. What a single code owes in exchange is the thing #82 was actually about:
  `details["problem"]` is on **every** arm and is one of `empty`, `too_many_files`, `too_large`,
  `unsafe_entry`, `entry_outside_session_directory`, `multiple_sessions`. Each arm still adds only
  the numbers its own sentence quotes -- `{files, max_files}`, `{bytes, max_bytes}`, `{entry}`,
  `{slugs}` -- because padding them to a common shape would state measurements nobody took.
- `InvalidArchiveError` is a tenth arm of the `InvalidSessionError` family, so `except
  InvalidSessionError` catches it alongside the two archive arms on either side of it. It is no
  longer an `InvalidModelError`, which is the breaking half for anyone who caught the class.
- No HTTP status moves. `invalid_archive` answers **400**, the same number `invalid_model` answered
  and the same one its two siblings on this path already give: the caller handed us this archive,
  nothing has been written to the store, and re-sending the same zip unchanged can never succeed.
  `session_exists` moves 400 -> **409**, which is the correction rather than a side effect.
- `SessionExistsError`'s docstring now names its second raiser. In `create_session` and
  `migrate_legacy` it is raised by the atomic claim itself; in `session import` it is a check, with
  the TOCTOU window that implies. That window predates this change and is not what moved -- it is
  written down so the docstring stops promising a guarantee one of its three raisers does not make.
- **A directory name inside an archive can no longer write a line of the refusal that reports it.**
  Found by this change's own audit, on a line this change edits. `_inspect_archive` interpolated the
  top-level directory names raw when refusing an archive holding more than one -- and those names are
  the one piece of archive text `validate_slug` has not seen yet, because validation runs on the
  single surviving slug after the count check. A directory whose name carried a newline ended the
  line and wrote the next at column 0 of stderr, which `safe_write` does not prevent: it guards
  encoding, not control characters. The two sibling refusals on the same path already rendered an
  entry name with `!r`; this one now renders each name through `display_token`, so a name with
  nothing to escape is unchanged and one that could break the line is quoted rather than dropped.
  `details["slugs"]` stays raw -- `json.dumps` escapes it, so `--json` was never exposed. Same class
  as #40 and #98, one function along.
- Compatibility: breaking - moving a condition from one error code to another is listed as breaking
  in `docs/compatibility.md`, and eight conditions move. Taken **in** 1.0.0 deliberately: this is the
  release that draws the boundary. After it, the same move costs a major version, or a code that
  permanently means eight things in the verb most likely to be scripted.

- **Every `--json` output is public — all fourteen** (#102). The page previously named six and
  justified them as "what the Claude Code plugin drives", which was wrong in both directions by three
  entries each: the plugin drives `session init`, `model validate` and `artifact save`, which were not
  listed, and does not drive `model diff`, `artifact list` or `session list`, which were. Eight
  outputs were in neither column, and #84 made a breaking change to one of them before anyone noticed
  there was no promise to break.
- The promise itself is unchanged and additive: fields get added, a populated field does not change
  meaning without a note. Widening it from six outputs to fourteen costs nothing, and the subset was
  the expensive half -- a subset needs a boundary somebody can check, and the only one ever offered
  was a claim about another artifact's current contents that nothing tested.
- `test_every_json_verb_is_inside_the_promise` is the guard, and it reads the verbs off the built
  argparse tree rather than grepping the source: a grep validates the reader's regex, and what is
  promised is what the command actually accepts. It checks both directions -- a verb with `--json`
  and no row, and a row for a verb that no longer takes one.
- Compatibility: compatible - eight outputs gain a promise; none loses one.

- `requivo artifact list --json` now prints an **envelope**, not the bare map of artifacts:
  `{"slug": "...", "artifacts": {"<type>": {...}}}`. The rows are unchanged — same keys, same order,
  keyed by artifact type as before — and the wrap is the whole difference (#107).
- Compatibility: breaking - the top level of a populated public `--json` output stops being data. A
  `jq '.prd.stale'` one-liner becomes `jq '.artifacts.prd.stale'`; nothing else moves, and no row
  field is renamed or removed. It ships **in** the 1.0 tag, which is the boundary itself; the next
  break of this class is a 2.0 (#107).
- This is #87's argument one shape along, and it is the last of the fourteen `--json` payloads with
  no real top level. #87 moved `session list` off a bare array because "an array has no top level,
  so no field could ever be added to it"; a top-level map keyed by data has that property in
  practice, because the natural consumer read is `for t, info in payload.items()` and any metadata
  key added later is both ambiguous with a future artifact type and breaks that loop. Holding the
  argument for an array and not for a map is not defensible (#107).
- `slug` is the only key the new top level carries. Every sibling session verb answers it and this
  one had nowhere to put it, which is the whole point of gaining a top level; a top level nobody
  needs yet is still worth having, and filling it speculatively is not (#107).
- A session with nothing saved now answers `{"slug": ..., "artifacts": {}}` where it answered `{}`.
  That is the case the old shape served worst: `{}` named neither the session nor the fact that the
  question had been answered, so a consumer could not tell it from a payload that failed to
  serialise (#107).

### Fixed

- One directory under the session root that the process cannot stat into no longer hides every
  healthy session. `requivo session list` exited **1** with an empty stdout and a raw
  `PermissionError` traceback, and every other session in the workspace was invisible: the partition
  that decides whether a name is a session probes `<name>/session.json`, and `Path.exists()` re-raises
  EACCES rather than swallowing it, so one entry aborted the scan for all of them (#80).
- The partition now answers in **three states, not two** — a session, not a session, and *could not
  tell*. An entry whose examination raised belongs in neither of the other two: filed as a non-session
  it would drop out of every listing, which is the invisible-entry defect #67 closed one function
  along; filed as a session it would be claimed to be one, which is exactly what the failed probe did
  not establish (#80).
- `requivo session list` gives such an entry a degraded row and exits **4**. The row names the entry
  and the error, states nothing it could not read — no revision, no provider, no timestamp — and every
  healthy session is still listed in full beside it. Exit 4 already means *the work was done and part
  of the answer was unreachable*, which is what this is (#80).
- `requivo doctor` reports the entry instead of declaring the whole session root unreadable. It said
  `sessions unreadable` with `<root> could not be listed` beneath it, which was a claim broader than
  what had failed — the root *was* listed. The whole-root arm stays for the case that genuinely is the
  whole root: `iterdir()` on the session root itself failing. `--json` gains
  `sessions.unexaminable`, kept out of `non_sessions` because that key states a fact and here nobody
  established one, and kept out of `total`, which stays the count that could be confirmed (#80).
- `session list`'s footer counts **entries** rather than sessions — `1 entry could not be read.`
  where it said `1 session could not be read.`. Every degraded row used to come from
  `list_session_slugs`, so the old word was true of all of them; it is not true of an entry nobody
  could examine, and the footer is the last line a reader takes away. It is also the word `doctor`
  uses for the same entry, so the two surfaces stop describing one thing two ways (#80).
- No traceback on **the three paths above** — `session list`, `doctor` and the web home page, which
  reaches this through the same `list_entries`. A `PermissionError` under someone's workspace is an
  ordinary condition, not a bug in Requivo, and Requivo does not change permissions in a workspace it
  reads. Stated as three paths and not as *any path*, because it is not yet true of any path:
  `session_exists` carries the identical unguarded probe, so `session verify <slug>` and
  `session show <slug>` on such an entry still raise. That is filed separately rather than ridden in
  here — the verdict class and exit code for a session `verify` cannot examine is a design decision,
  and `session_exists` has 17 call sites including write-path guards where answering `False` on
  EACCES would be a worse bug than this one (#80).
- Compatibility: compatible - every observable change is confined to a case that previously produced
  an unhandled traceback. The exit-code policy makes moving a condition from one code to another
  breaking, and 1 → 4 looks like exactly that; it is not, because 1 is documented as `RequivoError`
  and this condition never raised one. There was no working consumer to break: stdout was empty and
  the payload did not exist. `doctor --json`'s `sessions.unexaminable` is additive; `sessions.readable`
  and `total` do change value here, from `false`/`null` to `true`/`N`, and that is a correction rather
  than a repurposing — the old pair asserted a failure of the whole root that the root had not had.
  `session list --json` row keys are untouched; what widens is that a `readable: false` row can now
  name an entry not known to be a session, so `slug` on a degraded row is still not a name to pass to
  another verb. `SessionRepository` gains a required `list_unexaminable`, and `scan_session_root`
  returns a 3-tuple: both fall under *Python internals* in `docs/compatibility.md`, which are
  explicitly not stable, and are named here because that is not the same as nobody noticing (#80).

- `requivo doctor` escapes the name of an inconsistent session before printing it (#98). A directory
  whose name carried a newline and held a `session.json` wrote two further lines of `doctor`'s own
  report at column 0, indented like real rows — the same forgery #40 closed on the card-name half of
  this verb, on the one bucket that had no guard. `_print_non_sessions` and `_print_unexaminable`
  already escaped; the *sessions* bucket did not.
- Reachability was not assumed: the pre-1.0 release audit reasoned it from four code locations and
  said it had not executed it, and the repro is what settled it. A clean session name still renders
  byte-for-byte.
- `doctor --json` was never affected — `json.dumps` escapes a control character before it can reach a
  line of its own.
- Compatibility: compatible - the only output that changes is a name that could previously forge a
  line, which no honest session has.

- `docs/compatibility.md` no longer contradicts itself in three places, all found by the round-2
  release audit and all introduced or carried by this release cycle (#105).
- The bullet on the two provenance refusals said the unreadable arm *"kept `invalid_session`"* and
  then, six lines later, that a test asserts the two codes differ. It carries
  `unreadable_source_revision` since #82; the sibling page `docs/session-format.md` was corrected in
  the same delta and this one was not.
- The `#82` section's arithmetic did not close: *"six of the nine conditions change status"* beside
  *"four conditions keep 400"* sums to ten. Both sentences were true — one counts what #82 did on the
  family as it stood then, the other counts the family as it stands now with #101's tenth arm. The
  split is now visible in the prose rather than only in a commit message, because a reader adding two
  numbers three sentences apart cannot see which tense each is in.
- The `#101` change note was filed under *The other public surfaces*, among stability verdicts, which
  made that section's count read as stale and made it appear to hold a surface with no verdict in a
  section that says each gets one. Moved beside `#82`, its sibling, under the `--json` section. The
  rule that decides which section a subsection belongs in is now written down, since it had already
  been got wrong once.
- `test_every_refusal_on_the_import_path_names_what_it_is_about` asserted six of the seven codes its
  own docstring named as the pin for a table. The seventh, `import_move_failed`, is now driven — by
  patching the one destination under test rather than by arranging a filesystem that refuses a
  rename, because the conditions for that differ per platform and a fixture would test the platform
  on some legs and nothing on others.
- Compatibility: compatible - documentation and a test. Nothing in the product changed.

## [0.11.0] - 2026-08-20

### Added

- Contributing guide now explains the tracked `.claude/` directory, and `tests/test_agent_layer.py` guards what makes it harmless (#2). Issue #2 reported that the `mode: block` jit-context rule over `Read`/`Edit`/`Write`/`Glob`/`Grep` blocks every file operation for a contributor without the maintainer's plugins. Measured, it does not: a jit-context rule is data, the only thing that reads it is a `PreToolUse` hook shipped inside the `claude-jit-context` plugin, and this repository registers no hooks. The barrier existed in the reading of the directory, not in its behaviour — so the fix is the explanation plus a guard that fails if a tracked hook, or a committed hook script, ever makes the barrier real.
- `.gitignore` now excludes `.claude/settings.local.json` (#2). It was never excluded, and only looked excluded on the maintainer's machine, which carries a global ignore entry no contributor has. That file is where a personal hook would be written, and a hook committed by accident would run for everyone who clones — the barrier above, made real.

- CI now runs the test suite on macOS and Windows as well as Linux. Every job in every workflow was
  `ubuntu-latest` and the only matrix axis anywhere was `python-version`, so no leg had ever executed
  this code on the platform most of its users install it on (#3).
- The shape is 9 legs rather than 15, and deliberately so: all five Pythons on Linux, because that is
  where a language-level difference between 3.9 and 3.13 shows, plus the ends of the supported range
  on macOS and Windows, because a path separator, a console codepage or a rename-over-existing does
  not care which minor version it meets (#3).
- The platform legs are a separate `test-platforms` job rather than an `os` axis on the existing one,
  for a reason worth knowing before anyone tidies it: `main`'s branch protection requires the five
  `Test (py3.N)` checks by their exact names, and adding an axis renames all five, so none of the
  required checks would ever report again and no pull request could merge. The four new checks are
  not required yet — the one command that adds them is in a comment at the top of the job (#3).
- README now states which platforms are supported and which are tested, because from outside an
  untested platform and a supported one look identical (#3).

- The first run of the new matrix found three things on Windows that no existing leg could have —
  two product defects and one test-harness bug, listed next. That is the leg paying for itself on
  day one (#3).
- **A concurrent session creation could be rejected as an invalid slug.** `canonical_dir()` checked
  containment by comparing two independently resolved paths, so its verdict depended on what the
  filesystem happened to look like between the two calls — create a directory in that window and they
  disagree. `requivo discover` then failed with *invalid session slug* about a slug that was perfectly
  valid, because something else was creating a session at that moment. Four of twelve concurrent
  creators died this way on the Windows leg. The containment check now resolves only a path that is
  actually there, which is the only case that can fail it (#3).
- The same shape turned up twice more when the class was swept rather than the instance: in the
  artifact path builder, and in `session verify`'s own artifact check, where it would report
  *unsafe artifact filename* about a perfectly ordinary name — the verb whose job is answering
  whether a session is intact, accusing you again. All three are fixed together (#3).
- **And a fourth instance of it, in those same three places, for a different reason.** The check
  decided containment with `Path.resolve()`, which on Windows under Python 3.9 — and only there —
  cannot follow a symlink whose target does not exist, and hands back the link's own location
  instead. A dangling symlink therefore read as living inside the session root however far out it
  pointed: `discover` would accept it as a session directory, and `session verify` reported it as a
  *missing* file rather than an unsafe one, which is the wrong answer from the verb whose job is
  telling you whether a session is intact. All three sites now share one containment function. It
  resolves with `os.path.realpath`, which does read the link itself — and, so that the guarantee does
  not rest on a platform being able to look at all, it refuses any symlink whose resolution comes
  back equal to the link's own location, because that equality is the resolver saying it could not
  follow. Pinned by a test that gives every platform the 3.9 resolver's semantics, rather than by the
  one leg in thirteen that has them natively — and that leg is not a required check, so it was never
  the gate it looked like (#3, #11).
- One more 3.9-only hole closed on the way past, found while checking what else the two resolvers
  disagree about: a **symlink loop** under a session made `Path.resolve()` raise `RuntimeError`,
  which nothing on the path caught, so `session verify` died with a traceback instead of reporting a
  problem. `os.path.realpath` collapses it and returns an answer the check can act on, and the loop
  is then refused like any other path that does not resolve inside the session (#3, #11).
- The refusal messages now say *does not resolve to a path inside …* rather than *resolves outside*.
  There are two ways to fail that check — it points somewhere else, or the platform could not tell us
  where it points — and the old wording asserted the first about both (#3, #11).
- **A write could be lost to an antivirus scanner.** On Windows a rename over an existing file fails
  with *Access is denied* whenever anything holds a handle to the destination — a scanner or the
  Search Indexer, opening the file microseconds after it is written, neither of which Requivo can
  serialise against. `model.json` is the durable product, so it now retries that specific failure a
  few times over a few hundred milliseconds at most. A genuinely unwritable destination still fails at once
  (#3).
- **And one harness bug, fixed as a harness bug rather than as a product one**: the test comparing the
  bundled demo payload with the browsable copy read both files with the platform's codec, so it failed
  on Windows about files the product itself reads correctly. Every read in the suite now names its
  codec, and the guard added for #11 was extended to `tests/` to catch the next one — it had walked
  `src/` and `scripts/` only, so the one directory it did not walk is exactly where the next instance
  turned up (#3, #11).
- One harness bug found and fixed on the way in, before the new legs could report it as a product
  bug: a test fixture wrote a context card containing an em dash with the locale's codec and read it
  back through a product that decodes UTF-8, which disagree on Windows. A narrow guard now catches
  that class — a test writing non-ASCII without naming a codec — because it is the trap this issue
  warned about, a harness rendering an environment limit as a product verdict, and "add more tests
  for that platform" is the wrong lever on it (#3).

### Changed

- Requivo Web's cross-site guard raises **six codes instead of one**. `cross_site_request` carried six
  distinct facts whose `details` payloads had five different shapes between them, against the rule
  `docs/compatibility.md` states in this repository for exactly this reason: a code carries one fact
  and one `details` shape. A consumer matching the code and reading `details["origin"]` got a
  `KeyError` from a payload that correctly carried the code it matched (#52).
- The arms are `undetermined_host`, `host_not_allowed`, `cross_site_fetch`, `opaque_origin`,
  `origin_mismatch` and `missing_request_token`. Each carries one fact and one shape, and the table is
  in `docs/compatibility.md` (#52).
- Compatibility: breaking for anyone matching the string `cross_site_request` on the Web surface —
  nothing raises it any more. It survives as the family base and keeps its 403 status row, and all six
  arms remain `CrossSiteRequestError` subclasses, so catching the class or matching on the 403 is
  unaffected. Only a match on that exact code string needs changing, to the six above.
- **The decision, since the issue asked for one and either answer was defensible.** The alternative was
  an argued exception in the policy for a surface that does not serialize `details` — which is true:
  Requivo Web renders a refusal as HTML, so no consumer could observe the inconsistency today. What
  decided it against the exception is that the cost was already being paid rather than deferred: both
  #43 and #45 had to distinguish their new arm **by message**, and the same policy says never to match
  on the message. The only handle a caller had for the distinction was the one it is told not to use.
  `empty_selector_token` was split for the identical shape one release earlier (#52).
- Read against #57, which asks the same question about `unstated_source_revision` and notes the two may
  want one answer: they do not, and the difference is the point. That code carries **one** fact with a
  `details` shape byte-identical to its sibling raise — the policy is satisfied and the wish there is
  for a more precise *type*. This one violated the policy. #57 is untouched here (#52).
- `opaque_origin` and `origin_mismatch` deliberately share a `details` shape and are still two codes: a
  shared shape is not a shared meaning (#52).

- Requivo Web has a deliberate visual direction. Colour now encodes **state and nothing else** —
  emerald for what is known, amber for what is being assumed, slate for what is open — and the
  primary action carries a blue the triad never uses, so an action can no longer be mistaken for a
  grade. The previous stylesheet spent one indigo accent on buttons, links, focus, the coverage bar
  and the "what changed" panel at once, which left the one distinction this interface exists to make
  as the one its colour system said least about (#64).
- Evidence grade is legible without colour. Each of the three states now has its own mark **shape** in
  the per-topic list — a filled square, a rotated diamond, a hollow circle — in addition to its hue.
  Three coloured dots of identical shape were one state on a monochrome print and to a reader with a
  colour vision deficiency (#64).
- Every foreground/background pair is measured against WCAG AA in both light and dark, and UI
  boundaries against the 3:1 required of a control. Three values were wrong and are corrected: the
  token carrying every label, hint and section caption sat at 3.80:1 on the page, links at 4.48:1, and
  an input border at 1.52:1 against its own field — with a white field on a tinted page, that border
  is the only thing saying where the field is. Control edges now have their own token, held apart from
  the decorative rules they were sharing (#64).
- A session screen states where it is before it is scrolled: readiness, the count of open questions and
  whether a saved document needs updating now sit beside the title. Every value there is already
  computed and already stated in full further down — it is a summary of the page, never a second
  source for it (#64).
- The keyboard focus ring is no longer suppressed on form fields. `outline: none` on `:focus`
  out-specified the global `:focus-visible` rule, which removed the indicator from every input,
  textarea and select on the surface (#64).
- Two rules that had been silently dead since before this change: `ul.clean > li` was declared after
  `.session-list > li` and `ul.tight > li` at equal specificity and overrode both, so neither list ever
  got the spacing it declared. Scoped rather than reordered, so a later addition cannot re-break them
  (#64).
- Nothing persisted, generated or emitted by `--json` changes, and no user-facing term changes:
  `web/viewmodels/labels.py` remains the single definition of what a reader calls things (#64).

### Fixed

- `requivo artifact save` without `--revision` is now refused instead of being recorded as fresh.
  Omitting it used to mean *the session's current revision*, so freshness was computed against a
  revision nobody had claimed to read and the answer was `stale: false` every time — a source
  revision that *is* the current one cannot have moved. The number recorded was a real revision of a
  real session, so no reader downstream could tell the guess from a stated fact: `artifact list`,
  `session show`, `status --json` and the Web's *needs updating* panel all reported a superseded
  document current. Invariant 2 states the prohibition in those words — "never record `stale=False`
  because the caller didn't say otherwise" — and the default was the one thing violating it (#6).
- The refusal names what to pass and the revisions that exist, so the remedy is one flag. It is
  raised before anything is written, so a refused save leaves neither a file under `artifacts/` nor a
  status row in `session.json`.
- Compatibility: breaking - an `artifact save` that omitted `--revision` used to succeed and now
  exits 1 with a structured `unstated_source_revision` envelope (the refusal shipped here under
  `invalid_session`; #57 gave it its own code before either reached a release). Every documented
  invocation already passes it (`plugins/claude-code/REASONING.md`, the `prd` and `brief` skills,
  `docs/cli.md`), and both provider-backed paths in `DiscoveryService` always did, so nothing that
  follows the documentation changes behaviour. Only a caller relying on the undocumented default is
  affected, and that caller was being given a fabricated provenance. No session on disk changes shape
  and `format_version` stays 1.
- An artifact saved against a revision whose file is *present but unreadable* is refused cleanly
  rather than crashing. The guard that turns "I cannot establish freshness" into a refusal caught
  `RequivoError` only, which covers a *missing* revision file and nothing else — a truncated
  `revisions/NNNN-model.json` from an interrupted sync raised pydantic's `ValidationError`, a
  mis-encoded one raised `UnicodeDecodeError`, and a permissions or device fault raised `OSError`.
  None is a `RequivoError`, so the block never ran and a raw traceback came out of the service from
  inside the session lock, past the CLI's own handler. All three are caught now, and the failure's
  type and text are recorded in the error's `details.cause` (#6).
- Both refusals carry the same five `details` keys — `slug`, `type`, `source_revision`,
  `current_revision`, `cause`. They shared a code when this was written, which made the shape
  obligatory; #57 split the codes and the shape was kept anyway, because a key present on one payload
  and absent on the other is what a consumer following the documented advice (match the code, read
  the key) trips over. A test asserts the two key sets against each other rather than each on its own.

- One unreadable session no longer takes the whole listing down in Requivo Web. Invariant 15 — *a
  listing survives its own members* — was enforced one line below where it broke: `session_list`
  guarded `status()` per row, but the rows themselves came from a single-shot comprehension over
  `read_meta`, so a `session.json` this build cannot read raised before any row existed to degrade.
  The source of the rows is now guarded per member (#7).
- Two further ways the same page went down, both outside the old guard. `request_text` was outside the
  `try` entirely; and the `try` named `SessionNotFoundError` alone, so a `model.json` left truncated by
  a crash mid-write raised a pydantic `ValidationError` — not a `RequivoError`, so it missed that catch
  *and* the app's `RequivoError` handler and rendered as a 500 over the whole page. Measured per break
  mode against the unfixed code: a newer `format_version` gave 400, a truncated model 500, an
  unreadable `request.md` 500 — each on a page whose other sessions were all fine (#7).
- **The degraded row names the session.** Neither surface did before, so a user with one bad session
  could see that something was wrong and had no way to learn which — which is most of the cost. The row
  carries the underlying error text too, because *this session was written by a newer Requivo, upgrade*
  is a remedy and a flattened `unreadable` code is not (#7).
- The row states no fact it does not have: no timestamp, no question count, no freshness verdict. A
  plausible `0 open questions` on a session nobody managed to open is the quiet-wrong-answer form of
  the same bug. *Could not be read* and *not analysed yet* are two states and render differently (#7).
- The guard catches bare `Exception`, deliberately and with the reason recorded next to it. An
  aggregate's contract is that one member cannot take the view down, and the set of ways a member can
  be broken is open — naming a family is how a guard ends up nominally on and effectively off for the
  next failure mode, which is what #7 is. `doctor`'s `_session_health` had already made this call for
  the same question; this adopts it rather than re-litigating it (#7).
- Still outstanding, and reported rather than fixed: **`requivo session list` has the same duty and
  still has no guard.** It lives in `deterministic.py`, which was held by another change in the same
  round. The fix is one call — `SessionService.list_entries()` in place of `list_sessions()`, plus a
  degraded line naming the slug — and the service half it needs has shipped here (#7).

- Prompt caching no longer costs money on the verbs that could never benefit from it. The system
  prompt was sent with a `cache_control: ephemeral` breakpoint on every call, and a breakpoint bills
  the block at 1.25x input to write against 0.1x to read — so it only pays from the *second* send of a
  byte-identical prefix. `prd`, `criteria`, `epic`, `release`, `stories` and `estimate` each make one
  call, so each wrote a cache entry that nothing ever read: a flat ~25% surcharge on the largest part
  of the input, on every one of them (#9).
- The comment that justified it claimed the prompt was byte-identical "across the calls of a session".
  That is true across the calls of one *operation* — a golden capture's K runs, `converse()`'s turns,
  each JSON retry — and false across operations, because `build_prompt()` substitutes the shared
  schema and context cards into a **per-operation** template. Nothing failed and nothing warned; the
  rendered cost was correct throughout and simply read as normal, which is why it survived (#9).
- Moving the breakpoint or spending more of the API's four on it would not have helped, and this is
  worth writing down so it is not re-attempted: all eight templates place `{{SCHEMA}}`/`{{CONTEXT}}`
  near their end with an *Output format* section after them, so the shared bulk is a **suffix**.
  Caching is a prefix match, and a suffix has no prefix boundary to cache at (#9).
- Which calls get a breakpoint is now the caller's declaration (`reuse_system`) rather than a constant,
  because the same function is single-call in one caller and multi-call in another: `requivo brief`
  calls `advise()` once, and `scripts/golden_run.py --brief` calls it K times off one prompt. The
  harness passes `reuse_system=True` and keeps its saving; discovery keeps the breakpoint unconditionally,
  since `converse()` re-sends that prompt for up to 8 turns. `_complete` still defaults to caching, so
  a caller that has not thought about it pays the safe answer rather than silently losing a real cache (#9).
- The accepted cost, stated rather than glossed: a one-call verb *can* send twice, when the model
  returns malformed JSON and the retry loop re-sends the identical prompt. Those retries are no longer
  cached, so a generator that retries pays 2.0x the system block where it used to pay 1.35x. Not
  caching is the better bet while a retry is rarer than about one call in four, and it is; caching only
  from the second attempt was considered and rejected, because it costs 2.25x on two attempts — worse
  than 2.0x — and only wins past the same threshold at which caching everywhere would have been right
  to begin with (#9).
- No prompt asset changed, so no engine behaviour changed and no golden-harness cycle was spent: the
  system prompt sent is byte-for-byte what it was, and a test pins that. Making the cache pay *across*
  operations means moving the shared bulk to the front of all eight templates, which is a real change
  to what the model reads and is deliberately left for its own measured pull request (#9).

- Requivo now reads and writes text as UTF-8 everywhere, so a session written on one machine reads
  back byte-identically on another whatever the locale. 29 call sites — 28 reads, plus one write in
  the golden harness — took the platform default instead: UTF-8 on macOS and Linux, cp1252 on
  Windows. A French request round-tripped into mojibake that was still valid JSON, so nothing failed
  and the PRD shipped it (#11).
- `requivo session verify` no longer accuses you of editing a file nobody touched. It recomputed the
  hash from a mis-decoded string, so on Windows every session containing an accent or an em dash
  reported `revision_hash_mismatch` and `session import` refused a perfectly good archive on the same
  evidence (#11).
- `requivo discover`, `demo`, `schema` and `context` no longer die with a raw `UnicodeDecodeError`
  before doing anything. 20 of the bundled assets are not pure ASCII, so on an ASCII locale the
  primary verb could not start at all — observed, not reasoned:
  `LC_ALL=C LANG=C PYTHONUTF8=0 requivo schema` reproduced it on macOS (#11).
- Worth stating precisely, because the issue's own inventory had it the other way round: only 2 of
  those 20 are undecodable as cp1252. The other 18 decode *successfully* into mojibake, so on Windows
  the usual outcome was never a crash — it was a prompt quietly assembled from corrupted product
  context and shipped to the model, billed, looking like it had worked (#11).
- A file *you* name (`requivo discover ./brief.md`) is now refused by name if it is not UTF-8 —
  naming the offending byte and its position — instead of being decoded with the locale's codec into
  text that reads like prose and is wrong. Refusing is the point: mojibake validates (#11).
- A path or slug you supply can no longer forge a line of Requivo's own output. Six error messages
  in `deterministic.py` interpolated one raw — `no such file:`, `archive not found:`, the unreadable
  `.zip` message and three `no canonical session` messages — so a name carrying a newline and an ANSI
  escape could write what reads as a second, authoritative line at column 0. That is #40's class,
  found again by this branch's own guard test after the fix for #11 reintroduced it in a message it
  had just added. All six now go through `display_token`, the helper #40 produced, which is a no-op
  for any ordinary value (#11).
- `tests/test_encoding.py` is the guard that keeps it fixed. Passing `encoding=` at 29 call sites
  leaves the 30th written next week, and this repo has already watched that happen twice, so the
  check is a walk over `src/` and `scripts/` that fails on a bare `read_text()` — and refuses to
  answer at all when its scan set comes back empty (#11).

- A `model.json` written by a **newer** Requivo now loads, and keeps the field it added. Only
  `session.json` was forward-compatible; `model.json` and every `revisions/NNNN-model.json` were read
  through `EngineOutput`, which inherits `extra="forbid"` from the LLM boundary contract — so a key
  added by a later version (which `docs/compatibility.md` explicitly permits without a
  `format_version` bump) made the session unopenable, as a raw Pydantic `ValidationError` rather than
  as anything the surface could phrase. The documented promise and the code disagreed, and the
  document was the half that was right (#14).
- The two rules that collided are now two contracts. `StrictModel` and everything an LLM fills stays
  `extra="forbid"`: a field the model invented must still fail loudly and ride the JSON retry loop,
  because a dropped key makes a drifted prompt read as a clean success. The read path goes through
  `PersistedEngineOutput` instead — a subclass of `EngineOutput`, permissive at every level of the
  model tree, so nothing downstream changes type and every validator the strict tree carries still
  runs. What differs is what an unknown key is evidence *of*: from a provider something is wrong now
  and there is a retry that can fix it; from disk something is newer, there is no retry, and refusing
  costs a session that reads perfectly well (#14).
- `requivo doctor` and `requivo session verify` agree with the loader about the same file. They
  validated `model.json` through the strict contract too, so once the loader carried a newer version's
  field the checker would have reported `invalid_model` on a session that opens fine — a health
  verdict measured against a rule the code no longer follows. Both model checks are permissive now,
  and a model that is actually malformed still reports `invalid_model` / `invalid_revision_model` (#14).
- Reading permissively was only half of it, and the half on its own would have been worse than the
  bug. `ModelProposal.resolve` carries an unstated reasoning collection forward from the model being
  refined (invariant 10), so a decision loaded from a newer Requivo ended up under the *strict*
  tree's annotation — and pydantic serializes by the annotated type, so the unknown key stayed alive
  in memory and disappeared on the very next write. A key at the top level went the same way, since
  a proposal is `extra="forbid"` and cannot speak to a field it has never heard of. Either would
  have converted a refusal you could see into a silent loss on the first ordinary turn. Both are
  fixed in `resolve`, and a test drives a real refinement turn through `SessionService.update_model`
  rather than a re-save, because a re-save never reaches the code that dropped it (#14).
- The question cap is one number again. `ModelProposal` and its persisted mirror each carried a
  hand-written `max_length=6`, and nothing made them agree — a duplication introduced by the mirror
  itself, and the same defect class this change exists to remove, one field along. It fails
  asymmetrically and needs no version skew at all: raise the strict cap, miss the mirror, and a
  session *this build just wrote* with seven questions no longer loads. Both now read
  `MAX_QUESTIONS`, and a second field-graph guard compares the *constraints* of every field the
  mirror restates against the strict tree's, because the existing walk compares `extra` policy and
  cannot see this. The mirror cannot inherit its way out: pydantic drops the parent's `FieldInfo`
  when a subclass re-annotates, so leaving `Field(...)` off would lose the cap and the default and
  quietly make `questions` required — which is why the property is pinned rather than tidied (#14).
- Compatibility: compatible. Nothing that loaded before stops loading, no stored key changes meaning,
  and `format_version` stays 1 — this only widens what a reader accepts and what a writer keeps.
  Two limits are stated rather than left to be found: an apply **replaces** the slots, the summary
  and the questions, so an unknown key inside one of those is superseded by a value this version
  built; and an unknown **slot id** is still refused, because that is `schema_version`'s frontier and
  it already refuses a newer slot schema with a message naming the upgrade.

- Taking the session lock on a slug that has no session no longer creates one. `session_lock` called
  `mkdir(parents=True, exist_ok=True)` on the session directory before opening `.lock` inside it, so a
  lock taken on a name nothing had created left a directory behind holding only that lock file. It is
  invisible to `session list` (no `session.json`) and it is not empty, so `create_session`'s atomic
  rename — the one claim on a slug — lost to a session nobody had made, and reported **session already
  exists** about one neither the reader nor the tool could see or list (#22).
- Which callers reached the lock without a session, stated narrowly because it is narrower than it
  looks. `save_revision` and `save_session_artifact` in `requivo.core.persistence` take the lock
  before the metadata read that raises `session_not_found`, and so does `ArtifactService.mark_stale`,
  which has no preceding existence check. The CLI verbs are **not** among them: `model apply`,
  `artifact save` and `session export` each establish the session exists before the lock is taken.
  What was exposed is the layer underneath them — the one an external consumer calls directly (#22).
- What a leftover directory then did to the CLI is quieter than a refusal, and worth knowing if you
  have one on disk from a previous version. `requivo session init --slug later` does not report the
  clash: `SessionService.create_session` falls back to a hash-suffixed candidate when a slug is taken,
  so it silently creates `later-<hash>` instead. The name you asked for is simply gone, with nothing
  said. The `session_exists` refusal is what a direct `create_session` and `session migrate` see (#22).
- **A guard that creates the thing it guards is a second producer.** Creating a session is one atomic
  claim on its slug — a staging directory renamed into place, which either wins the name or reports
  it taken — and that claim is only decidable while nothing else can make a directory of the same
  name. The lock now refuses a slug with no session rather than materialising one, so a failed or
  released lock leaves the store exactly as it found it (#22).
- Deleting the directory again on the way out was the other repair and is worse: unlinking a `.lock`
  another process is holding is legal on POSIX and silently breaks mutual exclusion, leaving the
  waiter holding a lock on an inode with no name. That trades a misreported refusal for a corrupted
  one, which is the wrong direction (#22).
- `requivo session migrate` was the case where this stopped being cosmetic. It claims its slug through
  the same rename, and the bulk sweep reports a refusal as `skipped_already_present` — so a legacy
  session that had never been migrated was reported as one that was already there. A skip reads as a
  decision, which is worse than an error (#22).
- Compatibility: compatible in what a caller is told, and one step earlier in when. Every path that
  locks before reading the metadata still raises `session_not_found` with the same message and the
  same HTTP status; it is now raised by the lock rather than by the `read_meta` immediately inside it.
  The one message that changes is `ArtifactService.mark_stale` on a session that does not exist, which
  said *has no model yet* and now says there is no such session — the accurate of the two. A session
  that exists is untouched: the lock is still taken, still re-entrant within a thread, still exclusive
  across processes, and `.lock` still lives inside the session it locks (#22).

- A character your console cannot display no longer kills the command that was printing it. Requivo
  configures stdout and stderr once at startup so an unrepresentable glyph is escaped rather than
  raising `UnicodeEncodeError` — and escaped rather than dropped, because a reader cannot tell a
  substituted character from one that was never there (#29).
- The ordering is what made this worth fixing rather than a cosmetic complaint: the crash happened at
  the `print`, *after* the work that print was reporting had already landed. `requivo brief <slug>`
  completed its paid provider call, applied the revision and wrote the artifact, then died in the
  renderer — so the exit code described a crash, and re-running paid for a second call and stacked a
  second revision on the first (#29).
- `requivo doctor` was the worst of them: it died on the check mark of its very first line, having
  already computed the whole diagnosis it exists to report. It now also *reports* your console's
  encoding, with `lossy`, `will-crash` and `unknown` as distinct answers from `safe`, so a stream
  Requivo could not configure is a line you can read rather than an absence (#29).
- `lossy` is a separate verdict from `safe` on purpose. A console set to `errors=replace` or `ignore`
  cannot crash, but it drops the character with no mark — and reporting that as safe would have
  `doctor` endorse the exact quiet hole this fix exists to avoid (#29).
- The API usage line no longer kills a run that already paid for its call. `render_usage` prints a
  middle dot and an em dash, and two of its three call sites sat outside the guard — including the
  one that runs after a *wholly successful* command — so on an unreachable stream a successful
  `requivo brief` still died there, after the provider call was billed and the revision applied.
  It now degrades to a stated absence, which is deliberately not silence: a usage line nobody can
  read is a different thing from a run that made no calls (#29).
- The message printed in that case reads the run's usage ledger instead of asserting. It says a call
  **has** been billed only when one actually was — several verbs never call the provider at all, and
  telling you not to re-run a command that cost nothing would be the same misreport one layer up
  (#29).
- Where a stream cannot be made safe at all, the command exits **3** — a new code meaning *the work
  succeeded and you cannot see the output* — instead of a traceback. Exit 1 would have been a lie in
  the one case that costs money (#29).
- Exercised on every CI platform rather than only where the bug bites: the tests spawn subprocesses
  under `PYTHONIOENCODING=ascii`, which reaches a real console encoder. The previous suite captured to
  `io.StringIO` and so could never have caught this even with a Windows leg (#29).

- A refused submission in Requivo Web no longer costs you what you typed. Refusing an over-long
  request is correct and is unchanged — half a request folded into the model reads exactly like a whole
  one — but the refusal was a full-page error whose only affordance was *Back to sessions*, so a
  26,000-character client email that arrived through the clipboard had to be fetched again from
  wherever it came from. Every refusal on the request form now re-renders the page with the submission
  still in it (#30).
- The answers box was the worse of the two, and for a reason the issue names: it posts as an HTMX swap
  over `#session-body`, the region that *contains* the textarea, so the error fragment did not merely
  fail to preserve the text — it deleted the field the text was typed into, with no Back to return to.
  The whole region now comes back with the answers still in it and the refusal stated on the form
  (#30).
- Four refusals on that page round-trip, not one: the request textarea, both of the session-name
  field's refusals (too long, and not a usable slug), and the answers textarea. Leaving one of a single
  field's two refusals keeping your work and the other throwing it away is a worse state than either,
  because which one you hit is not something you can predict (#30).
- The context-card selection and the *On submit* choice survive a refusal too. A session's identity is
  its request **and** its card selection — the impact estimates are read against them — so handing back
  the textarea while silently clearing the checkboxes would return a form that no longer says what you
  told it (#30).
- Compatibility: compatible. These refusals keep their HTTP status (413 for a length, 400 for an
  unusable name) and their error code, which now rides the banner on the re-rendered form instead of a
  full error page. An unknown context card is deliberately **not** in this set and still raises: those
  boxes are checkboxes over a set the page itself rendered, so an unknown value did not come from a
  reader mistyping something they could correct on a re-render (#30).
- Narrowed from the issue as filed, on the issue's own second comment: the refusal already named the
  limit, and adding the submitted length was judged not to be what this issue is for. What was lost was
  the text, and that is what this restores (#30).
- **Follow-up on the above.** A refused submission no longer fills in a session name the reader never
  typed. `create_session` used one variable for two meanings — the string the reader put in the box,
  and the argument the service takes, where `None` means *derive a slug from the request*. An empty box
  collapsed to `None` before the empty-request arm was reached, and the re-render stringified it, so
  the reader got `value="None"` in a field they had not touched. It also fails that field's own
  `pattern`, so it had to be noticed and cleared before the form could be resubmitted: the refusal path
  built to stop costing the reader work had started adding some. The two meanings now have two names
  (#30).
- Found by review rather than by the tests, and the gap is worth naming: every case covering the
  preserved-input path submitted a session name, so none of them could see a refusal that invents one.
  The regression test is a matrix over each refusal paired with a **blank** name field (#30).

- The two places that still built an artifact path by hand now go through the chokepoint the rest of
  the store goes through (#36). `requivo artifact save` and the line every generator verb prints to say
  where its document went each re-joined `canonical_dir(slug) / "artifacts" / <recorded filename>`
  inline, so the guard #5 put on the writes and #23 extended to the read was closed in three places and
  open in two.
- Neither of the two was exploitable, and that is stated rather than assumed: both only *print* the
  path, and in both the filename reached them from the fixed `ARTIFACT_FILENAMES` table by way of a call
  that had already validated it. What is fixed is the inconsistency — the next person reading
  `deterministic.py` learned the wrong pattern from a file that is otherwise correct (#36).
- Display-only is not the same as harmless, and the reasoning now lives at `artifact_path()` rather
  than being rediscovered a fourth time. A read traversal answers what this code may *disclose* rather
  than what it may create, and a printed path is the plainest disclosure there is; the filename on both
  lines is a plain string off `session.json` that nothing re-validates when it is read back (#36).
- Which door is open is now stated rather than borrowed, because the obvious sentence is wrong.
  Invariant 14 argues that a persisted value is untrusted every time it is read, and it argues it about
  the context cards, which `session import` deliberately cannot resolve. That does not carry over to the
  artifact filename: import pins each one to its known value and to containment and refuses the whole
  archive otherwise — reproduced, for a traversal and for a merely wrong name. The route that is open is
  invariant 14's own, a consumer holding the services over a store that is not this one, which is what
  the tests drive (#36).
- Absence and refusal stay the two different answers #23 made them. A session with nothing generated is
  not newly an error — a name that is not a filename raises `invalid_filename`, and every legitimate one
  prints exactly the path it printed before (#36).
- Compatibility: compatible. No output changed for any name the store can actually write, and both
  lines are covered by a test that pins the legitimate path as well as the refusal — a guard that
  refused everything would satisfy the refusal half on its own (#36).

- Creating a session on an install with **no context cards at all** now says so, instead of blaming
  the name you typed. `resolve_cards` — the validator the CLI, the deterministic verbs, Requivo Web
  and `SessionService.create_session` all run on the way in — was the one card selector the
  empty-install guard was never wired into, so with nothing installed it answered *unknown context
  card(s): pricing. Available:* (an empty list) while the very next call answered *no context cards
  are installed … this install is incomplete*. One condition, two verdicts, and the one sending you
  to check spelling you had got right arrived first — at session creation, which is the first thing
  a fresh install does (#41).
- The three selectors that resolve a card name against the installed vocabulary — `resolve_cards`,
  `load_context` and `check_selection` — now share one guarded read of the card table rather than
  each remembering to call the guard. The original miss had a mechanism: the other two reach the
  table directly and this one reached it through `available_cards()`, so a sweep over the callers of
  the guarded function found two of three and looked complete. `available_cards()` stays deliberately
  outside the guard, because `doctor` reports an empty install as one of its three states and can
  only do that by observing the emptiness rather than raising on it (#41).
- Compatibility: breaking - one condition moves to a different code and across the 4xx/5xx line in
  Requivo Web. `POST /sessions`
  naming a card on a card-less install used to answer `400 unknown_context_card` and now answers
  `500 no_context_cards`. That is the correct side: nothing the caller sent caused an install to
  ship without cards, and the old 400 was the misattribution #34 fixed, one call earlier. Nothing
  changes on an install that has cards, where an unknown name is still `unknown_context_card` and a
  400; and passing no selection at all is untouched, since that is not a selection to validate.
- The unknown-card refusal now lists the available cards from the same read its lookup used. The
  vocabulary you were told to choose from was enumerated by a second `available_cards()` call, so it
  was not guaranteed to be the one your name had just been matched against (#41).

- Requivo Web accepts a form posted from `localhost` to `127.0.0.1` (and every other pairing of the
  three loopback spellings). The cross-site guard compared the two hostnames as strings while its own
  host allowlist treated them as one machine, so the request form returned *this request came from
  another origin* and could not be submitted at all — and switching the address bar to the other
  spelling resubmitted the stale `Origin` and reproduced the identical error, leaving no way forward
  (#43).
- A host you listed in `REQUIVO_WEB_ALLOWED_HOSTS` is deliberately **not** part of that equivalence:
  two real hostnames there must still match each other exactly, because whether they are one trust
  domain is the operator's call rather than something inferred from one comma-separated list.
- Two hostnames that could not be determined at all no longer read as a match. `""` is what the
  parser returns for an absent or unparseable `Host`, or for an origin such as `http:///` that names
  nobody, and two of those compared equal — so the one input where neither side was known produced the
  same verdict as a verified match. No browser can produce it and the request token gated it either
  way, but a check that could not look must say so rather than answer. Found by the audit on the #43
  fix, in the helper that fix introduces.
- `Origin: null` stays refused, now on purpose and with the reason in the code rather than as a
  side effect of parsing the literal string `"null"` into a hostname. An absent origin header stays
  accepted — a browser attaches `Origin` to every POST, so silence means a scripted client, which the
  request token gates. That token remains the load-bearing check for both, and `evil.example` posting
  to a loopback host is still refused (#43).

- Requivo Web refuses a request that does not say which host it was addressed to, instead of skipping
  its host check for that request. The check read `if host and host not in allowed_hosts()`, and the
  parser returns an empty string when it cannot determine a host at all — so an absent or empty `Host`
  header was treated as *no host check needed* rather than as a refusal. That check is the
  DNS-rebinding guard and the only one that also runs on reads, so it was nominally on, effectively
  off, and silent about it. Observed at the socket, not reasoned: `GET / HTTP/1.0` with no `Host`, and
  `GET / HTTP/1.1` with an empty one, both answered 200 (#45).
- Compatibility: breaking - an HTTP/1.0 client that sends no `Host` header now gets a 403 where it
  previously got a response. This is deliberate. HTTP/1.1 requires a `Host`, every browser and every ordinary client
  (`curl`, httpx, requests) sends one, and nothing in Requivo has ever documented HTTP/1.0 support.
  The browser path is unaffected (#45).
- The refusal names its own arm — *this request did not state which host it was addressed to* — rather
  than reusing the wording of a genuine host mismatch. A guard that could not read its input must not
  print what a guard that read it and refused prints; the same correction the opaque-origin arm got in
  #43, one seam over (#45).

- The origin check's stated rationale in `web/security.py` was false, and is corrected. It claimed a
  page at `http://localhost:8765` *"can only have been served by this process — nothing else is
  listening there"*, while the code it justifies discards the port on both sides: the set actually
  accepted is any page served by any process on any loopback port, which on a developer machine is a
  populated one. No behaviour changed — this is a prose fix, and the port-blindness it describes
  predates the loopback-spelling fix in #43 rather than following from it (#46).
- That port-blindness is now written down as a decision rather than left implicit, with its reason:
  the per-process request token is what gates a write, and a page on another loopback port cannot
  obtain one, because the browser's own same-origin policy counts the port and Requivo Web sends no
  CORS headers. Comparing ports in this check would add nothing and would reintroduce exactly the
  false positive #43 fixed, since a default port is elided in an `Origin` but spelled out in a `Host`.
  A test pins the behaviour, so tightening it later has to argue with the rationale instead of
  slipping past it (#46).

- Requivo Web's `Referrer-Policy` is `same-origin` rather than `no-referrer`, which is what made the
  product's entry path unusable in a browser. Under `no-referrer` a browser attaches `Origin: null` —
  the opaque origin — to an ordinary form post, and the cross-site guard refuses that deliberately
  (#43), so creating a session and running discovery answered 403 to a same-origin request carrying a
  valid request token. Both halves were individually correct and individually green; the defect existed
  only in their composition, which is why no per-file test could see it and none did (#47).
- Scope of the failure, narrowed from the report: only the two **plain** form posts were affected —
  *create a session* on the home page and *Analyse request* on a pending session. The HTMX posts
  (answers, generation) travel as XHR, which is CORS-mode, and Fetch consults the referrer policy for
  the `Origin` header only on requests that are *not* CORS-mode. So what broke was precisely the way
  into the product, while everything downstream of it kept working — which is why it read as one broken
  form rather than a broken app (#47).
- Why `same-origin` and not the alternatives, since this replaces a privacy decision with another one:
  it is the strictest value that still leaves the guard an origin to read. Cross-origin destinations
  get nothing at all, exactly as under `no-referrer`, so the whole privacy intent is kept for the only
  case where it ever did anything — navigating away. `strict-origin-when-cross-origin`, the browser
  default, also fixes the bug and hands a third party `http://localhost:8765` on an outbound
  navigation; for a local tool that is a gratuitous disclosure buying nothing. Dropping the header
  entirely would defer to whatever the browser defaults to, and this app states its headers. The cost
  of `same-origin` is that a same-origin `Referer` now carries the full URL, session slug included —
  the reader's own request name, travelling to the server that already holds it (#47).
- Worth recording because it inverts the intuition: a `Referrer-Policy` governs the requests *this
  app's own pages* make. A request some other page sends here is governed by that page's policy, not
  by ours. So this header was never part of the cross-site guard's defence — it could only ever
  constrain us, and it did (#47).
- The guard's refusal of `null` is unchanged and is still the right call. Accepting the opaque origin
  would have made this defect invisible rather than fixing it: the header was the thing that was wrong,
  and a guard loosened to tolerate it would have swallowed the evidence (#43, #47).
- A test now asserts the **composition** rather than either half — the policy the app really emits, fed
  through the Fetch rule for a same-origin form post, and then through the real guard. It carries its
  own limits in writing: neither `TestClient` nor `curl` implements a referrer policy, so the browser's
  half is modelled from the specification rather than executed. An end-to-end check in a real browser
  engine remains the missing coverage, and is the reason this shipped (#47).

- A session that has converged can still be refined. The answer form used to live inside the
  `{% if s.questions %}` arm of the session template, so when the engine returned no question — the
  state the home page presents as *Ready for a first decision brief* — the form disappeared along with
  the question list, and there was no way left to send the model a correction, a constraint that
  arrived late, or scope the client added afterwards. The form is now unconditional and the question
  list is what varies (#49).
- Nothing downstream ever required a question: `DiscoveryService.answer()` takes free text and folds it
  in through the same validated path as any other turn, and has never read the question list. The
  coupling was entirely presentational, which is what made it easy to ship and hard to notice (#49).
- With no questions the section reframes to *Anything to add?* and keeps the notice, so the reader is
  told the engine has nothing further to ask **and** given the box. The engine converging is an answer
  about which questions are worth asking; it is never an answer about what the reader still has to say,
  and collapsing the two presented the product's success state as the end of the conversation (#49).

- One provider call at a time in Requivo Web, enforced page-wide. Every generator under *More
  documents* is its own form posting to the same `#artifacts-region`, and all of them stayed clickable
  while a generation was already running, so a reader could start five. Each is a paid call; at most one
  result ever reached the page, and which one was not the reader's choice. While any request is in
  flight every submit button on the page is now muted, and the count is a counter rather than a flag, so
  the first response finishing does not hand back live buttons while a second call is still running
  (#50).
- The previous behaviour disabled only the submitting form's own button, which left every sibling live.
  That is now reproduced mechanically rather than described: a small DOM harness executes the real
  `static/js/app.js` and the shipped asset fails it with `disabled=[True, False, False]` — the clicked
  button muted, its two siblings ready to buy another call (#50).
- The busy state is re-asserted after each swap, because markup HTMX swaps in carries no `disabled`
  attribute of its own. A bfcache restore now clears the count too; previously a page returned to with
  Back kept a button disabled permanently, which the original report did not mention and the harness
  found (#50).
- **Correction to the mechanism recorded in the issue, which was reasoned rather than measured.** The
  issue states that HTMX 1.9 resolves `hx-target` through the issuing element's root node, so a form
  detached by an earlier swap drops its response silently. Read against the vendored HTMX 1.9.12, that
  is wrong twice. `hx-target` is resolved **once, at request-issue time**, and cached on the request
  context; `getRootNode` appears in that build only inside a shadow-DOM containment helper and is never
  consulted for targeting. And the element whose detachment matters is the **target**, not the form —
  the second response's cached target node is the `#artifacts-region` the first swap replaced, so the
  `outerHTML` swap reads `parentElement` on a detached node, gets `null`, and throws. It fires
  `htmx:swapError` and rethrows, so it is loud in the console and silent only on the page. The
  distinction is not pedantic: under the issue's mechanism, moving the forms outside the swapped region
  would have fixed this, and it would not have (#50).
- Adjacent, folded in because it is the same toolbar and the same symptom: the *Generating…* label was
  dead markup. It is a sibling of the generator forms, and HTMX's default indicator is the requesting
  element, so `.htmx-request .spinner` never matched it. `hx-indicator="closest .toolbar"` puts the
  class on the toolbar that contains both. Verified against the vendored build rather than assumed —
  when `hx-indicator` is present HTMX marks those elements *instead of* the requesting one, which
  nothing here depended on (#50).

- `_hostname` in Requivo Web's cross-site guard now refuses an authority it cannot determine a host
  from, instead of answering about it. `Host: evil.com@127.0.0.1` resolved to `127.0.0.1` and passed
  the allowlist, because `urlsplit` is a URL parser and correctly discards userinfo; `Origin:
  http://evil.com@127.0.0.1` came out same-trust-domain the same way (#51).
- The same fix refuses `Host: 127.0.0.1 evil.com`, which previously came back as that entire string —
  not a hostname, and refused only by happening to miss the allowlist. A parser that returns a non-host
  and leaves a later equality test to reject it is answering where it should be declining (#51).
- Not reachable from a browser, and fixed anyway. No browser serializes userinfo into a `Host`, an
  `Origin` or a `Referer`, and RFC 7231 requires a `Referer` to have it removed — so this closes a hole
  with no attacker who benefits. What earns the fix is that it is the **third** time this module's
  parser answered confidently about an input it should have refused: #43 was the opaque origin parsing
  to the plausible hostname `"null"`, #45 was an undetermined host read as *no host check needed*, and
  this is the same shape again (#51).
- **The refusal is the parser's, not each caller's.** The first two instances were closed with
  caller-side checks, which is a guarantee the next caller inherits without re-checking — and
  `_hostname` already has two callers, on two different headers (#51).
- Known residue, stated rather than implied: an **unbracketed** IPv6 literal (`Host: fe80::1`) still
  parses to `fe80` with the rest read as a port. It is malformed as an authority, no browser emits it,
  and it fails the allowlist — but the parser does answer, so the docstring does not claim the class is
  empty (#51).
- Compatibility: compatible for any caller that sends a hostname. A `Host` or `Origin` carrying
  userinfo, or whitespace inside the authority, now gets a 403 where it previously got a response —
  which is the fix.

- `requivo artifact save` without `--revision` now reports **`unstated_source_revision`** instead of
  `invalid_session` (#57). The refusal added in #6 inherited its sibling's code, because a new code
  needs a row in `web/app.py::_STATUS_BY_CODE` — which
  `tests/web/test_web.py::test_every_error_code_has_an_explicit_http_status` requires of every code in
  the vocabulary — and that file was held by another change in the same round. So the precision lived
  in the exception *type*, which a caller reading a serialized envelope never sees: the one handle it
  had could not tell *you left a flag off* from *this session is broken*. It is a **400**, the same
  status the condition already answered.
- The `details` shape is unchanged and deliberately so (#57). Both refusals still carry all five of
  `{slug, type, source_revision, current_revision, cause}`, with `cause: null` on the unstated arm.
  Sharing it was owed while the code was shared; with the codes split it is a decision, and narrowing
  a payload for nothing would hand a `KeyError` to a consumer reading `details["cause"]` across both
  arms — the failure #35 measured. #52 settled the same question the same way: `opaque_origin` and
  `origin_mismatch` share a shape and are still two codes, because a shared shape is not a shared
  meaning.
- Compatibility: compatible - `invalid_session` never named this condition in a released version. #6
  is still unreleased, so #6 and #57 reach users in the same release and no consumer ever saw the old
  code here. Moving a condition to a new code *is* breaking under `docs/compatibility.md`'s own policy
  and is recorded there as such anyway, because the policy is about the condition rather than about
  who happened to be watching. `UnstatedSourceRevisionError` remains an `InvalidSessionError`
  subclass, so catching the class is unaffected in either direction. This is filed under Fixed rather
  than Changed, which is where #52 filed its code split, for that one reason: `cross_site_request` had
  shipped and this code had not.
- **`artifact save --revision`'s help text no longer advertises a default it does not have** (#57). It
  still read `(default: the session's current revision)` after #6 removed exactly that behaviour — so
  the text a user reads *while deciding whether to pass the flag* was recommending the fabricated
  provenance the refusal exists to stop. Two reviewers on the #6 branch found it independently. It now
  says the flag is required and why: the session's current revision is a different fact, and only the
  caller knows what they read. The flag stays optional to `argparse` on purpose — the omission has to
  arrive as a structured envelope a `--json` caller can parse, not as a usage error and exit 2.
- The rest of the surface was swept rather than assumed (#57). On the help-text half nothing else was
  wrong: `docs/cli.md`, `docs/session-format.md` and `plugins/claude-code/REASONING.md` already state
  that `--revision` is required, and no other option on `artifact save` claims a default. A test now
  reads the rendered help and fails on either form this repository writes a default in.
- The same sweep for the code found two places that named `invalid_session` for this condition and are
  corrected here (#57): `docs/session-format.md`, which now names both codes and says which fact each
  one carries, and the unreleased `changelog.d/6.fixed.md`, which would otherwise have folded the old
  code into `CHANGELOG.md` in the release that removes it. A stale code name in a changelog is worse
  than none — it is the string a consumer would have written their match against.

- `discover`, `answer` and the Web's discovery routes no longer pay a ~25% surcharge on the largest
  part of their input. Every reasoning turn through the provider seam was writing a prompt-cache
  entry — 1.25x input to write, 0.1x to read — that nothing ever read back, because each of those
  operations makes exactly one call. #9 removed this from the six generators and `estimate`; these
  were the two call sites it could not reach that round (#58).
- The breakpoint stays where it is genuinely re-read: `converse()`'s interactive loop sends the same
  engine prompt for up to 8 turns, and the golden harness sends it K times. Both now declare that at
  the call site rather than inheriting it, because it is a per-call-site fact — the same `run()` is
  single-call under the provider seam and multi-call under `converse()` (#58).

- One unreadable session no longer takes `requivo session list` down. Invariant 15 — *a listing
  survives its own members* — was fixed for Requivo Web in #7 and left undone on the CLI, whose
  `deterministic.py` was held by another change that round. A `session.json` written by a newer
  Requivo made the command exit 1 with a single message, **every other session invisible and nothing
  naming which one was the problem** — the exact failure the invariant is about, on the surface that
  did not get the fix. The listing now comes from `list_entries()`, which degrades per member (#62).
- The degraded row names the session and carries the reason, because for the commonest break mode the
  reason *is* the remedy: *this session was written by a newer Requivo, upgrade* is actionable where a
  flattened `unreadable` is not. `requivo session verify <slug>` remains where the full story lives,
  and a footer line points at it (#62).
- It states no fact it could not read — no revision, no provider, no timestamp. A plausible `rev 0` on
  a session nobody managed to open is the quiet-wrong-answer form of the same bug. A session genuinely
  at revision 0 is a normal row and still reads as one: *we could not look* and *we have not looked
  yet* are two answers (#62).
- **A new exit code, 4.** A listing that degraded a row is neither of its neighbours: `0` says nothing
  is wrong and `1` says nothing was listed. It is safe to make non-zero precisely because nothing is
  withheld — the complete listing is still on stdout, so a caller that only wants the rows gets all of
  them. Documented in the exit-code table in `docs/cli.md` (#62).
- Compatibility: compatible. `session list --json` gained `readable` and `error` on every row, which is
  additive; a consumer reading only `slug`, `revision`, `provider` and `updated_at` is unaffected on a
  workspace where every session loads. A **degraded** row keeps the same key set with `null` in the
  three facts it could not read, rather than a shortened dict that would turn `row["revision"]` into a
  `KeyError` on a payload handed over deliberately. Branch on `readable`. The previous behaviour on
  such a workspace was no payload at all and exit 1, so nothing that worked stops working (#62).
- **The issue's own table is corrected, measured rather than assumed.** #62 carried #7's three break
  modes and said all three applied unchanged. They do not: the web row calls `request_text` and
  `status()`, while the CLI row reads nothing but the metadata, so only the `read_meta` mode ever
  reached this command. The other two are pinned as controls asserting they *stay* out of this path —
  a future row that reads the request or the status needs the per-row guard the web viewmodel carries,
  and the control is what will say so (#62).
- The degraded row is a new render site for two pieces of untrusted text, and both go through
  `display_token` (#40). The slug there is the raw directory name — `read_meta` would have refused a
  non-kebab one, but that refusal is why the row is degraded, so it never ran. The error text can be a
  four-line pydantic `ValidationError`, which printed raw turns one session into four rows of listing
  with no way to tell where the row ends (#62).
- **A `session.json` could forge a row of `session list`'s own output, and no longer can.** Found by
  the audit on this branch, and outside the issue as filed. The readable row printed `slug`, `provider`
  and `updated_at` unescaped, and all three are read straight out of the file's body — `read_meta`
  validates the slug it is *called with*, the directory name, and then returns `SessionMeta.slug`, a
  bare `str` with no pattern, from the JSON. Nothing checks the two agree outside `session import`.
  A hand-edited or imported `session.json` whose `slug` carried a newline printed a second, entirely
  fabricated row — `rev 999 (trusted, …)` — into the listing, and the command exited 0. This is
  invariant 14's second door, the same shape as the stored context-card name in #40. All three fields
  now go through `display_token`; a clean value is returned byte-for-byte, so no real session's row
  changes (#62).
- Reported rather than fixed, deliberately: **`requivo session show` has the identical defect in five
  fields** — `session_id`, `created_at`, `updated_at`, `provider` and `model_name` all reach column 0
  unescaped, measured the same way. It is a different verb needing its own tests and its own review,
  so it wants its own change rather than a rider on this one. `--json` is unaffected on both verbs:
  `json.dumps` defaults to `ensure_ascii=True`, which escapes a control character before it can reach
  a line of its own, and there is now a test pinning that this default is load-bearing (#62).

- `requivo doctor` now names what is under `.requivo/sessions/` and is **not** a session. Nothing
  could see one: `list_session_slugs` filters on `session.json`, `doctor` and `session verify` both
  reason over the slugs it returns, and `check_session` answers about a directory it is handed, which
  nobody could hand it a name for. #22 stopped `session_lock` producing these; it could not find the
  ones already on disk from before that fix (#67).
- **The consequence is printed, because it is the only symptom any of this ever had.** The name is
  taken, and `create_session`'s rename is the only claim on a slug (invariant 11) — it loses to
  anything already occupying the name, after which `SessionService` falls through to its
  hash-suffixed candidate. Ask for `leave-approval`, get `leave-approval-a1b2c3`, silently. A finding
  with no remedy is a line people learn to scroll past, so the row carries `[name taken]` and the
  mechanism is stated once beneath it (#67).
- **A report, not a repair.** Nothing is deleted, moved or rewritten, and no field states a
  conclusion — there is no `is_lock_ghost` anywhere. A directory holding only `.lock` is almost
  certainly a leftover lock and *almost certainly* is not a licence to act: a half-extracted archive
  and an interrupted copy are the same shape from outside, and this project's rule is that the
  evidence is the directory and only the directory (invariant 14). Each entry reports `name`, `kind`,
  the first five `entries` it holds and the true `entry_count`. The one derived value, `slug_shaped`,
  is a property of the *name* — whether `create_session` can be asked for it, and so whether the
  entry costs anybody anything — not a guess at where it came from (#67).
- Three things the review on this branch found and are fixed here. **A symlink is no longer reported
  as whatever it points at**: `Path.is_dir()` follows one, so a link at a slug name answered
  `directory` and then listed the *target's* filenames into a report about your workspace. It is
  `kind: "symlink"` now and is not followed. **`slug_shaped` asked the slug pattern alone**, and
  validity is the pattern *and* the length — an 81-character kebab-case name was marked as one a
  session would silently lose, when `canonical_dir` refuses it outright and loudly; it goes through
  `is_slug`, which calls `validate_slug`, so there is one rule rather than two. And **`doctor` takes
  one listing for both halves** (`scan_session_root`) rather than scanning twice: two scans are two
  instants, and a `session.json` landing between them put a name in *neither* answer — the invisible
  state this key exists to end, reintroduced by the key itself (#67).
- `_describe_non_session` never raises, and that is load-bearing rather than defensive. It runs inside
  the one `try` that also holds the session listing, so an exception escaping it discarded a session
  report that had already succeeded and told the reader the whole root was unlistable — a claim
  broader than what failed, invariant 15's shape one layer down. Both arms land in a state the entry
  already has (*could not stat* / *could not list*), so this is not a guard that cannot fire: on Linux
  a filename that is not valid UTF-8 arrives carrying surrogates, and APFS refuses such a name, so it
  could not be constructed here to be ruled out either way (#67).
- **A stray file at a slug name costs exactly the same**, found by sweeping the class rather than
  taking the issue's word for the instance: `rename` onto an existing file fails too, `d.exists()` is
  true, and the caller gets the identical substitution. Reporting only directories would have left an
  identical symptom with an identical remedy invisible, so each entry says what kind of thing it is
  instead of the report assuming they are all directories (#67).
- Three states, at both levels. `sessions.non_sessions` is `null` — never `[]` — when the session
  root could not be listed at all, matching what `sessions.total` already does, because an empty list
  there reads as *we looked and there is nothing else*. Within an entry, `entries: null` with an
  `error` is a directory we could not look inside, which must not render like an empty one — on POSIX
  an empty directory is the single shape that costs nothing at all, because `rename(2)` replaces an
  empty destination. Windows differs (`os.rename` refuses any existing destination), which is why an
  empty directory is still reported and still marked `[name taken]` (#67).
- `doctor` owns this rather than `session verify`, which is per-session and takes a slug — and the
  defining property of one of these is that no listing produces its name, so there is no slug for
  anybody to type. It gets a row of its own rather than a note on the sessions row, because
  `0 in this workspace` stays true: none of this is a session, and folding it in would trade a
  correct count for a vague one. `requivo session list` is unchanged and still lists only sessions
  (#67).
- The listing lives in `core/persistence.py` beside `list_session_slugs`, which owns the store
  layout, and both halves now come out of one predicate over one `iterdir` — a name in neither is
  precisely the state this issue is about, so stating the rule twice is how it comes back. Core
  reading a directory crosses no boundary: invariant 7 forbids importing a provider and touching
  argv, the streams, the environment and process exit, not IO (#67).
- The entry name and the names it holds are read off disk and go through `display_token` (#40).
  Printed bare, one carrying a newline does not merely look odd — it ends the line and starts another
  at column 0 of `doctor`'s own report. `--json` is unaffected and keeps the bytes verbatim (#67).
- Compatibility: compatible. `doctor --json` gains `sessions.non_sessions`, which is additive and is
  `[]` on any workspace Requivo alone has written. No existing field changes meaning, nothing on disk
  is touched, and no new exit code is introduced — the finding is a row, not a failure (#67).

- **A `session.json` could forge a line of `requivo session show`'s own output, and no longer can.**
  The same defect #62 fixed in `session list`, in the other verb: `read_meta` validates the slug it
  is *called with* — the directory name — and then returns every other value straight out of the
  file's body, where they are bare `str` fields with no pattern. A hand-edited or imported
  `session.json` carrying a newline printed **sixteen** lines where eight were real, including its
  own `revision 999` and `provider trusted`, and the command exited 0 (#70).
- **It is eight fields, not the five the issue counted**, and the count is reported rather than made
  to come out right. #62 named the five that are `SessionMeta` scalars — `session_id`, `created_at`,
  `updated_at`, `provider`, `model_name`. It left out `slug`, which is the same bare string here that
  it was on the listing, and the two that are not `SessionMeta` fields at all: the **keys** of
  `artifact_status`, a `dict[str, …]` whose keys are whatever the file says, and each artifact's
  `filename`. `core/integrity.py` already treats that recorded filename as untrusted input, so a
  render site that did not was the exception making the rule unreliable (#70).
- This verb is the sharper of the pair. Every line it prints is one Requivo writes itself, at a fixed
  column, in a fixed shape — so a stored value can print `  revision 0` beneath a session that is at
  revision 12, and nothing in the render tells the two apart. On a listing a forged row at least has
  to imitate a row (#70).
- `current_revision`, an artifact's `revision` and its `stale` flag are deliberately **not** wrapped
  and are named here as such: they are typed `int`, `int` and `bool`, so `read_meta` refuses a string
  in them before the render runs. Wrapping them defensively would say the type bought nothing (#70).
- `session_id` is **sliced before it is escaped**. The other order truncates the escaped form, which
  can cut an escape sequence in half and leave the quote unclosed — a second defect bought with the
  fix for the first (#70).
- **`requivo artifact list` had the same defect and is fixed alongside it, outside the issue's own
  footprint.** Found by sweeping the class rather than the instance: it renders two of the same
  strings — the `artifact_status` key and the `filename` — off the same file at the same fixed
  column, and a forged entry printed a fabricated second artifact row while the command exited 0.
  One line, in the same file, over the same two fields, with the same test fixture. It is called out
  here rather than left to read as scope creep: escaping a stored value in one of the two verbs that
  render it leaves the rule meaning *wherever somebody happened to look* (#70).
- **The reason `--json` is safe is corrected, having been measured rather than repeated.** #62, this
  issue's own text and this repository's docs all said `json.dumps` defaults to `ensure_ascii=True`
  and therefore escapes a control character. That is not what protects a newline — JSON's grammar
  forbids a literal control character below `U+0020` inside a string, so `\n` is escaped whether the
  flag is on or off, and a test probing with a newline is green either way and pins nothing. The
  default **is** load-bearing, for the *non-ASCII* half of the guarded range, `U+007F`–`U+009F`:
  `NEL` (`U+0085`), a line terminator `str.splitlines()` and some terminals honour, and `CSI`
  (`U+009B`), an escape introducer `core/selectors.py` already names. The test now probes both
  halves and fails if the default is turned off to make accented output readable (#70).
- **Where the terminal guard stops is now written down and pinned, rather than assumed to coincide
  with `str.splitlines()`.** Found by the audit on this branch. `core/selectors.py`'s
  `_CONTROL_CHARS` is C0, DEL and C1 — *the class that can move a terminal's cursor or end its line*
  — while `str.splitlines()` also breaks on `U+2028` and `U+2029`, which `display_token` returns
  byte-for-byte. On a terminal that is the right answer (xterm and the VT sequences behind it answer
  to CR and LF, not to Unicode `Zl`/`Zp`), so this is not a forgery on the surface the guard covers;
  it matters to anything reading the human-readable output line by line, and `--json` covers those
  two as well and is therefore the stricter of the two paths. Widening `_CONTROL_CHARS` is
  deliberately **not** done here — it would also change what `normalize_tokens` refuses, i.e. the
  public `unsafe_selector_token` behaviour, which is a decision for that module's owner (#70).
- Compatibility: compatible. A value that is already one safe line comes back byte-for-byte, so no
  session Requivo itself wrote renders differently on any of the three verbs; such a value can only
  have arrived by `session import` or by hand. `--json` is unchanged and was never affected (#70).

## [0.10.0] - 2026-08-18

### Added

- `CONTRIBUTING.md` now states what the changelog gate does **not** cover (#37). The gate triggers on
  `pull_request` only, so direct pushes to `main` are never checked by it — and this is a
  solo-maintained repository whose own working style is to commit straight to `main`. Every release
  cycle so far has included commits that reached `main` without a pull request, so the uncovered
  class is routinely non-empty rather than theoretical. A green board
  therefore means *the changelog gate passed on the commits it was shown*, not that every change in
  the release carries a fragment; those are different claims and nothing on the board distinguished
  them.
- The limit is documented rather than closed, deliberately (#37). Adding a `push:` trigger on `main`
  would fail *after* the fact — a fragment cannot be added retroactively to a commit already pushed —
  which installs a permanently red default branch, a worse lie than the one it fixes. Abandoning
  direct-to-main would cost the workflow this repository chose on purpose. So the honest move is to
  say where the coverage stops, next to the list of checks a change is expected to pass.
- Compatibility: compatible - documentation only; no workflow, code or behaviour changes.
  `.github/workflows/oss-changelog.yml` is deliberately untouched, since it is scaffolded by the
  `oss` plugin and overwritten on every scaffold run — an edit there would be lost silently, which is
  the same class of defect as the one being documented.

### Fixed

- `requivo session migrate` can no longer overwrite a live session (#4). `migrate_legacy()` checked
  only that the *legacy* model existed, so pointed at a slug a real session already occupied it reset
  `session.json` to revision 0 and wrote the legacy model over `revisions/0001-model.json` —
  destroying revision 1 with no copy anywhere, and leaving the session failing its own integrity
  check. It now makes the same atomic claim on the slug that `create_session` does, and refuses with
  `session_exists` before writing anything; the whole migration runs under one session lock instead of
  three, so a concurrent apply cannot interleave with it.

- Artifact filenames are validated like the slug beside them (#5). `write_artifact_file()` and
  `save_session_artifact()` validated `slug` and not `filename`, so a caller passing
  `../../../x.md` wrote outside the session directory entirely — and, on the second of the two,
  persisted that path into `session.json`, where the integrity checker and the artifact-show paths
  read it back. Both now go through one chokepoint, `artifact_path()`, which applies the new
  `validate_filename()` and confirms the resolved path is a genuine child of `artifacts/`. A filename
  must be a bare lowercase name such as `prd.md`; anything else raises `invalid_filename`. No
  in-repo caller could reach this, and none is affected — every one passes a literal or an
  `ARTIFACT_FILENAMES` lookup.

- The request and answer boxes no longer clip a long paste before the server can refuse it (#8).
  Both textareas — and the optional session-name field — carried an HTML `maxlength` set to the same
  number as the server's ceiling, and a browser enforces that attribute by silently dropping the
  overflow: no event, no message, no visual difference. Pasting a 26,000-character email thread
  submitted the first 20,000 of it, which is exactly the length the server admits, so the refusal
  that exists precisely to stop half a request being reasoned over as if it were the whole thing
  could never fire from the browser. The attributes are gone; an over-long paste now submits in full
  and comes back refused, on a page naming the limit it exceeded. The ceilings themselves are
  unchanged, and input at exactly the ceiling is still accepted. **What that refusal costs you today:
  it is a full-page error that preserves none of the submitted text** — a 26,000-character email
  thread that arrived through the clipboard has to be fetched again from wherever it came from, and
  on the answers box the error fragment replaces the region containing the textarea. That is a
  strictly better position than the silent truncation it replaced, and it is not the finished one;
  re-rendering the page with the text intact is tracked in #30.

- The architectural boundary guard (`tests/test_boundaries.py`) no longer passes while scanning
  nothing (#10). `Path.glob` over a directory that does not exist returns an empty list and raises
  nothing, so both boundary tests asserted "no offenders" over an empty set and went green — the
  package has already been renamed once (`product_copilot` -> `requivo`) and the guard survived it by
  luck. The scan set is now asserted non-empty and named, an unscannable root is an error rather than
  an all-clear, and every negative assertion is paired with a fixture the guard must flag.
- The same guard now sees three things it was blind to (#10): relative imports (`from .anthropic
  import ...`, which it skipped outright on `node.level != 0`), dotted absolute imports
  (`import requivo.providers.anthropic`), and anything in a `core/<subpackage>/`, since the walk was
  not recursive.
- The guard reads source with an explicit `encoding="utf-8"` (#10). Every module in core carries an
  em dash, and `read_text()` with no encoding decodes with the locale codepage, so under `LC_ALL=C`
  or a DBCS Windows shell the guard died with `UnicodeDecodeError` rather than running. Its control
  forces the fallback encoding and, on the interpreters where that force cannot be made to take,
  skips loudly naming what went untested instead of passing.
- The half of the core boundary that nothing enforced is now enforced (#10): no `print`, `input` or
  `breakpoint`, no `sys.argv`/`stdout`/`stderr`/`stdin`/`exit`, no `os.environ`/`getenv`/`putenv`, and
  no terminal framework. File IO and `logging` stay allowed, and a test pins that so a later
  tightening which would fail correct code goes red first. Invariant 7 said core was "IO-free", which
  was never true — `persistence.py`, `context.py`, `contracts.py` and `analysis.py` all read files by
  design. Its wording, and the matching lines in `docs/architecture.md` and
  `docs/open-source-strategy.md`, now say what the rule actually means.

- `requivo doctor` no longer renders two of its own failures as green ticks (#12). A context-card
  loading failure was written into the *schema* check's error field, left `schema.ok` true, and was
  printed nowhere — so a wheel or container layer that ships `assets/` but loses `assets/context/`
  showed three green ticks while every impact estimate was made with no product context at all. And
  an unreadable `.requivo/sessions/` was caught and reported as `{"total": 0}`, byte-identical to a
  genuinely empty workspace, so twelve unreachable sessions read as "you have none" and a user
  concludes they were deleted. Both checks now carry a third state: `doctor --json` gains a
  `context` verdict (`status` is `ok`, `empty` or `unreadable`, alongside the existing
  `context_cards` list, which is unchanged), and `sessions` gains `readable`/`error` with `total`
  set to `null` — not `0` — when the directory could not be listed.
- `requivo doctor` and `requivo session verify` now check that a session's saved context cards still
  resolve. Since #13 an unresolvable card selection is refused rather than silently loading nothing,
  so a session that has lost a card is hard-stopped at its next (paid) reasoning turn — and both
  health verbs called it healthy right up to that moment. `session verify --json` gains a
  `context_cards` block (`checked`, `problem`, `error`) and `doctor --json` a
  `sessions.unresolved_cards` map, each naming the missing cards and how to recover.
- This is reported as an *environment* finding and deliberately not as a session-integrity problem:
  a context card lives outside the session directory, so the same session would be "broken" on one
  machine and coherent on another, and `session import` — which refuses an archive on integrity
  problems — would reject a colleague's perfectly good session over a card you do not happen to
  have.
- A context-card directory that exists but **cannot be read** is now reported as `context_unreadable`
  instead of reading as a directory holding no cards. `Path.glob` swallows `PermissionError` and
  yields nothing, so a denied directory was indistinguishable from an empty one: the card vocabulary
  came back quietly short, `doctor` said `ok` at a smaller count when a second root was readable, and
  a session naming a card in the denied directory was told `unknown_context_card` — whose stated
  remedy is to restore a file that was in fact right there. The two conditions have opposite
  remedies, so they are now two errors.
- `session verify` and `session import` no longer stat a path a session names outside itself. A
  recorded artifact's `filename` is an unconstrained string read out of `session.json`, and it was
  joined into the artifacts directory and passed to `.is_file()` without validation — an absolute
  value replaces the prefix entirely under `pathlib`, so `artifacts/` + `/etc/passwd` was
  `/etc/passwd`. Recording a problem for an unknown artifact type or a filename mismatch did not stop
  it: execution fell through to the join either way. No content was ever read, but whether the reply
  carried `missing_artifact_file` answered whether that outside path existed. The name now goes
  through the same bare-filename chokepoint every artifact write uses, and a refused one is reported
  as `unsafe_artifact_filename` rather than probed.
- The Claude Code `discover` skill now confirms `context.ok` as well as `schema.ok` before
  reasoning; `schema.ok` was true in exactly the broken state above, so the plugin proceeded.
- Compatibility: compatible - every existing `doctor --json` and `session verify --json` key keeps
  its name and meaning, with three exceptions that are the fix: `sessions.total` is `null` instead of
  `0` when the session directory cannot be read; `session verify` now exits non-zero (and reports
  `ok: false`) for a session whose context cards no longer resolve, which is a session that cannot
  take another turn; and a session or archive recording an artifact under a name that is not a bare
  filename is now refused as `unsafe_artifact_filename`, where it previously passed whenever the path
  it named happened to exist.

- Selectors no longer widen to everything, or empty to nothing, when a name is blank or no longer
  resolves (#13). An empty slot name — `requivo impact <slug> ""`, which is what an unset shell
  variable expands to — matched every label and reported the whole model as changed with no unmatched
  token to explain it; an empty `--context` name fell through to "every card", the widening
  `resolve_cards` exists to prevent; and a context card that had been renamed, or that lived in
  `REQUIVO_CONTEXT_DIR` on another machine, silently replaced `{{CONTEXT}}` with the empty string on
  every later turn, so the engine reasoned with no product context at all and nothing said so. All
  three are now refusals carrying a structured error (`empty_selector_token`, `unknown_context_card`),
  from one shared rule in `requivo.core.selectors` rather than three local ones.
- Compatibility: breaking - a session whose `context_cards` no longer resolve now fails its next turn
  with a named card instead of quietly reasoning without product context, and `--context "a,"` is
  refused rather than read as `a`. Both were previously silent; neither has a persisted-format change,
  so an unaffected session is untouched.

- The `ReasoningProvider` protocol now declares `name`, the member `DiscoveryService` reads first
  (#19). It reaches for `provider.name` when it claims the session, before any reasoning happens, but
  the protocol declared only `analyze`, `generate`, `model_name` and `provenance` — so a second
  implementation satisfied the published contract, satisfied `isinstance`, and then failed with an
  `AttributeError` on the first `discover`. The seam reported conformance it had not checked.
  `isinstance(p, ReasoningProvider)` now catches that provider, because `@runtime_checkable` does
  cover non-method members; `docs/providers.md` lists `name` alongside the four methods, and states
  what that check can and cannot tell you.
- Compatibility: breaking - only for code calling `issubclass(X, ReasoningProvider)`, which Python
  refuses for any protocol carrying a non-method member and now raises `TypeError`; `isinstance` is
  the replacement and is stricter than before. Nothing in Requivo calls either, `AnthropicProvider`
  already carried `name`, and no persisted format or CLI output changes.

- Artifact filenames are validated on the way **out** as well as in (#23). #5 closed the traversal on
  the two mutating paths; `FileSessionRepository.load_artifact` still joined
  `canonical_dir(slug) / "artifacts" / filename` inline, one layer above the chokepoint, so
  `load_artifact(slug, "../../../../secret.md")` read and returned a file outside the session
  directory. The two are separate exposures — a write target decides what this code may create, a read
  target decides what it may disclose — so closing one did not close the other. Reads now go through
  the same `artifact_path()` chokepoint, which is where they should have been: the read side is the
  proof of the rule `artifact_path()` exists for, that a guard applied per-caller is a guard the next
  caller forgets.
- A refused read raises `invalid_filename`; only a genuinely missing artifact returns nothing (#23).
  Returning nothing for both was the tempting shape and the quiet one — a rejected traversal would
  have been indistinguishable from an artifact nobody has generated yet, and a caller that cannot tell
  a refusal from an absence has been handed the wrong answer in the more dangerous direction.
- Artifact content is decoded as UTF-8 explicitly, matching how it was written (#23). The same read
  took the locale's encoding, so under `LC_ALL=C` or a DBCS Windows shell an artifact died on its
  first em-dash — and every artifact this engine generates has them.
- Compatibility: compatible - no in-repo caller and no valid filename changes behaviour; every caller
  reaches this through `ArtifactService.show` with an `ARTIFACT_FILENAMES` lookup, and those still
  load byte for byte. The only behaviour that moved is for a filename that was previously a traversal,
  where an exception replaces a silently-wrong answer.

- The files that declare the project version are now checked against each other on every test run
  (#32). Four of them declare it — `pyproject.toml`, `src/requivo/__init__.py`, the Claude Code
  plugin manifest and the marketplace catalog — and nothing compared them; a release edits them by
  hand, one at a time. The two expensive drifts are both silent on both ends: a stale
  `plugins/claude-code/.claude-plugin/plugin.json` is the version the Claude Code updater compares,
  so a release that leaves it behind uploads to PyPI, announces itself correctly and is never
  offered to plugin users at all; a stale `src/requivo/__init__.py` is what `requivo doctor` prints
  as `requivo_version`, so the diagnostic whose job is answering *is anything wrong* becomes the
  thing that is wrong, and every bug report from that install cites the wrong version.
- The guard derives its site list by scanning for a version at a known structural position, rather
  than reading the registered list in `.oss.json` (#32). That distinction was not academic: the
  registry was swept by hand at `aed734c` specifically to catch unregistered sites, and that sweep
  still missed `src/requivo/__init__.py` — so a guard reading the registry would have certified
  agreement across the files somebody remembered while the one they forgot sat unchecked, which is
  the failure it exists to close. `src/requivo/__init__.py` is now registered, and the registry is
  cross-checked rather than trusted: a derived site missing from `version_sites` is itself a
  failure, since it is a site a release will not know to update.
- Scanning by structural position is also what keeps `CHANGELOG.md` out of it (#32). A history file
  names every version the project has ever had, and it is in `version_sites` because a release must
  *edit* it — not because it declares anything. The cross-check is therefore one-directional:
  registered-but-not-declaring is fine, declaring-but-unregistered is a finding.
- The guard has three states rather than two, and refuses on the third (#32). An unreadable
  manifest, a known site that has moved, and an empty scan are each a failure worded as *could not
  check* and distinguishable from drift — because a version guard that skips a file it could not
  read and passes anyway certifies an agreement it never looked for, converting "nobody checked"
  into "checked and fine". That is strictly worse than having no guard.
- Compatibility: compatible - a test-only addition plus one new entry in `.oss.json`. No product
  code, no public output and no on-disk format changes, and no version number moves.

- An install with no context cards at all is now refused instead of reasoning without them (#33).
  `load_context(None)` — what every session with no card selection sends on each turn — comprehended
  over an empty card directory and returned the empty string, and `build_prompt` substituted that into
  `{{CONTEXT}}` with no check. A wheel or container layer that shipped `assets/` but lost
  `assets/context/` therefore reasoned with no product context at all, on calls that cost money, for
  as long as the install lasted: `information_value = uncertainty x impact` is the engine's central
  idea and it runs entirely on those cards. Two earlier fixes had closed the narrow instances — a
  selection that resolves to nothing, and `doctor` learning to tell `ok` from `empty` from
  `unreadable` — but both are about a *selection*, and `None` is the absence of one. The new
  `no_context_cards` error names where it looked, `check_selection` reports it so `doctor` and
  `session verify` still answer for free and in advance, and the test that used to assert
  `load_context() == ""` asserted the defect as the contract.

- A server-side fault is no longer reported to the browser as your bad request (#34). Requivo Web
  mapped error codes to HTTP statuses through a table that defaulted to `400` for anything unlisted,
  so a code added anywhere else arrived wearing a plausible, wrong status. `context_unreadable` — the
  server unable to read its own context-card directory, entirely the operator's environment — told
  the reader they had done something wrong. Three more codes were sitting on that same default
  unnoticed: `provider_output_invalid` and `session_locked`, and `session_exists`. Every code now has
  an explicit status (`500`, `502`, `503` and `409` respectively), an unrecognised code is a `500`
  rather than a `400` because "we could not classify this" is not evidence the caller erred, and a
  test walks the error vocabulary so the next code added is a red build instead of a wrong answer to
  a user. A `5xx` is now also logged for the operator, who otherwise had no record of it.

- `empty_selector_token` no longer carries two different facts behind two different payload shapes
  (#35). One code covered both an empty *token inside* a selection (`details: {selector, position}`)
  and a selection that was *itself* empty (`details: {selector, tokens}`) — a distinction the
  selector's own docstring had already argued for, and then not honoured seventy lines later in the
  same change. Since the documented advice for consumers is to assert on the code, anyone following
  it and reading `details["position"]` got a `KeyError` from a payload that correctly carried the
  code they had matched. The second case is now `empty_selection`, a sibling rather than a subclass
  so the two cannot be re-conflated by an `except`, `position` is guaranteed on every
  `empty_selector_token` payload, and `docs/compatibility.md` carries the mapping for anyone matching
  the old code.

### Security

- A session's stored context-card names can no longer forge a line of `doctor` or `session verify`
  (#40). `context_cards` is an unconstrained list of strings in `session.json`, `session import`
  passes it through intact, and both health verbs rendered those names into their output bare. A
  name containing a newline therefore did not merely look odd — it ended the line and started a new
  one at whatever column it chose, so a session could print `doctor`'s own `sessions` row,
  byte-identical in shape and column, answering *all clear* directly beneath the row reporting it.
  `session verify`, the verb whose entire job is to say whether a session is telling the truth, was
  forged the same way and still exited 1, so its text and its exit code disagreed.
- The fix refuses rather than escapes, at `normalize_tokens` — the one function every selector
  passes through — so a hostile name cannot reach a render site at all, rather than being made safe
  at the two sites that exist today. The new refusal is `unsafe_selector_token`, with
  `details: {selector, position}`; it is reported by `check_selection` rather than raised, so one
  tampered session degrades its own row instead of taking the whole listing down. `session show`,
  a third render site the issue does not name, prints stored names through a one-line display rule
  instead, because nothing there is selecting and no refusal can run.
- Also fixed, and outside the issue's footprint: `_SLUG_RE` and `_FILENAME_RE` were anchored with
  `$`, which in Python matches at the end of the string *or just before a trailing newline*. Both
  guards therefore accepted one trailing newline, which is what made `integrity.py`'s
  `artifacts/<name> is missing` line — the one place that renders a recorded filename without `!r` —
  reachable with a name that splits it in two. Both are now anchored with `\Z`.
- Found in review of this fix and fixed here: `resolve_slots` echoed the **unstripped** token into
  its unmatched list, where `resolve_cards` and `_selection_keys` both echo `raw.strip()`. Because
  the new guard inspects the stripped token — `str.strip()` removes the control characters Python
  classifies as whitespace — a slot token with a *leading* newline passed the guard and then broke
  the line `requivo impact` prints. Lower severity than the card path (a slot token is a live argv
  value the same user typed, not persisted data), but `core/selectors.py` claimed the value could
  never reach a render site, so the claim had to be made true. The docstrings now state the guard's
  actual scope rather than the loose version of it.
- Compatibility: compatible - no name Requivo writes is affected, and `--json` output is unchanged
  and still carries the bytes verbatim. A `session.json` hand-edited or imported with a control
  character in `context_cards` now reports `unsafe_selector_token` where it previously printed the
  name; `docs/compatibility.md` carries the new code.

## [0.9.10] - 2026-08-04

A documentation release. No contract, no session format, no prompt, no generator and no engine
behaviour changed — 0.9.10 reasons exactly as 0.9.9 did. It exists because 0.9.9 was tagged but never
published, and because the quickstart it shipped with did not work on a machine without `uv`.

### Fixed

- **The install instructions assumed a tool the reader may not have.** Every line in the README and in
  getting-started began with `uv`, and neither said how to obtain it or offered an alternative. `uv`
  keeps the lead and now carries the one line that installs it; `pipx` is named as the equivalent, and
  getting-started gained an *Installing* section with a virtualenv route for pip-only setups, tested
  end to end on a clean Python 3.9. `pip install --user` is called out as the one to avoid: it
  succeeds while leaving `requivo` off the PATH, which reads as a broken package rather than a PATH
  problem.
- **`requivo demo` no longer asks you to clone the repository.** The payload ships in the wheel, so it
  runs straight after any install; the clone is now the alternative rather than the instruction.

### Changed

- **The README is an orientation again.** 250 → 160 lines. Requivo Web now opens the document instead
  of being the second of three equally-weighted quickstarts — the hierarchy is in the space each
  surface occupies, not only in its heading. *Core concepts* moved to
  [`docs/requirements-model.md`](docs/requirements-model.md), which gained the product ↔ engine
  vocabulary table it never had in writing; *What Requivo produces* folded into *How it works*; the
  13-row documentation table dropped to 5, the rest already being indexed in `docs/`.
- **The Web install is two lines.** `export ANTHROPIC_API_KEY` left the install block: `cli.py` loads a
  `.env` and `web/config.py` already reports a missing key in the interface, so presenting it as a
  prerequisite overstated it — the interface opens and reads existing sessions without one.

## [0.9.9] - 2026-08-04

The product release. Nothing about the engine changed — no contract, no session format, no prompt, no
generator. What changed is that the useful part is now the visible part: Requivo Web is built around
one workflow instead of around the model, and the three interfaces are no longer presented as three
equal choices. **Web is the product experience, Claude Code is an integration, the CLI is
infrastructure** — a difference in weight, never in capability.

### Added
- **"What changed"**, after every answer. The page now leads with what those answers moved: which parts
  of the solution changed, which decisions and contested premises need re-examining, which documents
  need updating. All of it is a projection of the `UpdateResult` the Core already returned — computed
  from the dependency graph, never generated. This was the product's differentiator and it had been
  rendering as a one-line notice.
- **`web/viewmodels/labels.py`** — the user-facing vocabulary in one table, so a term that appears in
  six templates cannot drift in six directions. *What we know*, *what we are assuming*, *open
  question*, *needs updating*, *are we ready?*, *decision brief*. A translation layer only: nothing
  stored, emitted by `--json`, or named in a contract changed.
- **`docs/product-validation.md`** — the manual protocol for the question the test suite cannot answer:
  is this better than a strong prompt to a capable model? It isolates the two moments where the answer
  actually lies — coming back to a session two days later, and changing an answer you already gave.
  Deliberately not folded into the golden harness, which would lend a measurement's precision to a
  judgment.
- **Traceability details** — one disclosure on the session page holding everything the engine knows:
  per-topic understanding, coverage, every open question, decisions, contested premises, provenance,
  raw model export. Hiding is presentational; the counts are always stated, so a short list can never
  be mistaken for the whole list.
- `core.analysis.slot_labels()` — the public form of the internal `_label`, so an interface translates
  slot ids through the schema instead of inventing its own names.

### Changed
- **The request box is the home page.** `/sessions/new` is retired (it redirects); the provider is
  resolved by the server rather than asked of the reader, and joins the session name and the product
  context cards under *Advanced settings*.
- **The session page reads in one order**: the request, what Requivo understood, at most five questions
  (each with *why it matters* and its likely area of impact), the answer form, *Are we ready?* as one
  action state with its reasons, then the decision brief. Everything else moved behind traceability.
- **One primary document action.** "Generate decision brief" leads; PRD, acceptance criteria, epic and
  release notes stay available under *More documents*. Six buttons of equal weight is not six options,
  it is no recommendation.
- **The decision brief is half deterministic.** `brief_markdown` now opens with *What is confirmed* and
  *Important assumptions*, read straight off the model rather than restated by the provider — a
  restatement can drift from what it restates; a projection cannot. The contract, the prompt and the
  filename (`solution-assessment.md`) are unchanged, so no session, script or golden baseline moves.
- **The engine's own `summary` is finally shown.** `scope`, `assumptions` and `blind_spot` were being
  produced on every turn and thrown away; they are the paragraph a reader needs to judge whether the
  engine understood them at all.
- The answers turn swaps the whole session body rather than the questions alone — a partial swap left
  the "needs updating" badges describing the previous revision, under the reader's eyes.
- README, `docs/`, the Claude Code plugin and the CLI help all speak the product's vocabulary and the
  Web → Claude Code → CLI hierarchy. `examples/leave-approval/` is the canonical example, and now ends
  with the change-impact walkthrough.

### Fixed
- **One un-analysed session no longer 404s the whole home page.** `session_list` asked every session
  for a `status()`, which needs a model; a session created through "save the request only" has none, and
  the exception took the entire listing with it — hiding every *other* session behind one that had
  simply not been analysed yet. A listing has to survive its own members (invariant 15).
- Opportunities rendered their leverage as `Leverage.high`: the view model dumped without
  `mode="json"`, and Jinja renders an enum by its repr.

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
  carrying them through 1.0.
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
service layer becoming the integrity boundary it has to be before anything external calls it directly.

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
  — the shape an external deployment constructs — wrote sessions to Postgres and artifacts to the local
  filesystem, with every call succeeding. It now follows the session service, and takes a `repo=`
  argument that configures both at once.

## [0.9.6] - 2026-08-02

The 0.9.5 review. Two correctness bugs sat where the product's own
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
  is always overwritten. A consumer needs to refer back to a decision; text is a poor handle.
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
terminal; it is deliberately bounded — no accounts, auth, database, remote storage, or telemetry
(see `docs/web.md`).

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

Correctness at the session boundary — the layer any external consumer sits on. From the same
external review's session-boundary list.

### Added
- **`SessionRepository` storage seam.** `SessionService` and `ArtifactService` no longer touch the
  filesystem directly — storage is injected as a `SessionRepository` (in `services/repository.py`),
  with `FileSessionRepository` (the default) delegating to `core.persistence`. The canonical-vs-legacy
  `out/` handling now lives inside the file repository, where it belongs. A deployment can supply a
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
preparation for a future Web UI, plus the formalized **open-source strategy** (the public / private
boundary). The model format is unchanged and the license stays MIT; the refactor itself changed no
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
  Claude Code / Community Web surface map, and the public-vs-private data boundary),
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

[Unreleased]: https://github.com/jbkkz/requivo/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/jbkkz/requivo/releases/tag/v1.0.0
[0.11.0]: https://github.com/jbkkz/requivo/releases/tag/v0.11.0
[0.10.0]: https://github.com/jbkkz/requivo/releases/tag/v0.10.0
[0.9.10]: https://github.com/jbkkz/requivo/releases/tag/v0.9.10
[0.9.9]: https://github.com/jbkkz/requivo/releases/tag/v0.9.9
[0.9.8]: https://github.com/jbkkz/requivo/releases/tag/v0.9.8
[0.9.7]: https://github.com/jbkkz/requivo/releases/tag/v0.9.7
[0.9.6]: https://github.com/jbkkz/requivo/releases/tag/v0.9.6
[0.9.5]: https://github.com/jbkkz/requivo/releases/tag/v0.9.5
[0.9.4]: https://github.com/jbkkz/requivo/releases/tag/v0.9.4
[0.9.3]: https://github.com/jbkkz/requivo/releases/tag/v0.9.3
[0.9.2]: https://github.com/jbkkz/requivo/releases/tag/v0.9.2
[0.9.1]: https://github.com/jbkkz/requivo/releases/tag/v0.9.1
[0.9.0]: https://github.com/jbkkz/requivo/releases/tag/v0.9.0
[0.8.2]: https://github.com/jbkkz/requivo/releases/tag/v0.8.2
[0.8.1]: https://github.com/jbkkz/requivo/releases/tag/v0.8.1
[0.8.0]: https://github.com/jbkkz/requivo/releases/tag/v0.8.0
[0.7.0]: https://github.com/jbkkz/requivo/releases/tag/v0.7.0
[0.6.3]: https://github.com/jbkkz/requivo/releases/tag/v0.6.3
[0.6.2]: https://github.com/jbkkz/requivo/releases/tag/v0.6.2
[0.6.0]: https://github.com/jbkkz/requivo/releases/tag/v0.6.0
[0.5.0]: https://github.com/jbkkz/requivo/releases/tag/v0.5.0
[0.4.0]: https://github.com/jbkkz/requivo/releases/tag/v0.4.0
[0.3.0]: https://github.com/jbkkz/requivo/releases/tag/v0.3.0
[0.2.0]: https://github.com/jbkkz/requivo/releases/tag/v0.2.0
[0.1.0]: https://github.com/jbkkz/requivo/releases/tag/v0.1.0
