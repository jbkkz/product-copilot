"""Session integrity — does this session directory tell the truth about itself?

A session is not one file. It is metadata claiming a revision count, a history of one file per
revision, a current model that should equal the last of them, and artifacts each pointing back at the
revision they came from. Every one of those claims can be false while each individual file is
perfectly valid JSON — an archive that lost its `revisions/`, a hand-edited `session.json`, an
interrupted copy, a model.json swapped out from under its own hash.

Validating the *shape* of each file (which is all `session import` used to do) cannot see any of that,
because nothing is malformed. Only the relationships are broken. This module checks the relationships,
and it is deliberately separate from `persistence`: the same function has to serve a session in the
store (`requivo session verify`, `doctor`) and a directory extracted from an archive that has not been
allowed into the store yet — so it takes a *path*, and it never writes.

It reports rather than raises. A caller decides what a problem means: `session verify` prints them all
and exits non-zero, `session import` refuses the archive, `doctor` names the sessions worth looking at.
Raising on the first one would answer a different, less useful question — "is it broken?" instead of
"what is broken?".

**The evidence is the directory, and only the directory.** That is one rule, and it binds in both
directions — they look like separate concerns and are the same sentence read forwards and backwards:

- *Nothing outside becomes a verdict.* Whether a session's context cards still resolve is a fact
  about the machine, not about the session, so it is not checked here. The same directory would be
  coherent on one machine and broken on another, which is not a property this function can have —
  and because `session import` refuses an archive on these problems, it would make a colleague's
  perfectly good session unimportable for want of a card you do not have. That check lives in
  `core.context.check_selection`, reported beside these problems by `doctor` and `session verify`.
- *Nothing inside sends us outside.* A claim in `session.json` is untrusted input, so it must not be
  able to aim a filesystem call anywhere else. `ArtifactStatus.filename` was joined into `artifacts/`
  and stat-ed unvalidated, and under `pathlib` an absolute component replaces the prefix outright —
  so the answer leaked whether an arbitrary path existed. See the artifact loop below.

Reading them together is what makes the pair coherent: an integrity answer is derived from the
directory's own bytes, and it neither imports facts from the environment nor exports questions to it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from requivo.core.contracts import PersistedEngineOutput
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.errors import InvalidFilenameError, RequivoError
from requivo.core.persistence import canonical_dir, content_hash, is_contained, migrate_session, validate_filename


@dataclass(frozen=True)
class IntegrityProblem:
    """One broken claim. `code` is a stable machine token (assert on it, not on the message)."""
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def _read_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as e:
        return None, str(e)


def _is_revision(filename: str, n: int) -> bool:
    """Whether `NNNN-model.json` names a revision this session claims to have."""
    head = filename.split("-", 1)[0]
    return head.isdigit() and 1 <= int(head) <= n


def check_session_dir(d: Path, *, expected_slug: str | None = None) -> list[IntegrityProblem]:
    """Every internal inconsistency in the session directory `d`, in reading order. Empty == coherent.

    `expected_slug` is the name the caller believes the session has — the directory name in the store,
    or the folder name inside an archive. A session that disagrees with its own container about its
    identity is the first thing to catch, because every later check keys on it.
    """
    problems: list[IntegrityProblem] = []

    def bad(code: str, message: str) -> None:
        problems.append(IntegrityProblem(code, message))

    meta_path = d / "session.json"
    if not meta_path.is_file():
        bad("no_session_json", f"{d.name}/session.json is missing — this is not a session directory")
        return problems
    raw, err = _read_json(meta_path)
    if raw is None:
        bad("unreadable_session_json", f"session.json cannot be read: {err}")
        return problems
    try:
        meta = migrate_session(raw)
    except (RequivoError, ValidationError) as e:
        # Both are expected here and neither should escape as a traceback: a *future* format is a
        # RequivoError by design, and a structurally wrong session.json is a Pydantic ValidationError.
        bad("invalid_session_json", f"session.json is not valid session metadata: {e}")
        return problems

    if expected_slug is not None and meta.slug != expected_slug:
        bad("slug_mismatch",
            f"the directory is {expected_slug!r} but session.json says {meta.slug!r} — the session "
            "does not agree with itself about its own identity")

    # ── the revision log ────────────────────────────────────────────────────────
    n = meta.current_revision
    if n < 0:
        bad("negative_revision", f"current_revision is {n}")
        return problems
    if len(meta.revisions) != n:
        bad("revision_count_mismatch",
            f"session.json says revision {n} but its log holds {len(meta.revisions)} record(s) — the "
            "history does not account for the model that is there")

    seen_hashes: dict[int, str] = {}
    for i, rec in enumerate(meta.revisions, start=1):
        if rec.revision != i:
            bad("revision_out_of_order",
                f"revision record {i} is numbered {rec.revision} — the log must be 1..N in order")
            continue
        expected_prev = None if i == 1 else i - 1
        if rec.previous_revision != expected_prev:
            bad("revision_chain_broken",
                f"revision {i} records previous_revision={rec.previous_revision}, expected "
                f"{expected_prev}")
        seen_hashes[i] = rec.model_hash

        f = d / "revisions" / f"{i:04d}-model.json"
        if not f.is_file():
            bad("missing_revision_file", f"revisions/{i:04d}-model.json is missing")
            continue
        payload = f.read_text(encoding="utf-8")
        if rec.model_hash and content_hash(payload) != rec.model_hash:
            bad("revision_hash_mismatch",
                f"revisions/{i:04d}-model.json does not match the hash recorded for it — the file "
                "was changed after it was written")
        try:
            # The permissive contract, matching `load_revision_model`: a field a newer Requivo added
            # is legal on disk, so a checker that refused it would report a defect in a session that
            # opens perfectly well — the diagnostic disagreeing with the loader about the same file
            # is worse than either answer on its own (#14).
            PersistedEngineOutput.model_validate_json(payload)
        except (ValidationError, ValueError) as e:
            bad("invalid_revision_model", f"revisions/{i:04d}-model.json is not a valid model: {e}")

    rev_dir = d / "revisions"
    if rev_dir.is_dir():
        extra = sorted(p.name for p in rev_dir.glob("*-model.json") if not _is_revision(p.name, n))
        if extra:
            bad("orphan_revision_file",
                f"revisions/ holds file(s) beyond revision {n}: {', '.join(extra)}")

    # ── the current model ───────────────────────────────────────────────────────
    model_path = d / "model.json"
    if n == 0:
        if model_path.is_file():
            bad("model_without_revision",
                "model.json exists but session.json is at revision 0 — a model that no revision "
                "accounts for has no provenance at all")
    elif not model_path.is_file():
        bad("missing_model", f"session.json is at revision {n} but there is no model.json")
    else:
        payload = model_path.read_text(encoding="utf-8")
        try:
            PersistedEngineOutput.model_validate_json(payload)  # permissive, as above
        except (ValidationError, ValueError) as e:
            bad("invalid_model", f"model.json is not a valid model: {e}")
        last_hash = seen_hashes.get(n)
        if last_hash and content_hash(payload) != last_hash:
            bad("model_is_not_the_last_revision",
                f"model.json does not match revision {n}, the revision it is supposed to be — "
                "the current model and the history describe different states")

    # ── artifacts ───────────────────────────────────────────────────────────────
    artifacts = d / "artifacts"
    for atype, st in meta.artifact_status.items():
        if atype not in ARTIFACT_FILENAMES:
            bad("unknown_artifact_type", f"session.json records an artifact of unknown type {atype!r}")
        elif st.filename != ARTIFACT_FILENAMES[atype]:
            bad("artifact_filename_mismatch",
                f"the {atype!r} artifact is recorded as {st.filename!r}, but that type is stored as "
                f"{ARTIFACT_FILENAMES[atype]!r}")

        # `st.filename` is an unconstrained `str` read out of session.json, and the two branches
        # above only *record* a problem — execution used to carry on to the join with the untrusted
        # value still in hand, so neither was a guard. `pathlib` makes the absolute case the sharp
        # one: an absolute component replaces everything before it, so `artifacts / "/etc/passwd"`
        # is `/etc/passwd` and the join never had to escape upwards at all. Nothing is ever read, so
        # this disclosed no content — it disclosed *existence*, because whether the row came back
        # `missing_artifact_file` answered whether that outside path was there.
        #
        # The name goes through the same `validate_filename` chokepoint every artifact write uses,
        # plus the containment confirmation `artifact_path` makes, and a refused name is *reported*
        # instead of probed. `artifact_path()` itself is deliberately not reused: it builds from
        # `canonical_dir(slug)`, the store, while this function is also handed a directory extracted
        # from an archive that is not in the store — it would answer confidently about the wrong
        # directory, which is this module's own defect class.
        #
        # The containment confirmation is `is_contained`, the store's, rather than a third statement
        # of the same rule (#3). It used to be written out here, and was then corrected twice for
        # defects the sibling copies had each been corrected for separately: a spurious disagreement
        # between two resolutions, reporting `unsafe_artifact_filename` about a perfectly bare name,
        # and a dangling symlink out of the session reported as a *missing* file on the one platform
        # whose resolver cannot follow one. Both are this module's own defect class — a confident
        # answer about the wrong thing — from the verb whose job is saying whether a session is
        # intact.
        #
        # The classification runs before the existence check below, and that ordering is what makes
        # the second of those visible at all: a name refused here is reported as refused, never
        # probed and then reported as absent.
        try:
            f = artifacts / validate_filename(st.filename)
            safe = is_contained(f, artifacts)
        except (InvalidFilenameError, OSError, ValueError):
            safe = False
        if not safe:
            bad("unsafe_artifact_filename",
                f"the {atype!r} artifact is recorded under {st.filename!r}, which this session "
                "cannot confirm is a bare file inside artifacts/ — refused without checking whether "
                "it exists")
        elif not f.is_file():
            bad("missing_artifact_file",
                f"session.json records a {atype!r} artifact but artifacts/{st.filename} is missing")

        if not 1 <= st.revision <= n:
            bad("artifact_revision_out_of_range",
                f"the {atype!r} artifact claims to come from revision {st.revision}, which this "
                f"session does not have (it has 1..{n or 0})")

    return problems


def check_session(slug: str) -> list[IntegrityProblem]:
    """`check_session_dir` for a session in the store."""
    return check_session_dir(canonical_dir(slug), expected_slug=slug)
