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

From one throwaway sentence — *"We'd like to set up a leave approval system."* — on a platform whose
context says *"approval usually hides a balance check + a multi-level circuit"*:

```
=== BUSINESS SUMMARY ===
Objective : Introduce a structured leave approval system to replace a manual process.
Blind spot: The client said "approval" but never said whether the circuit varies by client/
            contract/country — the single factor most likely to turn a small feature into a
            configurable engine, and it drives the permission model too.

=== PRIORITY QUESTIONS (Uncertainty × Impact) ===
1. Does approval need a balance/quota check before validating, and does that rule vary by
   contract, client, or country?          → [business_rules]
2. How many approval levels (manager only, or manager + HR), with escalation/delegation?
                                           → [workflow]
3. Should the circuit be standard for all clients, or configurable per client?
                                           → [config_vs_custom]

=== MODEL STATE (avg completeness: 7%) ===
  business_rules     0%    empty     impact=high     ← asked
  permissions        0%    empty     impact=high     ← asked
  reporting          0%    empty     impact=low      ← NOT asked (empty, but low stakes)
```

Note the last line: `reporting` is empty too, but it's low-impact, so the engine stays quiet.
That's the whole point — **the right questions, not all of them.**

---

## How it works

```
Vague request ─▶ [ Engine ] ─▶ Structured model ─▶ Artifacts (summary · questions · stories · estimate…)
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

- **The outputs are renders of one model**: the summary is the model as prose; the questions are its
  gaps as questions; stories and estimates are the same model projected further down the pipeline.

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

- [x] Model + priority questions + business summary
- [x] Multi-turn refinement
- [ ] User stories (in progress)
- [ ] Uncertainty-aware estimate (day ranges + complexity, spread driven by the soft slots)
- [ ] GitLab issue export
