# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Product Copilot — Requirements Engine.** Turns a vague client request into a *structured solution
model* ready for dev. It is **not a chatbot**: the chat is only the interface. The product is the
**model** (a set of typed slots) and the **engine** that progressively fills it until it is precise
enough to build from. The whole repo — code, comments, docs, prompts, context, and the engine's own
output — is in English.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then set ANTHROPIC_API_KEY (MODEL defaults to claude-sonnet-5)
python src/engine.py "We'd like to set up a leave approval system."
python src/engine.py examples/case1_leave.md   # a file path is read as the request; otherwise the arg is the request itself
python src/engine.py --once examples/case1_leave.md   # single pass, no interactive loop
```

No test/lint/build tooling exists yet — the whole runner is `src/engine.py`.

## Architecture

The engine is a **single Anthropic call** (per turn) whose intelligence lives entirely in assembled
prompt data, not in Python. `src/engine.py` is a thin runner:

1. `build_prompt(name)` loads a prompt file (`engine.md`, `stories.md`, `estimate.md`, `brief.md`)
   and substitutes two placeholders:
   - `{{SCHEMA}}` ← `framework/model_schema.json` (the slot definitions + the driver rule)
   - `{{CONTEXT}}` ← `load_context()`, which concatenates every `context/*.md` **except** files
     whose name starts with `_` (so `_template.md` is skipped).
2. Every model reply must be **JSON only**. `_complete()` is the shared call: `_first_text()` skips
   non-text blocks, `_extract_json()` strips a ```json fence or slices `{ … }`, and the result is
   validated against a Pydantic contract. On malformed/non-conformant JSON it retries (default 2×)
   with a corrective nudge in a *local* message copy, so the caller's clean history is never
   polluted. `run()`/`derive_stories()`/`estimate()`/`advise()` are thin wrappers over it.
3. Rendering is split: `render_turn()` is the lightweight per-turn view (discovery-status bars +
   priority questions); `render_brief()` is the deliverable (status, readiness, deferred slots,
   assumptions, design considerations, risks & opportunities, confidence footer). The bars, status
   words, readiness verdict and confidence counts are computed **in Python** from the model — only the
   advisory `Brief` (design considerations, risks, opportunities) is LLM-generated.

**Consequence for changes:** behavior is tuned by editing the Markdown/JSON assets, not the Python.

## The output contract (keep in sync)

Each stage has a Pydantic contract that must agree with its prompt's "Output format" block:
`EngineOutput` (`model`/`questions`/`summary`) ↔ `engine.md`, `Stories` ↔ `stories.md`,
`EstimateDraft` ↔ `estimate.md`, `Brief` (`introduces`/`complexity`/`cost_driver`/`risks`/
`opportunities`) ↔ `brief.md`. Slot ids live in `framework/model_schema.json` (which also carries
each slot's `pillar` and `label`, read back by the renderer via `_slot_meta()`).

Pydantic validates at the boundary, so a rename that breaks a contract fails **loudly in `_complete()`**
instead of silently mis-rendering. The field is literally named `model` (Pydantic
`protected_namespaces=()` allows it). Per-slot keys: `completeness` (0-100), `confidence`
(explicit|inferred|empty), `impact` (low|medium|high), `value`, `evidence`.

## The two core concepts

- **Slots (the atomic unit).** Every requirement lives in a slot (see keys above). Slots are grouped
  into 4 navigation pillars (Why / What / How / Validate) defined in `framework/elicitation.md`. Every
  output is a render of the same filled model: the status bars are its per-pillar completeness, the
  questions are its *gaps*, the brief is a consultant's read of it (`inferred` slots feed the
  "Assumptions to confirm" section).

- **The driver: `information_value = uncertainty × impact`.** The engine does **not** ask because a
  slot is empty — it asks where information value is high. Empty-but-low-impact slots are left alone;
  filled-but-risky slots get probed. **Impact is estimated from the product context** — so the engine
  is only as sharp as the `context/*.md` cards it's given. This is the central design idea; preserve
  it when editing prompts.

## Multi-turn refinement

Interactive mode (`converse()`) loops: `render_turn` → ask → collect answers → feed back → repeat, up
to `MAX_TURNS` (8). The previous turn's validated output IS the state being refined — it is carried in
the conversation history (re-serialized via `model_dump_json()`), not rebuilt from scratch. The
engine flips `inferred → explicit` and raises `completeness` as answers come in. **Stop signal:** the
model returns `questions: []` when nothing is both uncertain and high-impact; `converse()` then calls
`advise()` and prints the discovery brief. `--once` (or no TTY) does a single pass (status +
questions, no brief). `--stories` / `--estimate` chain the delivery pipeline after the model stage.

## Extending

- **New client/product context:** copy `context/_template.md` to `context/<name>.md` and fill it. It
  is picked up automatically (non-`_` prefix). Better context cards → better impact estimates → better
  questions.
- **`config_vs_custom` slot** is `optional: true` — the platform edge (hardcoded / configurable /
  per-client / reusable-for-all). On for configurable multi-client platforms, off for one-shot apps.
- `framework/elicitation.md` is the human-readable spec of the framework; `model_schema.json` is the
  machine version fed to the model. Keep them consistent when adding or renaming slots.
