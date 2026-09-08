---
name: answer
description: Fold the user's answers into an existing Requivo session and refine the model one turn. Reasoning is this Claude session (no API key); the refined model is validated and applied, and any now-stale artifacts are reported. Use after /requivo:discover when the user has answered the open questions.
allowed-tools: Bash(requivo:*), Read
---

# /requivo:answer

Refine an existing session with the user's answers. **You** reason; Requivo Core validates and applies.
Read `${CLAUDE_PLUGIN_ROOT}/REASONING.md` first.

## 0. Preflight
Run the shared **preflight** from REASONING.md before anything else: `requivo doctor --json`, checking
whether the command ran *at all* rather than what it reported. If it could not run, the `requivo` CLI
is not installed — say the four things REASONING.md lists and stop. This is before any mutation, so
the session is exactly as the user left it.

## 1. Load the session
`$ARGUMENTS` is the session slug (and, optionally, the answers). Run:
```
requivo model show <slug>          # the current model
requivo status <slug> --json       # the open questions / blockers, and the current revision
requivo context --session <slug>   # the same cards the model was built against
```
Read the context by session, not with a bare `requivo context`: the selection is part of the session,
and refining a model against a wider set than it was built on shifts the impact estimates underneath it.
Note the `revision` from the status JSON — call it `N`. That is the model you are about to reason
from, and you will state it when you apply (see the revision contract in REASONING.md).

If you don't already have the user's answers in the conversation, present the still-open questions and
**wait** for them. Never fabricate answers. Waiting is exactly when the session is most likely to move
under you, which is what `--expected-revision` is there to catch.

## 2. Reason → propose the refinement
Start from the current model. For each slot the answers touch: raise `completeness`, flip `inferred` →
`explicit` where the client confirmed it, and update `value`. Leave untouched slots as they are. Keep
**every** required slot present. Add follow-up `questions` only where information value is still high —
each one `{ "q": …, "slot": …, "why": … }`, the text field being **`q`** — and
emit `[]` when nothing is both uncertain and high-impact (discovery has converged). Pass the client's
answers through faithfully — do not embellish them.

Say nothing about `decisions`, `challenges` and `opportunities` — leave the keys out entirely. A
refinement turn is not re-deriving the brief, and what is established stands on its own (see the
reasoning layer in REASONING.md). Emitting `[]` for them means "these no longer hold", which is a real
deletion and marks what rested on them stale.

## 3. Validate → fix → apply
Feed the full updated model in on stdin — no temp file:
```bash
requivo model validate - --json <<'JSON'
{ … the full updated model … }
JSON

requivo model apply <slug> - --expected-revision N --json <<'JSON'
{ … the same model, once it validates … }
JSON
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
  brief rests on the whole understanding, so it needs updating on any material change,
- the new readiness (ready, or which slots still block it),
- the next small group of questions, verbatim, or that discovery has converged.

If converged, suggest `/requivo:brief <slug>`.
