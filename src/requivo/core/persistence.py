from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from requivo import __version__
from requivo.core.contracts import EngineOutput
from requivo.core.errors import (
    InvalidSessionError,
    InvalidSlugError,
    RevisionConflictError,
    SessionExistsError,
    SessionLockedError,
    SessionNotFoundError,
)
from requivo.paths import output_root, session_root

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

SESSION_FORMAT_VERSION = 1
# The framework's slot schema version. Bumped when the slot vocabulary changes shape; recorded on
# every session so a future reader knows which schema a model was authored against.
SCHEMA_VERSION = 1


def _atomic_write(path: Path, content: str) -> Path:
    """Write via a temp file + atomic rename, so an interruption can never leave a half-written file
    where a good one was. model.json is the durable product — a truncated JSON would be unrecoverable,
    and `os.replace` (via Path.replace) is atomic on the same filesystem.

    The temp name is unique per writer. A fixed one (`.model.json.tmp`) made concurrent writers share
    a scratch file: two of them interleaved write-then-rename, and the second `replace()` raised
    `FileNotFoundError` on a temp file the first had already renamed away — a crash where the caller
    should have seen either a clean write or a `RevisionConflictError`."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave scratch behind on a failed write
        raise
    return path


# ── Session locking ────────────────────────────────────────────────────────────
# A session mutation is a *compound* write: read the metadata, check the revision precondition, write
# model.json, write revisions/NNNN-model.json, then rewrite session.json. Between the check and the
# last write, another writer reading the same revision would produce a second revision from the same
# base and silently overwrite the first. `expected_revision` alone cannot prevent that — it is checked
# and then acted on, and the gap between the two is the race. These helpers close it.
#
# `flock` (and its Windows equivalent) is held by the *open file description*, so the kernel releases
# it when the process dies — a crash cannot leave a session permanently locked, which is the failure
# mode a lockfile-by-existence scheme has. Held locks are tracked per thread so the service layer can
# nest `lock()` around several core calls that each take it.

_LOCK_TIMEOUT_SECONDS = 30.0
_held_locks = threading.local()


def _acquire(fd: int, slug: str) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    if msvcrt is not None:  # pragma: no cover - Windows
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # blocks ~10s per attempt, then raises
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise SessionLockedError(
                        f"session '{slug}' is locked by another process; retry in a moment",
                        details={"slug": slug}) from None


def _release(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextmanager
def session_lock(slug: str) -> Iterator[None]:
    """Hold the exclusive lock on a session for the duration of the block.

    Re-entrant within a thread: a service that wraps a whole update can take the lock once, and the
    core calls inside it (`save_revision`, `save_session_artifact`) re-enter without deadlocking.
    Across threads and across processes the lock is genuinely exclusive."""
    depths: dict[str, int] = getattr(_held_locks, "depths", None) or {}
    _held_locks.depths = depths
    if depths.get(slug):
        depths[slug] += 1
        try:
            yield
        finally:
            depths[slug] -= 1
        return

    d = canonical_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        _acquire(fd, slug)
        acquired = True
        depths[slug] = 1
        yield
    finally:
        depths[slug] = 0
        try:
            if acquired:
                _release(fd)
        finally:
            os.close(fd)


# A slug becomes a directory name, so it is bounded by what the filesystem accepts (~255 bytes on ext4
# and APFS, and the whole *path* on Windows). 80 leaves generous room for the session subtree beneath
# it. `_slug()` stays under the smaller base ceiling so a uniqueness suffix still fits inside the cap.
MAX_SLUG_LENGTH = 80
_SLUG_BASE_LENGTH = 64


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:5]
    base = "-".join(words) or "discovery"
    if len(base) <= _SLUG_BASE_LENGTH:
        return base
    # Five words are usually short, but nothing guarantees it: one 300-character token yields a
    # 300-character directory name and the filesystem refuses it with a bare OSError. Truncate
    # deterministically, then re-attach identity as a short hash so two different long requests can
    # never collapse onto the same session directory.
    keep = base[:_SLUG_BASE_LENGTH - 7].rstrip("-")
    return f"{keep}-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:6]}"


def load_model(path: Path) -> EngineOutput:
    """Load a saved model so artifacts can be regenerated without redoing discovery."""
    return EngineOutput.model_validate_json(path.read_text())


# ── Canonical session store (.requivo/sessions/<slug>/) ────────────────────────
# The versioned, forward-compatible layout: a session is a directory holding session.json (the
# metadata + provenance), request.md, model.json (the current model), revisions/NNNN-model.json (the
# history, one file per applied revision), and artifacts/ (generated views, each tied to the revision
# it was produced from). Every write is atomic; a revision is preserved before the model is replaced.
# Legacy `out/<slug>/` sessions are read-only and copied in here on first mutation (`migrate_legacy`).


class ArtifactStatus(BaseModel):
    """Per-artifact provenance in session.json: which model revision produced it, its file, when it
    was written, and whether the model has since moved past that revision (stale)."""
    revision: int
    filename: str
    updated_at: str
    stale: bool = False


class RevisionRecord(BaseModel):
    """Provenance for one applied revision: who produced it and from what. A session's model can be
    moved by more than one surface over its life (the Anthropic provider, a Claude Code turn, the CLI,
    later the Web), so provenance belongs to each *revision*, not just the session's creation. `extra`
    is allowed so a newer Requivo can add a provenance field an older reader simply carries through."""
    model_config = ConfigDict(extra="allow")

    revision: int
    created_at: str
    previous_revision: Optional[int] = None   # the revision this one succeeded (None for the first)
    provider: Optional[str] = None            # "anthropic", "claude-code", "cli", …
    model_name: Optional[str] = None          # the reasoning model, when one produced it
    surface: Optional[str] = None             # the reasoning surface, e.g. "cli-discover", "requivo-answer"
    prompt_version: Optional[str] = None      # "sha256:…" of the prompt, when known
    model_hash: str = ""                      # "sha256:…" of the model payload — content identity


class SessionMeta(BaseModel):
    """The versioned session metadata (`session.json`). `migrate_session()` is the explicit version
    frontier.

    `extra="allow"` — matching `RevisionRecord` — so a field a *newer* Requivo added survives a
    round-trip through an older one. Under `extra="ignore"` the older reader loaded the session fine
    and then dropped the unknown field the moment it wrote the file back, which turns "an old reader
    tolerates a new field" into "an old reader silently destroys it on first use". Forward
    compatibility is a promise about the file, not just about the load."""
    model_config = ConfigDict(extra="allow")

    format_version: int = SESSION_FORMAT_VERSION
    requivo_version: str = __version__
    session_id: str
    slug: str
    created_at: str
    updated_at: str
    provider: Optional[str] = None          # "anthropic", "claude-code", or None (informational)
    model_name: Optional[str] = None        # the reasoning model, when a provider set one
    context_cards: Optional[list[str]] = None  # the card selection; None == all cards
    request_hash: str = ""               # "sha256:…" of the originating request
    schema_version: int = SCHEMA_VERSION
    # (A session-level `prompt_versions` map lived here and was never written. Prompt identity belongs
    # to the revision that was reasoned with it, not to the session — see RevisionRecord.prompt_version.
    # It is listed in _RETIRED_KEYS so `extra="allow"` doesn't carry the dead key forever.)
    current_revision: int = 0            # 0 == session created but no model applied yet
    revisions: list[RevisionRecord] = Field(default_factory=list)  # provenance log, one per applied revision
    artifact_status: dict[str, ArtifactStatus] = Field(default_factory=dict)


def _now() -> str:
    """UTC, second precision, Z-suffixed — one timestamp format across the whole session file."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# A slug names a directory under the session root; it must never be able to escape it. `_slug()` and
