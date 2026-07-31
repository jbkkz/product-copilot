"""Structured, provider-agnostic errors — the failure vocabulary of Requivo Core.

These exist so a failure can be surfaced four ways from one raise: printed cleanly in the CLI,
interpreted by Claude Code (via `.to_dict()` on a `--json` boundary), converted to an HTTP response
by the future Web layer, and asserted in a test **by code**, not by matching a fragile message string.

The base carries a stable machine `code` and an optional `path` (dotted, into the model/session that
failed) plus a `details` dict. `RequivoError.to_dict()` is the serializable envelope the spec fixes:

    {"code": "missing_required_slot", "message": "...", "path": "model.business_rules",
     "details": {"slot": "business_rules"}}

Core raises these; it never imports a provider or an LLM. Provider-transport failures are a separate
family (`providers`), so a bug in reasoning is never confused with a bug in the model.
"""

from __future__ import annotations


class RequivoError(Exception):
    """Base for every Requivo failure. `code` is the stable machine identifier; subclasses set it."""

    code = "requivo_error"

    def __init__(self, message: str, *, path: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.path = path
        self.details = details or {}

    def to_dict(self) -> dict:
        """The serializable envelope — safe for `--json` output, Claude Code, and later HTTP."""
        out: dict = {"code": self.code, "message": self.message}
        if self.path is not None:
            out["path"] = self.path
        if self.details:
            out["details"] = self.details
        return out


class InvalidModelError(RequivoError):
    """A proposed model is structurally or semantically invalid."""

    code = "invalid_model"


class UnknownSlotError(InvalidModelError):
    """A model (or a question) names a slot the schema does not define — a typo or hallucination."""

    code = "unknown_slot"


class MissingRequiredSlotError(InvalidModelError):
    """A model omits a required slot — it would become invisible to readiness and every view."""

    code = "missing_required_slot"


class InvalidSessionError(RequivoError):
    """A session on disk is malformed, unreadable, or of an unsupported format version."""

    code = "invalid_session"


class SessionNotFoundError(RequivoError):
    """No session matches the given reference (slug or path)."""

    code = "session_not_found"


class StaleArtifactError(RequivoError):
    """An artifact was produced from a revision the model has since moved past."""

    code = "stale_artifact"
