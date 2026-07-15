# Product Copilot

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Turn a vague client request into a buildable spec — by asking only the questions that matter.**

Built for Product Managers, Solutions Engineers and Business Analysts working on complex, configurable B2B products.

> **Product Copilot doesn't generate documents. It builds understanding.**

![Product Copilot — from a one-line request to a solution assessment](demo.gif)

```
                 Customer request
                         │
                         ▼
                   AI Discovery   ◀── product + client context
                         │
                         ▼
                 Structured model   ← the product (out/<slug>/model.json)
                         │
    ┌─────────┬──────────┼──────────┬───────────────┐
    ▼         ▼          ▼          ▼               ▼
Solution     PRD    User stories  Estimate   More artifacts
assessment
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
artifact (a solution assessment, a PRD, user stories, an estimate) is a view rendered from it.

The same discovery can later produce a PRD, a test plan, or a Jira export **without redoing the
conversation**.

---

## What you get

The deliverable is a **solution assessment** — a judgment on what you're about to build, not a recap
of what you said. It doesn't just organize the request; it **pushes back on it**, the way a senior PM
who has built this kind of system before would:

```
CHALLENGES
  ⚑ Immediate invoice on signature
      Premise      Invoices are generated the moment a contract is signed.
      Alternative  Many B2B contracts bill on a schedule — milestones, recurring
                   periods, usage — not a single lump sum at signature.
      Consequence  Signature-triggered invoicing multiplies cancellation and
                   credit-note handling when deals change before they start.
      Recommend    Validate the billing trigger with Finance before build.

DESIGN DECISIONS
  ✓ Draft-first invoices, reviewed by Finance before issuance
      Why          Finance sign-off is required for compliance.
      Alternative  Immediate issuance on the triggering event.
      Tradeoff     An extra approval step, in exchange for far lower compliance risk.
```

Above these sits a five-line **executive summary** (problem · solution · challenge · risks · next).
Below, the full analysis adds context-specific **risks**, ranked **opportunities**, a readiness
verdict with its single blocker — and the reasoning behind each. Every line is in a PM's language;
none of the engine's internals leak through.

---

## Example

```bash
pc discover "We'd like to set up a leave approval system."
```

From that one sentence, on a platform whose context says *"approval usually hides a balance check
and a multi-level circuit"*, the engine asks the few questions that matter — the multi-level circuit,
the per-client variation, the balance rule — and leaves the low-stakes ones (reporting) alone. Each
answer refines the model until nothing high-value is left to ask, then the solution assessment is produced.

---

## See a complete example

Walk through a full discovery example, end to end, in
[`examples/leave-approval/`](examples/leave-approval/) — no install required:

| File | What it is |
|---|---|
| [`request.md`](examples/leave-approval/request.md) | The one-sentence input |
| [`model.json`](examples/leave-approval/model.json) | The structured model the discovery built |
| [`solution-assessment.md`](examples/leave-approval/solution-assessment.md) | The deliverable — challenges, design decisions, risks, next steps |
| [`prd.md`](examples/leave-approval/prd.md) | A PRD generated from the same model |
| [`epic.json`](examples/leave-approval/epic.json) | The same model as a GitHub/GitLab-importable epic |

Each of these — plus user stories, an estimate, acceptance criteria and release notes — is generated
from the same `model.json`. That's the whole idea:

```bash
pc prd examples/leave-approval/model.json    # regenerate prd.md from the saved model
```

---

## How it works

The solution model is a set of typed *slots* — the problem, actors, business rules, permissions and
edge cases — grouped into four areas: **Why / What / How / Validate**.

It decides what to ask with one rule: **information value = uncertainty × impact**. It never asks
just because something is unknown — it asks when an answer would change what you build. Impact is
estimated from the product context, so the engine is only as sharp as the context you give it.

---

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .              # installs deps + the `pc` command
cp .env.example .env          # set ANTHROPIC_API_KEY
pc discover examples/case1_leave.md
```

It runs an interactive loop — showing what's understood, asking the priority questions, folding your
answers back in — then writes `out/<slug>/model.json` and produces the solution assessment.
Regenerate any deliverable from a saved model without redoing discovery:

```bash
pc prd   out/<slug>/model.json                       # also: stories · estimate · criteria · release · brief
pc epic  out/<slug>/model.json --github --gitlab     # + a tool-neutral epic.json and tracker issue plans
```

### Two interfaces, one engine

The product is the engine; the interfaces are thin layers over the same `product_copilot` core.

- **Terminal** — `pc <command>` (or `python pc.py <command>` with no install).
- **Claude Code** — `/pc-discover`, `/pc-status`, `/pc-generate`, `/pc-help` wrap the same CLI.

The legacy flag CLI (`python src/engine.py "…" --prd`, `--from out/<slug>/model.json`) still works
unchanged.

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
- Discovery engine — priority questions, multi-turn refinement, solution assessment (with challenges)
- Artifact generators — PRD, user stories, uncertainty-aware estimate, acceptance criteria, delivery
  epic, release notes
- Tool-neutral epic export (`epic.json`) — importable into GitHub / GitLab issues
- Tracker adapters — idempotent, n8n-ready issue-creation plans for GitHub (`epic.github.json`) and
  GitLab (`epic.gitlab.json`, with structured issue links)
- The model as a durable product (`model.json`), regenerable via `--from`
- Two interfaces over one presentation-free engine — a `pc` subcommand CLI and Claude Code slash
  commands (`/pc-discover`, `/pc-status`, `/pc-generate`), each a thin layer over the same core

**Upcoming**
- An HTTP API / MCP façade — another thin layer over the same core (for n8n and future web UIs)
- Jira adapter, alongside GitHub and GitLab
- Delivery integrations — authenticated push (via n8n), Notion and Confluence
- Context tooling — validation and assisted generation of product context cards

**Vision**
- A full artifact chain from a single model — the reasoning layer beneath product delivery

---

## License

[MIT](LICENSE) © jbkkz
