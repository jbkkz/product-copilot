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

## Cost and the usage footprint

A discovery is a few calls (one per turn, up to 8) plus one per generated artifact. The system prompt
(prompt + schema + context cards) is **prompt-cached** across a session, so the repeated calls of a run
are cheap.

Every command that hits the API prints its footprint when it finishes — calls, tokens (with the cached
share), latency, and an estimated cost — so you see the real number for *your* request. Tokens are
exact; the cost is a labelled estimate from a dated rate table, never treated as authoritative.

## Adding a provider

A provider implements the `ReasoningProvider` protocol in `providers/base.py`:

| Method | Returns |
|---|---|
| `analyze(request, current_model=…, answers=…, only=…)` | a validated `EngineOutput` — one discovery turn |
| `generate(artifact_type, model, only=…, **kwargs)` | the typed contract for that artifact |
| `model_name()` | the reasoning model, recorded on the session |
| `provenance(op, only=…)` | provider / model / prompt identity, recorded on each revision |

`analyze` returns a *resolved* model: a refinement reply says nothing about `decisions`, `challenges`
and `opportunities` (the engine prompt does not ask a turn to re-derive the brief), so a provider
parses its reply as a `ModelProposal` and resolves it against the `current_model` it was given —
carrying the established reasoning forward instead of returning a model that appears to have deleted
it. A provider that skips this step hands back a model with an empty reasoning layer, and the apply
path will faithfully store it.

`DiscoveryService` talks to the protocol and nothing else, so a second provider is a constructor
argument rather than a fork of the orchestration:

```python
DiscoveryService(MyProvider()).start("A leave approval system.")
```

Provenance comes from the provider rather than being assembled by the service, so a revision produced
by another implementation is stamped with *its* name and *its* prompt hash — nothing hard-codes
`"anthropic"`. `tests/test_sessions.py` runs a whole discovery through a provider that has no vendor
behind it; that test is what keeps the seam honest. The Core stays provider-free either way.
