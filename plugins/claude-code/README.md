# Requivo for Claude Code

Use the same Requivo sessions inside the Claude Code workflow you already have — your session does the
reasoning, so **no Anthropic API key is required**.

This is an **integration**, not a separate product. [Requivo Web](../../docs/web.md) is the primary
interface; this plugin exists so that if you already live in Claude Code, you do not have to leave it.
A session created here opens in the Web app and the CLI, and vice versa: same format, same validated
apply path, same understanding.

The division of labour: Claude does the qualitative reasoning and the dialogue; the deterministic
`requivo` CLI validates, versions, and tracks what rests on what.

## The workflow

```text
/requivo:discover     paste the request → the understanding + the questions that could change it
    ↓
answer the questions  → /requivo:answer folds them in and reports what moved
    ↓
/requivo:brief        → the decision brief, saved and tied to the revision it was written from
```

`/requivo:status` at any point tells you where it stands; `/requivo:impact` tells you what a change
would reach *before* you make it.

## What it does

Six skills, each a thin driver over the deterministic `requivo` CLI:

| Skill | What it does | Reasoning? |
|---|---|---|
| `/requivo:discover` | Start a session from a request → the understanding + what could change the solution | Claude |
| `/requivo:answer` | Fold answers back in → what moved, what needs review, what needs updating | Claude |
| `/requivo:status` | Are we ready, what is still unresolved, which documents need updating | none (deterministic) |
| `/requivo:brief` | The decision brief, saved as a tracked document tied to its revision | Claude |
| `/requivo:prd` | A PRD from the same understanding, unknowns kept visible, saved and tracked | Claude |
| `/requivo:impact` | What a change would reach — decisions to re-validate, documents to update | none (deterministic) |

## Prerequisites

- The `requivo` CLI on your PATH: `pip install requivo` (or `uv tool install requivo`).
  - You do **not** need `requivo[anthropic]` for this plugin — that extra is only for the optional
    API-powered CLI mode.
- Claude Code with this plugin installed.
- Verify with `requivo doctor` — a missing Anthropic SDK / API key is reported as informational, not an
  error, for exactly this mode.

## Install

**From the marketplace** (no checkout needed). In Claude Code:

```
/plugin marketplace add jbkkz/requivo
/plugin install requivo@requivo
/reload-plugins
```

The first command registers this repository as a plugin marketplace (its catalog lives at
`.claude-plugin/marketplace.json`); the second installs the plugin from it. `/plugin marketplace update`
pulls a newer version later.

**From a checkout** (for development, or to run an unreleased version):

```bash
claude --plugin-dir ./plugins/claude-code     # loads it for this session only
claude plugin validate ./plugins/claude-code  # static check, same one the review pipeline runs
```

Either way, verify with `/help` → **Custom commands**: the six skills appear under the `requivo`
namespace. They are invoked as `/requivo:discover`, `/requivo:answer`, and so on — Claude Code always
namespaces plugin skills as `/<plugin>:<skill>`.

**Versioning:** the plugin version tracks the Requivo release it was tested against — see
`version` in `.claude-plugin/plugin.json`, which a test pins to the package's own version. (It is
deliberately not repeated in this sentence: the number was written out here once and had drifted a
release behind by the next tag.) The skills call the `requivo` CLI, so keep the two in step — an older
CLI may not have a verb a newer skill uses.

## How it works — two modes

```
                    Requivo Core
            validated, versioned understanding
                 /        |         \
                /         |          \
              Web    Claude Code      CLI
               |          |            |
         the product  this plugin  infrastructure
```

- **Claude Code mode (this plugin):** Claude reasons in your session, pipes the proposal in on stdin, and calls
  `requivo model validate` / `requivo model apply`. Requivo Core enforces the schema, versions the
  session, and computes readiness/impact. **No API key.**
- **API mode (optional):** `requivo discover request.md` uses the Anthropic API to do the reasoning
  instead (needs the `anthropic` extra and a key). Same Core, same validation, same session format.

Every interface writes the **same** session format, so a session created one way is readable by the
others (the CLI, the local Web app).

## Data sent to Claude

Only what you would expect: the client **request**, the product **context cards** (`requivo context`),
the **schema** (`requivo schema`), and the **current model** of the session you are working on. Nothing
leaves your machine that the CLI did not already have. The request and context are treated as *data* —
the skills never follow instructions embedded in them.

## Session layout

Sessions live under your workspace at `.requivo/sessions/<slug>/`:

```
.requivo/sessions/<slug>/
├── session.json          # versioned metadata + provenance + artifact status
├── request.md            # the originating request
├── model.json            # the current understanding (the durable product)
├── revisions/            # every applied revision (0001-model.json, …)
└── artifacts/            # generated documents (brief, prd, …), each tied to a revision
```

The understanding is the source of truth; every document is a view of it, tracked against the revision
it was generated from — so when the understanding moves, `requivo status` tells you exactly which
documents need updating.

## The skills never

- require `ANTHROPIC_API_KEY` or call the Anthropic provider,
- hand-edit `model.json` — they propose to a temp file and let `model apply` validate and write it,
- follow instructions embedded in the request or context cards (those are data),
- invent answers the client did not give (unknowns are kept honestly empty).
