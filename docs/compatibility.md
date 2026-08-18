# Compatibility and deprecations

> What Requivo promises not to break, what it may change, and what is on the way out.

Requivo is pre-1.0 and versioned with [SemVer](https://semver.org/). Until 1.0, minor versions may
change interfaces — but not silently, and not the session format. This page is the specific list.

## The session format is public

`.requivo/sessions/<slug>/` is the interface between the CLI, the Claude Code plugin, the Web app, and
anything you build on top. It is a **published contract**, not an implementation detail: the layout is
documented in [session-format.md](session-format.md), and a session written by one interface is
readable by all the others and by later versions.

Concretely, guaranteed:

- The directory layout: `session.json`, `request.md`, `model.json`, `revisions/NNNN-model.json`,
  `artifacts/`.
- `model.json` is a serialized model whose slot ids come from `framework/model_schema.json` — the same
  vocabulary `requivo schema` prints.
- `session.json` carries `format_version`. Today it is **1**.
- **A session written by an older Requivo keeps loading.** A field that has been retired is ignored
  rather than fatal; a field added since takes its default. Both are pinned by a test that loads a
  frozen 0.8.2 `session.json` verbatim.
- **A session written by a newer Requivo is refused, clearly** (`invalid_session`, "upgrade requivo")
  rather than half-understood.
- **An older Requivo preserves a field it does not understand.** Since 0.9.4, an unknown key in
  `session.json` survives a round-trip: the older reader loads the session, mutates it, writes it back,
  and the newer version's field is still there. This is stronger than "readers ignore what they do not
  know", and it is what makes adding a field genuinely safe — before 0.9.4 the unknown key was dropped
  the first time an older Requivo wrote the file, so a mixed-version workspace quietly destroyed it.
  Keys that Requivo has *retired* are the deliberate exception: those are dropped, in `migrate_session()`.

What may change without a `format_version` bump:

- **Adding** a field, anywhere. Readers ignore what they do not know, and preserve it on write.
- **Retiring** a field that was never populated (this is how the unused session-level
  `prompt_versions` map was removed in 0.9.2).
- Adding a slot to the schema, a new artifact type, or a new value in a provenance field.

What requires a `format_version` bump, an entry in the changelog, and a migration in
`migrate_session()`:

- Renaming, removing or changing the meaning of a *populated* field.
- Changing the directory layout or a file's role.
- Any change that would make an older session read *incorrectly* rather than merely incompletely.

Slot ids are a separate contract with its own `schema_version`, recorded on every session — and, since
0.9.5, actually enforced: a session authored against a *newer* slot schema is refused with the same
clarity as a newer `format_version`, instead of failing later as an `unknown_slot` error naming a slot
the user never typed. An older `schema_version` keeps loading.

## The `--json` outputs are public

`requivo status --json`, `model apply --json`, `model diff --json`, `artifact list --json`, `doctor
--json` and the structured error envelope (`{code, message, path?, details?}`) are what the Claude Code
plugin drives. They follow the same rule as the session format: fields get added, populated fields do
not change meaning without a note in the changelog.

Error `code` values are stable identifiers — assert on the code, never on the message text.

One populated field has changed meaning under that rule, and the changelog carries the note:
`doctor --json`'s `sessions.total` is `null`, not `0`, when the session directory could not be read
at all. It reported `0` before, which was indistinguishable from an empty workspace — a reader
gating on `total == 0` was being told "you have no sessions" by a check that had not managed to look.
The sibling `sessions.readable` says which case you are in.

## What a proposal means

The JSON you hand to `model validate`, `model diff` and `model apply` is a **proposal**, and since
0.9.6 its shape is stated rather than implied:

- `model` is the complete slot set. An apply *replaces* the model — it does not merge — so a partial
  one is refused. Check a projection with `model validate --allow-partial`; that flag is gone from
  `apply` and `diff`, where it read as "apply a patch" and silently replaced a fifteen-slot model
  with whatever subset was sent.
- `summary.objective` must say something. A model of filled slots with nothing naming what it is for
  renders as a blank heading everywhere, so it is refused as incomplete (`invalid_model`).
- `decisions`, `challenges` and `opportunities` are **tri-state**: leaving the key out means "not
  speaking to it" and keeps what is established; `[]` deletes; a list replaces. Before 0.9.6 an
  omission was read as an empty list, so an ordinary refinement turn deleted the whole reasoning
  layer — and reported no change while doing it.

## Deprecations

| What | Status | Since | Removal | Instead |
|---|---|---|---|---|
| **`model apply --allow-partial`, `model diff --allow-partial`** | Removed — it merged nothing, it replaced | 0.9.6 | gone | `model validate --allow-partial` to check a projection; send the full slot set to apply |
| **Legacy flag CLI** (`python src/engine.py "…" --prd`) | **Removed** | deprecated 0.9.2 | 0.9.8 | `requivo discover` + `requivo prd` — see [cli.md](cli.md) |
| **`pc` command alias** | **Removed** | deprecated 0.7.0 (rename) | 0.9.8 | `requivo` |
| **Implicit `out/<slug>/` fallback** | **Removed** — migration is explicit | deprecated 0.8.0 | 0.9.8 | `requivo session migrate`, then `.requivo/sessions/` |
| **`/requivo-<skill>` plugin skill names** | Renamed | 0.9.2 | gone | `/requivo:<skill>` — Claude Code namespaces plugin skills |

The policy: anything deprecated keeps working for at least one minor version, says so when used where
that is possible, and names its replacement here. Nothing is removed in a patch release.

**The 0.9.8 removals.** These three were the last of the pre-store architecture, all deprecated for
several versions and none of them with a known user. They were carried on a "removal in 1.1.0" plan,
which would have meant maintaining them *through* 1.0 — the moment you least want
two answers to "where does a session live?". The public interface is unchanged: `requivo` and the
session store were already the only supported path. If you have `out/` sessions, `requivo session
migrate` still converts them, and it is now the only thing that reads that layout.

## What is explicitly *not* stable

- **Python internals.** `requivo.core`, `requivo.services` and `requivo.providers` are importable and
  documented, but they are the engine's own structure, not a published API. A refactor can move them.
  A downstream consumer that depends on them deliberately tracks the repo.
- **Prompt and context-card content.** These are tuned continuously — that is the point of the
  [golden harness](evaluations.md). Two versions can reason differently about the same request; the
  provenance recorded on each revision (model + prompt hash) is what makes that traceable.
- **Terminal output layout.** Parse `--json`, never the rendered view.
