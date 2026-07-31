"""The single model-validation entry point — provider-agnostic.

Every model update, whoever produces it (the Anthropic provider, a Claude Code proposal, a future
Web client), passes through here before it is applied. Pydantic already enforces the *shape* and the
slot *vocabulary* (`EngineOutput._validate_slot_vocabulary`); this layer adds the *completeness*
boundary (the full required slot set) and, crucially, translates every failure into a structured
`RequivoError` with a stable `code` — so the CLI's `model validate`/`model apply` can emit a machine
envelope and Claude Code can act on the `code` instead of scraping a Pydantic message.

This is the same guarantee `run()`'s `validate` hook enforces inside the provider; extracting it here
means the deterministic CLI path (which never calls an LLM) applies the identical rule.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from requivo.core.contracts import EngineOutput, missing_required_slots, unknown_slots
from requivo.core.errors import InvalidModelError, MissingRequiredSlotError, UnknownSlotError


def validate_proposal(data: dict | str, *, require_complete: bool = True) -> EngineOutput:
    """Validate a proposed model (a dict or a JSON string) into an `EngineOutput`, raising a
    structured `RequivoError` on any failure.

    `require_complete` gates the completeness boundary: True (the default, matching discovery) rejects
    a model missing any required slot; False allows a partial projection (used where a caller knowingly
    works with a subset). The vocabulary check (unknown slots) always runs — a hallucinated key is
    never acceptable."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            raise InvalidModelError(f"proposal is not valid JSON: {e}", path="model") from e
    if not isinstance(data, dict):
        raise InvalidModelError("proposal must be a JSON object", path="model")

    # Surface a precise slot-vocabulary error *before* Pydantic, so the caller gets `unknown_slot`
    # with the offending ids rather than a generic validation dump.
    model_slots = data.get("model")
    if isinstance(model_slots, dict):
        bad = unknown_slots(set(model_slots))
        if bad:
            raise UnknownSlotError(
                f"model names slots the schema does not define: {bad}",
                path="model",
                details={"slots": bad},
            )

    try:
        out = EngineOutput.model_validate(data)
    except ValidationError as e:
        raise InvalidModelError(f"proposal does not match the schema: {e}", path="model") from e

    if require_complete:
        missing = missing_required_slots(set(out.model))
        if missing:
            raise MissingRequiredSlotError(
                f"model is missing required slots: {missing}. Emit every schema slot.",
                path=f"model.{missing[0]}",
                details={"slots": missing},
            )
    return out
