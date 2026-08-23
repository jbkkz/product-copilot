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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from requivo import __version__
from requivo.core.contracts import EngineOutput, PersistedEngineOutput
from requivo.core.errors import (
    ArtifactRevisionOutOfRangeError,
    InvalidFilenameError,
    InvalidSlugError,
    RevisionConflictError,
    SessionExistsError,
    SessionLockedError,
    SessionNotFoundError,
    SessionUnreadableError,
    UnsupportedFormatVersionError,
    UnsupportedSchemaVersionError,
)
from requivo.paths import lock_root, output_root, session_root

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
        _replace_with_retry(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave scratch behind on a failed write
        raise
    return path


# On POSIX `rename` over an existing file always succeeds. On Windows it is `MoveFileEx`, which needs
# the destination to be openable, so it fails with `PermissionError(13, 'Access is denied')` whenever
# *anything* holds a handle to it — most often an antivirus scanner or the Search Indexer, which open
# a file microseconds after it is written, and neither of which this process can serialise against.
# The failure is transient by nature, and `model.json` is the durable product, so losing a completed
# write to a scanner is not an acceptable outcome.
#
# Deliberately bounded and deliberately narrow. It retries `PermissionError` only, a handful of times,
# over a few hundred milliseconds, and then re-raises the original: a genuinely unwritable destination
# (a read-only file, a real permissions problem) still fails loudly and quickly, because turning a
# permanent error into a slow permanent error helps nobody. This is the one place where retrying is
# right rather than a way of hiding something -- the operation is idempotent and the cause is external.
_REPLACE_ATTEMPTS = 8
_REPLACE_BACKOFF_S = 0.01


def _replace_with_retry(tmp: Path, path: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))


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


def _no_session(slug: str) -> SessionNotFoundError:
    """The one refusal for "there is no such session", so the lock and the metadata read cannot drift
    into telling a caller two different stories about the same absence."""
    return SessionNotFoundError(f"no session '{slug}' under {session_root()}", details={"slug": slug})


def lock_path(slug: str) -> Path:
    """The write lock for `slug`: `<workspace>/.requivo/locks/<slug>.lock`.

    **Outside the session directory, which is the whole of #113's fix.** `lock_root()` carries why;
    the short version is that a lock inside a directory `session import --force` renames is a claim
    on an inode that every writer under it has already stopped agreeing with.

    Validated exactly as `canonical_dir` and `artifact_path` validate theirs, and for the same
    reason: the slug reaches here from `session_lock`, whose callers include the service layer and
    therefore, under invariant 14, an external consumer. The pattern already makes a separator or a
    dot segment unrepresentable; `is_contained` is the belt to that pair of braces, and it is the
    one shared statement of that rule rather than a fourth local one."""
    root = lock_root()
    p = root / (validate_slug(slug) + ".lock")
    if not is_contained(p, root):
        raise InvalidSlugError(f"slug {slug!r} does not resolve to a lock file inside {root}",
                               details={"slug": slug})
    return p


@contextmanager
def session_lock(slug: str) -> Iterator[None]:
    """Hold the exclusive lock on a session for the duration of the block.

    Re-entrant within a thread: a service that wraps a whole update can take the lock once, and the
    core calls inside it (`save_revision`, `save_session_artifact`) re-enter without deadlocking.
    Across threads and across processes the lock is genuinely exclusive.

    **The lock file lives outside the session** (`lock_path`), so this function no longer touches the
    session directory at all. That is what lets `session import --force` hold it across the swap it
    could not hold it across before (#113), and it retires #22's coupling permanently rather than
    guarding it: `session_lock` is structurally incapable of producing a directory under the session
    root, so it can never again make `create_session`'s rename — the only claim on a slug under
    invariant 11 — lose to a ghost nobody created.

    **A session must still exist to be locked, and the check that decides that is taken *after* the
    lock is held.** It used to be closed by accident rather than by ordering: the lock file lived
    inside the session, so a directory deleted after the check made `os.open` raise
    `FileNotFoundError` and that arm mapped it onto "no such session". Opening
    `.requivo/locks/<slug>.lock` establishes nothing about `<slug>`, so the accident is gone and the
    check has to earn its place — which it does by moving under the lock, where invariant 9's rule
    ("a precondition is held across the writes it authorises") applies to it like any other.
    `test_a_session_deleted_before_the_lock_is_granted_is_refused` goes red if it moves back out.

    **The check before the open stays, and is deliberately not authoritative.** It buys two things
    and decides nothing: a slug with no session refuses without leaving an empty lock file behind,
    and — the reason it is worth a line — `os.open` is never reached for a name that is not a
    session, which on Windows keeps a slug spelling a reserved device (`con`, `nul`, `lpt1`) out of
    `os.open` entirely. `validate_slug` does not exclude those, and such a name can never be a real
    session on Windows anyway because `create_session`'s directory is refused first; this keeps the
    error for one immediate and correct instead of a 30-second `SessionLockedError` on a console
    handle. It can be wrong in exactly one direction — `_swap_in` holds this lock across two renames
    and `<root>/<slug>` does not exist for the microseconds between them, so a caller sampling that
    instant refuses `session_not_found` about a session that is merely being replaced. That window
    is the one the pre-#113 code already had at its `os.open`, and a refusal is the safe direction:
    this check can decline a lock, never grant one.

    Neither the lock file nor a session directory is ever removed here. Unlinking a lock file a
    concurrent process may be holding is legal on POSIX and silently breaks mutual exclusion — the
    repair #22 rejected, and the same reason `_swap_in` could not be written as a contents swap.

    **Legacy `.lock` files inside existing session directories are inert.** Nothing opens them now,
    `session export` already skips every dot-prefixed entry, and `check_session_dir` does not look
    for unexpected files. They cost a few empty bytes and are safe to delete."""
    depths: dict[str, int] = getattr(_held_locks, "depths", None) or {}
    _held_locks.depths = depths
    if depths.get(slug):
        depths[slug] += 1
        try:
            yield
        finally:
            depths[slug] -= 1
        return

    if not session_exists(slug):     # cheap, non-authoritative — see above
        raise _no_session(slug)
    p = lock_path(slug)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        # Not `SessionNotFoundError`: the session's existence is not what this failed to establish.
        # The old code mapped a `FileNotFoundError` here onto "no such session" because the lock file
        # lived inside the session; it no longer does, so that mapping would now be a sentence about
        # a session naming a cause that is not the cause — the shape #114 was filed for.
        raise SessionUnreadableError(
            f"could not open the write lock for session '{slug}': {e}", details={"slug": slug}) from e
    acquired = False
    try:
        _acquire(fd, slug)
        acquired = True
        if not session_exists(slug):
            raise _no_session(slug)
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
# it. `derive_slug()` stays under the smaller base ceiling so a uniqueness suffix still fits inside the cap.
MAX_SLUG_LENGTH = 80
_SLUG_BASE_LENGTH = 64


