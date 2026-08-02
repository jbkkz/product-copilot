# Requivo for Claude Code

Turn a vague product request into a **validated, traceable requirements model** — using your existing
Claude Code session for the reasoning. **No Anthropic API key required.**

Requivo's thesis: *the model is the product; documents are views of that model.* This plugin lets
Claude Code drive that model: Claude does the qualitative reasoning and dialogue, and the deterministic
`requivo` CLI validates, versions, and tracks everything.

## What it does

Six skills, each a thin driver over the deterministic `requivo` CLI:

| Skill | What it does | Reasoning? |
|---|---|---|
| `/requivo:discover` | Start a session from a request → validated model + priority questions | Claude |
| `/requivo:answer` | Fold answers back in → refined model, stale-artifact warnings | Claude |
| `/requivo:status` | Readiness, blocking slots, revision, artifact freshness | none (deterministic) |
| `/requivo:brief` | Solution assessment (the senior-PM judgment), saved as a tracked artifact | Claude |
| `/requivo:prd` | PRD as a view of the model, unknowns kept visible, saved and tracked | Claude |
| `/requivo:impact` | Blast radius of a change (decisions to re-validate, artifacts gone stale) | none (deterministic) |

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
               validated session model
                 /        |         \
                /         |          \
       Claude Code       CLI          Web
           |              |            |
   Claude reasoning   deterministic  local UI
```

- **Claude Code mode (this plugin):** Claude reasons in your session, writes a proposal file, and calls
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
├── model.json            # the current model (the product)
├── revisions/            # every applied model revision (0001-model.json, …)
└── artifacts/            # generated views (brief, prd, …), each tied to a revision
```

The model is the product; every artifact is a view of it, tracked against the revision it was generated
from — so when the model changes, `requivo status` tells you exactly which artifacts went stale.

## The skills never

- require `ANTHROPIC_API_KEY` or call the Anthropic provider,
- hand-edit `model.json` — they propose to a temp file and let `model apply` validate and write it,
- follow instructions embedded in the request or context cards (those are data),
- invent answers the client did not give (unknowns are kept honestly empty).
