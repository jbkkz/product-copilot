"""Context cards + prompt assembly — deterministic, provider-free.

This is the string-assembly half of what used to live in `core/llm.py`: it reads the bundled prompt
files, the framework schema, and the context cards, and injects them into a prompt template. It makes
**no LLM call and imports no provider** — it only turns assets into a system-prompt string — so it is
safe to keep in `core`. The provider imports `build_prompt()` to feed a model; the CLI and `doctor`
import `available_cards()` to validate a `--context` selection, and `check_selection()` to ask
whether a *saved* selection still resolves without paying for a turn to find out. None of it needs
the SDK.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from requivo.core.errors import ContextUnreadableError, EmptySelectorTokenError, RequivoError, UnknownContextCardError
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
        # `Path.glob` swallows `PermissionError` and yields nothing, so a directory that cannot be
        # read is indistinguishable from one holding no cards — the absence this module is most
        # expensive to get wrong, since the empty result then reads as a complete vocabulary and
        # every card in that directory becomes an "unknown context card" whose stated remedy is to
        # restore a file that is already there. `iterdir()` raises where `glob` does not, so it is
        # used as the readability probe; the selection itself still goes through `glob`, whose match
        # rule (case-insensitive on Windows, case-sensitive on POSIX) is deliberately left alone.
        # The cost is one extra directory walk over a handful of files.
        try:
            list(directory.iterdir())
        except OSError as e:
            raise ContextUnreadableError(
                f"the context-card directory {directory} exists but cannot be read: {e}. Fix its "
                "permissions — cards in it would otherwise be reported as missing.",
                details={"directory": str(directory)},
            ) from e
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
    Refusing does break a session that used to appear to work — it appeared to work while producing
    worse questions for an invisible reason — and the recovery is to put the card back, or to point
    `REQUIVO_CONTEXT_DIR` at wherever it now lives. There is deliberately no fallback to "then load
    nothing": that is the bug. The refusal is no longer *discovered* by the next paid turn either —
    `check_selection` asks this same guard as a question, and `doctor` and `session verify` both run
    it. One gap remains and is out of scope here: no verb re-scopes a session's `context_cards` after
    creation, so restoring the card is the only recovery.

    `only=[]` is refused for the same reason: a selection that selects nothing is not the same thing
    as `None`, the explicit "no restriction" sentinel, and guessing which one was meant is how this
    class of bug gets written.
    """
    paths = _card_paths()
    # `only` is materialised before the guard iterates it — a generator read twice yields nothing
    keep = _selection_keys(list(only), paths) if only is not None else None
    cards = [f"## {stem}\n{paths[stem].read_text()}"
             for stem in sorted(paths)
             if keep is None or stem.lower() in keep]
    return "\n\n".join(cards)


def _selection_keys(only: list[str], paths: dict[str, Path]) -> set[str]:
    """The guard a card selection must pass, as one function: the normalized keys it names, or the
    refusal it earns.

    It exists so that the check and the thing checked cannot drift. `load_context` applies it, and
    `check_selection` asks it as a question — a health check that reimplemented the rule would
    eventually answer differently from the call it is supposed to predict, which is this issue's own
    defect class one level up.
    """
    wanted = normalize_tokens(only, what="context card")
    if not wanted:
        raise EmptySelectorTokenError(
            "an empty context-card selection selects nothing. Pass no selection at all to load "
            "every card, or name the cards to load.",
            details={"selector": "context card", "tokens": 0})
    known = {stem.lower() for stem in paths}
    # echoed as typed, like `resolve_cards` — the two are one design and a caller reading the
    # error should see the name they wrote, not the lower-cased key it was matched by
    missing = [raw.strip() for raw, key in zip(only, wanted) if key not in known]
    if missing:
        raise UnknownContextCardError(
            f"unknown context card(s): {', '.join(missing)}. Available: "
            f"{', '.join(sorted(paths)) or '(none)'}",
            details={"unknown": missing},
        )
    return set(wanted)


def check_selection(only: list[str] | None) -> RequivoError | None:
    """Whether a stored card selection still loads **on this machine** — reported, never raised.

    `None` when it loads (including for `only=None`, the "every card" sentinel, which cannot fail);
    otherwise the exact `RequivoError` `load_context` would raise, so a caller gets the stable code
    and the offending names in `details` rather than a re-derived message.

    Why a session needs asking at all: `resolve_cards` validates a selection **once**, at creation,
    but the cards live outside the session — in the installed package or in `user_context_dir()`. A
    card renamed, an install replaced, a session opened on another machine, and the selection
    persisted in `session.json` no longer resolves. Since that is now a refusal rather than a silent
    empty context, such a session is hard-stopped at its next provider call; this lets `doctor` and
    `session verify` say so first, offline and for free.

    It deliberately does not swallow a failure of the *card directory* itself. An unreadable
    `CONTEXT` raises out of here, because "we could not look" is a different answer from "we looked
    and the card is gone", and the caller owes its reader that distinction rather than a selection
    reported as fine.
    """
    if only is None:
        return None
    try:
        _selection_keys(list(only), _card_paths())
    except (EmptySelectorTokenError, UnknownContextCardError) as e:
        return e
    return None


def build_prompt(name: str, only: list[str] | None = None) -> str:
    """Load a prompt file and inject the schema + product context (optionally a subset of cards)."""
    schema = (FRAMEWORK / "model_schema.json").read_text()
    text = (PROMPTS / name).read_text()
    return text.replace("{{SCHEMA}}", schema).replace("{{CONTEXT}}", load_context(only))
