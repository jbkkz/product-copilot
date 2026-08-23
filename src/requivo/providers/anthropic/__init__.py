"""The Anthropic reasoning provider — the only package that calls the Anthropic API.

`requivo.core` imports none of this. `anthropic` is an **optional** dependency
(`requivo[anthropic]`); importing this package without the SDK installed raises a clean, actionable
error at `new_client()` rather than an ImportError deep in a call stack. Prompt assembly (schema +
context injection) is deterministic and lives in `core.context`; this package only *feeds* the
assembled prompt to the model.

**Why it is a package** (#74). It was one 626-line module whose docstring announced seven
responsibilities — the SDK client, the retry loop, the JSON extraction, the usage ledger, the cost
estimate, discovery, and every generator. The point was never the line count: an accurate inventory
of seven things is the signal to split. The cuts, one module each:

- `client.py` — the SDK handle, the optional-import guard, the model id.
- `pricing.py` — Anthropic's dated rate tables and the stamp that puts a rate on a call. Kept apart
  from `client.py` because it is the part edited on a *calendar*, which is #74's own argument for
  cutting it out of the provider at all.
- `completion.py` — `_complete`, the retry loop, the JSON extraction, the truncation check.
- `generators.py` — the discovery turn, the seven generators, and the one `_GENERATORS`/`_OP_PROMPTS`
  registry every surface reaches through.
- `provider.py` — `AnthropicProvider`, the `ReasoningProvider` face over all of it.

Two things #74 grouped under `usage.py` left this package entirely, because #167 is the other half of
the same question — *which package does a neutral concept live in*: the ledger is `requivo.usage`
and `EngineError` is `requivo.providers.errors`. Neither is Anthropic's, and a renderer or a web
surface should not have to name a vendor to reach one.

This module re-exports the **public** surface, so `from requivo.providers.anthropic import X` keeps
working for every existing caller. `requivo.providers` is explicitly *not* a stable API
(`docs/compatibility.md`), so the re-export is a convenience rather than a promise — but a split is
not a good enough reason to make callers chase five new module paths.

The underscore names are deliberately **not** re-exported. `_complete`, `_extract_json` and
`_GENERATORS` are exercised directly by the suite, and a test that imports them from the module
they live in says where they live; laundering a private name through a package `__init__` is how a
split ends up describing a structure nobody can see from a call site.
"""

from __future__ import annotations

from requivo.providers.anthropic.client import MODEL_DEFAULT, Anthropic, APIError, current_model_name, new_client
from requivo.providers.anthropic.completion import MAX_OUTPUT_TOKENS
from requivo.providers.anthropic.generators import (
    advise,
    answer_turn,
    derive_stories,
    estimate,
    generate_criteria,
    generate_epic,
    generate_prd,
    generate_release,
    prompt_version,
    run,
)
from requivo.providers.anthropic.pricing import PRICING_AS_OF, price_call, price_per_mtok
from requivo.providers.anthropic.provider import AnthropicProvider

# `EngineError`, `UsageLedger`, `CallRecord` and `track_usage` are **not** re-exported here, and the
# omission is the point of #167 rather than an oversight. Re-exporting them would keep
# `from requivo.providers.anthropic import UsageLedger` working, which is precisely the import a
# renderer should not be able to write: the leak would stay one line away and nothing in the tree
# would catch the next one. They are `requivo.usage` and `requivo.providers.errors` now, and a
# caller that wants them says so.
__all__ = [
    "MAX_OUTPUT_TOKENS",
    "MODEL_DEFAULT",
    "PRICING_AS_OF",
    "APIError",
    "Anthropic",
    "AnthropicProvider",
    "advise",
    "answer_turn",
    "current_model_name",
    "derive_stories",
    "estimate",
    "generate_criteria",
    "generate_epic",
    "generate_prd",
    "generate_release",
    "new_client",
    "price_call",
    "price_per_mtok",
    "prompt_version",
    "run",
]