# `resolve_slug()` always emit this shape, but an *explicit* `--slug` (or a future API caller) is
# untrusted input — so the two path constructors below validate before joining. The pattern forbids
# every traversal vector at once: `/`, `\`, `.`, `..`, a leading root, and the empty string.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate_slug(slug: str) -> str:
    """Return `slug` if it is a safe session identifier, else raise `InvalidSlugError`. Lives in Core
    so every surface (CLI, provider, a future web service) inherits the same directory-traversal guard,
    not just FastAPI. Belt-and-suspenders: callers additionally confirm the resolved path stays under
    the root, but the pattern alone already makes a separator or dot segment unrepresentable."""
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise InvalidSlugError(
            f"invalid session slug {slug!r}; expected kebab-case [a-z0-9-], e.g. 'leave-approval'",
            details={"slug": slug})
    # Length is part of validity, not a separate concern: an over-long slug is a directory name the
    # filesystem rejects, and it fails deep inside a write as an OSError instead of at the boundary.
    # `_slug()` never emits one; an explicit --slug or an API caller can.
    if len(slug) > MAX_SLUG_LENGTH:
        raise InvalidSlugError(
            f"session slug is {len(slug)} characters; the maximum is {MAX_SLUG_LENGTH}",
            details={"slug": slug[:MAX_SLUG_LENGTH], "length": len(slug),
                     "max_length": MAX_SLUG_LENGTH})
    return slug


