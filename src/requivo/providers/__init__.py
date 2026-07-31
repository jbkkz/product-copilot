"""Reasoning providers — the only place an LLM is called.

`requivo.core` is provider-free by construction: it validates, versions, and reasons over the model,
but it never produces one. A provider does that. `base.ReasoningProvider` is the seam; `anthropic`
is the one concrete implementation today (behind the optional `requivo[anthropic]` extra). The
Claude Code surface is a *second* provider that lives outside Python entirely — Claude itself reasons
and calls the deterministic CLI — so it needs no module here.
"""
