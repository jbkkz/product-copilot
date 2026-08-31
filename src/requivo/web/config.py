"""Web configuration + provider capability probe — no secrets ever reach the browser.

The Anthropic API key is read from the *server* environment (never a form field, never rendered into
HTML). The UI only needs to know *whether* a provider action is possible, so it can offer discovery /
generation or fall back to 'create session only' — that boolean is all that crosses to the template.
"""

from __future__ import annotations

from dataclasses import dataclass

from requivo.core.contracts import MAX_INPUT_CHARS

# Field length ceilings — a local app still bounds input so a pasted megabyte can't wedge a turn. Past
# the ceiling the field is *refused*, never trimmed to fit: half a request folded into the model reads
# exactly like a whole one, so the user would never learn which half the engine saw. (The body itself
# is capped earlier still, in `security.py`, before anything is parsed.)
#
# `MAX_REQUEST_CHARS`/`MAX_ANSWERS_CHARS` are aliases for the one cap Core defines (#255) --
# `require_input_within_bounds` enforces it at the service layer, which is the integrity boundary
# (invariant 14) and where it holds for the CLI and any other caller too, not only this route's own
# early check. They stay as two names here because the routes import them for two different fields
# and a route-level rename is a bigger diff than this alias is worth; the *value* is one number now,
# not two hand-kept ones that could drift apart.
MAX_REQUEST_CHARS = MAX_INPUT_CHARS
MAX_ANSWERS_CHARS = MAX_INPUT_CHARS
MAX_SLUG_CHARS = 80


@dataclass(frozen=True)
class ProviderStatus:
    sdk_installed: bool   # the `anthropic` extra is importable
    key_present: bool     # a credential `new_client()` would authenticate from is set (#332)

    @property
    def available(self) -> bool:
        """A provider action (discovery, generation) can actually run."""
        return self.sdk_installed and self.key_present

    @property
    def reason(self) -> str:
        """Why the provider is unavailable, for a clear UI hint (empty when available)."""
        if not self.sdk_installed:
            return "Install the provider: pip install 'requivo[anthropic]'."
        if not self.key_present:
            # Names both names `credential_present()` reads (#332 review) -- a bearer-token-only
            # reader must not be told to set a credential they already have a working equivalent of.
            return (
                "Set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) in the server environment to "
                "enable provider actions."
            )
        return ""


def provider_status() -> ProviderStatus:
    """Probe the provider without importing a client or touching the key value itself.

    `key_present` reads `credential_present()` -- the same env-var names `new_client()` itself
    authenticates from -- rather than a second, independent `os.getenv("ANTHROPIC_API_KEY")`. Before
    #332 this probe checked only that one name while `new_client()` (widened by #201) also accepted
    `ANTHROPIC_AUTH_TOKEN`, so a working bearer-token install reported `key_present=False` here,
    `available` fell to False, and `routes/sessions.py` (which branches on `available`) silently
    dropped every provider action to `create_only` for an install that would actually have worked.
    Pinned by `test_a_bearer_token_alone_is_read_as_a_credential`.
    """
    try:
        # the SDK handle (or None if not installed) and the shared credential probe
        from requivo.providers.anthropic import Anthropic, credential_present
        sdk = Anthropic is not None
        key = credential_present()
    except Exception:
        sdk = False
        key = False
    return ProviderStatus(sdk_installed=sdk, key_present=key)
