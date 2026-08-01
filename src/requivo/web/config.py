"""Web configuration + provider capability probe — no secrets ever reach the browser.

The Anthropic API key is read from the *server* environment (never a form field, never rendered into
HTML). The UI only needs to know *whether* a provider action is possible, so it can offer discovery /
generation or fall back to 'create session only' — that boolean is all that crosses to the template.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Field length ceilings — a local app still bounds input so a pasted megabyte can't wedge a turn.
MAX_REQUEST_CHARS = 20_000
MAX_ANSWERS_CHARS = 20_000
MAX_SLUG_CHARS = 80


@dataclass(frozen=True)
class ProviderStatus:
    sdk_installed: bool   # the `anthropic` extra is importable
    key_present: bool     # ANTHROPIC_API_KEY is set in the server environment

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
            return "Set ANTHROPIC_API_KEY in the server environment to enable provider actions."
        return ""


def provider_status() -> ProviderStatus:
    """Probe the provider without importing a client or touching the key value itself."""
    try:
        from requivo.providers.anthropic import Anthropic  # the SDK handle, or None if not installed
        sdk = Anthropic is not None
    except Exception:
        sdk = False
    return ProviderStatus(sdk_installed=sdk, key_present=bool(os.getenv("ANTHROPIC_API_KEY")))
