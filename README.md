# Product Copilot

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Turn a vague client request into a buildable spec — by asking only the questions that matter.**

Built for Product Managers, Solutions Engineers and Business Analysts working on complex, configurable B2B products.

> **Product Copilot doesn't generate documents. It builds understanding.**

![Product Copilot — from a one-line request to a discovery brief](demo.gif)

```
        Customer request
              │
              ▼
         AI Discovery   ◀── product + client context
              │
              ▼
       Structured model   ← the product (out/<slug>/model.json)
              │
      ┌───────┼───────────┬───────────┐
      ▼       ▼           ▼           ▼
  Discovery  PRD      User stories  Estimate      …and more
   brief
```

---

## Why

Discovery tools either ask you *everything* — endless checklists no one finishes — or *nothing* — a
chat that nods along and hands back your own words. Neither helps you find the question you didn't
think to ask: the one that turns a "small feature" into a three-month build.

Product Copilot asks a question only when the answer would **materially change the solution**. The
rest, it infers and flags as an assumption. You spend your discovery time where it moves the needle.

### Why I built this

> After several years working on complex, configurable enterprise software, I realised that writing
> the specification was rarely the hardest part. The difficult part was building a shared
> understanding of the real problem before development started.
>
> Over time, I noticed the same reasoning pattern behind good discovery work: what do we actually
> know, what are we assuming, and what would materially change the solution? Product Copilot is my
> attempt to formalise that process.

---

## What it does

Product Copilot builds a **structured model of the solution** and refines it through a short,
targeted conversation. The chat is just the interface. **The product is the model** — and every
artifact (a discovery brief, a PRD, user stories, an estimate) is a view rendered from it.

Because the model is the product, the same discovery can later produce a PRD, a test plan, or a Jira
export **without redoing the conversation**.

---

## What you get

The deliverable is a **discovery brief** — and it opens with the answer a stakeholder actually wants:

```
READY FOR IMPLEMENTATION?
  Status               Nearly ready
  Blocking decision    Confirm the underlying business problem
  Remaining gaps       Success metrics, reporting expectations

MAIN RISKS
  ⚠ The shared approval framework is used across other modules — changes here can ripple
    into contracts and invoicing.
  ⚠ Balance checks under concurrent requests risk race conditions if not serialized.

OPPORTUNITIES
  High leverage    ◆ Generalize the approval-circuit engine — it could later drive invoice
                     sign-off and mission validation too.

RECOMMENDED NEXT STEPS
  1. Confirm the pilot's success metrics and target clients before committing an estimate.
  2. Nail down reporting and audit requirements early, given the regulatory sensitivity.
```

Above this sits a five-line **executive summary** (problem · solution · risks · unknowns · next
step). Below it, the full analysis includes a **decision log** of what's settled versus still open,
an understanding checklist, and the reasoning behind each recommendation.

---

## Example

```bash
python src/engine.py "We'd like to set up a leave approval system."
```

From that one sentence, on a platform whose context says *"approval usually hides a balance check
and a multi-level circuit"*, the engine asks the few questions that matter — the multi-level circuit,
the per-client variation, the balance rule — and leaves the low-stakes ones (reporting) alone. Each
answer refines the model until nothing high-value is left to ask, then the brief is produced.

---

## See a complete example

Walk through one real discovery end to end in [`examples/leave-approval/`](examples/leave-approval/) —
no install required:

| File | What it is |
|---|---|
| [`request.md`](examples/leave-approval/request.md) | The one-sentence input |
| [`model.json`](examples/leave-approval/model.json) | The structured model the discovery built |
| [`discovery-brief.md`](examples/leave-approval/discovery-brief.md) | The deliverable — brief with risks, decisions, next steps |
| [`prd.md`](examples/leave-approval/prd.md) | A PRD generated from the same model |

Same model, four views. That's the whole idea.

---

## How it works

Product Copilot builds a structured solution model rather than relying on conversation history. The
model is a set of typed *slots* (problem, actors, business rules, permissions, edge cases…) grouped
into four areas — **Why / What / How / Validate**.

It decides what to ask with one rule: **information value = uncertainty × impact**. It never asks
just because something is unknown — it asks when an answer would change what you build. Impact is
estimated from the product context, so the engine is only as sharp as the context you give it.

---

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set ANTHROPIC_API_KEY
python src/engine.py examples/case1_leave.md
```

It runs an interactive loop — showing what's understood, asking the priority questions, folding your
answers back in — then writes `out/<slug>/model.json` and produces the brief. Add `--prd`,
`--stories`, `--estimate`, `--criteria`, or `--epic` to generate more artifacts;
`--from out/<slug>/model.json` regenerates any of them without redoing discovery.

---

## Add your product

The engine is domain-agnostic; the context makes it smart. Drop a card in `context/` describing your
product, its entities, and its recurring traps:

```
context/
  hris.md        ← HR / people platforms
  crm.md         ← sales & pipeline tools
  erp.md         ← finance & operations suites
  my-product.md  ← yours
```

Better context → sharper impact estimates → better questions. Files prefixed with `_` are ignored.

---

## Roadmap

**Current**
- Discovery engine — priority questions, multi-turn refinement, discovery brief
- Artifact generators — PRD, user stories, uncertainty-aware estimate, acceptance criteria, delivery epic
- The model as a durable product (`model.json`), regenerable via `--from`

**Upcoming**
- More generators — release notes
- Exports — Jira / GitLab, Notion, Confluence

**Vision**
- A full artifact chain from a single model — the reasoning layer beneath product delivery

---

## License

[MIT](LICENSE) © jbkkz
