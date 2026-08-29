"""Session view models — the home page's rows and the full session screen.

Both are pure projections over `SessionService` (+ the persisted model's reasoning). No filesystem, no
readiness re-derivation — `status()` does that; here we only assemble what a screen shows, and decide
what it shows *first*.
"""

from __future__ import annotations

from requivo.core.errors import SessionNotFoundError
from requivo.services.discovery import GENERATABLE
from requivo.services.sessions import SessionService
from requivo.web.viewmodels.labels import PRIMARY_ARTIFACT, artifact_label
from requivo.web.viewmodels.status import PRIORITY_QUESTIONS, readiness_view, understanding_view, understood_view

# How much of the request a home-page row shows. A session is recognised by what was asked, not by its
# slug — the slug is derived from the request and truncates exactly where the meaning starts.
TITLE_CHARS = 110


def _title(request_text: str, slug: str) -> str:
    """A row's human title: the opening of the request, or the slug when there is no request."""
    text = " ".join(request_text.split())
    if not text:
        return slug
    return text if len(text) <= TITLE_CHARS else text[:TITLE_CHARS].rstrip() + "…"


def generatable_view() -> list[dict]:
    """Every document the shared service can produce, taken from its vocabulary rather than a list
    kept in the template — a generator registered once shows up on every surface. The primary one is
    split out by the caller; this stays the complete set."""
    return [{"type": t, "label": artifact_label(t)} for t in GENERATABLE]


def _artifacts_view(status: dict) -> list[dict]:
    arts = status.get("artifacts", {})
    return [
        {"type": t, "label": artifact_label(t), "revision": a["revision"],
         "filename": a["filename"], "stale": a["stale"]}
        for t, a in sorted(arts.items())
    ]


def _unreadable_row(slug: str, error: str | None) -> dict:
    """A row for a session nobody could read — the third state, rendered rather than swallowed.

    It states no fact it does not have. `updated_at` is empty rather than a plausible timestamp,
    `open_questions` is `None` rather than `0` (we did not count zero questions, we failed to count),
    and `needs_update` claims nothing. Inventing any of the three would be the quiet-wrong-answer
    form of the very bug this guard exists for.

    The title is the slug, because naming *which* session is the point: before #7 a reader with one
    broken session was shown an error for the whole page and had no way to learn which one it was.
    """
    return {"slug": slug, "title": slug, "updated_at": "", "state": "unreadable",
            "status_label": "Could not be read", "open_questions": None, "needs_update": False,
            "error": error or "no further detail"}


def _readable_row(sessions: SessionService, meta) -> dict:
    """The ordinary row. Raises whatever its reads raise — `session_list` owns the degradation, so
    that every failure below this line lands in one place instead of one `try` per call."""
    row = {
        "slug": meta.slug,
        "title": _title(sessions.request_text(meta.slug), meta.slug),
        "updated_at": meta.updated_at,
        "error": None,
    }
    try:
        status = sessions.status(meta.slug)
    except SessionNotFoundError:
        # Not a failure: the 'capture the request now, analyse later' path has no model yet, and
        # `status()` needs one. This arm stays narrow on purpose — widening it would render an
        # un-analysed session and an unreadable one identically, which is the distinction the row
        # below exists to keep.
        return {**row, "state": "awaiting", "status_label": "Awaiting analysis",
                "open_questions": 0, "needs_update": False}
    arts = status.get("artifacts", {})
    open_questions = len(status.get("questions", []))
    ready = status["readiness"]["ready"]
    return {
        **row,
        "state": "ready" if ready else "in_progress",
        "status_label": "Ready for a first decision brief" if ready
        else (f"{open_questions} open question{'' if open_questions == 1 else 's'}"
              if open_questions else "In progress"),
        "open_questions": open_questions,
        "needs_update": any(a["stale"] for a in arts.values()),
    }


def _most_recent_first(items: list[dict]) -> list[dict]:
    """Order the home page's rows the way its heading claims: the session that moved last, first.

    **Ordering is presentation, so it happens here and not in the service** (#237). The rows arrive
    from `SessionService.list_entries()` sorted by slug, which is correct for that method — it is what
    `requivo session list` prints, and that output is a public surface whose order other callers read.
    On a screen headed *Recent* it was simply wrong: with a handful of sessions the one touched five
    minutes ago sat at the bottom while an abandoned `a-test` experiment led the page a returning
    reader resumes from. `test_the_cli_listing_order_is_not_what_changed` is what says which of the
    two moved.

    Two sorts rather than one key, because Python's sort is stable and `reverse=True` does not reverse
    the order of equal keys: the slug pass supplies the tie-break, and the recency pass overrides it
    only where the instants differ. Second-precision timestamps collide often enough for that to
    matter — without it, two sessions saved in the same second would swap places between reloads.

    A row nobody could read carries an empty `updated_at`, deliberately (invariant 15: we did not
    read a time, so we state none). An empty string is also the smallest string, so sorting on the
    value alone would put every broken session at the *top*. `bool(...)` in the key is what pins them
    last instead — the third state ordered explicitly rather than left to fall out of a comparison.
    Pinned by `test_a_row_nobody_could_read_sorts_last_rather_than_first`.
    """
    items.sort(key=lambda r: r["slug"])
    items.sort(key=lambda r: (bool(r["updated_at"]), r["updated_at"]), reverse=True)
    return items


