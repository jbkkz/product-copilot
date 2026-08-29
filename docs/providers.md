# Providers

> The Core never calls an LLM. A **provider** does. Today there is one: Anthropic.

## The Anthropic provider

Automated discovery and generation (the CLI's `discover` / `answer` / generators, and the Web's
provider actions) call the Claude API through the provider. It is an **optional extra**:

```bash
pip install 'requivo[anthropic]'     # or:  uv tool install 'requivo[anthropic]'
export ANTHROPIC_API_KEY="…"
```

The key is read from the environment (or `.env`) and used only to authenticate those calls. In **Claude
Code** mode there is no provider and no API key — Claude reasons in your session; the deterministic CLI
applies. `requivo demo`, `requivo status` and `requivo impact` make no API call at all.

## Models

Developed and measured against `claude-sonnet-5` (the default). Any current Claude model works via the
`MODEL` environment variable:

```bash
MODEL=claude-opus-4-8 requivo discover "…"
```

The exact model a session ran against is recorded in its revision provenance (see
[session-format.md](session-format.md)).

## What a run costs

You pay Anthropic directly, on your own key, and these are the numbers to expect before you spend
anything. `requivo demo` is free and needs no key at all.

| Step | Calls | Input tokens | Output tokens | Estimated cost |
|---|---|---|---|---|
| One provider call | 1 | 7,300–8,900 | 1,300–3,800 | **$0.03–$0.06** |
| A full interactive discovery (8 turns + the assessment) | 9 | — | — | **$0.25–$0.50** |
| Every remaining artifact (prd, stories, estimate, criteria, epic, release) | 7 | — | — | **$0.19–$0.39** |
| A complete session, end to end | 16 | — | — | **$0.44–$0.89** |

Priced at **$2.00 / $10.00 per million tokens** (input / output) for `claude-sonnet-5`, rates as of
**2026-08-29**. An estimate, never a bill — and a *derived* one: `tests/test_cost_claims.py`
recomputes every figure in that table from the rate table in `providers/anthropic/pricing.py` and
fails when the two disagree, so a price change breaks this page instead of quietly outdating it.

Two honest limits, because a number with an unstated method is worth less than no number.

- **The token counts are estimated at four characters per token.** The input figure is the real
  assembled system prompt for each operation; the output figure is the real replies captured in
  `fixtures/golden/`. Neither is a token count from the API — no ledger output is committed to this
  repository, and the test suite makes no calls. Both are bracketed by the same test, so a prompt
  that grows past the published range is a red build.
- **The table charges every call at full price.** Prompt caching (below) makes a long interactive
  discovery cheaper than nine independent calls, and the retry path makes a rare one dearer. The
  range is wide enough to hold both, and the exact number for *your* request is printed by the verb
  that spent it.

## The usage footprint

A discovery is a few calls (one per turn, up to 8) plus one per generated artifact. The system prompt
(prompt + schema + context cards) is **prompt-cached where the same prompt is sent again** — a
discovery's turns and a golden capture's K runs — so those repeats cost ~0.1x input instead of full
price.

