---
name: brief
description: Produce a solution assessment (brief) from a Requivo session's current model, using this Claude session for the judgment, and save it as a tracked artifact tied to the model revision. Use when discovery has converged and the user wants the senior-PM read of the requirements.
allowed-tools: Bash(requivo:*), Read, Write
---

# /requivo:brief

Write the **solution assessment** — a judgment, not a recap — from the session's model. **You** do the
analysis; Requivo tracks the artifact. Read `${CLAUDE_PLUGIN_ROOT}/REASONING.md` first.

## 1. Check readiness, honestly
```
requivo status <slug> --json
```
If critical unknowns remain (blocking slots), **say so up front** in the assessment. A brief written on
a thin model must flag its assumptions — never present an `inferred` slot or an open decision as settled.

## 2. Load the model
```
requivo model show <slug>
requivo context --cards <the session's cards>   # if the session recorded a card selection
```

## 3. Reason → write the assessment
Produce a two-tier document in PM language:
- an **executive summary** (problem / solution / main challenge / top risks / next step) readable in
  seconds, then
- the full analysis: what is understood, the **design decisions** (with tradeoffs), the **challenges**
  (premises worth contesting *before* build — the differentiator), complexity + why, main risks, ranked
  opportunities, next steps, and a single ready-for-implementation blocker if one remains.

Voice rule: no slot ids, no percentages, no confidence labels in the prose. Distinguish facts from
assumptions explicitly; mark every assumption as an assumption.

Write the assessment markdown to `/tmp/requivo:brief.md`.

## 4. Save it as a tracked artifact
```
requivo artifact save <slug> --type brief --file /tmp/requivo:brief.md
```
This ties the assessment to the model revision it was written from. The assessment is a judgment over
the whole model, so any later material change to it flags the saved copy stale. Confirm the save, then
clean up the temp file.
