# `elicitation.md` and `model_schema.json` stay hand-kept

**Slug:** `elicitation-schema-hand-kept`

## Context

`src/requivo/assets/framework/elicitation.md` is the human-readable spec of the framework;
`model_schema.json` is the machine version fed to the model. CLAUDE.md's Extending section asks a
person adding or renaming a slot to "keep them consistent" -- an unenforced, by-hand rule, in a repo
whose stated philosophy (invariant 8's own history, "completeness stated in two places... used to
state it separately, and drifted") is that this is exactly how such things go stale.

#278 asked whether that rule needs a guard. Measuring it rather than assuming an answer:

- **What elicitation.md actually names.** One slot by its literal schema id, `config_vs_custom`
  ("One slot is optional: `config_vs_custom`"). Everything else is prose: the four-pillar table
  lists all 15 slots by a **short label of its own choosing** -- "Current process", not
  `model_schema.json`'s `"Current process (as-is)"`; "Integrations", not "Integrations &
  notifications"; "Config vs custom", not "Config vs customization"; "Reporting", not "Reporting &
  visibility". The two documents were never meant to mirror each other's exact wording --
  `elicitation.md` deliberately shortens for a first-time human reader, and today's difference is
  editorial, not drift.
- **What a byte-level containment test (the issue's own proposed shape) would actually catch.**
  Comparing elicitation.md's prose labels against `model_schema.json`'s `label` field as written
  would fail immediately, on the current, correct state of both files -- not because anything is
  wrong, but because the two were never equal strings. Making it pass would need a hand-authored
  normalization or a hand-authored label→id mapping table, which is the same by-hand consistency
  problem restated one layer down, in test code instead of prose, with its own maintenance cost
  every time either label wording changes for readability alone.
- **What the one genuinely machine-checkable claim is worth.** `config_vs_custom` is the sole
  literal identifier elicitation.md names. It is also referenced by the same literal id in four
  context cards (`b2b-platform.md`, `financial-reporting.md`, `document-management.md`,
  `event-ops.md`) and `prompts/brief.md`, none of which has a consistency guard against
  `model_schema.json` either (checked: `schema_slot_ids()` is imported nowhere outside
  `core/`, `analysis.py` and the test files that already assert against the schema directly --
  grep confirms no test walks context cards or prompts for slot-id references). A guard scoped to
  `config_vs_custom` alone in `elicitation.md` would close the smallest possible slice of a wider,
  already-present gap while leaving the larger one -- and the larger one has never caused an
  incident either.
- **Has this actually drifted, ever?** No. `git log` on `elicitation.md` and `model_schema.json`
  shows no incident where the two disagreed and nobody noticed. The `completeness_gap()` history
  CLAUDE.md cites as the cautionary precedent is a different pair of files entirely (two live
  *code* consumers of one completeness rule, both actually enforced), not this doc/schema pair.
- **What a reader loses if it does drift.** `elicitation.md` is read by exactly one thing in the
  tree: `deterministic/doctor.py`'s `--framework` branch of `_cmd_schema`, printing it for a human
  or an agent running `requivo schema --framework`. Nothing else consumes it, nothing computes
  from it, and no artifact, session, or discovery call is affected by it going stale. The cost of
  drift is a confused reader of one CLI subcommand's prose -- not a corrupted model, a wrong
  answer, or a silent behavior change.

## Decision

No guard. `elicitation.md`'s consistency with `model_schema.json` stays hand-kept, and CLAUDE.md's
existing sentence in the Extending section stands as written ("Keep them consistent when adding or
renaming a slot"). This repo's own decision-record README names a "cost tradeoff with a threshold"
as one of the three shapes that belong here rather than in a test -- this is that shape: the
drift surface is one literal identifier plus prose paraphrase the two files were never meant to
share verbatim, no incident has occurred, and the blast radius of a miss is one human-facing CLI
subcommand's prose going momentarily stale, caught the next time anyone reads it.

`CLAUDE.md`'s Extending section is the natural place to point at this record (`decision:
elicitation-schema-hand-kept`), but `CLAUDE.md` is held by another change in flight as this is
written; that one-line addition is reported to the maintainer to land separately rather than
reached for here.

## What breaking it cost

Nothing yet, and no incident is on record for this specific pair -- which is the whole basis for
the decision above, not a gap in the writing of it. If `elicitation.md` and `model_schema.json` do
desync in a way that misleads a reader of `requivo schema --framework`, that is the concrete cost
that would reopen this decision, and it should be named here when it happens rather than argued
about in the abstract a second time.

## Alternatives rejected

- **A one-way containment test: every slot id/pillar name elicitation.md mentions must exist in
  model_schema.json.** As proposed in #278, this fails immediately against the current, correct
  files, because elicitation.md paraphrases rather than quotes. Making it pass needs a
  hand-authored normalization table mapping each prose label to a slot id -- which is the same
  by-hand consistency this issue set out to remove, moved into test code, with its own drift risk
  (a schema label edited for readability, with no id or pillar change at all, would need the test's
  mapping table edited in lockstep or it goes red for a change that broke nothing).
- **A narrower test scoped to `config_vs_custom` alone**, the one literal id elicitation.md
  actually names. Considered because it is genuinely cheap and mechanical. Rejected because it
  closes the smallest possible slice of a strictly larger, already-present, already-unguarded
  gap -- the same slot id is referenced literally in four context cards and one prompt, none of
  which this would touch -- and this repo's own meta-guard budget (CLAUDE.md, "Where a bug
  narrative lives") asks for a named incident before adding a guard, not merely a plausible
  mechanism. There is no incident here, for either the wide or the narrow version.
- **Generating `elicitation.md` from `model_schema.json`.** #278's own "Out of scope" already
  rejects this as over-engineering for a prose document meant to read naturally for a first-time
  human, and nothing found while writing this record changes that.
