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


def session_list(sessions: SessionService) -> list[dict]:
    """One row per local session for the home page.

    A row states only what helps a reader pick one up again: what was asked, whether it is waiting on
    them, and whether its brief has drifted. Revisions, provider names and artifact internals belong
    to the session screen's traceability section, not to a list.

    A session with no model yet — the 'capture the request now, analyse later' path — is a normal row,
    not an error. `status()` needs a model and raises without one, which used to take the *whole* home
    page down with a 404: one un-analysed session and a reader lost the list of every other. The
    listing has to survive its own members."""
    items = []
    for meta in sessions.list_sessions():
        row = {
            "slug": meta.slug,
            "title": _title(sessions.request_text(meta.slug), meta.slug),
            "updated_at": meta.updated_at,
        }
        try:
            status = sessions.status(meta.slug)
        except SessionNotFoundError:
            items.append({**row, "state": "awaiting", "status_label": "Awaiting analysis",
                          "open_questions": 0, "needs_update": False})
            continue
        arts = status.get("artifacts", {})
        open_questions = len(status.get("questions", []))
        ready = status["readiness"]["ready"]
        items.append({
            **row,
            "state": "ready" if ready else "in_progress",
            "status_label": "Ready for a first decision brief" if ready
            else (f"{open_questions} open question{'' if open_questions == 1 else 's'}"
                  if open_questions else "In progress"),
            "open_questions": open_questions,
            "needs_update": any(a["stale"] for a in arts.values()),
        })
    return items


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
