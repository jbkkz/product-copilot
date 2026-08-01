---
name: requivo-prd
description: Generate a PRD from a Requivo session's model — a view of the model, with unknowns kept visible and open decisions left open — and save it as a tracked artifact. Use when the user wants a Product Requirements Document from a discovered model.
allowed-tools: Bash(requivo:*), Read, Write
---

# /requivo-prd

Generate a **PRD as a view of the model** — not new invention. **You** write it from the model; Requivo
tracks it. Read `${CLAUDE_PLUGIN_ROOT}/REASONING.md` first.

## 1. Load the model
```
requivo model show <slug>
requivo status <slug> --json
```

## 2. Reason → write the PRD
Turn the model into a PRD (title, summary, problem, goals, users, in/out of scope, requirements with
priorities, workflow, business rules, permissions, integrations, edge cases, acceptance criteria,
assumptions, open questions, risks). Rules that keep it faithful to the model:

- **Unknowns stay visible.** A slot that is empty or `inferred` becomes an explicit *assumption* or an
  *open question* in the PRD — never a stated requirement.
- **Open decisions stay open.** Do not silently resolve a decision the model left open.
- **Traceability.** Each requirement should be traceable to the slot(s) it comes from; keep them
  grounded in the model, not added from outside it.

Write the PRD markdown to `/tmp/requivo-prd.md`.

## 3. Save it as a tracked artifact
```
requivo artifact save <slug> --type prd --file /tmp/requivo-prd.md
```
The PRD is now tied to the model revision it was written from, so a later change to any slot it rests
on flags it stale (`requivo status` will show it). Confirm the save; clean up the temp file.
