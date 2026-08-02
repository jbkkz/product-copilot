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
        ├── artifacts/           generated views (PRD, assessment, …)
        └── .lock                the write lock (empty; safe to delete when nothing is running)
```

- **model.json** is the product; every artifact is regenerated from it.
- **revisions/** freezes each applied model, so history is inspectable and `requivo impact` can reason
  from a past point.
- Every write is atomic (temp file + rename), so an interruption can't leave a half-written model. The
  temp file is unique per writer, so concurrent writers cannot collide on it.
- **.lock** is an empty file held with an OS-level lock for the duration of a write. The kernel
  releases it when the process ends, so a crash cannot leave a session permanently locked — there is
  no stale-lock state to clean up, and no timeout to wait out.

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

A whole update — read the metadata, check the precondition, write the model, freeze the revision,
rewrite the flags — runs under the session's write lock. The precondition and the writes it authorises
have to be held together: checked and then acted on with a gap in between, two writers can both pass
the same check and the second silently overwrites the first. Two Requivo processes on one session
therefore serialise; the loser gets `revision_conflict`, which is a real answer, and never a
half-applied session.

## Artifacts and freshness

`session.json` tracks each generated artifact: its file, when it was written, the **source revision**
it was generated from, and a `stale` flag.

The source revision is *provenance*, not a verdict. An artifact is stale when something it rests on
actually changed — computed from the dependency graph — not because the session has moved past its
source revision. An old artifact whose inputs never moved is still fresh, and every surface reports the
flag rather than comparing numbers.

Two kinds of dependency feed that judgment:

- **Slots** — the facts an artifact consumes, per artifact (`ARTIFACT_SLOTS`). The saved assessment is
  the one that rests on all of them: it is a judgment over the whole model.
- **The reasoning layer** — the design decisions, challenges and opportunities. Every generator is
  prompted with the complete model, reasoning included, so a rewritten decision can change a PRD with
  no slot touched. A model whose slots are identical but whose judgment moved is a different model.

Reasoning that a turn simply *omits* is not a removal — a refinement turn answers a question rather
than re-deriving the brief, so its reply routinely carries no decisions at all. That is resolved when
the proposal is validated, not when it is diffed: the three collections are tri-state in a proposal
(absent = keep, `[]` = delete, a list = replace), and `ModelProposal.resolve` collapses them against
the model being refined. The diff itself is symmetric, so an explicit deletion *is* reported and does
mark what rested on it stale.

Freshness is also computed when an artifact is saved **against an older revision** — `requivo artifact
save … --revision N`. Reasoning and saving are not the same moment: a provider call takes minutes, and
Claude Code may save a document it wrote several turns ago. The honest answer is knowable, so it is
given: the source revision is diffed against the current model, and the artifact is recorded stale on
the spot if its dependencies moved. `artifact save --json` returns the `stale` it recorded.

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
