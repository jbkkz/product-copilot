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
    from anthropic import Anthropic, APIError, AuthenticationError, PermissionDeniedError, RateLimitError
except ImportError as _e:  # pragma: no cover - exercised only in a no-SDK install
    Anthropic = None  # type: ignore[assignment,misc]
    APIError = Exception  # type: ignore[assignment,misc]
    # Bound to a private sentinel rather than to `Exception`, and the difference is the whole point:
    # `except Exception` would make `completion.py`'s auth arm swallow every transport failure and
    # advise a key remedy for a network drop. A class nothing ever raises catches nothing, which is
    # the correct behaviour when the SDK that defines these is absent -- and the SDK being absent
    # means no call can be made anyway. Pinned by `test_the_typed_error_arms_are_inert_without_the_sdk`.
    class _NeverRaised(Exception):
        """Unreachable stand-in for an SDK error class that is not importable."""

    AuthenticationError = _NeverRaised  # type: ignore[assignment,misc]
    PermissionDeniedError = _NeverRaised  # type: ignore[assignment,misc]
    RateLimitError = _NeverRaised  # type: ignore[assignment,misc]
    _IMPORT_ERROR = _e
else:
    _IMPORT_ERROR = None

MODEL_DEFAULT = "claude-sonnet-5"

# The two environment variables the SDK will authenticate from. `ANTHROPIC_AUTH_TOKEN` is checked
# alongside the key so a bearer-token setup is not false-refused by a guard meant to help; Requivo
# does nothing else to support that flow, and does not need to.
#
# This is narrower than everything the installed SDK itself resolves credentials from -- its own
# `Anthropic.__init__` docstring also documents `ANTHROPIC_PROFILE`, workload identity federation
# env vars, and an on-disk active profile, none of which any Requivo surface checks for (#332,
# filed as a follow-up rather than folded into this tuple: reading those is a design decision, not
# a name to add here).
_AUTH_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def credential_present() -> bool:
    """Whether a credential is visible in the environment, by the same names `new_client()`
    authenticates from -- the **one** definition every "is there a key" reader shares.

    Before #332, `web/config.py` and `deterministic/doctor.py` each kept their own
    `os.getenv("ANTHROPIC_API_KEY")`, current at the time `new_client()` read only that name too.
    #201 widened `new_client()` to `_AUTH_ENV_VARS` for a bearer-token setup and left the other two
    behind, so a working bearer-token install built a client from the CLI while both the web surface
    and `requivo doctor --json` reported no key. This function is what both now read instead of a
    second copy of the tuple above, so the two cannot drift again the next time this tuple widens.
    Pinned by `test_credential_present_is_the_one_definition_new_client_reads`.
    """
    return any(os.getenv(var) for var in _AUTH_ENV_VARS)

# Said once, here, because three surfaces used to say a version of it and the paid CLI path said
# nothing at all.
_NO_KEY_MESSAGE = (
    "No Anthropic API key found. Set ANTHROPIC_API_KEY in your environment, or put it in a `.env` "
    "file in the directory you run from (see .env.example). `requivo doctor` reports whether a key "
    "is visible. You do NOT need a key for `requivo demo`, for the offline verbs (status, impact, "
    "session, model, artifact), or for Requivo inside Claude Code."
)


def new_client() -> Anthropic:
    """Construct an Anthropic client, or refuse cleanly when the install cannot make a call.

    Every provider-backed verb funnels through here, so the two install/config remedies are stated
    once rather than scattered: the SDK is missing, or no credential is visible.

    **The key check is here and not at the call site, because `Anthropic()` does not raise.** The SDK
    constructs fine with no credential and defers auth resolution to the first request, where it
    raises a bare `TypeError` ("Could not resolve authentication method") out of its own internals.
    That threaded through this function, through `_complete`'s `except APIError` and through
    `cli.app()`'s `except RequivoError` untouched, so the single most likely failure of a fresh
    `pip install requivo[anthropic]` -- run the command the demo suggests before setting a key --
    was twenty-five lines of traceback naming neither the environment variable, nor `.env`, nor
    `requivo doctor` (#201). Refusing here also makes it free: nothing is claimed and nothing is
    billed, where the alternative is a session claimed at revision 0 and a 401.
    Pinned by `test_a_missing_api_key_refuses_before_the_sdk_can_traceback`.
    """
    if Anthropic is None:
        raise EngineError(
            "The Anthropic provider is not installed. Install it with `pip install 'requivo[anthropic]'` "
            "(or `uv tool install 'requivo[anthropic]'`). You do NOT need it to use Requivo inside "
            f"Claude Code — that mode uses no API key. (import error: {_IMPORT_ERROR})"
        )
    if not credential_present():
        raise EngineError(_NO_KEY_MESSAGE)
    return Anthropic()


def current_model_name() -> str:
    """The model id this process will call — the env override or the default. Exposed so provenance
    (session.json) records the exact model a discovery ran against."""
    return os.getenv("MODEL", MODEL_DEFAULT)
