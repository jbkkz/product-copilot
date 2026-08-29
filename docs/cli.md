# CLI reference

> Every `requivo` command. For a first run, see [getting-started.md](getting-started.md).

Run as `requivo <command>` after an install, `uv run requivo <command>` with uv, or
`python scripts/requivo_cli.py <command>` from a bare clone. Commands that call the Anthropic API need
the `anthropic` extra and `ANTHROPIC_API_KEY`; everything else is offline.

Verbs take a session **slug**. `status` and `impact` also accept a path to a saved `model.json`, so
they can read a model that is not in a session store; every other verb resolves a session, because it
writes a revision or an artifact back into one.

## Discovery and refinement

| Command | Does |
|---|---|
| `requivo discover <request\|file>` | Analyse a request and create a session (interactive; `--once` for a single pass, `--context a,b` to scope cards) |
| `requivo answer <slug> "<answers>"` | Fold answers in and refine the model one more turn |
| `requivo status <slug>` | Understanding checklist + readiness (`--json` for a machine snapshot). No network |
| `requivo impact <slug> [slots…]` | What rests on given slots — decisions to re-validate + artifacts that go stale (no slots = full map). No network |

The context-card selector is spelled **`--context`** everywhere — on `discover`, on `session init`,
on `session rescope` and on `context`. `--cards` is a permanent alias of it on all four, kept because
`context` spelled it that way first (#85); the two are one option, so they can never mean different
things.

A selector — `--context a,b`, or the slot names given to `impact` — is checked rather than best-guessed.
An **empty** name is refused: `requivo impact <slug> ""`, which is what an unset shell variable expands
to, used to match every label and report the whole model as changed with nothing in the output to say
the input was malformed. A slot name that matches nothing is listed as unmatched and the rest still
resolve; an unknown *card* is a hard error, since dropping it would silently load every card instead of
the ones you asked for. Pass no selector at all to select everything deliberately. See
[context-cards.md](context-cards.md#scoping-a-session-to-relevant-cards).

**`discover` claims its session before it reasons**, on both the interactive path and `--once`. Two
consequences, and both are the point:

- Running `discover` again on a request whose session already carries a model is refused
  (`revision_conflict`) **before any API call**, not after the turns you paid for — a fresh discovery
  reasons from the request alone, so it would replace that work rather than refine it. Use
  `requivo answer <slug> "…"` to refine, or a different slug for a genuinely separate discovery.
- **Nothing you pay for is discarded after the session is claimed.** Stopping an interactive
  discovery early — `q`, an empty answer, Ctrl-C — keeps the turns that already ran: the session
  lands at revision 1 with its questions still open, which is exactly what `--once` produces, and the
  command names the `requivo answer <slug> "…"` that continues it. A provider failure part-way
  through the loop does the same, reporting the error alongside what was saved. Only a failure on the
  *first* turn leaves revision 0, because there is nothing to keep — that is the state
  `requivo session init` produces, and there `discover` is still the right retry.

## Artifact generators (provider-backed)

Each is a view of the saved model: `requivo <verb> <slug>`.

| Command | Produces |
|---|---|
| `requivo brief <slug>` | The decision brief — what to review before estimating |
| `requivo prd <slug>` | Product Requirements Document |
| `requivo stories <slug>` | User stories |
| `requivo criteria <slug>` | Given/When/Then acceptance criteria |
| `requivo estimate <slug>` | Uncertainty-aware estimate (derives stories first) |
| `requivo epic <slug> [--export-json] [--github] [--gitlab]` | Delivery epic + optional tracker issue plans and a tool-neutral `epic.json` |
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
| `requivo session init\|list\|show\|verify\|rescope\|migrate\|export\|import` | Session lifecycle (`verify` checks a session against itself; `rescope` re-scopes an existing session's context cards, see [context-cards.md](context-cards.md#re-scoping-an-existing-sessions-cards); `import --force` to replace a session of the same slug) |
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
| `sessions.non_sessions` | What is under the session root and is **not** a session — see [Something here that is not a session](#something-here-that-is-not-a-session). `null`, not `[]`, when the root could not be listed |
| `sessions.unexaminable` | Names under the session root that could **not be examined**, so whether they are sessions is unknown — `name` and `error` per entry. Not folded into `non_sessions`, which states a fact, nor into `total`, which stays what could be confirmed. `null`, not `[]`, when the root could not be listed. See [Something here that could not be examined](#something-here-that-could-not-be-examined) |
| `locks.readable` / `locks.total` / `locks.error` | Whether `.requivo/locks/` could be listed at all, and how many `<slug>.lock` files it holds (#180) |
| `locks.sessions_checked` / `locks.unmatched` | Which of those slugs currently name no session — candidate residue from a hand-deleted one, since there is no `session delete` verb. `unmatched` is `null`, not `[]`, when the *current session list* itself could not be read, on the same reasoning as `sessions.cards_checked` |
| `locks.unexpected` | Names under `.requivo/locks/` that are not a `<slug>.lock` file `session_lock` could have written — a stray file, a directory, a symlink. `null`, not `[]`, when the lock root could not be listed at all |
| `locks.unexaminable` | Entries under `.requivo/locks/` whose examination raised — `name` and `error` per entry, on the same terms as `sessions.unexaminable`. `null`, not `[]`, when the lock root could not be listed |
| `output.streams[].state` | `safe` (a character the console cannot encode is escaped visibly, never fatal), `lossy` (it cannot crash but drops or blanks the character with no mark — only reachable by setting `errors=replace`/`ignore` yourself), `will_crash` (a strict handler on a narrow codec, so a glyph would kill the command mid-report) or `unknown` (the stream does not expose a codec, so this check could not look) |

An `empty` context is a broken install rather than a quiet inconvenience: the cards are what impact
is estimated against, and impact is half of `information_value = uncertainty × impact`. Discovery
would still run and still produce a model — it would just ask duller questions, for a reason nothing
on screen would name.

### Something here that is not a session

A directory under `.requivo/sessions/` with no `session.json` is invisible to everything that lists
sessions — which, until 0.10.0, was everything. `list_session_slugs` filters on `session.json`;
`doctor` and `session verify` both reason over the slugs it returns; and `check_session` answers about
a directory it is handed, which nobody could hand it a name for. The commonest source is an older
Requivo: `session_lock` used to create the session directory in order to open `.lock` inside it, so
locking a slug with no session left one behind (#22). Those are still on disk.

Nothing in this version can produce one. The refusal #22 added stopped new ones, and the lock no
longer lives inside a session at all — it is at `.requivo/locks/<slug>.lock` — so `session_lock`
cannot create a directory under `sessions/` even in principle. What follows describes what is
**found** on disk, not what Requivo makes.

The symptom is not where the cause is. **The name is taken**, and `create_session`'s rename is the
only claim on a slug — it loses to anything already occupying the name, after which the CLI falls
through to its hash-suffixed candidate. Ask for `leave-approval` and you get `leave-approval-a1b2c3`,
silently, with nothing anywhere explaining why the name you asked for was unavailable. A stray *file*
at a slug name costs exactly the same and is reported the same way.

`doctor` names them, under `sessions.non_sessions` and on a row of its own (#67):

```
  ✅ sessions        0 in this workspace
  🟡 other entries   1 entry under this directory that Requivo does not read
     └─ leave-approval — a directory holding 1 entry: .lock  [name taken]
     [name taken]: a new session asked for that name will not get it. …
```

`doctor` rather than `session verify`, because `verify` is per-session and takes a slug — and the
defining property of one of these is that no listing produces its name, so there is no slug to type.
`session list` still shows only sessions: a listing of sessions must not grow a row for something
that is *established* not to be one.

**It is a report, not a repair.** Requivo does not delete, move or rewrite anything here, and it does
not say what these are. A directory holding only `.lock` is almost certainly a leftover lock, and
almost certainly is not enough: a half-extracted archive and an interrupted copy are the same shape
from the outside, and this project's rule is that the evidence is the directory and only the
directory. So each entry carries what was found — `name`, `kind` (`directory` / `file` / `symlink` /
`other` / `unknown`), `entries` (up to five names) and `entry_count` — and one derived flag,
`slug_shaped`, which is a property of the *name*: whether `create_session` can be asked for it at
all, and so whether the entry costs anybody anything. A name too long to be a slug is `false` there,
because `canonical_dir` refuses such a name outright and loudly rather than substituting silently.
There is no field spelling a conclusion.

A **symlink is not followed**. It would otherwise be reported as whatever it points at, and the
listing beneath it would carry that target's filenames into a report about your workspace.

Three states here too. `entries: null` with an `error` means that directory could not be listed —
never `[]`. On Linux and macOS an empty directory is the one shape that costs nothing at all, because
`rename(2)` replaces an empty destination and the session still gets the name it asked for; on Windows
it does not, so an empty directory is still reported and still marked `[name taken]`. `entries: null`
with no error is a `file` or an `other`: there is nothing to look inside. And the whole key is
`null` when the session root itself could not be listed, where an empty list would read as *we looked
and there is nothing else here*.

Dot-prefixed entries are never reported: a slug cannot start with a dot, so they are `create_session`
staging directories — a session in flight, not something left behind.

### Something here that could not be examined

A third state, and the sentence about what is *established* above is what it turns on (#80). Deciding
whether a name is a session means asking whether `<name>/session.json` is there — and that question
can itself fail. A directory the process cannot stat into (mode `000`, or one owned by another user)
answers neither yes nor no, and that failure used to escape the partition and take **every** entry
with it: `session list` exited 1 with an empty listing and a raw `PermissionError`, and every healthy
session in the workspace was invisible.

Such an entry is now its own answer, because both of the others would be claims nobody established.
Calling it a non-session hides it from `session list` — the invisible entry the section above is
about, one step along. Calling it a session says it *is* one, which is exactly what could not be
checked. So it reaches both surfaces as *we could not tell*:

```
  🟡 sessions        1 in this workspace · 1 entry that could not be examined
     └─ blocked — could not be examined: [Errno 13] Permission denied: …/sessions/blocked/session.json
     Requivo cannot tell whether this is a session, so the count above (1) is what it could confirm,
     not what is there. …
```

The count stays what could be **confirmed**. Absorbing the entry into it would trade a correct number
for a vague one, which is the same trade `other entries` declines above. In `--json` it is
`sessions.unexaminable`, one object per entry with `name` and `error`, and `null` rather than `[]`
when the root could not be listed at all — the same reading as its neighbour.

`session list` gives it a degraded row and exits **4**: the entry is named, every healthy session is
still listed in full, and the row states nothing it could not read. `doctor` keeps its *whole root
unreadable* arm for the case that genuinely is the whole root — `iterdir()` on `.requivo/sessions/`
itself failing. One entry failing is not that, and answering it that way was a claim broader than
what happened.

**A report, not a repair here too.** Requivo reads your workspace; it does not change permissions in
it.

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

That rule now covers **every string `session show` prints**, not just the card names (#70). The card
selection was the field #40 happened to be about; `slug`, `session_id`, `created_at`,
`updated_at`, `provider`, `model_name`, and each artifact's type and filename are read back out of
the same file and were reaching the terminal bare. `session list` was fixed for three of them in #62
and this is the same defect in the other verb — with a sharper edge, because every line `session
show` prints is one Requivo writes itself at a fixed column, so a forged `  revision 0` under a
session that is at revision 12 is indistinguishable from the real thing. `current_revision`, an
artifact's `revision` and its `stale` flag need nothing: they are typed `int` and `bool`, so
`read_meta` refuses a string there before the render runs.

`artifact list` prints two of the same fields — the `artifact_status` key and the `filename` — and
is escaped alongside it. Found by sweeping the class rather than the instance: escaping a stored
value in one of the two verbs that render it leaves the rule meaning *wherever somebody looked*.

`--json` is unaffected on all three verbs, and the reason is narrower than it looks. JSON's grammar
forbids a literal control character below `U+0020` inside a string, so a newline is escaped
regardless of any encoder option. `json.dumps`' `ensure_ascii=True` default is what covers the rest
of the guarded range, `U+007F`–`U+009F` — `NEL`, a line terminator, and `CSI`, an escape introducer.
Both halves are pinned by test, because a test probing only with a newline would be green with that
default turned off.

**What the terminal rule does not cover**, stated because the two are easy to assume identical. The
one-line rule guards C0, DEL and C1 — the class that can move a terminal's cursor or end its line.
`str.splitlines()` breaks on a wider set, including `U+2028` and `U+2029`, which are returned
unchanged. On a terminal that is right: xterm and the VT sequences behind it answer to CR and LF, not
to Unicode `Zl`/`Zp`. It matters if you parse this human-readable output line by line — don't; that
is what `--json` is for, and `--json` escapes those two as well, which makes it the stricter of the
two paths.

### What `artifact list --json` answers

```json
{"slug": "leave-approval",
 "artifacts": {"prd": {"revision": 3, "filename": "prd.md",
                       "updated_at": "2026-08-20T14:25:28Z", "stale": false}}}
```

The rows live under `artifacts`, keyed by type, and `stale` is the dependency graph's verdict — not
a comparison of `revision` against the session's current one, which is provenance (see
[dependencies and staleness](requirements-model.md#dependencies-and-staleness)).

`slug` is the name you asked under, not the one stored inside `session.json` — the same reading
`session verify` and `session import` give it, and the safe side of a value that is untrusted on
every read back.

A session with nothing saved answers
`{"slug": …, "artifacts": {}}`, which states which session was asked about; it used to answer `{}`,
which a consumer could not tell from a payload that failed to serialise.

**This payload used to be the bare inner map** (#107) — `{"prd": {…}}`, top level keyed by artifact
type. **Breaking**, same class as #87 and #84: `jq '.artifacts'` where you had `jq '.'`. The rows
are untouched. The reason is #87's, one shape along — a top level made of data cannot gain a field,
because the consumer read is `for t, info in payload.items()` and any key added later is both
ambiguous with a future artifact type and breaks that loop.

### What `model apply` takes

A proposal replaces the model, so it carries the **complete** slot set and a non-empty
`summary.objective`. The three reasoning collections are the exception, and they are tri-state: leave
`decisions`, `challenges` or `opportunities` out and the established ones stand; send `[]` and they are
deleted (and what rested on them goes stale); send a list and it replaces. A refinement normally says
nothing about them. To check a partial projection without applying it, use
`model validate --allow-partial`. See [compatibility.md](compatibility.md#what-a-proposal-means).

### Exit codes, and what 3 and 4 mean

Requivo reads and writes UTF-8 throughout, whatever the machine's locale. A file *you* name —
`requivo discover ./brief.md`, `requivo model apply <slug> proposal.json` — must be UTF-8 too; one
that is not is refused by name, with the offending byte and its position, rather than decoded with
the locale's codec into something that would look like prose and be wrong.

On output, a console that cannot represent a character gets a visible backslash escape in its place,
rather than a crash or a silent hole — `backslashreplace`, deliberately not `replace`, because a
reader cannot tell a substituted question mark from a character that was never there. Where even that
is impossible — a stream Requivo could not reconfigure, which
`doctor` names — the command exits **3** instead of dying in a traceback:

| Exit | Means |
|---|---|
| 0 | Success |
| 1 | A clean, expected failure — an invalid proposal, a missing session, a provider error |
| 2 | Bad arguments (argparse) |
| 3 | **The command's work finished and its output could not be encoded.** The message says whether a provider call was billed |
| 4 | **The work was done and part of the answer was unreachable.** What was produced is on stdout in full |

Three exists because 1 would be a lie in the one case that costs money. `requivo brief <slug>` makes
its provider call, applies the revision and writes the artifact *before* it prints anything — so a
renderer that dies at the final `print` and reports failure invites a re-run that pays for a second
call and stacks a second revision on the first.

The message reads the run's usage ledger rather than assuming: it says a call **has** been billed
only when one was, because several verbs (`doctor`, `status`, `schema`) never call the provider at
all and `discover` prints before it does. Telling you not to re-run a command that cost nothing
would be the same misreport one layer up.

Four exists for the same reason one number along, and it describes a **shape of answer rather than a
verb** — it is not `session list`'s code, and a number minted per verb would rebuild the collapse it
was introduced to undo. Two commands reach it today.

`requivo session list` is the first. A session written by a newer Requivo, or one left half-written
by a crash, cannot be read — and it used to answer that by exiting 1 with a single message, **every
other session invisible and nothing naming which one was the problem**. It now lists every session
it can and gives the one it could not its own row:

```
Sessions under /work/.requivo/sessions:
  leave-approval                           rev 3  (anthropic, 2026-08-19T09:04:11Z)
  event-checkin                            could not be read — session format v2 is newer than this Requivo understands (v1) — upgrade requivo.

1 entry could not be read. `requivo session verify <slug>` reports what is wrong in full.
```

The footer counts **entries**, not sessions: since #80 one of these rows can be an entry nobody
could examine, and calling that a session is the one claim the third state exists to refuse.

The degraded row **names the session and states nothing it could not read** — no revision, no
provider, no timestamp. A plausible `rev 0` on a session nobody managed to open is a worse answer
than no answer. It keeps the underlying error text, because *written by a newer Requivo, upgrade* is
a remedy where a flattened *unreadable* is not. `session verify <slug>` is where the full story lives:
it reports an integrity code for each way a `session.json` can be refused — a newer `format_version`,
an unparseable file, a field of the wrong type.

Two things it cannot report on. A session directory whose *name* is not a valid slug, since it has no
slug to take; there the row's own line is already the whole answer. And an entry that could not be
**examined** — `session_exists` probes `session.json` the way the listing itself used to, so `verify`
still raises there rather than answering. That is a known gap with its own issue, not a state this
verb reports: for such a row the line in the listing is the whole of what Requivo can say today.

A session at **revision 0** is not this state. It has no model yet because nothing has analysed it,
which is a normal row and reads as one — *we could not look* and *we have not looked yet* are two
different answers, and only the first is a problem.

**A second condition reaches 4 on the same command** (#80), and it is one number further out than the
paragraph above: an entry under the session root that could not be *examined at all*, so whether it
is a session is unknown. That failure happened in the scan that produces the row set — before any row
existed to degrade — so it used to take the whole listing down rather than degrade one member. It is
now a degraded row like any other, and the same 4 covers it, because 4 describes the shape of the
answer and this is that shape. See
[Something here that could not be examined](#something-here-that-could-not-be-examined).

Neither 0 nor 1 is true of a listing with a hole in it, which is why it gets a number of its own:
0 says nothing is wrong, 1 says nothing was listed. Making it non-zero is safe precisely because
nothing is withheld — a script that only wants the rows still gets all of them on stdout, and
`--json` carries `readable` and `error` per row, plus a top-level `degraded` count, for a caller
that would rather branch than parse.

`requivo session verify` is the second, and it reaches 4 from the other side. It answers three
different things and had two exit codes:

| What happened | Exit |
|---|---|
| The session is internally inconsistent — a complete answer | 1 |
| Its product context was read and does not resolve — also complete | 1 |
| Its product context **could not be read at all** — not an answer | 4 |

The third already had a rendering of its own — *Could not check `<slug>`'s product context* — and
then exited 1 beside a session that really is broken, in the verb whose whole job is to say whether a
session is sound. Where both happen at once, the **firm negative wins**: a session that is
inconsistent *and* whose cards were unreadable exits 1, because a script gating on *is this usable*
wants the definite answer and there is one. `--json` carries the whole story at every code.

**`requivo doctor` exits 0 whatever it finds, and that is deliberate.** It looks like `verify`'s
sibling and is not one. `verify` is a **gate**: you run it to decide, and its exit code is the
decision. `doctor` is a **report** — it describes what is on this machine and never concludes what
it means, because the same directory can be a half-extracted archive or a leftover lock and nothing
in it says which. A report that exits non-zero is concluding. Harmonising the two would cost the one
verb that must not, so the difference is written down here rather than left to look like an
oversight: read `doctor`'s output, not its status.

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

**What a `--json` consumer branches on.** Every refusal here names the archive or the store, never a
model. Assert on the code, never on the message.

| Code | HTTP | The archive… |
|---|---|---|
| `unreadable_archive` | 400 | is not a readable `.zip` at all. `details`: `{archive}` |
| `invalid_archive` | 400 | opens, but is not shaped like an export. `details`: `{problem, …}` |
| `inconsistent_archive` | 400 | holds a session that fails the integrity check. `details`: `{slug, problems}` |
| `session_exists` | 409 | is fine; that slug is taken and `--force` was not passed. `details`: `{slug}` |
| `import_destination_occupied` | 409 | is fine; something that is **not** a session already sits at the slug's directory. `details`: `{slug, path}` |

`invalid_archive` covers seven conditions under one code because they share one remedy — *give me a
different archive*. `details["problem"]` is present on all seven and says which: `empty`,
`too_many_files`, `too_large`, `unsafe_entry`, `entry_outside_session_directory` or
`multiple_sessions`. The size and count arms add the numbers they quote (`{files, max_files}`,
`{bytes, max_bytes}`), the path arms add `{entry}` and the multi-session arm adds `{slugs}`; nothing
is padded to a common shape, so read the shape after you have branched on `problem`.

The first three are arms of `InvalidSessionError`, so `except InvalidSessionError` catches every
*archive* refusal without enumerating them. Before #101 the seven shape conditions and the occupied
slug all answered `invalid_model` — a code documented for a malformed *proposal*, which is not what
anyone hands `session import`.

Three more codes reach this verb and are about neither the archive's shape nor the store's state:
`session_not_found` when the path you named is not a file, `invalid_slug` when the archive's one
directory is named something that could not be a session, and `import_move_failed` (500) when the
validated session could not be moved into place — the archive was fine and the store refused it.

`import_destination_occupied` is the one that used to be reported as `import_move_failed`, and the
distinction is worth knowing because the remedies differ. The import claims a free slug by renaming
the extracted directory onto it, and that rename does not answer the same way everywhere: on POSIX
an **empty** destination directory is replaced silently, while on Windows it is `MoveFileExW`, which
refuses *any* existing destination directory. A stray `mkdir` at the slug therefore used to import on
macOS and Linux and fail on Windows with a message about a move that had nothing wrong with it. Both
platforms now refuse it by name, before the rename is attempted. `--force` does **not** lift this
refusal — it replaces a *session*, and the point of this code is that there is no session there. Move
or delete the directory yourself; the import never removes something it cannot interpret.

`session export` reads under the session's write lock, so an archive can never combine an old
`session.json` with a newer `model.json`, and it excludes any dot-prefixed entry — a scratch file
from an interrupted write, and a legacy `.lock` left inside a session by an earlier Requivo.
The write lock itself now lives at `.requivo/locks/<slug>.lock`, outside every session directory, so
a current session has nothing of the kind in it. See
[session-format.md](session-format.md#layout) for why it is out there: `session import --force`
renames the session directory, and a lock inside a directory being renamed is both meaningless
(the writers under it resolve by pathname) and impossible on Windows.

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