def _child_of(root: Path, slug: str) -> Path:
    """`root / slug`, having validated the slug and confirmed the resolved path is genuinely a child of
    `root` — the defence-in-depth check the traversal guard is built around."""
    d = root / validate_slug(slug)
    if not d.resolve().is_relative_to(root.resolve()):
        raise InvalidSlugError(f"slug {slug!r} resolves outside the session root", details={"slug": slug})
    return d


def canonical_dir(slug: str) -> Path:
    """The canonical session directory `<workspace>/.requivo/sessions/<slug>/`."""
    return _child_of(session_root(), slug)


def legacy_dir(slug: str) -> Path:
    """The legacy `out/<slug>/` directory — read-only, migrated on first mutation."""
    return _child_of(output_root(), slug)


def session_exists(slug: str) -> bool:
    return (canonical_dir(slug) / "session.json").exists()


def legacy_exists(slug: str) -> bool:
    return (legacy_dir(slug) / "model.json").exists()


def write_meta(slug: str, meta: SessionMeta) -> Path:
    d = canonical_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    return _atomic_write(d / "session.json", meta.model_dump_json(indent=2))


# Keys a past Requivo wrote (or declared) and no longer means anything. `extra="allow"` preserves
# every unknown key, which is right for a key from the *future* and wrong for one from the past — so
# retirement is explicit here, in the migration, rather than implicit in the model config.
_RETIRED_KEYS = ("prompt_versions",)


def migrate_session(data: dict) -> SessionMeta:
    """The version frontier: turn a raw session.json dict into a `SessionMeta`, upgrading old formats.
    Only v1 exists today, but the boundary is explicit — a session written by a *newer* Requivo is
    rejected clearly rather than silently mis-read. Unknown keys are carried through untouched (see
    `SessionMeta`); known-retired ones are dropped."""
    fv = data.get("format_version", SESSION_FORMAT_VERSION)
    if fv > SESSION_FORMAT_VERSION:
        raise InvalidSessionError(
            f"session format v{fv} is newer than this Requivo understands (v{SESSION_FORMAT_VERSION}) "
            "— upgrade requivo.",
            details={"format_version": fv},
        )
    # The slot vocabulary is a second, independent contract, and it was recorded on every session and
    # then read by nothing. A model authored against a newer schema can hold slots this build has no
    # definition for; without this check the first symptom is an `unknown_slot` error naming a slot the
    # user never typed. An *older* schema is fine — that is ordinary backward compatibility.
    sv = data.get("schema_version", SCHEMA_VERSION)
    if isinstance(sv, int) and sv > SCHEMA_VERSION:
        raise InvalidSessionError(
            f"this session was authored against slot schema v{sv}, newer than this Requivo understands "
            f"(v{SCHEMA_VERSION}) — upgrade requivo.",
            details={"schema_version": sv, "supported_schema_version": SCHEMA_VERSION},
        )
    return SessionMeta.model_validate({k: v for k, v in data.items() if k not in _RETIRED_KEYS})


def read_meta(slug: str) -> SessionMeta:
    p = canonical_dir(slug) / "session.json"
    if not p.exists():
        raise SessionNotFoundError(f"no session '{slug}' under {session_root()}", details={"slug": slug})
    try:
        return migrate_session(json.loads(p.read_text()))
    except (OSError, json.JSONDecodeError) as e:
        raise InvalidSessionError(f"session '{slug}' has an unreadable session.json: {e}",
                                  details={"slug": slug}) from e


