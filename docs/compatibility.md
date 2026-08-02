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

## Deprecations

| What | Status | Since | Removal | Instead |
|---|---|---|---|---|
| **Legacy flag CLI** (`python src/engine.py "…" --prd`) | Deprecated, frozen, prints a notice | 0.9.2 | **1.1.0** | `requivo discover` + `requivo prd` — see [cli.md](cli.md) |
| **`pc` command alias** | Deprecated alias for `requivo` | 0.7.0 (rename) | next major | `requivo` |
| **Legacy `out/<slug>/` sessions** | Read-only, migrated on first write | 0.8.0 | no date | `.requivo/sessions/`; bulk-convert with `requivo session migrate` |
| **`/requivo-<skill>` plugin skill names** | Renamed | 0.9.2 | gone | `/requivo:<skill>` — Claude Code namespaces plugin skills |

The policy: anything deprecated keeps working for at least one minor version, says so when used where
that is possible, and names its replacement here. Nothing is removed in a patch release.

## What is explicitly *not* stable

- **Python internals.** `requivo.core`, `requivo.services` and `requivo.providers` are importable and
  documented, but they are the engine's own structure, not a published API. A refactor can move them.
  requivo-cloud depends on them deliberately and tracks the repo.
- **Prompt and context-card content.** These are tuned continuously — that is the point of the
  [golden harness](evaluations.md). Two versions can reason differently about the same request; the
  provenance recorded on each revision (model + prompt hash) is what makes that traceable.
- **Terminal output layout.** Parse `--json`, never the rendered view.
