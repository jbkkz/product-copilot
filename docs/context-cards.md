# Context cards

> The engine is domain-agnostic; the context makes it smart. Better context → sharper impact estimates
> → better questions. Measure the effect with the [golden harness](evaluations.md).

A context card is a Markdown file describing a product, its entities, and its recurring traps. Impact —
the driver behind which questions get asked — is estimated from these cards.

## Add a card (from a clone)

The built-in cards live in the package at `src/requivo/assets/context/`. Working from a clone (or an
editable `pip install -e .`), drop a card there:

```text
src/requivo/assets/context/
  hris.md        ← HR / people platforms
  crm.md         ← sales & pipeline tools
  erp.md         ← finance & operations suites
  my-product.md  ← yours
```

Files prefixed with `_` (e.g. `_template.md`) are ignored. Copy `_template.md` to start.

## Add a card (pip install, no checkout)

Drop cards in a user directory — no need to touch the package:

```bash
export REQUIVO_CONTEXT_DIR=~/.config/requivo/context   # also the default location
mkdir -p "$REQUIVO_CONTEXT_DIR" && $EDITOR "$REQUIVO_CONTEXT_DIR/my-product.md"
```

User cards merge with the built-ins; a user card whose name matches a built-in **overrides** it, so you
can tweak a bundled card without editing the package.

## Scoping a session to relevant cards

By default every card is loaded for every request, so cards can dilute one another. Scope a session to
the cards that matter:

```bash
requivo discover --context b2b-platform,financial-reporting "…"
```

The selection is held constant across the session's turns (so the cached system prompt survives) and
reused by the generators.
