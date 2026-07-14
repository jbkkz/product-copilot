# Product Copilot

**Turn a vague client request into a buildable spec — by asking only the questions that matter.**

Discovery tools either ask you *everything* (endless checklists) or *nothing* (a chat that nods
along). Product Copilot does neither. It builds a structured model of the solution and asks a
question only when the answer would actually change what you build — high **uncertainty** on a
high-**impact** slot. The rest, it infers and flags as an assumption.

The chat is just the interface. The product is the model, and the artifacts you render from it.

---

## Quickstart

```bash
git clone <repo> && cd product-copilot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set ANTHROPIC_API_KEY
python src/engine.py "We'd like to set up a leave approval system."
```

Pass a request inline, or a file: `python src/engine.py examples/case1_leave.md`.

By default it runs an interactive loop: it shows the model, asks the priority questions, folds your
answers back in, and repeats until nothing high-value is left to ask. Add `--once` for a single
pass (also the default when piped or in CI).

---

## What you get

Each turn shows where the discovery stands and asks only the questions that still matter:

```
DISCOVERY STATUS
  Business understanding   █████████░  Strong
  Scope & rules            ███████░░░  Solid  · gap: Business rules
  Configuration & access   ████░░░░░░  Partial
  Validation & rollout     ██░░░░░░░░  Thin

  Readiness   ⛔ Not ready — 4 high-impact areas still open
              → Real problem, Business rules, Permissions, Config vs customization

PRIORITY QUESTIONS  (uncertainty × impact)
  1. Does approval need a balance/quota check, and does that rule vary by client/country?
     → [business_rules]
  2. Should the circuit be standard for all clients, or configurable per client?
     → [config_vs_custom]
```

When nothing high-value is left to ask, it produces the deliverable — a **discovery brief**:

```
DEFERRED  (low impact — revisit after launch)
  • Reporting & visibility — low impact for this request; follow-up after launch.

DESIGN CONSIDERATIONS
  Introduces: a client-configurable approval-circuit engine · an entitlement/balance
  engine · HR self-service administration · concurrency at approval time
  Estimated complexity   HIGH        Main cost driver   the entitlement rule engine

POTENTIAL RISKS & OPPORTUNITIES
  ⚠ The approval-circuit framework is shared across modules — extending it risks
    touching contracts and invoicing.
  ◆ Generalize it into a shared workflow service instead of building leave-specific.

Confidence   11/15 areas confirmed · 4 assumptions · 1 to validate before build
```

`reporting` is deferred, not forgotten — it's low-impact, so it never costs a question. That's the
point: **the right questions, then a brief that advises, not just a summary that restates.**

---

## How it works

```
Vague request ─▶ [ Engine ] ─▶ Structured model ─▶ Discovery brief · stories · estimate
                     ▲
              Product + client context
```

- **The model** is a set of typed *slots* (problem, actors, business rules, permissions, edge
  cases…) grouped into four pillars — **Why / What / How / Validate**. Each slot tracks how well
  it's known (`completeness`), where that knowledge came from (`confidence`:
  explicit/inferred/empty), and how much it moves the solution (`impact`).

- **The driver** is `information_value = uncertainty × impact`. The engine never asks just because a
  slot is empty. It asks where an answer would change the build. **Impact is estimated from the
  product context** — so the engine is only as sharp as the context cards you give it.

- **Multi-turn**: each answer refines the same model (`inferred → explicit`, completeness climbs)
  until no high-value question remains. Every reply is schema-validated (Pydantic) at the boundary.

- **The outputs are renders of one model**: the status and questions are its state and gaps; the
  brief adds a consultant's read (design considerations, risks, reuse opportunities); stories and the
  estimate project the same model further down the delivery pipeline.

---

## Add your product

The engine is domain-agnostic; the context makes it smart. Drop a card in `context/`:

```bash
cp context/_template.md context/my-product.md   # then fill it in
```

Better context → better impact estimates → better questions. Files prefixed with `_` are ignored.

---

## Layout

| Path | What's there |
|---|---|
| `framework/` | The model: slots, pillars, the uncertainty×impact driver. **The core.** |
| `context/`   | Product & client context cards that ground prioritization. |
| `prompts/`   | The prompts, one per artifact (`engine.md`, `stories.md`, …). |
| `src/engine.py` | Thin runner: assemble prompt → call → validate → render. |
| `examples/`  | Real one-line requests to try. |

## Roadmap

- [x] Model + priority questions
- [x] Multi-turn refinement
- [x] Discovery brief — status, readiness, design considerations, risks & opportunities
- [x] User stories
- [x] Uncertainty-aware estimate (day ranges + complexity, spread driven by the soft slots)
- [ ] GitLab issue export
