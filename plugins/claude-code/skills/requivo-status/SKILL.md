---
name: requivo-status
description: Show a Requivo session's readiness, blocking slots, current revision, and artifact freshness. Pure deterministic read — no reasoning, no API key. Use when the user asks where a session stands or what is still blocking it.
allowed-tools: Bash(requivo:*)
---

# /requivo-status

Report where a session stands. This is a **deterministic read** — do not re-analyse the request with
Claude; just run the command and translate the result.

## Run
`$ARGUMENTS` is the session slug. Run:
```
requivo status <slug> --json
```
(Add nothing else — the numbers come from Core, not from you.)

## Relay
Translate the JSON into plain language:
- **Readiness**: ready to build, or not — and if not, name the **blocking slots** (use their `label`,
  not the raw id).
- **Revision**: the current model revision.
- **Artifacts**: for each, whether it is fresh or **stale** (produced from an older revision — it
  should be regenerated).

Be clear about what is still blocking. If the user wants the full understanding checklist and open
questions, run `requivo status <slug>` without `--json` and show that view. Do not invent detail the
command did not return.
