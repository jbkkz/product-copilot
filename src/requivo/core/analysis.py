from __future__ import annotations

import functools
import json

from requivo.core.contracts import SOFT_COMPLETENESS, Confidence, EngineOutput, Impact, Slot, schema_slot_ids
from requivo.paths import FRAMEWORK


@functools.lru_cache(maxsize=1)
def _slot_meta() -> tuple[dict, dict]:
    slots = json.loads((FRAMEWORK / "model_schema.json").read_text())["slots"]
    return ({s["id"]: s["pillar"] for s in slots}, {s["id"]: s["label"] for s in slots})


@functools.lru_cache(maxsize=1)
def _default_impacts() -> dict[str, Impact]:
    """Each slot's baseline impact from the schema — used to judge a slot the model omitted entirely
    (where there's no live impact to read)."""
    slots = json.loads((FRAMEWORK / "model_schema.json").read_text())["slots"]
    return {s["id"]: Impact(s["impact_default"]) for s in slots}


def _label(slot_id: str) -> str:
    return _slot_meta()[1].get(slot_id, slot_id)


def soft_slots(out: EngineOutput) -> list[str]:
    """Slots that still carry real uncertainty AND move the solution — the objective drivers of
    the estimate spread. Soft = medium/high impact and (low completeness or not yet explicit)."""
    soft = []
    for slot_id, s in out.model.items():
        if s.impact in (Impact.medium, Impact.high) and (
            s.completeness < SOFT_COMPLETENESS or s.confidence is not Confidence.explicit
        ):
            soft.append(slot_id)
    return soft


def estimate_confidence(n_soft: int) -> str:
    """Estimate confidence derived from how many high-impact slots are still soft."""
    if n_soft <= 1:
        return "high"
    if n_soft <= 3:
        return "medium"
    return "low"


def _is_deferred(s: Slot) -> bool:
    """Low-impact, unfilled slots are intentionally parked, not weaknesses."""
    return s.impact is Impact.low and s.completeness < SOFT_COMPLETENESS


def _readiness_blockers(out: EngineOutput) -> list[str]:
    """High-impact slots not yet explicitly confirmed — what stands between here and build.

    Iterates the schema's required slots, not just the ones the model returned: a required slot the
    model omitted is treated as unknown at its baseline impact, so a missing high-impact dimension
    reads as a blocker instead of vanishing. This is the readiness guarantee — 'ready' can never be
    reached with a high-impact gap, whether the gap is empty-but-present or absent entirely."""
    _, required = schema_slot_ids()
    blockers = []
    for sid in required:
        s = out.model.get(sid)
        impact = s.impact if s is not None else _default_impacts().get(sid, Impact.low)
        explicit = s is not None and s.confidence is Confidence.explicit
        if impact is Impact.high and not explicit:
            blockers.append(sid)
    return [sid for sid in _slot_meta()[1] if sid in set(blockers)]  # schema order


def _state_of(s: Slot) -> str:
    if s.confidence is Confidence.explicit:
        return "confirmed"
    if s.confidence is Confidence.inferred:
        return "inferred"
    return "unknown"
