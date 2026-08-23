"""The failure vocabulary of the provider seam — provider-neutral, and free of any SDK.

One class today. It lives here rather than in `providers/anthropic/` because nothing about it is
Anthropic's: a transport failure is what the `ReasoningProvider` contract fails with, so a second
implementation raises it too, and it should not have to import it from a competitor's module (#167).
It is not in `core/errors.py` either — core raises errors *about the model*, and its own docstring
draws that line: "Provider-transport failures are a separate family (`providers`), so a bug in
reasoning is never confused with a bug in the model." This module is that family.

Importing nothing but `core.errors` is deliberate. `web/app.py` needs this class to map a transport
failure onto 502, and it used to reach it through `providers/anthropic.py`, which probes for the
optional SDK and pulls in the contracts, the prompt assembly and every generator on the way. One
exception class should not cost that.
"""

from __future__ import annotations

from requivo.core.errors import RequivoError


class EngineError(RequivoError):
    """A clean, provider-transport failure (API unavailable, output truncated). The CLI catches this
    and prints the message without a traceback. A run that raises this never modifies the saved model —
    the call failed before any write. It is a `RequivoError` so a single `except RequivoError` at the
    CLI boundary catches both reasoning-transport and core-validation failures.

    `code` is `provider_unavailable` and is published in the `--json` envelope and in the web error
    banner, so it is fixed independently of where the class lives: this move is a relocation, not a
    rename (`docs/compatibility.md`)."""

    code = "provider_unavailable"
