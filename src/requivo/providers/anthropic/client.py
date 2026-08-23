"""The SDK handle and the model id — the two facts about the vendor that everything else needs.

`anthropic` is an **optional** dependency (`requivo[anthropic]`); importing this module without the
SDK installed binds `Anthropic` to None and raises a clean, actionable error at `new_client()`
rather than an ImportError deep in a call stack. That is why the whole package can be imported by a
surface that only wants to probe whether the extra is present (`web/config.py`).
"""

from __future__ import annotations

import os

from requivo.providers.errors import EngineError

try:  # The SDK is an optional extra: the deterministic core + CLI work without it (Claude Code mode).
    from anthropic import Anthropic, APIError
except ImportError as _e:  # pragma: no cover - exercised only in a no-SDK install
    Anthropic = None  # type: ignore[assignment,misc]
    APIError = Exception  # type: ignore[assignment,misc]
    _IMPORT_ERROR = _e
else:
    _IMPORT_ERROR = None

MODEL_DEFAULT = "claude-sonnet-5"


def new_client() -> Anthropic:
    """Construct an Anthropic client, or raise a clean error if the optional SDK is not installed.
    Every provider-backed CLI verb funnels through here so the 'install requivo[anthropic]' guidance
    is stated once, not scattered."""
    if Anthropic is None:
        raise EngineError(
            "The Anthropic provider is not installed. Install it with `pip install 'requivo[anthropic]'` "
            "(or `uv tool install 'requivo[anthropic]'`). You do NOT need it to use Requivo inside "
            f"Claude Code — that mode uses no API key. (import error: {_IMPORT_ERROR})"
        )
    return Anthropic()


def current_model_name() -> str:
    """The model id this process will call — the env override or the default. Exposed so provenance
    (session.json) records the exact model a discovery ran against."""
    return os.getenv("MODEL", MODEL_DEFAULT)
