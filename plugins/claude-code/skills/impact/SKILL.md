---
name: impact
description: Show what a change to a Requivo session touches — the decisions to re-validate and the artifacts that go stale — from the dependency DAG. Pure deterministic query, no reasoning, no API key. Use when the user asks "what does changing X affect?" or wants to know what to regenerate after a model change.
allowed-tools: Bash(requivo:*), Read
---

# /requivo:impact

Report the blast radius of a change. This is a **deterministic DAG query** — no Claude reasoning; run
the command and translate.

## Preflight
Read-only and safe-sounding, so this is a likely first command for someone who has just installed the
plugin — and the `requivo` CLI is a **separate install**. Start with the shared **preflight** in
`${CLAUDE_PLUGIN_ROOT}/REASONING.md`: run `requivo doctor --json` and check whether the command ran
*at all* — not what it reported. If it could not run, the CLI is not installed; say the four things
REASONING.md lists and stop. This skill only queries, so nothing was changed.

## Run
`$ARGUMENTS` is the session slug, optionally followed by slot names/ids to probe.
```
requivo impact <slug> [slot ...]     # blast radius of those slots
requivo impact <slug>                # the full dependency map
```
To see what has *already* drifted since the last generation, also run:
```
requivo status <slug> --json         # each artifact's `stale` flag — the source revision is provenance
```

## Relay
Present, in plain language:
- the **decisions to re-validate** (they rested on a changed slot via `derived_from`),
- the **premises to re-examine** (challenges that contest a changed slot via `contests`),
- the **artifacts that go stale** and should be regenerated (`/requivo:prd`, `/requivo:brief`, …) — the
  saved decision brief rests on the whole understanding, so any material change reaches it,
- and recommend **only the necessary** regenerations — not everything.

Do not invent dependencies the command did not report; the DAG is authoritative.
