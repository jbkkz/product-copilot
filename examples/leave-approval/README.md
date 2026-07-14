# Example — Leave approval

One discovery, end to end. Read it in order; no install required.

| Step | File | What it is |
|---|---|---|
| 1 | [`request.md`](request.md) | The raw input — a single vague sentence. |
| 2 | [`model.json`](model.json) | The structured model the discovery built (the product). |
| 3 | [`discovery-brief.md`](discovery-brief.md) | The deliverable — executive summary, decision log, risks, opportunities, next steps. |
| 4 | [`prd.md`](prd.md) | A PRD generated from the *same* model. |
| 5 | [`acceptance-criteria.md`](acceptance-criteria.md) | Given/When/Then recette checklist, from the *same* model. |
| 6 | [`epic.md`](epic.md) | A delivery epic — work broken into trackable issues, from the *same* model. |

Steps 3 through 6 are all views of step 2. Any other artifact (user stories, an estimate) comes from
the same `model.json` — that's the point: the model is the product, everything else is a render of it.

## Reproduce it

```bash
python src/engine.py --from examples/leave-approval/model.json             # regenerate the brief
python src/engine.py --from examples/leave-approval/model.json --prd       # regenerate the PRD
python src/engine.py --from examples/leave-approval/model.json --criteria  # regenerate the criteria
python src/engine.py --from examples/leave-approval/model.json --epic      # regenerate the epic
```

The `model.json` here was produced by a real interactive discovery from `request.md`.
