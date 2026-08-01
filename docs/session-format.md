# Session format

> Where and how a session is stored. For what the model contains, see
> [requirements-model.md](requirements-model.md). For what is guaranteed not to break, see
> [compatibility.md](compatibility.md) — this layout is a **published contract**, at
> `format_version` 1.

A session is a directory under your **workspace** (the current directory, or `--workspace` /
`REQUIVO_WORKSPACE`). It is local, versioned, and shared by every interface — the CLI, the Claude Code
plugin and the Web app all read and write the same layout.

## Layout

```text
.requivo/
└── sessions/
    └── <slug>/
        ├── session.json        metadata + provenance + artifact status
        ├── request.md          the originating request
        ├── model.json          the current model — the durable product
        ├── revisions/
        │   └── 0001-model.json  one frozen file per applied revision
        └── artifacts/           generated views (PRD, assessment, …)
```

- **model.json** is the product; every artifact is regenerated from it.
- **revisions/** freezes each applied model, so history is inspectable and `requivo impact` can reason
  from a past point.
- Every write is atomic (temp file + rename), so an interruption can't leave a half-written model.

## Revisions and provenance

Each applied revision records **who produced it**, in a `revisions` log in `session.json`:

| Field | What it answers |
|---|---|
| `provider`, `model_name` | which engine reasoned |
| `prompt_version` | `sha256:…` of the exact system prompt — prompt file + schema + the context cards actually selected |
| `surface` | which interface asked (`cli-discover`, `web-answer`, `cli-brief`, a Claude Code turn…) |
| `previous_revision`, `created_at` | where it sits in the history |
| `model_hash` | content identity of the model that was written |

Provenance belongs to the revision, not the session, because a model is moved by more than one surface
over its life. The prompt hash is there because behaviour is tuned by editing prompts and context
cards: "which model produced this" answers half the question, and the other half is what changed
between two runs that look identical in the log.

Updates go through the single validated apply path and support an **optimistic-locking** precondition:
`requivo model apply <slug> proposal.json --expected-revision N` fails cleanly with a
`revision_conflict` if the session has moved on, instead of silently overwriting a concurrent change.
Provider-backed operations set it for you — a generation or an answers turn holds the revision it read,
so a change that lands while the provider is reasoning is a clean conflict rather than a lost update.

## Artifacts and freshness

`session.json` tracks each generated artifact: its file, when it was written, the **source revision**
it was generated from, and a `stale` flag.

The source revision is *provenance*, not a verdict. An artifact is stale when a slot it rests on
actually changed — computed from the dependency graph at apply time — not because the session has moved
past its source revision. An old artifact whose inputs never moved is still fresh, and every surface
reports the flag rather than comparing numbers.

## Stable identifiers

Design decisions, challenges and opportunities each carry an `id` (`dec_…`, `chl_…`, `opp_…`) derived
from their own content and recomputed on every validation. It is the same value across revisions,
surfaces and machines for as long as the statement is unchanged, so a decision can be referred back to
without quoting its text. A supplied id is never trusted — it is always recomputed, so neither a model
nor a hand-edited session file can invent an identity. A reworded statement gets a new id; nothing in
the data says the rewording preserved the intent.

## Slugs

A slug names the session directory, so it is validated in the Core: strict kebab-case
(`^[a-z0-9]+(?:-[a-z0-9]+)*$`), no path separators or dot segments. An explicit `--slug ../../escaped`
is rejected before any path is built.

## Legacy `out/` sessions

Before this layout, sessions lived in `out/<slug>/`. Those are **read-only** and migrated into
`.requivo/sessions/` on first change — or in bulk with `requivo session migrate`. New work always uses
`.requivo/`.