def create_session(slug: str, request: str, *, provider: str | None = None,
                   model_name: str | None = None, context_cards: list[str] | None = None) -> SessionMeta:
    """Create a fresh session directory from a request — no model yet (current_revision 0). The
    model is applied later via `save_revision` (deterministic `model apply`, or a provider turn).

    The session is assembled beside its destination and moved in with a single rename, which is the
    *claim* on the slug: either this call created the session, or it learns one was already there
    (`SessionExistsError`). Two things follow, and both were bugs before. Creation is atomic, where a
    preceding `has_meta` check was not — two concurrent creations both passed it, and the second
    rewrote the first's metadata, giving the session a new id and losing the provider and context
    cards the first had recorded. And a session becomes visible *complete*: with a directory created
    first and the metadata written after, a concurrent reader could find a session whose `session.json`
    did not exist yet."""
    now = _now()
    meta = SessionMeta(
        session_id=uuid.uuid4().hex, slug=slug, created_at=now, updated_at=now,
        provider=provider, model_name=model_name, context_cards=context_cards,
        request_hash=_hash(request),
    )
    d = canonical_dir(slug)
    d.parent.mkdir(parents=True, exist_ok=True)
    # Dot-prefixed, so a staging directory can never be mistaken for a session: slugs are validated and
    # cannot start with a dot, and `list_session_slugs` skips them.
    staging = d.with_name(f".{d.name}.new-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        (staging / "revisions").mkdir(parents=True)
        (staging / "artifacts").mkdir()
        _atomic_write(staging / "request.md", request)
        _atomic_write(staging / "session.json", meta.model_dump_json(indent=2))
        try:
            staging.rename(d)
        except OSError as e:
            if not d.exists():  # the rename failed for some other reason — don't mislabel it
                raise
            raise SessionExistsError(f"session '{slug}' already exists", details={"slug": slug}) from e
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return meta


def save_revision(slug: str, model: EngineOutput, *, expected_revision: int | None = None,
                  provenance: dict | None = None) -> tuple[int, SessionMeta]:
    """Persist a new model revision: write model.json AND revisions/NNNN-model.json (the prior model
    is already frozen in an earlier revision file), record the revision's provenance, then bump
    current_revision + updated_at. Returns (new_revision, updated_meta).

    `expected_revision` is an optimistic-locking precondition: when given, the write fails with
    `RevisionConflictError` unless the session is still at that revision — so two updates racing from
    the same base can't both land silently. The single-user CLI omits it (last-writer-wins is fine
    locally); a concurrent Web service passes the revision the client read. `provenance` carries the
    surface-supplied fields (provider / model_name / surface / prompt_version) for the revision log.

    The precondition and every write it guards run under `session_lock`, because a check that is not
    held across the writes it authorises is not a precondition — two writers could both read revision
    N, both pass the check, and both write revision N+1."""
    with session_lock(slug):
        meta = read_meta(slug)  # raises SessionNotFoundError if the session isn't there
        if expected_revision is not None and meta.current_revision != expected_revision:
            raise RevisionConflictError(
                f"session '{slug}' is at revision {meta.current_revision}, not the expected "
                f"{expected_revision} — reload the current model and re-apply",
                details={"slug": slug, "expected": expected_revision,
                         "actual": meta.current_revision})
        d = canonical_dir(slug)
        (d / "revisions").mkdir(parents=True, exist_ok=True)
        rev = meta.current_revision + 1
        payload = model.model_dump_json(indent=2)
        _atomic_write(d / "model.json", payload)
        _atomic_write(d / "revisions" / f"{rev:04d}-model.json", payload)
        prov = dict(provenance or {})
        meta.revisions.append(RevisionRecord(
            revision=rev,
            created_at=_now(),
            previous_revision=meta.current_revision or None,
            model_hash=_hash(payload),
            provider=prov.get("provider"),
            model_name=prov.get("model_name"),
            surface=prov.get("surface"),
            prompt_version=prov.get("prompt_version"),
        ))
        meta.current_revision = rev
        meta.updated_at = _now()
        write_meta(slug, meta)
        return rev, meta


def load_session_model(slug: str) -> EngineOutput:
    """The current model of a canonical session."""
    p = canonical_dir(slug) / "model.json"
    if not p.exists():
        raise SessionNotFoundError(
            f"session '{slug}' has no model yet (apply a proposal first)", details={"slug": slug})
    return EngineOutput.model_validate_json(p.read_text())


def load_revision_model(slug: str, revision: int) -> EngineOutput:
    """A historical model revision — the basis for `impact` since a given point."""
    p = canonical_dir(slug) / "revisions" / f"{revision:04d}-model.json"
    if not p.exists():
        raise SessionNotFoundError(
            f"session '{slug}' has no revision {revision}", details={"slug": slug, "revision": revision})
    return EngineOutput.model_validate_json(p.read_text())


def session_request(slug: str) -> str:
    p = canonical_dir(slug) / "request.md"
    return p.read_text() if p.exists() else ""


def save_session_artifact(slug: str, artifact_type: str, filename: str, content: str,
                          source_revision: int, *, stale: bool = False) -> ArtifactStatus:
    """Write an artifact under artifacts/ and record its provenance (source revision) in session.json.

    The revision is validated against the session's history first: provenance that cannot be true is
    worse than none, because every freshness question downstream is answered from it. A revision in
    the future (or before the first model) is refused rather than recorded.

    `stale` is supplied by the caller, which is the layer that knows the dependency graph — see
    `ArtifactService.save`. Core records freshness; it does not decide it.
    """
    with session_lock(slug):
        meta = read_meta(slug)
        if not 1 <= source_revision <= meta.current_revision:
            raise InvalidSessionError(
                f"cannot record {artifact_type!r} against revision {source_revision}: session '{slug}' "
                f"has revisions 1..{meta.current_revision or 0}",
                details={"slug": slug, "source_revision": source_revision,
                         "current_revision": meta.current_revision})
        d = canonical_dir(slug)
        (d / "artifacts").mkdir(parents=True, exist_ok=True)
        _atomic_write(d / "artifacts" / filename, content)
        st = ArtifactStatus(revision=source_revision, filename=filename, updated_at=_now(), stale=stale)
        meta.artifact_status[artifact_type] = st
        meta.updated_at = _now()
        write_meta(slug, meta)
        return st


def write_artifact_file(slug: str, filename: str, content: str) -> Path:
    """Write a raw file into a session's artifacts/ directory (no status tracking) — for the neutral
    epic exports (epic.json / epic.github.json / …) that are extra views of one generated artifact."""
    d = canonical_dir(slug)
    (d / "artifacts").mkdir(parents=True, exist_ok=True)
    return _atomic_write(d / "artifacts" / filename, content)


def session_artifact_files(slug: str) -> set[str]:
    """Filenames currently under artifacts/ — the on-disk set change-detection intersects with the
    blast radius (only artifacts that were actually generated can go stale)."""
    d = canonical_dir(slug) / "artifacts"
    return {p.name for p in d.iterdir() if p.is_file()} if d.exists() else set()


def list_session_slugs() -> list[str]:
    """Slugs of all canonical sessions, sorted — the backbone of `session list`."""
    root = session_root()
    if not root.exists():
        return []
    # Dot-prefixed directories are never sessions (a slug cannot start with one) — they are the
    # staging areas `create_session` assembles a session in before renaming it into place.
    return sorted(p.name for p in root.iterdir()
                  if not p.name.startswith(".") and (p / "session.json").exists())


def migrate_legacy(slug: str) -> SessionMeta:
    """Copy a legacy `out/<slug>/` session into the canonical store, **preserving the originals**.

    Called on the first mutation of a legacy session (never a bulk sweep). The existing model becomes
    revision 1; provenance is recovered from the old session.json where present; known artifact files
    are copied into artifacts/ and recorded at revision 1. The legacy directory is left untouched."""
    from requivo.core.dependencies import ARTIFACT_FILES  # local import avoids a load-time cycle

    src = legacy_dir(slug)
    if not (src / "model.json").exists():
        raise SessionNotFoundError(f"no legacy session '{slug}' under {output_root()}",
                                   details={"slug": slug})
    request = ""
    for name in ("request.md", "request.txt"):
        if (src / name).exists():
            request = (src / name).read_text()
            break
    old: dict = {}
    if (src / "session.json").exists():
        try:
            old = json.loads((src / "session.json").read_text())
        except (OSError, json.JSONDecodeError):
            old = {}

    d = canonical_dir(slug)
    (d / "revisions").mkdir(parents=True, exist_ok=True)
    (d / "artifacts").mkdir(parents=True, exist_ok=True)
    now = _now()
    if request:
        req_hash = _hash(request)
    else:
        # Fall back to the legacy session.json's hash, normalising a bare hex digest to "sha256:…".
        legacy_hash = str(old.get("request_sha256", ""))
        req_hash = legacy_hash if legacy_hash.startswith("sha256:") or not legacy_hash else "sha256:" + legacy_hash
    meta = SessionMeta(
        session_id=uuid.uuid5(uuid.NAMESPACE_URL, f"requivo:legacy:{slug}").hex,
        slug=slug, created_at=old.get("created_at", now), updated_at=now,
        provider=old.get("provider"), model_name=old.get("model_name"),
        context_cards=old.get("context_cards"), request_hash=req_hash, current_revision=0,
    )
    if request:
        _atomic_write(d / "request.md", request)
    write_meta(slug, meta)  # so save_revision can read/update it

    model = EngineOutput.model_validate_json((src / "model.json").read_text())
    rev, _ = save_revision(slug, model)  # existing model → revision 1

    filename_to_type = {fn: t for t, fn in ARTIFACT_FILES.items() if fn}
    for fn, atype in filename_to_type.items():
        legacy_file = src / fn
        if legacy_file.exists():
            content = legacy_file.read_text()
            save_session_artifact(slug, atype, fn, content, source_revision=rev)
    return read_meta(slug)
