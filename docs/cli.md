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

A selector — `--context a,b`, or the slot names given to `impact` — is checked rather than best-guessed.
An **empty** name is refused: `requivo impact <slug> ""`, which is what an unset shell variable expands
to, used to match every label and report the whole model as changed with nothing in the output to say
the input was malformed. A slot name that matches nothing is listed as unmatched and the rest still
resolve; an unknown *card* is a hard error, since dropping it would silently load every card instead of
the ones you asked for. Pass no selector at all to select everything deliberately. See
[context-cards.md](context-cards.md#scoping-a-session-to-relevant-cards).

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
| `requivo doctor [--json]` | Environment + install check (see [What `doctor` answers](#what-doctor-answers)) |
| `requivo schema` / `requivo context` | Inspect the slot schema / available context cards (`context --session <slug>` for exactly the cards that session uses) |
| `requivo session init\|list\|show\|verify\|migrate\|export\|import` | Session lifecycle (`verify` checks a session against itself; `import --force` to replace a session of the same slug) |
| `requivo model show\|validate\|apply\|diff <slug>` | Inspect and mutate a model through the validated path (`model apply --expected-revision N` for optimistic locking) |
| `requivo artifact save\|list\|show <slug>` | Record and read generated artifacts (`save --revision N` is required — the revision the content was reasoned from is the one fact only the caller holds) |

The deterministic verbs and `--json` outputs are what the Claude Code plugin drives — Claude reasons,
these apply.

### What `doctor` answers

`doctor` is the verb whose only job is *is anything wrong*, so every check it makes has three
answers, not two: it passed, it failed, or **it could not be made**. The third is the one that used
to be missing, and a check that reports "nothing found" when it could not look is worse than no check
at all.

| `--json` field | Reads |
|---|---|
| `schema.ok` / `schema.slots` / `schema.error` | The slot schema loaded, and how many slots it defines |
| `context.status` | `ok`, `empty` (the install has no context cards) or `unreadable` (a card directory exists but could not be enumerated — permissions, usually). `context.ok` is true only for `ok` |
| `context_cards` | The card names themselves — the plain list it has always been |
| `sessions.readable` / `sessions.total` / `sessions.error` | Whether the session directory could be listed at all. When it could not, `total` is `null` rather than `0`, because *no sessions* and *we could not look* are different answers and a user told the first concludes their sessions were deleted |
| `sessions.inconsistent` | `{slug: [integrity codes]}` — run `session verify <slug>` on each |
| `sessions.unresolved_cards` | `{slug: error}` for a session whose saved context cards no longer resolve here |
| `sessions.cards_checked` | False when the card directory itself was unreadable, so `unresolved_cards` being empty means nothing |

An `empty` context is a broken install rather than a quiet inconvenience: the cards are what impact
is estimated against, and impact is half of `information_value = uncertainty × impact`. Discovery
would still run and still produce a model — it would just ask duller questions, for a reason nothing
on screen would name.

### Context cards a session can no longer find

A session records the context cards it was created with, and those cards live **outside** the
session — in the installed package, or in `REQUIVO_CONTEXT_DIR`. Rename a card, replace an install,
or open the session on another machine, and the saved selection no longer resolves. Since that is now
a refusal rather than a silently empty context, such a session is stopped at its next reasoning turn,
which is a paid call minutes into a conversation.

Both health verbs now say so first, offline: `doctor` lists the sessions under
`sessions.unresolved_cards`, and `session verify <slug>` reports it in a `context_cards` block and
exits non-zero. Recovery is to put the card back, or to point `REQUIVO_CONTEXT_DIR` at wherever it
now lives.

It is deliberately **not** a session-integrity problem. `session verify`'s `problems` list, and
`check_session_dir` behind it, answer whether a session directory tells the truth *about itself* —
and a card is not in the directory. Reporting a lost card there would make the same session coherent
on one machine and broken on another, and would make `session import` (which refuses an archive on
integrity problems) reject a colleague's perfectly good session because you lack one of their cards.
So the two are reported side by side and named differently: `problems` is internal, `context_cards`
is environment. Both count towards the top-level `ok`, because either one means the session cannot
take another turn.

### A card name cannot write a line of the receipt

A session's `context_cards` is caller text that **persists**, and `session import` accepts an archive
without inspecting it — deliberately, because a card lives outside the session directory and its
absence is a fact about your machine, not about the archive. Both health verbs then render those
names into their output.

Until 0.10.0 they rendered them bare, so a name containing a newline did not merely look odd: it
ended the line and started a new one at whatever column it chose. A session could print `doctor`'s
own `sessions` row, byte-identical in shape and column, saying *all clear* directly beneath the row
that was reporting it — and forge `session verify`, the verb whose entire job is to say whether a
session is telling the truth, while `verify` still exited 1 (#40).

A selector token carrying a control character is now **refused** rather than displayed, at the one
function every selector passes through, and reported as `unsafe_selector_token`. The refusal names
the offending value in escaped form, on one line. Two consequences worth knowing:

- `doctor` and `session verify` report such a session as unhealthy and name it. The finding is not
  dropped — it is shown, quoted and escaped, so you can see exactly what is stored.
- `--json` output is unaffected and keeps the bytes verbatim. The escaping is a property of rendering
  to a terminal, not of the finding, and JSON already carries a newline safely.

`session show` prints the stored names without selecting anything with them, so no refusal can run
there; it renders each through the same one-line rule instead. A name that was already safe is
printed byte-for-byte, so ordinary output is unchanged.

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
