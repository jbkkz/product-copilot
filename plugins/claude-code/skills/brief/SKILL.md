---
name: brief
description: Produce a solution assessment (brief) from a Requivo session's current model, using this Claude session for the judgment, and save it as a tracked artifact tied to the model revision. Use when discovery has converged and the user wants the senior-PM read of the requirements.
allowed-tools: Bash(requivo:*), Read
---

# /requivo:brief

Write the **solution assessment** — a judgment, not a recap — from the session's model. **You** do the
analysis; Requivo tracks the artifact. Read `${CLAUDE_PLUGIN_ROOT}/REASONING.md` first.

## 1. Check readiness, honestly
```
requivo status <slug> --json
```
Note the `revision` — call it `N`. It is the model this assessment will rest on, and you will state it
when you save.

If critical unknowns remain (blocking slots), **say so up front** in the assessment. A brief written on
a thin model must flag its assumptions — never present an `inferred` slot or an open decision as settled.

## 2. Load the model
```
requivo model show <slug>
requivo context --session <slug>    # exactly the cards this session was created with
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

## 4. Fold the reasoning back into the model — do not skip this
The prose is the *view*. The **reasoning behind it is part of the model**, and every later generator
(PRD, epic, criteria, release notes) is prompted with the model, so a PRD written after this brief
should inherit the decisions and challenges you just made — not rediscover them.

Take the model from step 2 unchanged and add the structured form of your reasoning:

```bash
requivo model apply <slug> - --expected-revision N --json <<'JSON'
{
  "model": { … exactly as it was … },
  "questions": [ … ],
  "summary": { … },
  "decisions":     [{"decision": "…", "why": "…", "alternative": "…", "tradeoff": "…",
                     "derived_from": ["<slot ids the decision rests on>"]}],
  "challenges":    [{"headline": "…", "premise": "…", "alternative": "…", "consequence": "…",
                     "recommendation": "…", "contests": ["<slot ids whose premise this contests>"]}],
  "opportunities": [{"text": "…", "leverage": "high|medium|future", "modules": ["…"]}]
}
JSON
```

These are the same items as in your prose, stated structurally. `derived_from` and `contests` are the
dependency edges: they are what lets Requivo tell the user *which* later change unseats *which*
decision. A decision with no edges still records fine, but it can never be reported as invalidated.

Note the revision the apply returns — call it `M`. Slots may not have moved at all; the reasoning is
the change, and Requivo tracks it as one.

## 5. Save the document as a tracked artifact
```bash
requivo artifact save <slug> --type brief --file - --revision M --json <<'MD'
# … the assessment you wrote in step 3 …
MD
```
`M` is the revision the apply just created — the model the assessment actually describes, reasoning
included. (If you skipped step 4, use `N` from step 1: the honest revision is whichever one you truly
reasoned from, never simply the latest.)

The assessment is a judgment over the whole model, so any later material change to it flags the saved
copy stale. Read `stale` back from the save output: if it is `true`, the model moved while you were
writing — tell the user plainly that the assessment is already behind and offer to redo it.
