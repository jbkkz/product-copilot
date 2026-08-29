"""The shared rule for a caller-supplied selector — the one place a token list becomes a filter.

Three selectors in this package turn caller-typed tokens into a subset of a vocabulary:
`resolve_cards` and `load_context` (context cards) and `resolve_slots` (slot ids). Each of them
independently reached the same failure, in opposite directions and always silently:

  * an **empty or whitespace token** matches every candidate a substring test is run against, and is
    invisible to an exact-match test. `--slots "workflow,"` reported the *entire* model as changed
    with zero unmatched tokens; `--context ","` loaded every card while looking like a narrowing.
  * dropping such a token instead is no safer, because it can leave the selection empty, and an
    empty selection is what every reader downstream spells "all of them".

Both are the absence-shaped bug invariant 3 names — *refuse, don't filter* — and a selection is
exactly where it is most expensive, because the widened answer is well-formed and arrives calmly.

So the rule lives here rather than in each selector: an empty token is a **refusal**, stated once, so
a fourth selector inherits it instead of re-deriving it. What a selector does with a token that is
well-formed but matches nothing is its own business and stays at the call site — the vocabularies
differ (a card name is exact, a slot token is an id *or* a label substring) and folding those into one
helper would mean a flag per caller, which is three local rules again with an import in front.

A second rule now lives here for the same reason (#40): a token carrying a **control character** is a
refusal too. Every selector echoes an offending token back into an error message, and a card name
additionally *persists* — `session.json` stores the selection, `session import` passes it through
intact, and `doctor` and `session verify` render it into a receipt. A newline inside such a name does
not look odd, it ends the line: a session could write `doctor`'s own `sessions` row and answer *all
clear* underneath the row reporting it, while `session verify` — the anti-tampering verb — still
exited 1. Escaping at the print sites would have closed the two that existed and said nothing about
the third; refusing here means a render site cannot be handed one, which is the same choice
`validate_slug` and `validate_filename` make for their own untrusted siblings.

Stated precisely, because the loose version of that sentence was false when it was first written:
the guard inspects the **stripped** token, so what every selector may echo is a token with no
interior control character. It is not a promise that no control character was ever typed — a leading
or trailing one is normalised away by `str.strip()`, not refused. The two halves only add up while
each selector echoes `raw.strip()` rather than the original, which is a discipline at the call site
and not something this module can enforce. `resolve_slots` echoed the original and leaked a leading
newline into `requivo impact`'s output; that is fixed, and the rule is written into
`normalize_tokens`' own docstring so the next selector meets it.

`display_token` is the companion for the one shape that guard cannot cover: a site that *shows* a
stored token without selecting anything with it, where no refusal can run. Nothing makes an arbitrary
future f-string safe, and this module does not pretend otherwise — the guarantee it offers is about
the data, and the display helper is what the exceptions call.

Pure and IO-free: it reads no file and knows no vocabulary, so it stays inside core's boundary.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from requivo.core.errors import EmptySelectorTokenError, UnsafeSelectorTokenError

# C0 (including NUL, tab, CR, LF and the ANSI escape introducer), DEL, and C1 — which carries CSI at
# U+009B, an escape introducer in its own right on terminals that decode it. Nothing wider: this is
# the class that can *move the cursor or end the line*, which is the property being guarded. Bidi
# overrides and confusable glyphs are a different question with its own issues (#11, #29) and a
# different remedy; folding them in here would make one guard answer two questions and be argued
# about as a whole.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def display_token(value: str) -> str:
    """One caller-supplied token, rendered as **one line** of terminal output.

    The render-side companion to the guard in `normalize_tokens`, for the sites that show a stored
    token without selecting anything with it — `session show` prints a session's `context_cards`
    straight out of its metadata, so no selector ever runs and no refusal can reach it. There is no
    mechanism that makes an arbitrary future `print(f"…{x}…")` safe, so this is not offered as one:
    it is the thing such a site calls, named so that the reason travels with it.

    A value that is already one safe line comes back byte-for-byte, so ordinary output is unchanged
    and no reader learns a new shape for the normal case. Only a value that could break the line is
    quoted and escaped, which is the same `!r` treatment `core/integrity.py` gives the recorded
    artifact filename — its sibling untrusted field, read out of the same file.
    """
    return value if not _CONTROL_CHARS.search(value) else repr(value)


def display_text(value: str) -> str:
    """Untrusted **prose**, rendered so it cannot write a line of its own (#213).

    The sibling of `display_token`, and they differ because their subjects do. A token is one word
    and is shown as one line, so quoting the whole of it when anything is wrong costs nothing and
    keeps the reader's model simple. Prose is a paragraph a user has to *read*: `repr()`-ing a
    two-hundred-character challenge because it carries one stray character would be a worse outcome
    than the injection, and would ship green, because nobody re-reads output that looks busy. So
    this escapes **per character** and leaves everything else exactly as written.

    Same class as `display_token` and `normalize_tokens`, from the same regex, and deliberately no
    wider: what is neutralized is what can move a cursor or end a line. Same escape vocabulary too --
    `repr()` of the single character, so a newline reads as a backslash-n and an escape introducer as
    a backslash-x-1-b, exactly as the token sites already spell them.

    **What it does not do, said here rather than assumed at the call site.** It makes a *value* safe;
    it does not make a renderer safe. There is no mechanism that reaches an f-string somebody writes
    next, which is why `tests/test_render_untrusted_output.py` sweeps every renderer with every field
    forged at once rather than trusting the fifteen call sites to stay complete.

    The threat this exists for is not a hostile model. A client request is untrusted business data by
    SECURITY.md's own framing, the engine turns it into prose, and the prose is printed. `streams.py`
    is no help: `backslashreplace` acts on what a console cannot *encode*, and ESC encodes fine in
    UTF-8.

    Pure, like everything else here.
    """
    return _CONTROL_CHARS.sub(lambda m: repr(m.group())[1:-1], value)


def normalize_tokens(tokens: Iterable[str], *, what: str) -> list[str]:
    """Strip and lower-case caller-supplied selector tokens; refuse an empty one, and refuse one
    carrying a control character.

    Returns the tokens in the order given, stripped and lower-cased (matching is case-insensitive on
    every selector). Duplicates are kept — de-duplication is each selector's own business. An empty
    *list* is not an empty token: it means no selection was made at all, which is a legitimate state
    each caller reads for itself.

    `what` names the vocabulary being selected from, so the refusal can say what the caller was
    choosing between; the position is carried in `details` because a comma-split list is usually long
    enough that "one of these is empty" is not an actionable sentence.

    **The control-character guard inspects the token *after* stripping, and that is the whole of its
    scope — say it here rather than let a caller infer more.** `str.strip()` removes the control
    characters Python classifies as whitespace (tab, newline, vertical tab, form feed, carriage
    return, U+001C–U+001F, and NEL), so a token whose control character is leading or trailing is
    normalised away rather than refused; only an *interior* one is a refusal. That is safe **only
    because every selector echoes the stripped token**, so what a caller renders is what this
    function checked. A selector that echoed the unstripped original would leak a leading newline
    into its own error line — `resolve_slots` did exactly that, and it is why this paragraph exists.
    A new selector inherits the guard; it does not inherit that discipline, so echo `raw.strip()`.
    """
    out: list[str] = []
    for position, token in enumerate(tokens):
        raw = token.strip()
        key = raw.lower()
        if not key:
            raise EmptySelectorTokenError(
                f"empty {what} selector at position {position} — an empty token matches everything, "
                f"so it would widen the selection instead of narrowing it. Remove it (a stray comma "
                f"is the usual cause), or pass no selector at all to select everything deliberately.",
                details={"selector": what, "position": position})
        # The *stripped* token is what is checked, because the stripped token is what every caller
        # echoes back into its error message — `_selection_keys` and `resolve_cards` both name the
        # unknown card as `raw.strip()`. Checking the unstripped token instead would guard a string
        # nobody renders while leaving the rendered one unexamined. `.strip()` removes surrounding
        # whitespace; an *interior* newline it does not, and that is the one #40 is about.
        #
        # Named as typed rather than as lower-cased, matching the convention the two card selectors
        # already state in a comment of their own: a reader should see the name they wrote, not the
        # key it was matched by. `.lower()` neither adds nor removes a control character, so the two
        # forms are the same guard and only the message differs.
        if _CONTROL_CHARS.search(raw):
            raise UnsafeSelectorTokenError(
                f"the {what} selector at position {position} contains a control character: {raw!r}. "
                "A name carrying a newline, a tab or an escape sequence can write its own lines into "
                "a diagnostic that reports it, so it is refused rather than displayed.",
                details={"selector": what, "position": position})
        out.append(key)
    return out
