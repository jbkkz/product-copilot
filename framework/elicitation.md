# Elicitation framework

The core of the product. Product-agnostic: it encodes *how* a PM turns a vague request into a
solution model. The context (`context/*.md`) grounds it in a specific product.

## The 4 pillars (navigation + pitch)

| Pillar | Question | Slots |
|---|---|---|
| 🟢 **Why**      | Why?      | Real problem · Current process (as-is) · Success criteria |
| 🟡 **What**     | What?     | Actors & roles · Business objects · Business rules · Workflow/lifecycle |
| 🔵 **How**      | How?      | Integrations & notifications · Permissions · Config vs custom · Constraints |
| 🔴 **Validate** | Validation | Edge cases · Reporting · Acceptance criteria · Risks & rollout |

Pillars are **navigation buckets**. The atomic unit stays the **slot** — it's what gets filled,
tracked, and what the artifacts are generated from.

## Each slot is an object

```
slot {
  completeness : 0-100        # how well we know it
  confidence   : explicit | inferred | empty
  impact       : low | medium | high    # how much it changes the shape/cost of the solution
  value        : what we know
  evidence     : where it comes from (client's words / inference / answer)
}
```

`confidence` carries the **provenance**: an `inferred` slot is an **assumption to confirm** — exactly
what feeds the *"Assumptions made"* section of the summary.

## The driver: Uncertainty × Impact

The engine does **not** ask a question because a slot is empty. It computes the **information
value**:

```
information_value = uncertainty × impact
```

- **Uncertainty** ← derived from `completeness` + `confidence`.
- **Impact** ← how much this slot changes the solution. **Estimated using the product context.**
  Without context, impact is a guess: the engine is only as smart as the context you give it.

We ask **the right** questions, not all of them.

- Empty slot, low impact (e.g. Reporting on adding a field) → **we don't ask.**
- Partial slot, high impact, medium confidence (e.g. a business rule that varies by country) → **we dig.**

## config-vs-custom: the platform edge

`optional` slot: ON for configurable products (multi-client platforms), OFF for a one-shot app.
Almost every discovery tool is greenfield-minded and never asks
*"hardcoded / configurable / client-specific / reusable for all?"*. That's THE question that
separates a scalable platform from a pile of per-client forks.

## What the engine produces (v0)

Two renders of the same model:
1. **Business summary** — objective · likely scope · assumptions made · main blind spot.
2. **Priority questions** — sorted by information value, with the *why* of each question.
