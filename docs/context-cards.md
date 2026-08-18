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

Every name is checked, on every turn, against the cards actually on disk — not only when the session is
created. A misspelled card is refused rather than dropped, because dropping it leaves an empty
selection and an empty selection means *every* card; a selection that resolves to nothing is refused
for the mirror-image reason, because an empty context is not visibly different from a good one:

```console
$ requivo discover --context b2b-platform, "…"
empty context card selector at position 1 — an empty token matches everything, so it would widen the
selection instead of narrowing it. Remove it (a stray comma is the usual cause), or pass no selector
at all to select everything deliberately.
```

The case that costs the most is the one you did not type. A session scoped to a card that lives in
`REQUIVO_CONTEXT_DIR` carries that name in its `session.json`; open the same session on another
machine, or rename the card, and the name no longer resolves. Requivo says so and stops:

```console
$ requivo answer my-session "…"
unknown context card(s): acme-crm. Available: b2b-platform, document-management, event-ops, financial-reporting
```

That is deliberately a refusal rather than a quiet fallback to no context at all. Impact estimation is
what decides which questions get asked, it is estimated from these cards, and a turn that runs without
them produces a plausible answer for a reason nothing on screen would have shown you.

**To recover, put the card back** — restore the file, or point `REQUIVO_CONTEXT_DIR` at wherever it now
lives — and the turn runs.

You do not have to wait for a paid turn to find out. Three offline checks answer it:

```bash
requivo doctor                     # lists every session whose saved cards no longer resolve
requivo session verify <slug>      # reports it for one session, and exits non-zero
requivo context --session <slug>   # prints the cards that session is actually reasoning with
```

`doctor` reports it under `sessions.unresolved_cards`, and `session verify` in a `context_cards`
block beside the session's integrity problems — beside, and not among, because a card lives outside
the session directory: the same session is fine on a machine that has the card. See
[cli.md](cli.md#context-cards-a-session-can-no-longer-find) for why that distinction is load-bearing
for `session import`.

A card directory that exists but **cannot be read** is reported separately, as
`context_unreadable`, and never as a missing card. The two have opposite remedies — restore the file
versus fix the permissions — and they used to be indistinguishable, because `Path.glob` yields
nothing rather than raising when a directory is denied: the card vocabulary came back quietly short,
so a card sitting in that directory was reported as unknown and you were told to put back a file that
was already there.

One limit remains before you scope a session to a card that only exists on one machine: there is no
verb that re-scopes an existing session's cards, so the alternative to restoring the file is editing
the `context_cards` key in the session's `session.json` by hand — the layout is a published contract,
see [session-format.md](session-format.md).
