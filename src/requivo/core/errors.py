"""Structured, provider-agnostic errors — the failure vocabulary of Requivo Core.

These exist so a failure can be surfaced four ways from one raise: printed cleanly in the CLI,
interpreted by Claude Code (via `.to_dict()` on a `--json` boundary), converted to an HTTP response
by Requivo Web, and asserted in a test **by code**, not by matching a fragile message string.

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


class UnknownContextCardError(InvalidModelError):
    """A caller named a context card that does not exist. A hard error on every surface: silently
    dropping the unknown name falls back to *all* cards, which widens the context instead of narrowing
    it — the opposite of what a caller asking for a subset intended."""

    code = "unknown_context_card"


class EmptySelectorTokenError(RequivoError):
    """A caller-supplied selector carried an empty or whitespace-only token — a stray comma, usually.

    Refused rather than dropped, because an empty token is not a narrow selection: it is *no*
    selection wearing the shape of one. Matched as a substring it hits every candidate; dropped, it
    can leave the selection empty, and every reader downstream spells an empty selection "all of
    them". Both directions are silent, and both render exactly like a precise answer.
    """

    code = "empty_selector_token"


class InputTooLargeError(RequivoError):
    """A supplied text exceeds the ceiling the engine accepts. Raised rather than truncated: a request
    or an answer silently cut mid-sentence is reasoned over as if it were the whole thing, and the
    caller never learns which half the model saw."""

    code = "input_too_large"


class InvalidSessionError(RequivoError):
    """A session on disk is malformed, unreadable, or of an unsupported format version."""

    code = "invalid_session"


class SessionNotFoundError(RequivoError):
    """No session matches the given reference (slug or path)."""

    code = "session_not_found"


class InvalidSlugError(RequivoError):
    """A slug is not a safe session identifier. A slug names a directory under the session root, so it
    must be a strict kebab-case token — anything with a path separator or a dot segment could escape
    the store (directory traversal). Enforced in Core, so every surface inherits the guarantee."""

    code = "invalid_slug"


class StaleArtifactError(RequivoError):
    """An artifact was produced from a revision the model has since moved past."""

    code = "stale_artifact"


class RevisionConflictError(RequivoError):
    """A write expected the session at one revision, but it has already moved on — two updates raced
    from the same base. The caller must reload the current model and re-apply. Harmless for a single
    local CLI user (which omits the precondition); a hard requirement for a concurrent Web service."""

    code = "revision_conflict"


class SessionExistsError(RequivoError):
    """A session already occupies that slug. Raised by the *creation* itself rather than found by a
    prior existence check, so two callers creating the same session concurrently cannot both believe
    they made it: the claim is the directory creation, and exactly one of them wins it."""

    code = "session_exists"


class SessionLockedError(RequivoError):
    """Another writer holds the session lock and did not release it within the timeout. Distinct from
    `RevisionConflictError`: nothing raced to a conclusion here, the write never got to start, so
    retrying it unchanged is the correct response."""

    code = "session_locked"


class ProviderOutputError(RequivoError):
    """A provider could not be made to return output matching the contract, after every retry. The
    retry loop's own failure is a Requivo condition with a cause the user can act on (usually: try
    again, or a model that cannot hold the schema) — not an internal defect, so it must not reach a
    surface as a bare traceback."""

    code = "provider_output_invalid"
