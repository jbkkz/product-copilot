"""Context cards + prompt assembly — deterministic, provider-free.

This is the string-assembly half of what used to live in `core/llm.py`: it reads the bundled prompt
files, the framework schema, and the context cards, and injects them into a prompt template. It makes
**no LLM call and imports no provider** — it only turns assets into a system-prompt string — so it is
safe to keep in `core`. The provider imports `build_prompt()` to feed a model; the CLI and `doctor`
import `available_cards()` to validate a `--context` selection, none of which needs the SDK.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from requivo.core.errors import EmptySelectorTokenError, UnknownContextCardError
from requivo.core.selectors import normalize_tokens
from requivo.paths import CONTEXT, FRAMEWORK, PROMPTS, user_context_dir


def _card_paths() -> dict[str, Path]:
    """Loadable context cards keyed by stem: the bundled cards in the package, plus any the user drops
    in `user_context_dir()` (so a pip-installed setup is extensible without a source checkout). A user
    card whose stem matches a bundled one **overrides** it — you can tweak a built-in without editing
    the package. `_`-prefixed files are skipped. Emitted in sorted-stem order so the assembled system
    is deterministic and the prompt cache holds."""
    paths: dict[str, Path] = {}
    for directory in (CONTEXT, user_context_dir()):  # user dir second → its cards win on stem clash
        if not directory.exists():
            continue
        for p in sorted(directory.glob("*.md")):
            if not p.name.startswith("_"):
                paths[p.stem] = p
    return paths


def available_cards() -> list[str]:
    """Stems of the loadable context cards (bundled + user), sorted — the vocabulary of the
    `--context` selector."""
    return sorted(_card_paths())


def resolve_cards(tokens: Iterable[str]) -> list[str] | None:
    """Map caller-supplied card names to card stems, case-insensitively. Returns None when *no*
    selection was made (== all cards), and raises on a name that does not exist or on an empty token.

    The failure mode this closes is silent *widening*: filtering unknown names out of the list leaves
    an empty selection, which every downstream reader treats as "load every card" — so a typo in a
    two-card selection quietly loads all of them. One resolver, shared by the CLI and the Web, so no
    surface can be lenient where another is strict.

    It used to leave one door into that same widening open. `if not key: continue` dropped an empty
    token, so a selection of *only* empty tokens (`--context ","`) fell through to `picked or None`
    and bought every card — the widening this function's own docstring says it closes, reached
    through the empty-token entrance. The empty-token rule now lives in `normalize_tokens`, shared
    with every other selector, so `picked` can only be empty when `tokens` itself was.
    """
    tokens = list(tokens)
    keys = normalize_tokens(tokens, what="context card")
    avail = {c.lower(): c for c in available_cards()}
    picked, unknown = [], []
    for raw, key in zip(tokens, keys):
        # an unknown name is echoed as typed (stripped), so the error names what the caller wrote
        (picked if key in avail else unknown).append(avail.get(key, raw.strip()))
    if unknown:
        raise UnknownContextCardError(
            f"unknown context card(s): {', '.join(unknown)}. Available: {', '.join(available_cards())}",
            details={"unknown": unknown},
        )
    return picked or None


def load_context(only: list[str] | None = None) -> str:
    """Concatenate the context cards. `only` (card stems) restricts the set — this is how a session
    trims irrelevant cards so they don't dilute impact estimation (every card is loaded otherwise).
    Selection is per-session, so the assembled system stays byte-identical across a run's calls and
    the prompt cache still holds.

    **A selection that resolves to nothing is a refusal, not an empty context.** `resolve_cards` is
    the guard on the way in, but it runs once, at session creation; this function is called on every
    later turn with the list read back out of `session.json`. A card renamed, or a session opened on
    a machine where a `user_context_dir()` card does not exist, therefore used to swap `{{CONTEXT}}`
    for the empty string on every subsequent call — the engine reasoning with no product context at
    all, which is the `information_value = uncertainty x impact` driver gone, silently, mid-session.
    Refusing does break a session that used to appear to work; it appeared to work while producing
    worse questions for an invisible reason, and the caller can restore the card or re-scope the
    session. `only=[]` is refused for the same reason: a selection that selects nothing is not the
    same thing as `None`, the explicit "no restriction" sentinel, and guessing which one was meant is
    how this class of bug gets written.
    """
    paths = _card_paths()
    keep = None
    if only is not None:
        wanted = normalize_tokens(only, what="context card")
        if not wanted:
            raise EmptySelectorTokenError(
                "an empty context-card selection selects nothing. Pass no selection at all to load "
                "every card, or name the cards to load.",
                details={"selector": "context card", "tokens": 0})
        known = {stem.lower() for stem in paths}
        missing = [name for name in wanted if name not in known]
        if missing:
            raise UnknownContextCardError(
                f"unknown context card(s): {', '.join(missing)}. Available: "
                f"{', '.join(sorted(paths)) or '(none)'}",
                details={"unknown": missing},
            )
        keep = set(wanted)
    cards = [f"## {stem}\n{paths[stem].read_text()}"
             for stem in sorted(paths)
             if keep is None or stem.lower() in keep]
    return "\n\n".join(cards)


def build_prompt(name: str, only: list[str] | None = None) -> str:
    """Load a prompt file and inject the schema + product context (optionally a subset of cards)."""
    schema = (FRAMEWORK / "model_schema.json").read_text()
    text = (PROMPTS / name).read_text()
    return text.replace("{{SCHEMA}}", schema).replace("{{CONTEXT}}", load_context(only))
