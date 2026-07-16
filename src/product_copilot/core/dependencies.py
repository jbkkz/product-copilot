"""The dependency DAG — impact propagation over a saved model.

The model is not a flat snapshot: its parts rest on each other. A design decision rests on the
slots it was derived from; a generated artifact consumes a known set of slots. When a slot changes,
the things that rest on it go stale. This module makes that graph explicit and answers one question:

    change these slots → which decisions must be re-validated, and which artifacts go stale?

It is **pure** (no I/O, no LLM, no argv/stdout): `render/` prints an `ImpactReport`, `cli.py` wires
it to a verb. The two edge sets are:

  slot ──derived_from──> decision   from DesignDecision.derived_from (filled by advise())
  slot ──consumed_by───> artifact   from ARTIFACT_SLOTS below (static, honest, coarse)

The artifact edges need no LLM, so propagation works even on a model whose decisions predate
`derived_from` — the decision layer just *explains* the staleness on top of the artifact backbone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from product_copilot.core.analysis import _label, _slot_meta
from product_copilot.core.contracts import EngineOutput


def _all_slot_ids() -> set[str]:
    return set(_slot_meta()[1])  # (pillars, labels) — labels is keyed by every slot id


# Which slots materially shape each artifact. Deliberate, not "everything": an over-broad map makes
# every change invalidate everything, which is the same as saying nothing. The names match the
# buildable generators. The `brief`/assessment is deliberately absent — it is the live analysis
# layer, regenerated each discovery, not a downstream deliverable that goes "stale".
_ARTIFACT_SLOTS_RAW: dict[str, set[str] | str] = {
    "prd": {"problem", "success_metrics", "actors", "business_objects", "business_rules",
            "workflow", "integrations", "permissions", "constraints", "edge_cases",
            "acceptance", "risks"},
    "stories": {"actors", "business_objects", "business_rules", "workflow", "permissions"},
    "estimate": {"business_objects", "business_rules", "workflow", "integrations",
                 "permissions", "config_vs_custom", "constraints"},
    "criteria": {"workflow", "business_rules", "permissions", "edge_cases", "acceptance"},
    "epic": {"actors", "business_objects", "business_rules", "workflow", "integrations",
             "permissions", "config_vs_custom", "constraints"},
    "release": {"problem", "success_metrics", "workflow", "risks"},
}

# The persisted file for each artifact, or None when the artifact is only rendered to the terminal
# (stories, estimate). Used by change-detection to flag *existing* stale files on disk.
ARTIFACT_FILES: dict[str, str | None] = {
    "prd": "prd.md", "stories": None, "estimate": None,
    "criteria": "acceptance-criteria.md", "epic": "epic.md", "release": "release-notes.md",
}


def artifact_slots() -> dict[str, set[str]]:
    """Resolve the artifact→slots map, expanding `*` to every slot id."""
    every = _all_slot_ids()
    return {name: (set(every) if slots == "*" else set(slots))
            for name, slots in _ARTIFACT_SLOTS_RAW.items()}


def resolve_slots(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Map user-typed tokens (slot ids OR label substrings, PM-friendly) to slot ids.
    Returns (resolved ids in schema order, unmatched tokens)."""
    _, labels = _slot_meta()
    resolved, unmatched = [], []
    for tok in tokens:
        key = tok.strip().lower()
        if key in labels:  # exact slot id
            hit = [key]
        else:  # label substring, e.g. "permission" → permissions
            hit = [sid for sid, lab in labels.items() if key in lab.lower()]
        if hit:
            resolved.extend(hit)
        else:
            unmatched.append(tok)
    ordered = [sid for sid in labels if sid in set(resolved)]  # schema order, de-duped
    return ordered, unmatched


@dataclass
class DecisionImpact:
    decision: str
    rests_on: list[str]  # labels of the changed slots this decision was derived from


@dataclass
class ImpactReport:
    changed: list[str]  # labels of the slots in question
    decisions: list[DecisionImpact] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)  # artifact names whose slot set is touched

    @property
    def empty(self) -> bool:
        return not self.decisions and not self.artifacts


def propagate(out: EngineOutput, changed: list[str]) -> ImpactReport:
    """Given slot ids that changed (or are being probed), report what rests on them:
    the design decisions to re-validate and the artifacts that go stale."""
    changed_set = set(changed)
    report = ImpactReport(changed=[_label(sid) for sid in changed])

    for d in out.decisions:
        hit = [sid for sid in d.derived_from if sid in changed_set]
        if hit:
            report.decisions.append(DecisionImpact(d.decision, [_label(sid) for sid in hit]))

    amap = artifact_slots()
    report.artifacts = [name for name in _ARTIFACT_SLOTS_RAW if amap[name] & changed_set]
    return report


def diff_models(old: EngineOutput, new: EngineOutput) -> list[str]:
    """Slot ids that materially changed between two model versions — the trigger for staleness.
    A slot changed if its value, confidence or impact moved (completeness alone is noise)."""
    changed = []
    for sid, new_slot in new.model.items():
        old_slot = old.model.get(sid)
        if old_slot is None or (
            old_slot.value.strip() != new_slot.value.strip()
            or old_slot.confidence != new_slot.confidence
            or old_slot.impact != new_slot.impact
        ):
            changed.append(sid)
    return changed
