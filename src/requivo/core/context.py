"""Context cards + prompt assembly — deterministic, provider-free.

This is the string-assembly half of what used to live in `core/llm.py`: it reads the bundled prompt
files, the framework schema, and the context cards, and injects them into a prompt template. It makes
**no LLM call and imports no provider** — it only turns assets into a system-prompt string — so it is
safe to keep in `core`. The provider imports `build_prompt()` to feed a model, which assembles the
cards through `load_context()`; every surface imports `resolve_cards()` to validate a `--context`
selection on the way in; `doctor` and `session verify` import `check_selection()` to ask whether a
*saved* selection still resolves without paying for a turn to find out, and `available_cards()` to
report the vocabulary itself. None of it needs the SDK.

Exactly three of those resolve a name against the installed cards — `resolve_cards`, `load_context`
and `check_selection` — and they must agree about an install that has none, so
`_cards_for_selection()` is the single guarded read all three share. `available_cards()` is
deliberately outside it, because reporting an empty install is its job rather than refusing one.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from requivo.core.errors import (
    ContextUnreadableError,
    EmptySelectionError,
    EmptySelectorTokenError,
    NoContextCardsError,
    RequivoError,
    UnknownContextCardError,
    UnsafeSelectorTokenError,
)
from requivo.core.selectors import normalize_tokens
from requivo.paths import CONTEXT, FRAMEWORK, PROMPTS, user_context_dir

# Every refusal `load_context` can produce, so `check_selection` can report exactly what the loader
# would raise without listing them twice. `ContextUnreadableError` is deliberately absent: "we could
# not look" is not a verdict about the selection, and `check_selection` lets it propagate.
#
# `UnsafeSelectorTokenError` has to be in here rather than escaping (#40). A hostile card name only
# ever arrives *persisted* — `create_session` resolves the selection against the installed cards, so
# the door is `session import` or a hand-edited `session.json` — which means the first code to see
# one is a health check. A health check that raises takes the whole listing down with it rather than
# degrading the one row (invariant 15), and `doctor` would then answer nothing at all about a
# workspace containing one tampered session. Reported, never raised.
_SELECTION_REFUSALS = (
    NoContextCardsError, EmptySelectionError, EmptySelectorTokenError, UnknownContextCardError,
    UnsafeSelectorTokenError,
)


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


def _cards_for_selection() -> dict[str, Path]:
    """The card table a **selection** is resolved against: `_card_paths()` with the empty-install
    guard already applied.

    It exists because "call `_require_any_card` too" is a rule three functions each had to remember,
    and one of them did not (#41). The miss had a mechanism worth naming: `load_context` and
    `check_selection` reach the table through `_card_paths()` directly, while `resolve_cards` reached
    it through `available_cards()` — so a sweep over the callers of `_card_paths()` found two of the
    three and reported itself complete. One name, called by all three, makes that sweep exhaustive
    and makes a fourth selector inherit the guard instead of re-deriving it.

    `available_cards()` deliberately does **not** route through here, and that is the reason the
    guard cannot simply live in `_card_paths()` itself: `doctor` reports the card vocabulary in three
    states — `ok`, `empty`, `unreadable` — and `empty` is a public `--json` field it can only produce
    by *observing* an install with no cards rather than raising on one. A table that refused to be
    empty would leave the one caller whose job is to see the emptiness unable to. So the split is
    between looking (`_card_paths`, `available_cards`) and selecting (this), not between guarded and
    unguarded by accident.
    """
    paths = _card_paths()
    _require_any_card(paths)
    return paths


def available_cards() -> list[str]:
    """Stems of the loadable context cards (bundled + user), sorted — the vocabulary of the
    `--context` selector.

    Reports an empty install as `[]` rather than refusing it; `_cards_for_selection` is the guarded
    read, and the paragraph there says why this one must stay observational."""
    return sorted(_card_paths())


def card_byte_size(path: Path) -> int:
    """The bytes one card contributes to a prompt — **not** its size on disk.

    `st_size` is the wrong measurement and the difference is a platform, not a rounding: git checks
    a text file out with CRLF line endings on Windows by default and this repository declares no
    `.gitattributes`, while `load_context()` reads in text mode, where the decoder collapses CRLF to
    LF before a single byte reaches `{{CONTEXT}}`. So `st_size` over-counts by one byte per line on
    exactly one platform, and the number #257 exists to disclose — what a card costs you per call —
    was inflated there in the CLI's pre-call line and the Web create form's hint alike.

    Decoding and re-encoding is what makes the answer the same everywhere, because it is the same
    operation the loader performs. It also means an undecodable card raises here rather than
    reporting a plausible size for a file `load_context()` would refuse — the disclosure and the
    loader now fail on the same inputs, which is the point (invariant 16).
    """
    return len(path.read_text(encoding="utf-8").encode("utf-8"))


def average_card_byte_size() -> int | None:
    """Average prompt weight, in bytes, across every loadable card (bundled + user) — `None` for an
    empty install. Used only to disclose the cost/dilution tradeoff of the all-cards default before a
    paid call (#257): the CLI's pre-call line and the Web create form's hint both read this rather
    than a number typed into prose, so the figure moves itself when a card is added, removed or
    resized, instead of quietly going stale the way a hardcoded one would (CLAUDE.md's own rule about
    a count nothing can falsify). Deliberately observational, like `available_cards()` beside it —
    a UI hint has no business raising on an empty install any more than the card list does.

    Measured through `card_byte_size`, never `st_size`; that function says why."""
    paths = _card_paths()
    if not paths:
        return None
    return sum(card_byte_size(p) for p in paths.values()) // len(paths)


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

    **An install with no cards at all is refused here too, ahead of the whole selection** (#41) —
    ahead of the name lookup and ahead of the token-shape checks, so on a card-less install even a
    stray comma reports the install rather than the comma. That precedence is not new and is not this
    function's to choose: `load_context` has diagnosed the install ahead of `_selection_keys` since
    #33, deliberately and with a test on it, and the two are read side by side. This used to run off
    a bare `available_cards()`, so on a card-less install it answered
    `unknown_context_card` for a name that was typed correctly — sending the reader to check their
    spelling when the fault is that there is nothing to match against, which is the sentence
    `_require_any_card`'s own docstring gives as the reason it must run first. This is the earliest
    and therefore the worst place to get it wrong: every surface resolves its selection here, at
    session creation, so a broken install blamed the reader (400 in the Web) and then blamed itself
    (500) on the very next call — one condition wearing two verdicts, the wrong one arriving first.

    **A selection of no tokens at all is deliberately left outside that guard**, and the reason is
    uniformity rather than leniency. `[]` is not an empty token: `normalize_tokens` documents it as
    *no selection was made*, and the answer is `None`, the every-card sentinel. `SessionService`,
    the CLI and the deterministic verbs all skip this function entirely when no cards were named, so
    refusing `[]` would make the Web — the one caller that passes it through — the single surface
    that refuses, which is the lenient/strict split this function exists to prevent. The install is
    still caught, by `load_context`, at the point the cards are actually read.
    """
    tokens = list(tokens)
    if not tokens:
        return None
    # One guarded read of the table, used for both the lookup and the error's `Available:` line. Those
    # were two separate `available_cards()` calls, so the vocabulary a reader was told to choose from
    # was enumerated separately from the one their name was matched against.
    paths = _cards_for_selection()
    keys = normalize_tokens(tokens, what="context card")
    # `sorted` is a tie-break, not tidiness, so do not drop it: two installed stems can differ only
    # in case — a bundled `foo.md` beside a user `Foo.md` — and they are two entries here, since
    # `_card_paths()` only collapses an *exact* stem clash. Which one a typed `foo` resolves to is
    # then decided by iteration order, and `sorted` is what `available_cards()` applied before this
    # read replaced it. Preserved deliberately: which of the two should win is a real question, and a
    # bug fix silently loading a different card than it did yesterday is not the place to answer it.
    avail = {stem.lower(): stem for stem in sorted(paths)}
    picked, unknown = [], []
    for raw, key in zip(tokens, keys):
        # an unknown name is echoed as typed (stripped), so the error names what the caller wrote
        (picked if key in avail else unknown).append(avail.get(key, raw.strip()))
    if unknown:
        raise UnknownContextCardError(
            f"unknown context card(s): {', '.join(unknown)}. Available: {', '.join(sorted(paths))}",
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
    it. Restoring the card is one recovery; `session rescope` (#168) is the other, and re-scopes a
    session's `context_cards` without touching `session.json` by hand.

    `only=[]` is refused for the same reason: a selection that selects nothing is not the same thing
    as `None`, the explicit "no restriction" sentinel, and guessing which one was meant is how this
    class of bug gets written.

    **An install with no cards at all is refused too** (#33), and that is the wide instance the two
    narrow guards above left open. Both of them are about a *selection*; `only=None` never reaches
    either, and `only=None` is exactly what a session with no card selection sends on every turn. So
    a wheel or container layer that shipped `assets/` without `assets/context/` produced an empty
    `{{CONTEXT}}` on every paid call, forever, and nothing on that path said so. One rule covers all
    three: an empty context is never a legitimate thing to send a provider, whatever emptied it.
    """
    paths = _cards_for_selection()
    # `only` is materialised before the guard iterates it — a generator read twice yields nothing
    keep = _selection_keys(list(only), paths) if only is not None else None
    # `encoding` is explicit because `read_text()` defaults to the *locale's* encoding, not the file's:
    # every bundled card carries an em dash or an arrow, so on a cp1252 Windows console they decoded to
    # mojibake and were sent to the provider that way, and under an ASCII locale the read raised
    # outright. Neither is visible from a UTF-8 machine, which is every developer machine here.
    cards = [f"## {stem}\n{paths[stem].read_text(encoding='utf-8')}"
             for stem in sorted(paths)
             if keep is None or stem.lower() in keep]
    return "\n\n".join(cards)


def _require_any_card(paths: dict[str, Path]) -> None:
    """Refuse an install that has no context cards at all.

    The third state beside "the card you named is not there" and "we could not look": we looked, at
    every root, and there is nothing. It is checked before the selection because with no cards
    installed *every* name is unknown — technically true, and it sends the reader to check the name
    they typed when the fault is that there is nothing to match against.
    """
    if paths:
        return
    roots = [str(CONTEXT), str(user_context_dir())]
    raise NoContextCardsError(
        "no context cards are installed, so there is no product context to reason from — impact "
        "estimation is the product's central idea and it runs on these cards. Looked in: "
        f"{' and '.join(roots)}. This install is incomplete: reinstall requivo, or point "
        "REQUIVO_CONTEXT_DIR at a directory holding your cards.",
        details={"roots": roots})


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
        # `EmptySelectionError`, not `EmptySelectorTokenError` (#35). They are two facts, as
        # `normalize_tokens`' own docstring argues: an empty *token inside* a selection carries a
        # `position`, a selection that is *itself* empty has no position to carry. They shared one
        # code with two `details` shapes behind it, so a consumer following the documented advice —
        # match the code — and reading `details["position"]` got a KeyError from a payload that
        # correctly carried the code it matched.
        raise EmptySelectionError(
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

    `None` when it loads; otherwise the exact `RequivoError` `load_context` would raise, so a caller
    gets the stable code and the offending names in `details` rather than a re-derived message.

    `only=None` — the "every card" sentinel — used to short-circuit to `None` here on the grounds
    that it cannot fail. That was true until #33 and false after it: with no cards installed at all,
    `load_context(None)` refuses, and a checker that reports a session as fine while the call it
    predicts refuses is this module's own defect class one level up. It now asks the same two guards
    the loader applies, in the same order.

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
    try:
        paths = _cards_for_selection()
        if only is not None:
            _selection_keys(list(only), paths)
    except _SELECTION_REFUSALS as e:
        return e
    return None


def build_prompt(name: str, only: list[str] | None = None) -> str:
    """Load a prompt file and inject the schema + product context (optionally a subset of cards)."""
    # Explicit encoding for the same reason as the cards above: these assets are UTF-8 on disk and
    # `read_text()` would decode them with whatever the locale happens to be.
    schema = (FRAMEWORK / "model_schema.json").read_text(encoding="utf-8")
    text = (PROMPTS / name).read_text(encoding="utf-8")
    return text.replace("{{SCHEMA}}", schema).replace("{{CONTEXT}}", load_context(only))