def session_list(sessions: SessionService) -> list[dict]:
    """One row per local session for the home page.

    A row states only what helps a reader pick one up again: what was asked, whether it is waiting on
    them, and whether its brief has drifted. Revisions, provider names and artifact internals belong
    to the session screen's traceability section, not to a list.

    **The listing has to survive its own members** (invariant 15), and this used to enforce that one
    line below where it broke (#7). Three things sat outside the guard: `list_sessions()` is a
    single-shot comprehension over `read_meta`, so an unreadable `session.json` or a newer
    `format_version` raised before any row existed to degrade; `request_text` was outside the `try`;
    and the `try` named `SessionNotFoundError` alone, so a truncated `model.json` raised a pydantic
    `ValidationError` that missed this catch *and* the app's `RequivoError` handler and rendered as a
    500 over the whole page. Measured, per break mode: 400, 500 and 500 respectively, on a page whose
    other sessions were all fine.

    So the source is `list_entries()`, which degrades per member, and everything read *on* a row is
    inside one bare `except Exception`. Bare because the set of ways a session can be broken is open
    — that is the argument in `SessionService.list_entries`, and this is the call site it was made
    for. The narrow `SessionNotFoundError` arm survives inside `_readable_row`, because *not analysed
    yet* is a normal state and must not render like *we could not look*.
    """
    items = []
    for entry in sessions.list_entries():
        if not entry.readable:
            items.append(_unreadable_row(entry.slug, entry.error))
            continue
        try:
            items.append(_readable_row(sessions, entry.meta))
        except Exception as e:  # noqa: BLE001 - one member must not take the listing down
            items.append(_unreadable_row(entry.slug, str(e)))
    return _most_recent_first(items)


def session_detail(sessions: SessionService, slug: str) -> dict:
    """The session screen, in the order it is read: the request, what Requivo understood, the few
    questions that could change the solution, whether it is ready, and the decision brief.

    Everything the engine knows is still here — the full question list, the per-topic understanding,
    the decisions and challenges — but split into what leads the page and what sits behind the
    traceability disclosure. The split is presentational: nothing is dropped, and the counts are
    always stated so a reader can tell there is more."""
    status = sessions.status(slug)
    model = sessions.load_model(slug)
    questions = status.get("questions", [])
    artifacts = _artifacts_view(status)
    generatable = generatable_view()
    return {
        "slug": slug,
        "revision": status.get("revision"),
        "request_text": sessions.request_text(slug),
        "understood": understood_view(status),
        "readiness": readiness_view(status),
        # The few that lead the page, and the rest one disclosure away. The engine caps its reply at
        # six, so this rarely hides more than one — it is about where the eye lands, not about volume.
        "questions": questions[:PRIORITY_QUESTIONS],
        "more_questions": questions[PRIORITY_QUESTIONS:],
        "question_count": len(questions),
        "summary": status.get("summary", {}),
        "understanding": understanding_view(status),
        "remaining_gaps": status.get("remaining_gaps", []),
        "context_cards": status.get("context_cards"),
        "artifacts": artifacts,
        # The primary document is called out on its own; the rest live under "More documents".
        "primary_artifact": next((a for a in artifacts if a["type"] == PRIMARY_ARTIFACT), None),
        "other_artifacts": [a for a in artifacts if a["type"] != PRIMARY_ARTIFACT],
        "generatable": generatable,
        "primary_generatable": next((g for g in generatable if g["type"] == PRIMARY_ARTIFACT), None),
        "more_generatable": [g for g in generatable if g["type"] != PRIMARY_ARTIFACT],
        # `mode="json"` so enums arrive as their value. A plain dump leaves `Leverage.high` in the
        # dict, and Jinja renders an enum by its repr — the page read "leverage Leverage.high".
        "decisions": [d.model_dump(mode="json") for d in model.decisions],
        "challenges": [c.model_dump(mode="json") for c in model.challenges],
        "opportunities": [o.model_dump(mode="json") for o in model.opportunities],
    }