def derive_slug(text: str) -> str:
    """Derive a session directory name from arbitrary text — the one producer of the canonical shape.

    Public because it is consumed outside this module: `SessionService.slug_hint` is the surface's
    route to it, and `validate_slug` below is written against exactly what this emits.
    """
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
    """Load a saved model so artifacts can be regenerated without redoing discovery.

    Read through `PersistedEngineOutput` — still an `EngineOutput`, so the annotation holds — because
    a model on disk may have been written by a newer Requivo, and refusing an unknown key there costs
    the reader a session they can otherwise understand completely. The block at the foot of
    `contracts.py` says why the disk side and the provider side answer that question oppositely.

    The explicit codec is #11's and is not optional here either: `_atomic_write` writes UTF-8, so a
    read that takes the platform default decodes a model holding an accented value into mojibake that
    is still valid JSON, on exactly the platforms this repo now has CI legs for."""
    return PersistedEngineOutput.model_validate_json(path.read_text(encoding="utf-8"))


# ── Canonical session store (.requivo/sessions/<slug>/) ────────────────────────
# The versioned, forward-compatible layout: a session is a directory holding session.json (the
# metadata + provenance), request.md, model.json (the current model), revisions/NNNN-model.json (the
# history, one file per applied revision), and artifacts/ (generated views, each tied to the revision
# it was produced from). Every write is atomic; a revision is preserved before the model is replaced.
# Legacy `out/<slug>/` sessions are read-only and are copied in here only by the explicit
# `requivo session migrate` (`migrate_legacy`). Nothing has read that layout implicitly since 0.9.8.


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


