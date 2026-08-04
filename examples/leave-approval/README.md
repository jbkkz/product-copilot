# Example — Leave approval

The canonical example. One vague sentence, taken through the questions, the brief, and — at the end —
a changed answer that moves the scope. Read it in order; no install required.

| Step | File | What it is |
|---|---|---|
| 1 | [`request.md`](request.md) | The raw input — a single vague sentence. |
| 2 | [`model.json`](model.json) | The understanding the analysis built: what is known, what is assumed, what is still open. |
| 3 | [`solution-assessment.md`](solution-assessment.md) | The decision brief — what to review before committing to the scope. |
| 4 | [`prd.md`](prd.md) | A PRD generated from the *same* understanding. |
| 5 | [`acceptance-criteria.md`](acceptance-criteria.md) | Given/When/Then checklist, from the *same* understanding. |
| 6 | [`epic.md`](epic.md) | A delivery epic — work broken into trackable issues. |
| 7 | [`epic.json`](epic.json) | The same epic as a tool-neutral, GitHub/GitLab-importable export. |
| 8 | [`release-notes.md`](release-notes.md) | Client-facing release notes. |
| 9 | [`epic.github.json`](epic.github.json) | A GitHub issue-creation plan (adapter over the neutral export). |
| 10 | [`epic.gitlab.json`](epic.gitlab.json) | A GitLab plan — `depends_on` becomes structured issue links. |

Steps 3 through 10 are all views of step 2. Any other document (user stories, an estimate) comes from
the same `model.json` — that is the point: the shared understanding is the source of truth, and every
document is generated from it.

> **Note on step 3.** `solution-assessment.md` is a frozen capture from an earlier run, and predates
> the current decision-brief layout (which opens with *what is confirmed* and *what is being assumed*,
> read straight off the model). Regenerating it takes one provider call — see below. The filename
> stays `solution-assessment.md` because that is the artifact's name on disk in every session; only
> what a reader is shown changed.

## Reproduce it

Each command regenerates one view from the saved understanding — no re-analysis needed. Output lands
in `out/leave-approval/` (it doesn't overwrite the files you're reading here):

```bash
requivo brief    examples/leave-approval/model.json                          # the decision brief
requivo prd      examples/leave-approval/model.json                          # the PRD
requivo criteria examples/leave-approval/model.json                          # the acceptance criteria
requivo epic     examples/leave-approval/model.json --json --github --gitlab # epic.md + neutral export + tracker plans
requivo release  examples/leave-approval/model.json v1.0                     # the release notes
requivo stories  examples/leave-approval/model.json                          # user stories (also: requivo estimate)
```

The `model.json` here was produced by a real interactive discovery from `request.md`.

## The part a chat transcript cannot do

The steps above show generation. This part shows the reason the understanding is kept at all: when an
answer changes, Requivo can say what that costs — and it says it from the dependency graph, not from a
model's opinion.

Run it yourself, in the Web interface or the CLI. The answers below are the ones this example is built
around.

**1 — Analyse the request.**

```bash
requivo discover "We'd like a leave approval system."
```

Among the questions it raises is one about how the new system and the existing HR tool relate.

**2 — Answer it one way.**

```bash
requivo answer <slug> "During the pilot both systems stay in sync — the legacy HR tool
                       keeps being written to, and balances have to match on both sides."
```

Two-way synchronization is now part of the understanding, and the reasoning follows it: reconciliation
rules, conflict handling, an ownership rule per field.

**3 — Generate the brief, and check what rests on what.**

```bash
requivo brief  <slug>
requivo impact <slug> integrations     # no provider call — a query over the dependency graph
```

`impact` answers the question *before* you spend anything: which decisions rest on the integration
topic, and which documents consume it.

**4 — Change the answer.**

```bash
requivo answer <slug> "Correction: the migration is one-time. After cutover the legacy
                       system becomes read-only — nothing writes back to it."
```

**5 — Read what moved.**

```bash
requivo status <slug>
```

The integration topic has changed, the two-way-sync decision is flagged for re-validation, and the
brief is marked as needing an update. Nothing was regenerated on your behalf — Requivo reports, you
decide. That report is computed, not generated: the same change would produce the same list every
time, which is exactly what a generated answer cannot promise.

In the Web interface the same sequence renders as a **What changed** block with a **Needs review**
list, immediately under the answer you just gave.
