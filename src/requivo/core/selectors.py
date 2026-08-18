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

Pure and IO-free: it reads no file and knows no vocabulary, so it stays inside core's boundary.
"""

from __future__ import annotations

from collections.abc import Iterable

from requivo.core.errors import EmptySelectorTokenError


def normalize_tokens(tokens: Iterable[str], *, what: str) -> list[str]:
    """Strip and lower-case caller-supplied selector tokens; refuse an empty or whitespace-only one.

    Returns the tokens in the order given, stripped and lower-cased (matching is case-insensitive on
    every selector). Duplicates are kept — de-duplication is each selector's own business. An empty
    *list* is not an empty token: it means no selection was made at all, which is a legitimate state
    each caller reads for itself.

    `what` names the vocabulary being selected from, so the refusal can say what the caller was
    choosing between; the position is carried in `details` because a comma-split list is usually long
    enough that "one of these is empty" is not an actionable sentence.
    """
    out: list[str] = []
    for position, token in enumerate(tokens):
        key = token.strip().lower()
        if not key:
            raise EmptySelectorTokenError(
                f"empty {what} selector at position {position} — an empty token matches everything, "
                f"so it would widen the selection instead of narrowing it. Remove it (a stray comma "
                f"is the usual cause), or pass no selector at all to select everything deliberately.",
                details={"selector": what, "position": position})
        out.append(key)
    return out
