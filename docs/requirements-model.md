# The requirements model

> The model is the product; documents are views of it. This is what the model is made of and how
> Requivo reasons over it.

## Slots and pillars

The model is a set of typed **slots** — the problem, actors, business objects, business rules,
workflow, permissions, edge cases, constraints, and more — grouped into four areas:

- **Why** — the problem, current process, success criteria
- **What** — actors, business objects, business rules, workflow
- **How** — permissions, integrations, constraints, config-vs-custom
- **Validate** — acceptance, edge cases, risks

Each slot carries a value plus its **evidence** and **coverage** (below). Every artifact is a render of
this same filled model.

## The driver: information value = uncertainty × impact

Requivo does **not** ask because a slot is empty — it asks where an answer would change the solution.
Empty-but-low-impact slots are left alone; filled-but-risky ones get probed. **Impact is estimated
from the product context**, so the engine is only as sharp as the [context cards](context-cards.md)
it's given.

## Evidence vs coverage

Two independent signals, deliberately not collapsed:

- **Evidence** — *how we know* a slot: `explicit` (stated by the client), `inferred` (assumed from
  context), or `unknown`.
- **Coverage** — *how fully* a slot is covered (its completeness). A slot can be `explicit` yet thinly
  covered — stated in one word. That still blocks readiness; it reads as "partial", not "confirmed".

## Decisions, challenges, opportunities

The assessment layer, persisted into the model so every generator inherits it:

- **Decision** — a settled choice, with its *why*, the *alternative* weighed, and the *trade-off*
  accepted.
- **Challenge** — a contested premise: the assumption the request takes for granted, a concrete
  alternative, the consequence, and a recommendation. This is the differentiator — it pushes back on
  the request rather than organising it.
- **Opportunity** — a leverage point, ranked, naming the modules it reaches.

## Dependencies and staleness

The model is not a flat snapshot — its parts rest on each other:

- A decision records the slots it was **derived from**; a challenge records the slots it **contests**.
- Each buildable artifact records the slots it **consumes**.

So a change knows its blast radius. `requivo impact <slug> <slots>` shows the decisions to re-validate
and the artifacts that would go **stale**; a discovery turn that materially moves the model flags the
already-generated files that no longer match it. An unrelated (or completeness-only) change leaves an
artifact fresh — staleness follows the dependency graph, not the revision number.

## Readiness

Readiness is binary: a high-impact slot must be both `explicit` **and** covered above the soft
boundary to stop blocking the build. A high-impact gap — empty, unknown, or stated-but-thin — keeps a
session out of "ready". Requivo does not invent graded "nearly ready" levels; it shows what blocks.
