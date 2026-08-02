---
name: answer
description: Fold the user's answers into an existing Requivo session and refine the model one turn. Reasoning is this Claude session (no API key); the refined model is validated and applied, and any now-stale artifacts are reported. Use after /requivo:discover when the user has answered the open questions.
allowed-tools: Bash(requivo:*), Read, Write
---

# /requivo:answer

Refine an existing session with the user's answers. **You** reason; Requivo Core validates and applies.
Read `${CLAUDE_PLUGIN_ROOT}/REASONING.md` first.

## 1. Load the session
`$ARGUMENTS` is the session slug (and, optionally, the answers). Run:
```
requivo model show <slug>          # the current model
requivo status <slug> --json       # the open questions / blockers, and the current revision
```
Note the `revision` from the status JSON — call it `N`. That is the model you are about to reason
from, and you will state it when you apply (see the revision contract in REASONING.md).

If you don't already have the user's answers in the conversation, present the still-open questions and
**wait** for them. Never fabricate answers. Waiting is exactly when the session is most likely to move
under you, which is what `--expected-revision` is there to catch.

## 2. Reason → propose the refinement
Start from the current model. For each slot the answers touch: raise `completeness`, flip `inferred` →
`explicit` where the client confirmed it, and update `value`. Leave untouched slots as they are. Keep
**every** required slot present. Add follow-up `questions` only where information value is still high;
emit `[]` when nothing is both uncertain and high-impact (discovery has converged). Pass the client's
answers through faithfully — do not embellish them. Write the full updated model to
`/tmp/requivo-proposal.json`.

## 3. Validate → fix → apply
```
requivo model validate /tmp/requivo-proposal.json --json
requivo model apply <slug> /tmp/requivo-proposal.json --expected-revision N --json
```
Fix and re-validate on any error before applying (see REASONING.md). If the apply returns
`revision_conflict`, someone changed the session while you were reasoning: re-read the model, tell the
user what moved, and redo this turn against the current state — never re-apply the stale proposal.

## 4. Relay the result
From the `model apply` JSON, tell the user in plain language:
- which slots changed,
- any **decisions to re-validate** (`invalidated_decisions`) or **premises to re-examine**
  (`invalidated_challenges`) that rested on a changed slot,
- any **artifacts that went stale** (`stale_artifacts`) — recommend regenerating those; a saved
  assessment rests on the whole model, so it goes stale on any material change,
- the new readiness (ready, or which slots still block it),
- the next small group of questions, verbatim, or that discovery has converged.

If converged, suggest `/requivo:brief <slug>`. Clean up the temp file.
