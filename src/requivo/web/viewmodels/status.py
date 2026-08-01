"""Status view models — thin reshaping of `SessionService.status()` for templates.

`status()` already returns the computed picture (readiness, understanding grouped by state, questions,
gaps). These helpers only relabel it for display; they never recompute readiness or coverage.
"""

from __future__ import annotations

from typing import Any

# Human labels for the three understanding states the Core emits (evidence, NOT coverage — coverage is
# the separate `thin`/`completeness` signal each entry carries).
UNDERSTANDING_STATES = [
    ("confirmed", "Explicit facts", "Stated directly by the client."),
    ("inferred", "Inferred assumptions", "Assumed from context — confirm before building."),
    ("unknown", "Unknowns", "Not yet known; may need a question."),
]


def readiness_view(status: dict) -> dict:
    """A display-ready readiness block. Readiness is binary in the Core (ready + blocking topics); we
    do NOT invent graded levels ('nearly ready') the model does not produce — we show what blocks."""
    rd = status.get("readiness", {})
    blocking = rd.get("blocking_slots", [])
    return {
        "ready": rd.get("ready", False),
        "blocking": blocking,
        "headline": "Ready for implementation" if rd.get("ready")
        else ("Ready for assessment" if not blocking else "Blocked"),
        "blocking_labels": [b["label"] for b in blocking],
    }


def understanding_view(status: dict) -> list[dict]:
    """The understanding checklist grouped by state, in a fixed display order, each group carrying its
    label and its slots (with the `thin` coverage flag Core sets)."""
    groups = status.get("understanding", {})
    out = []
    for key, label, hint in UNDERSTANDING_STATES:
        entries = groups.get(key, [])
        if entries:
            out.append({"key": key, "label": label, "hint": hint, "slots": entries})
    return out


def update_result_view(result: Any) -> dict:
    """An `UpdateResult` (from an answers turn) reshaped for the HTMX status refresh: what changed, what
    reasoning it unseated, and which artifacts went stale."""
    return {
        "revision": result.revision,
        "changed_slots": result.changed_slots,
        "invalidated_decisions": result.invalidated_decisions,
        "invalidated_challenges": result.invalidated_challenges,
        "stale_artifacts": result.stale_artifacts,
        "ready": result.readiness.ready,
    }
