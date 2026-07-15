from __future__ import annotations

import functools
import json

from product_copilot.core.contracts import Confidence, EngineOutput, Impact, Slot, SOFT_COMPLETENESS
from product_copilot.paths import ROOT


@functools.lru_cache(maxsize=1)
def _slot_meta() -> tuple[dict, dict]:
    slots = json.loads((ROOT / "framework" / "model_schema.json").read_text())["slots"]
    return ({s["id"]: s["pillar"] for s in slots}, {s["id"]: s["label"] for s in slots})


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
    """High-impact slots not yet explicitly confirmed — what stands between here and build."""
    return [sid for sid, s in out.model.items() if s.impact is Impact.high and s.confidence is not Confidence.explicit]


def _state_of(s: Slot) -> str:
    if s.confidence is Confidence.explicit:
        return "confirmed"
    if s.confidence is Confidence.inferred:
        return "inferred"
    return "unknown"