def content_hash(text: str) -> str:
    """The persisted hash format — `sha256:<hex>` — as `model_hash` and `request_hash` carry it on disk.

    Public because `integrity.py` recomputes it to check a session against its own recorded hashes.
    A second implementation of this line would drift, and a drifted rehash reports
    `revision_hash_mismatch` against a file nobody touched.
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# A slug names a directory under the session root; it must never be able to escape it. `derive_slug()` and
# `resolve_slug()` always emit this shape, but an *explicit* `--slug` (or a future API caller) is
# untrusted input — so the two path constructors below validate before joining. The pattern forbids
# every traversal vector at once: `/`, `\`, `.`, `..`, a leading root, and the empty string.
#
# `\Z` and not `$`, here and on `_FILENAME_RE` below (#40, adjacent). Python's `$` matches at the end
# of the string **or just before a trailing newline**, so `validate_slug("ok\n")` returned its
# argument unchanged — a guard whose stated job is to make a control character unrepresentable,
# admitting exactly one. `\Z` is the anchor both docstrings were already describing.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")


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
    # `derive_slug()` never emits one; an explicit --slug or an API caller can.
    if len(slug) > MAX_SLUG_LENGTH:
        raise InvalidSlugError(
            f"session slug is {len(slug)} characters; the maximum is {MAX_SLUG_LENGTH}",
            details={"slug": slug[:MAX_SLUG_LENGTH], "length": len(slug),
                     "max_length": MAX_SLUG_LENGTH})
    return slug


def is_slug(name: str) -> bool:
    """Whether `name` is a slug `create_session` can be asked for — the same question `validate_slug`
    answers, as a predicate.

    Deliberately implemented by *calling* it rather than by re-testing `_SLUG_RE`. Validity is the
    pattern **and** the length, and a second statement of that drifts: `slug_shaped` was written
    against the pattern alone and marked an 81-character kebab-case directory as a name a session
    would silently lose, when `canonical_dir` refuses such a name outright and loudly. One rule, one
    place, found by review."""
    try:
        validate_slug(name)
    except InvalidSlugError:
        return False
    return True


# A filename is the *other* half of an artifact write target, and it was unvalidated while its slug
# sibling on the same call was not. The same shape as `_SLUG_RE`, one separator class wider: a name is
# runs of [a-z0-9] joined by single `.`, `-` or `_`. That forbids every vector at once — `/`, `\`, a
# `..` segment (two dots in a row cannot be written), a leading or trailing separator, a leading dot,
# and the empty string — while still admitting every name the store actually writes
# (`solution-assessment.md`, `acceptance-criteria.md`, `epic.github.json`).
#
# Deliberately lowercase-only, matching the slug: a rejection is loud and one edit away, whereas a
# permissive pattern is the thing being removed here. Every filename in `ARTIFACT_FILENAMES` and every
# epic export name already fits.
_FILENAME_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")

# Room for the whole name plus the unique scratch suffix `_atomic_write` appends (a dot, the pid, 8
# hex and `.tmp` — about 20 characters), inside the ~255-byte ceiling ext4 and APFS impose.
MAX_FILENAME_LENGTH = 120


def validate_filename(filename: str) -> str:
    """Return `filename` if it is a safe bare filename, else raise `InvalidFilenameError`.

    The sibling of `validate_slug`, and it exists for the reason stated there: the guard belongs in
    Core so every surface inherits it, not in the callers that happen to be careful. Every in-repo
    caller passes a literal or an `ARTIFACT_FILENAMES` lookup — which is precisely why this was
    missing, and precisely the argument invariant 14 makes for putting it here anyway: the threat
    model is the external consumer calling the service directly, not the CLI."""
    if not isinstance(filename, str) or not _FILENAME_RE.match(filename):
        raise InvalidFilenameError(
            f"invalid artifact filename {filename!r}; expected a bare lowercase name such as "
            "'prd.md' — no directories, no dot segments, no leading dot",
            details={"filename": filename})
    # Length is part of validity for the same reason it is for a slug: an over-long name is refused by
    # the filesystem deep inside the write, as a bare OSError, instead of at the boundary.
    if len(filename) > MAX_FILENAME_LENGTH:
        raise InvalidFilenameError(
            f"artifact filename is {len(filename)} characters; the maximum is {MAX_FILENAME_LENGTH}",
            details={"filename": filename[:MAX_FILENAME_LENGTH], "length": len(filename),
                     "max_length": MAX_FILENAME_LENGTH})
    return filename


def _resolve(path: Path) -> Path:
    """Canonicalise `path` for a containment comparison — `os.path.realpath`, deliberately, and not
    `Path.resolve()`.

    The two agree on POSIX and on Windows from CPython 3.10, where pathlib routes `resolve()` through
    `realpath()` — with one exception worth knowing, because it is a second thing this switch quietly
    fixed: on 3.9 a **symlink loop** makes `Path.resolve()` raise `RuntimeError`, which no caller here
    catches (`check_session_dir` catches `OSError`/`ValueError`), while `realpath` collapses the path
    lexically and returns an answer the containment check can act on. Verified on 3.9.6: a self-
    referencing link raises `RuntimeError: Symlink loop from …` through pathlib and resolves cleanly
    to its out-of-root target through `realpath`, where it is then correctly refused.

    **On Windows under 3.9 the two disagree in the direction that matters, and the disagreement was a
    hole in the containment check below.** `_WindowsFlavour.resolve` asks `nt._getfinalpathname`,
    which has to
    *open* the path, so it fails on a symlink whose target does not exist; the non-strict branch then
    splits the unresolvable tail off, resolves the longest prefix that does exist, and re-joins the
    rest verbatim. A dangling symlink at `<root>/<slug>` therefore comes back as
    `<resolved root>/<slug>` — reported as living exactly where it sits, however far out of the root
    it actually points — and `is_relative_to` says yes. `ntpath.realpath` does not take that route:
    when `_getfinalpathname` fails it reads the reparse point itself and follows the link to its
    missing target, which is the answer a containment check needs.

    Seen as `DID NOT RAISE InvalidSlugError` on the `py3.9, windows-latest` leg and on no other: the
    3.13 Windows leg and every POSIX leg were green, because only that one combination takes the
    broken route (#3, #11). Nothing here needs anything newer than 3.9 — `os.path.realpath` is
    non-strict by default, and the `strict=` keyword that would otherwise be the obvious thing to
    reach for is 3.10+.
    """
    return Path(os.path.realpath(path))


def is_contained(child: Path, parent: Path) -> bool:
    """Is `child` genuinely inside `parent`? The one containment decision in the store.

    `_child_of`, `artifact_path` and `check_session_dir` each used to state this in their own words,
    and each then had to be corrected for the same two defects in turn — the race below, and the
    dangling link above. Three statements of one rule is three places for the next correction to miss,
    and this branch has already missed one of them once.

    Public for that reason. `check_session_dir` is in `integrity.py` and imports this rather than
    restating it, so the name is a cross-module contract and not a local helper.

    The resolution happens **only when `child` is there in some form**, which is load-bearing rather
    than an optimisation. Comparing two independently resolved paths, where one is derived from the
    other, gives a verdict that depends on what the filesystem looked like between the two calls:
    create a directory in that window and they disagree, so `canonical_dir("s")` raised
    `InvalidSlugError` — *you gave me a bad slug* — about a perfectly good slug, because somebody else
    was creating a session at that moment. Reproduced on POSIX by symlinking a parent between the two
    calls, and seen in CI as four of twelve concurrent creators crashing on the Windows leg (#3).

    `is_symlink()` as well as `exists()`, and not `exists()` alone: `exists()` follows the link, so a
    **dangling** symlink pointing out of `parent` reports False and would skip the very check it is
    the reason for.

    Answering True for a child that is not there is a claim about the caller as much as about the
    path. Every caller has put the name through `validate_slug` or `validate_filename` first, so what
    was joined is a single flat component — no separator, no dot segment, nothing absolute — sitting
    lexically one level below `parent`. The only way out is a symlink at `child` itself, and a path
    that does not exist is not a symlink. So the check runs in exactly the case that can fail it,
    against a parent resolved once.

    False therefore means *not confirmed to be inside*, which is two situations and deliberately one
    answer: the resolved path is somewhere else, or the resolver could not tell us where it goes. The
    second is the third state, and folding it in with *inside* is what the Windows 3.9 defect was —
    a guard that could not look, reporting what it reports when it looked and found nothing. Callers
    word their refusal to cover both, because from here they are the same decision.
    """
    if not (child.exists() or child.is_symlink()):
        return True
    root = _resolve(parent)
    resolved = _resolve(child)
    # A resolver that could not follow the link hands back the link's own location — literally so on
    # 3.9/Windows, whose non-strict branch re-joins the unresolvable tail to the parent it *could*
    # resolve (see `_resolve`). A symlink never legitimately resolves to where it sits, so the
    # equality is a reliable tell and not a heuristic, and the answer to it is to refuse.
    #
    # This is what keeps the guarantee off the platform. Without it the containment decision rests on
    # the resolver being able to follow a link, which is an assumption that held on twelve legs of
    # thirteen and was invisible on the twelfth. `child.parent` is `parent` at all three call sites,
    # so `root / child.name` costs no third resolution; anywhere it is not, the equality simply does
    # not match and the containment test below answers on its own.
    if child.is_symlink() and resolved == root / child.name:
        return False
    return resolved.is_relative_to(root)


def _child_of(root: Path, slug: str) -> Path:
    """`root / slug`, having validated the slug and confirmed the result is genuinely a child of
    `root` — the defence-in-depth check the traversal guard is built around. `is_contained` carries
    the reasoning for both halves of that confirmation, and for why it is one function."""
    d = root / validate_slug(slug)
    if not is_contained(d, root):
        raise InvalidSlugError(f"slug {slug!r} does not resolve to a path inside the session root",
                               details={"slug": slug})
    return d


def canonical_dir(slug: str) -> Path:
    """The canonical session directory `<workspace>/.requivo/sessions/<slug>/`."""
    return _child_of(session_root(), slug)


def legacy_dir(slug: str) -> Path:
    """The legacy `out/<slug>/` directory — read-only, and migrated only by an explicit
    `requivo session migrate`, never on a read or a first write (see `migrate_legacy`)."""
    return _child_of(output_root(), slug)


def artifact_path(slug: str, filename: str) -> Path:
    """`<session>/artifacts/<filename>`, with **both** halves validated — the single chokepoint every
    artifact read and write goes through.

    One function rather than a check at each call site, for the reason `_child_of` gives for the slug:
    a rule applied per-caller is a rule the next caller forgets. Belt-and-suspenders in the same
    shape too — the pattern already makes a separator or a dot segment unrepresentable, and the
    result is confirmed to be a genuine child of `artifacts/` anyway, through the same
    `is_contained` the slug half uses. `artifacts/` is created lazily, so the race that check is
    written around is real here too.

    **Display-only callers come through here too, and that is not ceremony.** Two sites printed
    `canonical_dir(slug) / "artifacts" / <recorded filename>` inline — a path neither of them ever
    opened — and survived both the sweep that closed the writes (#5) and the one that closed the
    read (#23), because "it only prints it" reads as harmless. It is a different harm rather than an
    absent one: a read traversal answers what this code may *disclose* rather than what it may
    create, and a printed path is the plainest disclosure there is.

    The name in both arrives on an `ArtifactStatus`, whose `filename` is a plain `str` that nothing
    re-validates when `read_meta` loads it back — so it is invariant 14's threat model exactly: the
    external consumer holding the services over a repository that is not this file backing, where
    `save_artifact` hands back whatever its store held. **`session import` is not that door, and
    saying so is the point.** The invariant's argument is written about `context_cards`, which import
    deliberately cannot resolve, and it does *not* carry over here: `check_session_dir` pins every
    recorded filename to its `ARTIFACT_FILENAMES` value and to containment, and `session import`
    refuses the whole archive when either fails — reproduced, both for a traversal and for a merely
    wrong name. Read as covering both fields, this would claim a vector that is shut and quietly drop
    the one that is open.

    Coming through here also means such a name cannot forge a line in the terminal it is printed to:
    `_FILENAME_RE` is anchored at end-of-string and admits no line break (#40).

    A target that is not there is not an error here. `is_contained` does stat it — `exists()` is a
    stat — and answers True for what it cannot find rather than raising, so routing a display site
    through this does not turn a session with nothing generated into a refusal. Absence and refusal
    stay the two different answers `read_artifact_file` keeps them as."""
    d = canonical_dir(slug) / "artifacts"
    p = d / validate_filename(filename)
    if not is_contained(p, d):
        raise InvalidFilenameError(
            f"artifact filename {filename!r} does not resolve to a path inside {d}",
            details={"slug": slug, "filename": filename})
    return p


def _probe(marker: Path, slug: str) -> bool:
    """Is `marker` there? — with the third answer routed out through the error channel.

    `Path.exists()` has two returns and three outcomes: it swallows `ENOENT`/`ENOTDIR` into `False`,
    which is right, and **re-raises everything else**, which used to escape these two functions as a
    bare `PermissionError` traceback. That is the identical unguarded probe #80 had to remove from
    `_scan_session_root`, and it mattered more after #80 than before: `session list` now renders a
    degraded row for an entry it could not examine and prints a footer pointing the reader at
    `session verify <slug>` — which opened with `session_exists` and crashed on the one slug it had
    just been told to look at (#97).

    **The bool is not widened, because a bool cannot hold three states.** Answering `False` here
    would be the collapse: `cli.py` and `session import --force` read these to decide whether to
    *create or overwrite*, so turning *I could not tell* into *there is nothing here* is a write
    proceeding on an unknown. The third state leaves as `SessionUnreadableError` — #82's code for a
    fact about the store rather than about the request, already 500 over HTTP and already what
    `read_meta` raises when its read fails. `ENOENT` still returns `False`: absent is a real answer
    and the commonest one."""
    try:
        return marker.exists()
    except OSError as e:
        raise SessionUnreadableError(
            f"could not determine whether session '{slug}' exists: {e}",
            details={"slug": slug}) from e


def session_exists(slug: str) -> bool:
    return _probe(canonical_dir(slug) / "session.json", slug)


def legacy_exists(slug: str) -> bool:
    return _probe(legacy_dir(slug) / "model.json", slug)


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
        raise UnsupportedFormatVersionError(
            f"session format v{fv} is newer than this Requivo understands (v{SESSION_FORMAT_VERSION}) "
            "— upgrade requivo.",
            details={"format_version": fv, "supported_format_version": SESSION_FORMAT_VERSION},
        )
    # The slot vocabulary is a second, independent contract, and it was recorded on every session and
    # then read by nothing. A model authored against a newer schema can hold slots this build has no
    # definition for; without this check the first symptom is an `unknown_slot` error naming a slot the
    # user never typed. An *older* schema is fine — that is ordinary backward compatibility.
    sv = data.get("schema_version", SCHEMA_VERSION)
    if isinstance(sv, int) and sv > SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"this session was authored against slot schema v{sv}, newer than this Requivo understands "
            f"(v{SCHEMA_VERSION}) — upgrade requivo.",
            details={"schema_version": sv, "supported_schema_version": SCHEMA_VERSION},
        )
    return SessionMeta.model_validate({k: v for k, v in data.items() if k not in _RETIRED_KEYS})


def read_meta(slug: str) -> SessionMeta:
    p = canonical_dir(slug) / "session.json"
    if not p.exists():
        raise _no_session(slug)
    try:
        return migrate_session(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as e:
        raise SessionUnreadableError(f"session '{slug}' has an unreadable session.json: {e}",
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
        request_hash=content_hash(request),
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
            model_hash=content_hash(payload),
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
    return PersistedEngineOutput.model_validate_json(p.read_text(encoding="utf-8"))


def load_revision_model(slug: str, revision: int) -> EngineOutput:
    """A historical model revision — the basis for `impact` since a given point."""
    p = canonical_dir(slug) / "revisions" / f"{revision:04d}-model.json"
    if not p.exists():
        raise SessionNotFoundError(
            f"session '{slug}' has no revision {revision}", details={"slug": slug, "revision": revision})
    return PersistedEngineOutput.model_validate_json(p.read_text(encoding="utf-8"))


def session_request(slug: str) -> str:
    p = canonical_dir(slug) / "request.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def save_session_artifact(slug: str, artifact_type: str, filename: str, content: str,
                          source_revision: int, *, stale: bool = False) -> ArtifactStatus:
    """Write an artifact under artifacts/ and record its provenance (source revision) in session.json.

    The revision is validated against the session's history first: provenance that cannot be true is
    worse than none, because every freshness question downstream is answered from it. A revision in
    the future (or before the first model) is refused rather than recorded.

    `stale` is supplied by the caller, which is the layer that knows the dependency graph — see
    `ArtifactService.save`. Core records freshness; it does not decide it.

    `filename` is validated exactly as `slug` is, and before the lock is taken: it is the *other* half
    of the write target, and it is also recorded into session.json, where `integrity.py` and the
    artifact-show paths read it back — so an unvalidated one both escapes the directory and persists.
    """
    path = artifact_path(slug, filename)   # refuse a bad target before taking the lock
    with session_lock(slug):
        meta = read_meta(slug)
        if not 1 <= source_revision <= meta.current_revision:
            raise ArtifactRevisionOutOfRangeError(
                f"cannot record {artifact_type!r} against revision {source_revision}: session '{slug}' "
                f"has revisions 1..{meta.current_revision or 0}",
                details={"slug": slug, "source_revision": source_revision,
                         "current_revision": meta.current_revision})
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, content)
        st = ArtifactStatus(revision=source_revision, filename=filename, updated_at=_now(), stale=stale)
        meta.artifact_status[artifact_type] = st
        meta.updated_at = _now()
        write_meta(slug, meta)
        return st


def write_artifact_file(slug: str, filename: str, content: str) -> Path:
    """Write a raw file into a session's artifacts/ directory (no status tracking) — for the neutral
    epic exports (epic.json / epic.github.json / …) that are extra views of one generated artifact.

    Both halves of the target go through `artifact_path`: the mutating route validated its slug and
    not the filename beside it, so `write_artifact_file(slug, '../../../x.md', …)` wrote outside the
    session entirely."""
    path = artifact_path(slug, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    return _atomic_write(path, content)


def read_artifact_file(slug: str, filename: str) -> Optional[str]:
    """The saved content of a file under a session's artifacts/, or None if there is no such file.

    The read sibling of `write_artifact_file`, and it exists so that the read goes through
    `artifact_path` instead of re-joining the path a second time. `FileSessionRepository` built
    `canonical_dir(slug) / "artifacts" / filename` inline, one layer above the chokepoint — which is
    precisely how it escaped the sweep that routed the two *mutating* paths through it, and
    precisely what `_child_of` means by a rule the next caller forgets. A read traversal is also a
    different exposure from a write one: not what this code may create, but what it may disclose.

    **Absence and refusal are deliberately different answers.** An unsafe `filename` raises
    `InvalidFilenameError`; only a genuinely missing file returns None. Returning None for both
    would be the quiet answer — a rejected traversal would then be indistinguishable from an
    artifact nobody has generated yet, and the caller cannot tell it has been refused.

    Decoded as UTF-8 explicitly, matching `_atomic_write`'s encoding on the way in. `read_text()`
    with no encoding uses the locale's, which on Windows is typically cp1252 and silently mojibakes
    any generated artifact containing an em-dash rather than failing."""
    p = artifact_path(slug, filename)
    return p.read_text(encoding="utf-8") if p.exists() else None


def session_artifact_files(slug: str) -> set[str]:
    """Filenames currently under artifacts/ — the on-disk set change-detection intersects with the
    blast radius (only artifacts that were actually generated can go stale)."""
    d = canonical_dir(slug) / "artifacts"
    return {p.name for p in d.iterdir() if p.is_file()} if d.exists() else set()


# How much of a non-session directory's contents is worth carrying into a report. A lock ghost holds
# one entry; a half-extracted archive can hold thousands, and a diagnostic that prints all of them
# stops being read at all. Five is enough to tell those two apart on sight, which is the whole job.
# The true total travels beside the sample, so a truncated list can never be mistaken for the whole
# of what is there.
_NON_SESSION_SAMPLE = 5


@dataclass(frozen=True)
class NonSessionEntry:
    """Something under the session root that is **not** a session, described and not interpreted.

    A directory holding only `.lock` is almost certainly what `session_lock` left behind before #22,
    and *almost certainly* is not a licence to say so: a half-extracted archive, an interrupted copy
    and a directory a user made by hand are the same shape from here, and `integrity.py`'s rule is
    that the evidence is the directory and only the directory. So every field is an observation —
    the name, what kind of thing it is, what it holds. There is deliberately no field spelling a
    conclusion, because a reader acts on the name of the field and not on the paragraph beside it.

    `slug_shaped` is the one derived value, and it is a property of the *name* rather than a guess at
    the entry's history: whether `create_session` can be asked for it, which is what decides whether
    this entry costs anybody anything. A name that is not slug-shaped is unreachable — `canonical_dir`
    refuses it before any rename is attempted, loudly — so it is reported and carries no consequence.
    It is answered by `is_slug`, which calls `validate_slug`, because validity is the pattern *and*
    the length and testing the pattern alone marked an 81-character name as one a session would
    silently lose (found by review).

    `entries` is capped at `_NON_SESSION_SAMPLE` and `entry_count` is the true total. Three states,
    as everywhere:

    - `entries` populated with `error` None — we looked inside;
    - `entries` None with an `error` — we could not, which must not render like an empty directory.
      On POSIX an empty directory is the one shape that costs nothing at all: `rename(2)` replaces an
      empty destination, so `create_session` still wins the name. (Windows does not — `os.rename` is
      `MoveFileEx` without `MOVEFILE_REPLACE_EXISTING` and refuses any existing destination — which
      is why `slug_shaped` does not exempt an empty one.) Telling *empty* from *could not look* is
      therefore the difference between a finding and a non-finding, on at least one platform;
    - `entries` None with no `error` on a `file` or `other` — there is nothing to look inside, which
      is a third kind of absence again.
    """
    name: str
    kind: str
    entries: list[str] | None
    entry_count: int | None
    error: str | None
    slug_shaped: bool

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "entries": self.entries,
                "entry_count": self.entry_count, "error": self.error,
                "slug_shaped": self.slug_shaped}


@dataclass(frozen=True)
class UnexaminableEntry:
    """A name under the session root whose examination **raised** — the partition's third outcome.

    Not a session, and not *not* a session: unknown. The probe that decides which one it is failed,
    so both of the other answers would be claims nobody established.

    `error` is the exception's own text rather than a code, for the reason every other third state
    in this codebase keeps it: *permission denied on this path* is a remedy and `unexaminable` is
    not. It carries the path, which is the part a user acts on."""
    name: str
    error: str

    def to_dict(self) -> dict:
        return {"name": self.name, "error": self.error}


def _scan_session_root() -> tuple[list[str], list[Path], list[UnexaminableEntry]]:
    """One listing of the session root, partitioned three ways: the canonical sessions, everything
    else, and the entries whose examination raised.

    **Three outcomes, because the predicate can fail.** `(p / "session.json").exists()` is what
    decides whether a name is a session, and `Path.exists()` swallows only the errnos in
    `pathlib._IGNORED_ERRNOS` — ENOENT, ENOTDIR, EBADF, ELOOP. `EACCES` is not among them, so a
    directory the process cannot stat into propagated out of this loop and aborted the partition for
    *every* entry: `session list` exited 1 with an empty stdout and a raw traceback, and every
    healthy session in the workspace was invisible (#80).

    The first two halves used to be described here as each other's complement, and for two states
    that was exactly right. It is not right for three, and the third belongs in neither of theirs:

    * in `others` it would never come back from `list_session_slugs`, so `session list` would omit it
      silently — the invisible entry #67 exists to close, reintroduced one function along;
    * in `slugs` it would be claimed to *be* a session, which is the one thing the failed probe did
      not establish, and every read path downstream reasons over that list.

    So the predicate is still stated once and the buckets are still disjoint; what changed is that
    the answer has a third value, and a caller has to be able to say *we could not tell*.

    Dot-prefixed entries are in none of the three, on purpose. A slug cannot start with a dot, so
    they are `create_session`'s staging areas: a session in flight rather than something left behind,
    and reporting one is a race the reader cannot act on.

    A root that does not exist is an empty workspace and returns nothing. A root that cannot be
    *listed* is not the same answer, and this still raises rather than flattening the two — that
    failure is genuinely the whole root, there is no entry to name it against, and the caller is the
    one that has to be able to say `we could not look`. Per-entry and whole-root are two different
    claims and this function must not merge them in either direction."""
    root = session_root()
    if not root.exists():
        return [], [], []
    slugs: list[str] = []
    others: list[Path] = []
    unexaminable: list[UnexaminableEntry] = []
    for p in sorted(root.iterdir(), key=lambda p: p.name):
        if p.name.startswith("."):
            continue
        try:
            is_session = (p / "session.json").exists()
        except Exception as e:  # noqa: BLE001 - the third outcome, not a failure of the listing
            # `Exception` rather than `OSError`, for `_describe_non_session`'s reason one function
            # down: the set of ways a probe of a name off a directory listing can fail is open —
            # EACCES here, and on Linux a filename that is not valid UTF-8 comes back from
            # `iterdir` carrying surrogates, which every path operation on `p` is a candidate for.
            # Whatever it was, it lands in a state this partition now has. `BaseException` is not
            # caught: a `KeyboardInterrupt` is not an unexaminable directory.
            unexaminable.append(UnexaminableEntry(p.name, str(e)))
            continue
        if is_session:
            slugs.append(p.name)
        else:
            others.append(p)
    return slugs, others, unexaminable


def list_session_slugs() -> list[str]:
    """Slugs of all canonical sessions, sorted — the backbone of `session list`.

    **Names known to be sessions, and this contract does not widen.** `doctor`, `session verify` and
    every read path reason over what comes back here, so an entry the partition could not examine is
    deliberately not in it — see `list_unexaminable_entries`, which is where it goes instead."""
    return _scan_session_root()[0]


def _describe_non_session(p: Path) -> NonSessionEntry:
    """Describe one entry, and **never raise**.

    Totality is the point, not politeness. This runs inside the one `try` in `_session_health` that
    also holds the session listing, so an exception escaping here discards a session report that had
    already succeeded and tells the reader the whole root was unlistable — a claim broader than what
    failed, which is invariant 15's shape one layer down. The two arms below are therefore `Exception`
    rather than `OSError`, and each still lands in a state this entry already has: *we could not stat
    it* and *we could not list it*. That is not the guard-that-provably-cannot-fire invariant 15 warns
    against — it is the same third state reached from a wider set of causes, and the cause I could not
    rule out is real: on Linux a filename that is not valid UTF-8 comes back from `iterdir` carrying
    surrogates, and every consumer of `p.name` downstream is a candidate. APFS refuses such a name, so
    it could not be constructed here to be ruled out either way."""
    slug_shaped = is_slug(p.name)
    try:
        # `is_symlink` first, and it does not follow. `is_dir()` does: a symlink at a slug name
        # pointing anywhere else reported as a plain `directory`, and then `iterdir` listed the
        # **target's** filenames into a report about this workspace. A symlink is a third shape, not
        # a directory, and this file already treats one as the single case a containment guard has to
        # answer for (invariant 17). Found by review.
        if p.is_symlink():
            return NonSessionEntry(p.name, "symlink", None, None, None, slug_shaped)
        kind = "directory" if p.is_dir() else ("file" if p.is_file() else "other")
    except Exception as e:  # noqa: BLE001 - a describe that raises blanks a report that succeeded
        # `Path.is_dir()` swallows only what `_ignore_error` covers — ENOENT, ENOTDIR, ELOOP — and
        # re-raises the rest, EACCES among them. A stat we are not allowed to make lands here, and
        # what this is is then genuinely unknown: answering `other` would be a claim we cannot make.
        return NonSessionEntry(p.name, "unknown", None, None, str(e), slug_shaped)
    if kind != "directory":
        return NonSessionEntry(p.name, kind, None, None, None, slug_shaped)
    try:
        names = sorted(c.name for c in p.iterdir())
    except Exception as e:  # noqa: BLE001 - same reason; the kind is known, the contents are not
        return NonSessionEntry(p.name, kind, None, None, str(e), slug_shaped)
    return NonSessionEntry(p.name, kind, names[:_NON_SESSION_SAMPLE], len(names), None, slug_shaped)


def scan_session_root() -> tuple[list[str], list[NonSessionEntry], list[UnexaminableEntry]]:
    """All three parts of the session root from **one** listing — for the caller that asks all of it.

    `list_session_slugs`, `list_non_session_entries` and `list_unexaminable_entries` each scan on
    their own, which is right when only one question is being asked and wrong when more than one is.
    `doctor` asks all three, and two scans are two instants: a `session.json` appearing between them
    puts a name in *neither* answer, which is the invisible state #67 is about, reintroduced by the
    report meant to close it; one disappearing puts it in both. Transient and diagnostic-only, and
    still not something to leave in the one verb whose job is to say whether anything is wrong.
    Found by review.

    The describe step is here rather than in `_scan_session_root` so that `list_session_slugs` — on
    every one of its call paths, `session list` included — keeps paying nothing for it: a stray
    directory holding ten thousand files is one `iterdir` this function makes and that one does not.
    The third part carries no describe step at all: whatever we would ask it, we have just failed to
    ask it once."""
    slugs, others, unexaminable = _scan_session_root()
    return slugs, [_describe_non_session(p) for p in others], unexaminable


def list_unexaminable_entries() -> list[UnexaminableEntry]:
    """Names under the session root whose examination raised — the partition's third answer (#80).

    Neither `list_session_slugs` nor `list_non_session_entries` returns one, and that is the point:
    calling it a session claims what the failed probe did not establish, and calling it a non-session
    hides it from `session list`, which is #67's defect one function along. It reaches a surface as a
    fact of its own — a degraded row on `session list`, its own line under `doctor`'s sessions check.

    **A report, not a repair**, on `list_non_session_entries`' terms: Requivo reads a workspace and
    does not chmod anything in it. What is here is a name and the reason the probe failed.

    A caller that wants the other parts too should take `scan_session_root()` instead: this one scans
    on its own, and two scans are two instants."""
    return scan_session_root()[2]


def list_non_session_entries() -> list[NonSessionEntry]:
    """Everything else under the session root, described — `_scan_session_root`'s second part.

    Second of three since #80, not the other half of two: an entry whose examination *raised* is
    neither a session nor established to be one of these, and is returned by
    `list_unexaminable_entries` instead. Putting it here would hide it from `session list` for want
    of a `session.json` nobody could look for, which is this function's own defect class.

    Nothing could see these before #67. `list_session_slugs` skips them for want of a `session.json`,
    so `doctor` and `session verify` never reach one; and `check_session` answers about a directory it
    is handed, which nobody can hand it a name for. The only symptom was at the next `create_session`
    on that name: the rename that *is* the claim on a slug (invariant 11) loses to a directory that is
    already there, and `SessionService` falls through to its `<slug>-<identity hash>` candidate — so
    the user gets a session under a name they did not ask for, with nothing anywhere explaining why
    the one they asked for was unavailable.

    **A report, not a repair.** This reads; it never deletes, moves or rewrites. #22 stopped
    `session_lock` producing these, and clearing one on sight would be the same mistake pointing the
    other way: unlinking a `.lock` a concurrent process is holding is legal on POSIX and silently
    breaks mutual exclusion, and nothing in the directory tells a ghost from a half-extracted archive.

    It lives in Core beside `list_session_slugs` because that function owns the store layout and the
    answers come out of one predicate. Core reading a directory is not a boundary crossing:
    invariant 7 forbids importing a provider and touching argv, the streams, the environment and
    process exit — not IO, which this module is made of.

    A caller that wants the other parts too should take `scan_session_root()` instead: this one scans
    on its own, and two scans are two instants."""
    return scan_session_root()[1]


def migrate_legacy(slug: str) -> SessionMeta:
    """Copy a legacy `out/<slug>/` session into the canonical store, **preserving the originals**.

    Called explicitly (`requivo session migrate`), never on a read. The existing model becomes
    revision 1; provenance is recovered from the old session.json where present; known artifact files
    are copied into artifacts/ and recorded at revision 1. The legacy directory is left untouched.

    **The claim on the slug is `create_session`'s rename**, not an existence check. This function
    *creates* a session, so it makes its claim the same way the only other creator does, and invariant
    11 applies to it verbatim: a preceding check is passed by two concurrent callers at once. It used
    to check only that the legacy *model* existed, so pointed at a slug a live session already
    occupied it rewrote session.json at revision 0 and then wrote the legacy model over
    revisions/0001-model.json — and revisions/ is the only durable copy, so revision 1 was destroyed
    with no copy anywhere. Now the rename loses and `SessionExistsError` is raised before anything is
    written; the caller decides whether that is a skip or a failure.

    Everything after the claim runs under one `session_lock` (invariant 9), so the metadata patch, the
    revision and the artifact writes are a single unit rather than three separately-locked ones, and
    `expected_revision=0` holds the session to the state the claim left it in."""
    from requivo.core.dependencies import ARTIFACT_FILES  # local import avoids a load-time cycle

    src = legacy_dir(slug)
    if not (src / "model.json").exists():
        raise SessionNotFoundError(f"no legacy session '{slug}' under {output_root()}",
                                   details={"slug": slug})
    request = ""
    for name in ("request.md", "request.txt"):
        if (src / name).exists():
            request = (src / name).read_text(encoding="utf-8")
            break
    old: dict = {}
    if (src / "session.json").exists():
        try:
            old = json.loads((src / "session.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = {}
    # Parse the legacy model *before* claiming the slug: a malformed out/ model should fail without
    # leaving an empty session behind holding a name nothing can now use.
    model = PersistedEngineOutput.model_validate_json((src / "model.json").read_text(encoding="utf-8"))

    if request:
        req_hash = content_hash(request)
    else:
        # Fall back to the legacy session.json's hash, normalising a bare hex digest to "sha256:…".
        legacy_hash = str(old.get("request_sha256", ""))
        req_hash = legacy_hash if legacy_hash.startswith("sha256:") or not legacy_hash else "sha256:" + legacy_hash

    # The claim. Raises SessionExistsError if a canonical session already occupies the slug.
    create_session(slug, request, provider=old.get("provider"), model_name=old.get("model_name"),
                   context_cards=old.get("context_cards"))

    with session_lock(slug):
        # The three fields `create_session` cannot know, because they belong to the *legacy* session:
        # its original creation date, the request hash a migration may have to recover from the old
        # metadata when no request file survived, and an id derived from the slug so re-reading a
        # migrated session finds the identity a previous migration of it would have given.
        meta = read_meta(slug)
        meta.session_id = uuid.uuid5(uuid.NAMESPACE_URL, f"requivo:legacy:{slug}").hex
        meta.created_at = old.get("created_at", meta.created_at)
        meta.request_hash = req_hash
        write_meta(slug, meta)

        rev, _ = save_revision(slug, model, expected_revision=0)  # existing model → revision 1

        filename_to_type = {fn: t for t, fn in ARTIFACT_FILES.items() if fn}
        for fn, atype in filename_to_type.items():
            legacy_file = src / fn
            if legacy_file.exists():
                content = legacy_file.read_text(encoding="utf-8")
                save_session_artifact(slug, atype, fn, content, source_revision=rev)
        return read_meta(slug)
