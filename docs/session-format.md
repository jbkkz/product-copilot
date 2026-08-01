# Session format

> Where and how a session is stored. For what the model contains, see
> [requirements-model.md](requirements-model.md).

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

Each applied revision records **who produced it** — provider, model name, the reasoning surface
(`cli-discover`, `web-answer`, a Claude Code turn…), the previous revision, and a content hash — in a
`revisions` log in `session.json`. Provenance belongs to the revision, not just the session, because a
model is moved by more than one surface over its life.

Updates go through the single validated apply path and support an **optimistic-locking** precondition:
`requivo model apply <slug> proposal.json --expected-revision N` fails cleanly with a
`revision_conflict` if the session has moved on, instead of silently overwriting a concurrent change.

## Slugs

A slug names the session directory, so it is validated in the Core: strict kebab-case
(`^[a-z0-9]+(?:-[a-z0-9]+)*$`), no path separators or dot segments. An explicit `--slug ../../escaped`
is rejected before any path is built.

## Legacy `out/` sessions

Before this layout, sessions lived in `out/<slug>/`. Those are **read-only** and migrated into
`.requivo/sessions/` on first change — or in bulk with `requivo session migrate`. New work always uses
`.requivo/`.