It is deliberately *not* cached across operations, and that is a saving rather than an omission. Each
operation builds its system prompt from its own template, so a cache breakpoint costs 1.25x input to
write and only pays back on a second send of the identical prefix; a one-call verb (`prd`, `criteria`,
`epic`, `release`, `stories`, `estimate`) normally has no second send, so writing one was a flat ~25%
surcharge on the largest part of its input. Callers declare which they are (`reuse_system`), because
the same generator is one call in the CLI and K calls in the golden harness (#9).

`reuse_system` is on `analyze` too, and it is the caller's answer to *will this exact system prompt be
sent again* — not an instruction about caching. The one looping caller is
`DiscoveryService.draft_turn`, the interactive `discover` loop, which repeats one prompt for up to
eight turns; every other operation is one call and passes the default. An implementation with no
prompt caching may ignore the flag entirely.

One honest caveat: a one-call verb *can* send twice, when the model returns malformed JSON and the
retry loop re-sends the identical prompt. Those retries are no longer cached, so a generator that
retries now pays 2.0x the system block where it used to pay 1.35x. That trade is deliberate — it is
the better bet while a retry is rarer than about one call in four — but it is a real cost on a rare
path rather than a free win.

Every command that hits the API prints its footprint when it finishes — calls, tokens (with the cached
share), latency, and an estimated cost — so you see the real number for *your* request. Tokens are
exact; the cost is a labelled estimate from a dated rate table, never treated as authoritative.

The Web reports the same figures (#253), and it is worth being precise about where, because "every
command" above is a claim about the CLI and used to be read as a claim about the product. A paid
action that answers with a fragment — folding in answers, generating a document — carries the
footprint in that fragment. The two that answer with a redirect record it to the `requivo.web`
logger instead: a 303 has no body, and the alternative would be carrying the numbers to the next
request. Both channels go through the same `UsageLedger`, so a call is never billed and unrecorded.

## Adding a provider

A provider implements the `ReasoningProvider` protocol in `providers/base.py`:

| Member | Is |
|---|---|
| `name` | an attribute — short identity of the implementation (`"anthropic"`), stamped on the session |
| `analyze(request, current_model=…, answers=…, only=…, reuse_system=…)` | a validated `EngineOutput` — one discovery turn |
| `generate(artifact_type, model, only=…, **kwargs)` | the typed contract for that artifact |
| `model_name()` | the reasoning model, recorded on the session |
| `provenance(op, only=…)` | provider / model / prompt identity, recorded on each revision |

`name` is a plain attribute, not a method, because that is how it is read — `DiscoveryService` reaches
for `provider.name` when it claims the session, *before* any reasoning happens, so an implementation
without one fails on its first `discover` rather than at some later edge. It is the first thing to
check when porting a provider.

The protocol is `@runtime_checkable`, so `isinstance(p, ReasoningProvider)` is a real conformance check
and it does cover `name` — Python checks non-method members too. Two limits worth knowing before you
lean on it: `issubclass()` is *refused* for this protocol (`TypeError`, which Python raises for any
protocol carrying a non-method member — use `isinstance`), and `isinstance` checks that a member is
**present**, never that it has the right type or signature. It will not tell you that `analyze` returns
the wrong thing. Nothing inside Requivo runs this check — a provider is injected and duck-typed — so it
is a self-test for an implementation to run against itself, not a gate the library applies to you.

`analyze` returns a *resolved* model: a refinement reply says nothing about `decisions`, `challenges`
and `opportunities` (the engine prompt does not ask a turn to re-derive the brief), so a provider
parses its reply as a `ModelProposal` and resolves it against the `current_model` it was given —
carrying the established reasoning forward instead of returning a model that appears to have deleted
it. A provider that skips this step hands back a model with an empty reasoning layer, and the apply
path will faithfully store it.

Two things a second implementation needs are deliberately **not** in the Anthropic package, so
nothing has to import a competitor's module to reach them (#167):

| Need | Where |
|---|---|
| A transport failure the surfaces already catch (`provider_unavailable`, 502 on the web) | `providers/errors.py` — `EngineError`, no SDK behind it |
| Recording what a call spent | `requivo.usage` — `CallRecord`, `UsageLedger`, `track_usage`, `record_call` |

The ledger holds **no price table**. A record carries `rate_per_mtok` and `priced_as_of`, stamped by
the provider as it files the call, and `cost_usd()` is arithmetic over those. So an implementation
brings its own rates without registering them anywhere, and an estimate spanning a price change is
right on both sides of it — the rate recorded is the one in force when the tokens were spent, not
whichever is live when the total is printed. A provider that prices nothing leaves both fields
absent; `cost_usd()` then returns `None` and the CLI says *no price on file* rather than guessing.

`DiscoveryService` talks to the protocol and nothing else, so a second provider is a constructor
argument rather than a fork of the orchestration:

```python
DiscoveryService(MyProvider()).start("A leave approval system.")
```

Provenance comes from the provider rather than being assembled by the service, so a revision produced
by another implementation is stamped with *its* name and *its* prompt hash — nothing hard-codes
`"anthropic"`. `tests/test_sessions.py` runs a whole discovery through a provider that has no vendor
behind it; that test is what keeps the seam honest. The Core stays provider-free either way.
