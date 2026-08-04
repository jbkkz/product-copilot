# CLI reference

> Every `requivo` command. For a first run, see [getting-started.md](getting-started.md).

Run as `requivo <command>` after an install, `uv run requivo <command>` with uv, or
`python scripts/requivo_cli.py <command>` from a bare clone. Commands that call the Anthropic API need
the `anthropic` extra and `ANTHROPIC_API_KEY`; everything else is offline.

Most verbs take a session **slug** or a path to a saved `model.json`.

## Discovery and refinement

| Command | Does |
|---|---|
| `requivo discover <request\|file>` | Analyse a request and create a session (interactive; `--once` for a single pass, `--context a,b` to scope cards) |
| `requivo answer <slug> "<answers>"` | Fold answers in and refine the model one more turn |
| `requivo status <slug>` | Understanding checklist + readiness (`--json` for a machine snapshot). No network |
| `requivo impact <slug> [slots…]` | What rests on given slots — decisions to re-validate + artifacts that go stale (no slots = full map). No network |

## Artifact generators (provider-backed)

Each is a view of the saved model: `requivo <verb> <slug>`.

| Command | Produces |
|---|---|
| `requivo brief <slug>` | The decision brief — what to review before estimating |
| `requivo prd <slug>` | Product Requirements Document |
| `requivo stories <slug>` | User stories |
| `requivo criteria <slug>` | Given/When/Then acceptance criteria |
| `requivo estimate <slug>` | Uncertainty-aware estimate (derives stories first) |
| `requivo epic <slug> [--github] [--gitlab] [--json]` | Delivery epic + optional tracker issue plans and a tool-neutral `epic.json` |
| `requivo release <slug> [version]` | Client-facing release notes |

## Local browser interface

| Command | Does |
|---|---|
| `requivo web [--host --port --workspace --no-open --reload]` | Launch the local Web interface (needs the `[web]` extra). Binds to `127.0.0.1` by default. See [web.md](web.md) |

## Offline / deterministic verbs (no LLM, no key)

| Command | Does |
|---|---|
| `requivo demo` | Replay a bundled run — no key, no network |
| `requivo doctor [--json]` | Environment + install check |
| `requivo schema` / `requivo context` | Inspect the slot schema / available context cards (`context --session <slug>` for exactly the cards that session uses) |
| `requivo session init\|list\|show\|verify\|migrate\|export\|import` | Session lifecycle (`verify` checks a session against itself; `import --force` to replace a session of the same slug) |
| `requivo model show\|validate\|apply\|diff <slug>` | Inspect and mutate a model through the validated path (`model apply --expected-revision N` for optimistic locking) |
| `requivo artifact save\|list\|show <slug>` | Record and read generated artifacts (`save --revision N` for the revision it was reasoned from) |

The deterministic verbs and `--json` outputs are what the Claude Code plugin drives — Claude reasons,
these apply.

### What `model apply` takes

A proposal replaces the model, so it carries the **complete** slot set and a non-empty
`summary.objective`. The three reasoning collections are the exception, and they are tri-state: leave
`decisions`, `challenges` or `opportunities` out and the established ones stand; send `[]` and they are
deleted (and what rested on them goes stale); send a list and it replaces. A refinement normally says
nothing about them. To check a partial projection without applying it, use
`model validate --allow-partial`. See [compatibility.md](compatibility.md#what-a-proposal-means).

### Documents on stdin

Every command that takes a document accepts `-` in place of a path, and reads it from stdin:

```bash
requivo model apply <slug> - --expected-revision 3 --json <<'JSON'
{ "model": { … }, "questions": [], "summary": { "objective": "…" } }
JSON

requivo artifact save <slug> --type prd --file - --revision 3 --json < prd.md
echo "We need a leave approval system." | requivo session init -
```

This is what the Claude Code skills use. A caller that already holds the content should not have to
invent a file for it — the temp files the skills used to write were a shared path (two sessions
overwrote each other), used a filename that is illegal on Windows, and needed `rm` to clean up.

### Importing a session

`session import` validates before it writes anything: the archive must hold exactly one session
directory whose name is a valid slug, within a file-count and expanded-size ceiling, with no entry
that could escape the session root. It is then extracted to scratch space and put through the same
integrity check as `session verify` — the revision log accounts for the model, every revision file is
there and matches the hash recorded for it, the current model *is* the last revision, every artifact
has a file — and only then moved into place.

A slug that already exists is refused unless `--force`, and a forced replacement is a swap: the
existing session steps aside and is deleted only once the new one is in place, so a failure leaves you
with the session you had rather than neither.

`session export` reads under the session's write lock, so an archive can never combine an old
`session.json` with a newer `model.json`, and it excludes `.lock` and any scratch file.

## Sessions from the `out/` layout

Before the versioned session store, discovery wrote to `out/<slug>/`. Nothing has written there since
0.8.0, and since 0.9.8 nothing reads it implicitly either — the automatic fallback and the
migrate-on-first-write are gone, along with the flag CLI (`python src/engine.py …`) that produced that
layout.

One command remains, and it is the only thing that opens an `out/` directory:

```bash
requivo session migrate        # convert every out/<slug>/ session into .requivo/sessions/
```

It copies rather than moves — the originals stay where they are — and the converted model becomes
revision 1, with its artifacts recorded against it. A session still only in `out/` is reported as
missing with that command named in the error, rather than silently working at half capability.
