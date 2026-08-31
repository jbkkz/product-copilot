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

    `details` always carries `{"selector", "position"}` — the position is the point, because a
    comma-split list is usually long enough that "one of these is empty" is not an actionable
    sentence. Its sibling `EmptySelectionError` is the *other* fact, and used to share this code
    while carrying a different shape (#35).
    """

    code = "empty_selector_token"


class UnsafeSelectorTokenError(RequivoError):
    """A caller-supplied selector token carried a control character — a newline, a carriage return, a
    tab, a NUL, or an ANSI escape introducer.

    Refused rather than escaped-at-render, and the distinction is the whole of #40. A card name is
    persisted in `session.json`, `session import` passes it through intact, and both health verbs
    render it into a line of a receipt. A newline inside one does not merely look odd: it *ends the
    line* and starts a new one at whatever column the attacker chose, so a session could write
    `doctor`'s own `sessions` row and answer *all clear* directly underneath the row reporting it —
    while `session verify`, the anti-tampering verb, still exited 1.

    Escaping at the print sites would have closed the two that exist today and nothing about the
    third one somebody adds next year. Refusing here means the value cannot reach a render site at
    all: the token is rejected by the one function every selector goes through, in the same shape as
    `validate_slug` and `validate_filename`, and — like both of those — the refusal names the
    offending value in escaped form, because a refusal that forges the line reporting it is no guard.

    `details` carries `{"selector", "position"}`, the same shape as `EmptySelectorTokenError`: both
    are a fault in one token at a known place in the list. A **sibling** of it and not a subclass,
    for the reason #35 gives — two facts sharing one `except` is how they get re-conflated.
    """

    code = "unsafe_selector_token"


class EmptySelectionError(RequivoError):
    """A selection object was supplied and it selects nothing — `--context ""` reaching a selector as
    an empty list, or a persisted selection that has been emptied.

    A **sibling** of `EmptySelectorTokenError`, deliberately not a subclass of it. They are two
    facts, as `normalize_tokens`' own docstring has always argued: an empty *token inside* a
    selection, against a selection that is *itself* empty. They shared one code until #35, with two
    different `details` shapes behind it — so a consumer following the documented advice (match the
    code) and reading `details["position"]` got a `KeyError` from a payload that correctly carried
    the code it matched. Subclassing would re-conflate exactly what splitting them fixes.

    `details` carries `{"selector", "tokens": 0}`. Refused rather than widened, because an empty
    selection is not `None`: `None` is the explicit "no restriction" sentinel, and guessing which one
    the caller meant is how this class of bug gets written.
    """

    code = "empty_selection"


class ContextUnreadableError(RequivoError):
    """A context-card directory exists but could not be enumerated — permissions, usually.

    Distinct from `UnknownContextCardError`, and the distinction is the whole point: that one means
    the card is not there and the remedy is to restore it, this one means the card may well be there
    and the remedy is to fix the permissions. Told apart only by raising, because the enumeration
    that produced them is the same one.

    It is raised rather than skipped because a card directory that cannot be read leaves the card
    *vocabulary* incomplete, and every later answer is then confidently wrong in the same direction:
    a selection naming a card in that directory resolves to "unknown card", and a turn that loaded
    the readable roots only reasons from a quietly smaller product context. `Path.glob` returns an
    empty iterator in exactly this case, which is what made all of that silent.
    """

    code = "context_unreadable"


class NoContextCardsError(RequivoError):
    """No context cards are installed at all — every card root was readable and every one was empty.

    The third state beside `UnknownContextCardError` (the card named is not there) and
    `ContextUnreadableError` (we could not look). Here we looked, at every root, and there is
    nothing to look at: a wheel or container layer that shipped `assets/` and lost
    `assets/context/`.

    Raised rather than tolerated because an empty `{{CONTEXT}}` is never a legitimate thing to send
    a provider. `build_prompt` substituted the empty string with no check, so the engine reasoned
    with no product context at all — `information_value = uncertainty x impact`, the central design
    idea, silently off — on a call that costs money. A context-free run is not currently a supported
    mode; if it ever becomes one it wants an explicit flag, not an accident of an empty directory.

    `details` carries `{"roots"}`: the directories that were searched, because "no cards" is only
    actionable once you know where the search looked.
    """

    code = "no_context_cards"


class InputTooLargeError(RequivoError):
    """A supplied text exceeds the ceiling the engine accepts. Raised rather than truncated: a request
    or an answer silently cut mid-sentence is reasoned over as if it were the whole thing, and the
    caller never learns which half the model saw."""

    code = "input_too_large"


class InvalidSessionError(RequivoError):
    """A session on disk is malformed, unreadable, or of an unsupported format version.

    **A family, and nothing raises it directly** (#82). It carried seven different facts across eight
    raise sites with four `details` shapes, and it is not inert the way `cross_site_request` was:
    `cli.py` serializes `to_dict()` — `details` included — on every `--json` verb, so a consumer could
    observe the inconsistency, and `details["slug"]` raised `KeyError` on three of the eight. Worse,
    `docs/compatibility.md` promised *"a session written by a newer Requivo is refused, clearly
    (`invalid_session`)"* while the same page says to assert on the code and never on the message —
    so the only handle that separated that promise from a corrupt zip was the one handle the document
    forbids.

    The family is kept because `except InvalidSessionError` should still catch every arm, and because
    a caller that wants *any* malformed-session refusal should not have to enumerate eight names.
    `UnstatedSourceRevisionError` in `services.artifacts` is a ninth member, split out earlier by #57.

    **The arms do not share a `details` shape, and that is the point.** Padding eight payloads to one
    key set would answer the `KeyError` by stating facts nobody measured — three of them identify no
    session at all, because no session has been identified yet when a zip will not open. The
    `KeyError` goes away because the *code* now carries the fact and a consumer branches on it before
    reading `details`. Each subclass names its keys below; `docs/compatibility.md` carries the same
    table.
    """

    code = "invalid_session"


class UnsupportedFormatVersionError(InvalidSessionError):
    """The session was written by a newer Requivo than this one. `details`:
    `{format_version, supported_format_version}` — both, because *newer than what* is half the fact
    and the reader has no other way to learn which build they are holding.

    This is the arm `docs/compatibility.md` has promised by name since before the split, and the one
    a consumer is most likely to match on.
    """

    code = "unsupported_format_version"


class UnsupportedSchemaVersionError(InvalidSessionError):
    """The model was authored against a newer *slot schema* than this build defines. `details`:
    `{schema_version, supported_schema_version}`.

    A second, independent contract from the session format, which is why it is a second code rather
    than a `details` field on the one above: a session can be format-current and schema-ahead, and
    without this check the first symptom is an `unknown_slot` error naming a slot the user never typed.
    """

    code = "unsupported_schema_version"


class SessionUnreadableError(InvalidSessionError):
    """`session.json` is absent-shaped, truncated, mis-encoded or not JSON — **or** the session's
    write lock could not be opened at all. `details`: `{slug}`.

    A fact about the state of the store, not about what the caller sent — which is why it answers 500
    rather than 400. `changelog.d/62` put the raw message text into `session list --json`'s `error`
    field precisely because no code existed for this and *written by a newer Requivo, upgrade* is a
    remedy where a flattened `unreadable` is not. The text stays; a consumer can now branch instead.

    The second condition arrived with #113, when the lock moved to `.requivo/locks/<slug>.lock`. It is
    the same *kind* of fact — the store, not the request — and it deliberately does **not** answer
    `session_not_found`: the old code mapped a failed `os.open` onto "no such session" because the
    lock file lived inside the session, and repeating that mapping now would be a sentence about a
    session naming a cause that is not the cause, which is the shape #114 exists to remove. A reader
    who branches on this code must read the message: `docs/compatibility.md` lists both conditions.
    """

    code = "session_unreadable"


class ModelUnreadableError(InvalidSessionError):
    """`model.json` (or a `revisions/NNNN-model.json`) is truncated, mis-encoded, not JSON, or does
    not validate. `details`: `{path}`, plus `slug` and `revision` when the caller knew them.

    A sibling of `session_unreadable` rather than a third condition under it, and the reason is the
    remedy. When `session.json` will not parse the session cannot be opened at all and nothing on
    disk recovers it; when the *model* will not parse the session opens, the listing is unaffected,
    `session verify` answers, and `revisions/` holds every applied model. Those are different
    situations to be in, so a consumer that branches on the code learns something — which is the
    whole argument #82 made for eight codes over one, applied to the file this product calls its
    durable output.

    `details` carries only what was actually known: a bare `model.json` path has no session and no
    revision, and padding those keys with nulls would state facts nobody measured (see the family
    note in `docs/compatibility.md`).
    """

    code = "model_unreadable"


class ArtifactRevisionOutOfRangeError(InvalidSessionError):
    """An artifact was recorded against a revision this session does not have. `details`:
    `{slug, source_revision, current_revision}`."""

    code = "artifact_revision_out_of_range"


class InconsistentArchiveError(InvalidSessionError):
    """An imported archive holds a session that does not tell the truth about itself. `details`:
    `{slug, problems}` — the same integrity codes `session verify` reports.

    The caller handed us this archive, so it answers 400 rather than 500: the two archive arms are
    the only members of this family that are about the request.
    """

    code = "inconsistent_archive"


class UnreadableArchiveError(InvalidSessionError):
    """The file is not a readable `.zip`. `details`: `{archive}` and no `slug`, because nothing has
    been identified yet — a `slug: null` here would state a fact nobody measured."""

    code = "unreadable_archive"


class InvalidArchiveError(InvalidSessionError):
    """The archive opens, but its *shape* is not an export: no entries, too many raw entries (files
    and directories together), too many files, expanding past the size ceiling, an entry that is not
    safely inside exactly one session directory, or more than one session in it.

    **Why it is in this family and not on its own.** It sits between `unreadable_archive` and
    `inconsistent_archive` on one code path in `_cmd_session_import`, and the three answer the same
    two questions the same way: *is this a session we can accept* — no — and *whose fault is it* —
    the caller's, so 400. `unreadable_archive`'s docstring argues that nothing has been identified
    yet, which is true here too and did not take it out of the family; a consumer writing
    `except InvalidSessionError` for *any* malformed-session refusal would otherwise catch the arm on
    either side of this one and miss the eight in between. It was `InvalidModelError` until #101 —
    a code documented as *"a proposed model is structurally or semantically invalid"*, answering for
    an archive nobody proposed a model with.

    **One code for eight conditions now, and what that costs.** They share a remedy — *give me a
    different archive* — and #82's rule is that a candidate sending a reader where an existing code
    already sends them has not earned a line. But #82's finding was that `details` varying silently
    under one code is what raised `KeyError` on three of eight payloads, so a single code owes a
    discriminator rather than being excused one:

    - `details["problem"]` is on **every** arm, and is one of `empty`, `too_many_entries`,
      `too_many_files`, `too_large`, `unsafe_entry`, `entry_outside_session_directory`,
      `multiple_sessions`. A consumer that needs the distinction branches on a key that is always
      present.
    - Each arm adds the numbers its own sentence quotes, and only those: `{entries, max_entries}`,
      `{files, max_files}`, `{bytes, max_bytes}`, `{entry}`, `{slugs}`. `empty` adds nothing. Padding
      them to a common shape would state measurements nobody took, which is the half of #82 that was
      a decision rather than an obligation.

    **`too_many_entries` is the eighth, added by #219.** `too_many_files` and `too_large` are both
    computed over `z.infolist()` with directory entries filtered out, so an archive built entirely of
    directory entries declared zero files and ~zero bytes and passed both — while the extraction loop
    still created every one of them, an inode-exhaustion bound the file-only caps never covered.
    `too_many_entries` bounds the raw entry count, files and directories together, before either
    file-only cap runs.
    """

    code = "invalid_archive"


class ImportMoveFailedError(InvalidSessionError):
    """The validated session could not be moved into place. `details`: `{slug}`.

    The archive was fine and the store refused it, so this is 500 while its two archive siblings are
    400 — the same shape, a different answer to *whose fault is this*.
    """

    code = "import_move_failed"


class SessionNotFoundError(RequivoError):
    """No session matches the given reference (slug or path)."""

    code = "session_not_found"


class InvalidSlugError(RequivoError):
    """A slug is not a safe session identifier. A slug names a directory under the session root, so it
    must be a strict kebab-case token — anything with a path separator or a dot segment could escape
    the store (directory traversal). Enforced in Core, so every surface inherits the guarantee."""

    code = "invalid_slug"


class InvalidFilenameError(RequivoError):
    """A filename is not a safe name for a file inside a session's `artifacts/`. The sibling of
    `InvalidSlugError`, and for the same reason: a filename is a *path target*, so it must be a bare
    name — anything with a path separator, a dot segment or a leading dot can put the access outside
    the session directory (or shadow the store's own reserved dot-prefixed entries). Enforced in Core,
    at the same chokepoint as the slug, so every surface inherits it rather than the one caller that
    happened to pass a literal.

    Raised on reads as well as writes, and the two are separate exposures: a write target decides
    what this code may create, a read target decides what it may disclose. Closing one does not
    close the other."""

    code = "invalid_filename"


class StaleArtifactError(RequivoError):
    """An artifact was produced from a revision the model has since moved past."""

    code = "stale_artifact"


class RevisionConflictError(RequivoError):
    """A write expected the session at one revision, but it has already moved on — two updates raced
    from the same base. The caller must reload the current model and re-apply. Harmless for a single
    local CLI user (which omits the precondition); a hard requirement for a concurrent Web service."""

    code = "revision_conflict"


class SessionExistsError(RequivoError):
    """A session already occupies that slug.

    In `create_session` and `migrate_legacy` it is raised by the *creation* itself rather than found
    by a prior existence check, so two callers creating the same session concurrently cannot both
    believe they made it: the claim is the directory creation, and exactly one of them wins it
    (invariant 11).

    **`session import` is the one raiser where it is a check, and the difference is stated rather
    than papered over** (#101). Import refuses an occupied slug unless `--force` was passed, and that
    decision is made by a `session_exists` stat call before the archive is extracted — so it carries
    the TOCTOU window the atomic claim above does not have: a session created between the check and
    the final move is replaced without `--force`, silently. That window predates this code and is not
    what #101 changed; what #101 changed is that the refusal used to answer `invalid_model` / 400,
    which described a proposal nobody sent. The same shape is already written down for the sibling
    case at `deterministic/sessions.py`'s `_cmd_session_migrate`, where the check is *reporting, not the
    guard*. `details`: `{slug}`.
    """

    code = "session_exists"


class ImportDestinationOccupiedError(RequivoError):
    """`session import` found something at the slug's directory that is **not** a session. `details`:
    `{slug, path}` — the path because the remedy is to move or remove that directory, and naming the
    slug alone would leave the reader to reconstruct where it is.

    **Deliberately outside the `InvalidSessionError` family, and deliberately not a
    `SessionExistsError`.** That family means *a session on disk is malformed*, and here there is no
    session at all. `session_exists` is nearer but carries a remedy that cannot work: `--force`
    replaces a session, and the store never made this directory, so offering it would send the reader
    down a path that fails the same way twice.

    409 for the reason `session_exists` is 409 — a conflict with the store's current state, not a
    malformed request. It is emphatically not `import_move_failed`/500: nothing about the move is
    wrong, and that code sent the reader at their filesystem looking for a fault that is not there
    (#114).
    """

    code = "import_destination_occupied"


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
