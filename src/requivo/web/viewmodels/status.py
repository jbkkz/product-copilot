"""Status view models — thin reshaping of `SessionService.status()` for templates.

`status()` already returns the computed picture (readiness, understanding grouped by state, questions,
gaps). These helpers only relabel it for display; they never recompute readiness or coverage.
"""

from __future__ import annotations

from typing import Any

# The three understanding states the Core emits (evidence, NOT coverage — coverage is the separate
# `thin` flag each entry carries), each with its display tag and dot colour class.
UNDERSTANDING_STATES = [
    ("confirmed", "FACT", "fact"),
    ("inferred", "ASSUM", "assum"),
    ("unknown", "UNKWN", "unkwn"),
]


def readiness_view(status: dict) -> dict:
    """A display-ready readiness block. Readiness is binary in the Core (ready + blocking topics); we
    do NOT invent graded levels ('nearly ready') the model does not produce — we show what blocks. The
    coverage count (explicit slots / total) is a purely visual progress signal for the segmented bar,
    not a second readiness concept."""
    rd = status.get("readiness", {})
    blocking = rd.get("blocking_slots", [])
    groups = status.get("understanding", {})
    total = sum(len(v) for v in groups.values())
    resolved = len(groups.get("confirmed", []))
    return {
        "ready": rd.get("ready", False),
        "blocking": blocking,
        "headline": "Ready for implementation" if rd.get("ready")
        else ("Ready for assessment" if not blocking else "Blocked"),
        "blocking_labels": [b["label"] for b in blocking],
        "resolved": resolved,
        "total": total,
    }


def understanding_view(status: dict) -> list[dict]:
    """The understanding as a flat, dot-coded row list (facts, then assumptions, then unknowns) — the
    'model' view: each row carries its tag (FACT/ASSUM/UNKWN), dot colour, slot label, pillar, and the
    `thin` coverage flag. Flat rather than grouped so it reads like the landing's model card."""
    groups = status.get("understanding", {})
    rows = []
    for key, tag, dot in UNDERSTANDING_STATES:
        for e in groups.get(key, []):
            rows.append({"tag": tag, "dot": dot, "name": e["label"],
                         "pillar": e["pillar"], "thin": e.get("thin", False)})
    return rows


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
