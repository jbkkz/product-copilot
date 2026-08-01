# CLI reference

> Every `requivo` command. For a first run, see [getting-started.md](getting-started.md).

Run as `requivo <command>` after an install, `uv run requivo <command>` with uv, or
`python scripts/requivo_cli.py <command>` from a bare clone. The short alias `pc` is a deprecated
synonym. Commands that call the Anthropic API need the `anthropic` extra and `ANTHROPIC_API_KEY`;
everything else is offline.

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
| `requivo brief <slug>` | Solution assessment (the senior-PM judgment) |
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
| `requivo schema` / `requivo context` | Inspect the slot schema / available context cards |
| `requivo session init\|list\|show\|migrate\|export\|import` | Session lifecycle |
| `requivo model show\|validate\|apply\|diff <slug>` | Inspect and mutate a model through the validated path (`model apply --expected-revision N` for optimistic locking) |
| `requivo artifact save\|list\|show <slug>` | Record and read generated artifacts |

The deterministic verbs and `--json` outputs are what the Claude Code plugin drives — Claude reasons,
these apply.

## Legacy flag CLI (deprecated)

The original flag interface still runs, unchanged:

```bash
python src/engine.py "…request…" --prd
python src/engine.py --from out/<slug>/model.json --prd
```

It is **frozen and scheduled for removal in 1.1.0**, and it prints a deprecation notice when used. It
writes to the old `out/<slug>/` layout rather than the versioned session store, so its output has no
revisions, no provenance and no staleness tracking — everything the subcommand CLI above gives you.
The equivalents:

| Legacy | Modern |
|---|---|
| `python src/engine.py "request" --prd` | `requivo discover "request"` then `requivo prd <slug>` |
| `python src/engine.py --from out/x/model.json --epic` | `requivo epic x` |

The code lives in `requivo/legacy.py`; deleting that file is the whole removal.
