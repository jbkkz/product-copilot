# Example — Leave approval

One discovery, end to end. Read it in order; no install required.

| Step | File | What it is |
|---|---|---|
| 1 | [`request.md`](request.md) | The raw input — a single vague sentence. |
| 2 | [`model.json`](model.json) | The structured model the discovery built (the product). |
| 3 | [`solution-assessment.md`](solution-assessment.md) | The deliverable — executive summary, challenges, design decisions, risks, opportunities, next steps. |
| 4 | [`prd.md`](prd.md) | A PRD generated from the *same* model. |
| 5 | [`acceptance-criteria.md`](acceptance-criteria.md) | Given/When/Then recette checklist, from the *same* model. |
| 6 | [`epic.md`](epic.md) | A delivery epic — work broken into trackable issues, from the *same* model. |
| 7 | [`epic.json`](epic.json) | The same epic as a tool-neutral, GitHub/GitLab-importable export. |
| 8 | [`release-notes.md`](release-notes.md) | Client-facing release notes, from the *same* model. |
| 9 | [`epic.github.json`](epic.github.json) | A GitHub issue-creation plan (adapter over the neutral export). |
| 10 | [`epic.gitlab.json`](epic.gitlab.json) | A GitLab plan — `depends_on` becomes structured issue links. |

Steps 3 through 10 are all views of step 2. Any other artifact (user stories, an estimate) comes from
the same `model.json` — that's the point: the model is the product, everything else is a render of it.

## Reproduce it

Each command regenerates one view from the model — no discovery needed. Output lands in
`out/leave-approval/` (it doesn't overwrite the files you're reading here):

```bash
pc brief    examples/leave-approval/model.json                          # the solution assessment
pc prd      examples/leave-approval/model.json                          # the PRD
pc criteria examples/leave-approval/model.json                          # the acceptance criteria
pc epic     examples/leave-approval/model.json --json --github --gitlab # epic.md + neutral export + tracker plans
pc release  examples/leave-approval/model.json v1.0                     # the release notes
pc stories  examples/leave-approval/model.json                          # user stories (also: pc estimate)
```

The legacy flag CLI still works too, e.g. `python src/engine.py --from examples/leave-approval/model.json --prd`.

The `model.json` here was produced by a real interactive discovery from `request.md`.
