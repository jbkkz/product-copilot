from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from requivo import __version__
from requivo.core.contracts import EngineOutput, PersistedEngineOutput
from requivo.core.errors import (
    ArtifactRevisionOutOfRangeError,
    InvalidFilenameError,
    InvalidSlugError,
    ModelUnreadableError,
    RevisionConflictError,
    SessionExistsError,
    SessionLockedError,
    SessionNotFoundError,
    SessionUnreadableError,
    UnsupportedFormatVersionError,
    UnsupportedSchemaVersionError,
)
from requivo.core.selectors import display_token
from requivo.paths import output_root as _ambient_output_root
from requivo.paths import workspace_root

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
        # newline="" disables universal-newline translation on write -- the direct analogue of the
        # explicit encoding= one keyword back (invariant 16). Without it, text mode with the default
        # newline=None translates every '\n' in content to os.linesep on write; a no-op on POSIX
        # (os.linesep == '\n'), but on Windows a lone CR already in content becomes '\r\r\n' on disk,
        # a line the document never had (#464). See test_atomic_write_passes_newline_empty_to_disable_translation.
        tmp.write_text(content, encoding="utf-8", newline="")
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
# revisions/NNNN-model.json, write model.json, then rewrite session.json. Between the check and the
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

# The POSIX poll interval (#265). `flock(LOCK_EX | LOCK_NB)` either succeeds immediately or raises,
# so contention is a poll loop, not a single blocking call, and the interval trades latency for CPU:
# too short spins the CPU on a genuinely stuck holder for the whole 30s deadline, too long adds
# needless latency to the overwhelmingly common case, a holder that finishes in milliseconds. Fixed
# rather than backed off, on purpose: writes hold this lock for milliseconds (see the module
# docstring above), so contention that outlasts a handful of polls is already the pathological case
# the deadline exists for, and a growing interval would only add latency to the *ordinary* one it
# does not help. At 20ms the full deadline is ~1500 wakeups -- negligible CPU for a bound that fires
# only when something is stuck.
_LOCK_POLL_INTERVAL_S = 0.02


def _acquire(fd: int, slug: str) -> None:
    """Take the OS lock on `fd`, bounded by `_LOCK_TIMEOUT_SECONDS` on every platform (#265).

    The two branches used to disagree about what a stuck holder looks like: `msvcrt.locking` polls
    on its own (it blocks ~10s per attempt and raises `OSError` between attempts) so the Windows loop
    could turn that into a deadline, but POSIX's `fcntl.flock(fd, fcntl.LOCK_EX)` is a single call
    that blocks until it succeeds, with nothing to loop on -- so a stuck holder (a SIGSTOPped
    process, a debugger, an NFS-mounted workspace) hung the CLI silently and forever on the two
    primary platforms, while Windows raised the structured `SessionLockedError` this module already
    defines. `LOCK_EX | LOCK_NB` makes the POSIX call symmetric with the Windows one: it never blocks
    the kernel, so the same poll-until-deadline shape now governs both.

    Re-entrancy (invariant 9) is unaffected by this: `session_lock`'s own depth counter decides a
    nested acquisition on the same thread *before* this function is ever called, so a nested `with
    session_lock(slug):` never reaches `_acquire` a second time regardless of whether this function
    blocks once or polls. Pinned by
    `test_reentrant_acquisition_within_a_thread_still_never_touches_the_lock_twice`; the deadline
    itself by `test_a_contended_lock_raises_within_the_deadline_instead_of_hanging` (POSIX;
    the `msvcrt` branch's own bound is unchanged and untouched here).

    **`BlockingIOError`, not a bare `OSError`** (caught in review before this shipped). `flock(...,
    LOCK_NB)` raises exactly that -- CPython maps `EAGAIN`/`EWOULDBLOCK` to it since PEP 3151 -- when
    and only when the lock is genuinely held elsewhere; a bare `except OSError` would also catch
    `ENOLCK`, `EBADF` or a filesystem that refuses `flock` outright (NFS misconfigured, some network
    mounts), none of which will ever resolve by waiting. The single blocking call this replaced let
    such an error surface immediately as what it was; masking it behind up to 30 seconds of retries
    and then relabelling it "locked by another process" would trade a loud, honest failure for a
    quiet, misleading one -- the same rule `_replace_with_retry`'s own narrow `except PermissionError`
    states two functions up in this file. Anything else still fails immediately and honestly."""
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    if fcntl is not None:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SessionLockedError(
                        f"session '{slug}' is locked by another process; retry in a moment",
                        details={"slug": slug}) from None
                time.sleep(_LOCK_POLL_INTERVAL_S)
    if msvcrt is not None:  # pragma: no cover - Windows
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


class Store:
    """One workspace's `.requivo/` layout, addressed by an explicit `root` rather than by reading
    `paths.workspace_root()` (`REQUIVO_WORKSPACE`/cwd) fresh on every call (#272).

    Every method below used to be a free function in this module, each resolving
    `session_root()`/`lock_root()`/`store_root()`/`output_root()` from `requivo.paths` ambiently --
    which is what made two `FileSessionRepository` instances in one process indistinguishable (see
    that class's own docstring in `services/repository.py`). This class is the "one construction
    site" `docs/cloud-boundary.md` (§3.1) argues for: an object holds the root, and
    everything that needs to know *which* workspace it is addressing reads it off `self` instead of
    off the process.

    **That file will not resolve on this branch alone.** It lands via the 2026-09 readiness audit
    (#441/#442), which merged to `main` after this branch was cut from an older commit -- confirmed
    with `git merge-tree` to bring in no conflict here, `services/discovery.py` included (disjoint
    hunks). Found in review, twice, independently: a reader on this branch who follows the citation
    before it is rebased or merged onto current `main` finds nothing. The argument itself is
    reproduced in full in `docs/decisions/0004-workspace-root-as-constructor-state.md`'s own first
    draft, which this branch drafted independently and then deleted once the landed audit page
    turned out to say the same thing in more depth -- see this issue's pull request body for that
    history, since the record itself no longer exists on this branch to point at.

    The module-level functions of the same names, below, are kept -- unchanged in name and signature
    -- as thin wrappers over a **freshly-resolved default instance**, `Store(workspace_root())`,
    built again on every call. That is what preserves the CLI's `--workspace`/`REQUIVO_WORKSPACE`
    behaviour byte-for-byte: mutating the environment mid-process (`cli.py`'s `--workspace` handling)
    is picked up by the very next ambient call, exactly as before this class existed. An explicit
    `Store(root)` -- what `FileSessionRepository(root=...)` builds once, at construction -- is immune
    to that mutation by design; see that class's own docstring for the ambient-vs-fixed asymmetry
    this is for.

    **Root identity, not object identity, decides lock re-entrancy.** `session_lock`'s re-entrancy
    depth used to be tracked in a thread-local dict keyed by `slug` alone, which was safe only because
    there was ever exactly one implicit root live in a process at a time. Two genuinely different
    roots that happen to share a slug name are two different sessions and must never be treated as
    the same held lock -- and the *ambient* module-level wrapper below constructs a fresh `Store`
    instance on every call, so keying by `id(self)` would have broken re-entrancy for that path
    instead (a nested ambient call would look like a different lock and either deadlock retrying the
    OS lock, or -- worse -- silently skip acquiring it while believing it already holds it). Keyed by
    the resolved root instead, both properties hold at once. Pinned by
    `test_two_roots_sharing_a_slug_do_not_share_a_lock` and
    `test_reentrant_acquisition_across_fresh_ambient_stores_is_still_recognised`.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        # Resolved once, here, rather than by `_lock_key` on every `session_lock` call (found in
        # review, #272). `self.root` never changes after construction, so re-resolving it on every
        # acquisition -- including every re-entrant nested one -- bought nothing but a repeated
        # `os.path.realpath` stat on a hot path that used to cost zero syscalls (the pre-#272 key was
        # the bare `slug` string). Worse than wasteful if the *answer* could ever change between an
        # outer and inner acquisition of the same instance -- an ancestor symlink of `root` being
        # repointed mid-hold -- since `session_lock`'s re-entrancy check would then miss, and a
        # second `os.open`/`flock` on what POSIX treats as a distinct open file description can block
        # the process against its own already-held lock (`flock` is scoped to the open file
        # description, not the process). Fixing it here removes the question rather than arguing it
        # cannot occur: the value is now fixed at the same moment `self.root` itself is.
        self._root_key = str(_resolve(root))

    # ── roots -- mirroring paths.py's ambient functions, bound to self.root instead of the process ──

    def store_root(self) -> Path:
        return self.root / ".requivo"

    def session_root(self) -> Path:
        return self.store_root() / "sessions"

    def lock_root(self) -> Path:
        return self.store_root() / "locks"

    def debug_root(self) -> Path:
        return self.store_root() / "debug"

    def output_root(self) -> Path:
        """The retired `out/` layout root -- deliberately NOT derived from `self.root`, unlike every
        other root on this class. `paths.output_root()`'s own docstring already says why:
        `REQUIVO_OUTPUT_DIR`/cwd was always a knob independent of `REQUIVO_WORKSPACE` -- so
        `--workspace <dir>` alone, with no `REQUIVO_OUTPUT_DIR`, has always looked for the legacy
        `out/` layout under the process's cwd, never under `<dir>`. Reading `self.root / "out"` here
        (an earlier version of this method did) silently substituted the workspace root for cwd,
        breaking `session migrate` for anyone using `--workspace` without also setting
        `REQUIVO_OUTPUT_DIR` -- found in review (#272), before it shipped past this branch. #272's
        own scope excludes changing env-var semantics, so this reads the identical ambient value
        `paths.output_root()` always has, on every `Store` alike, explicit root or not. An
        explicitly-rooted deployment has no legacy `out/` layout to migrate from in the first place;
        this is dead code for that shape rather than a workspace-scoped feature worth inventing.
        Pinned by `test_an_explicit_stores_legacy_root_still_honours_the_ambient_output_dir_override`."""
        return _ambient_output_root()

    def _lock_key(self, slug: str) -> tuple[str, str]:
        """The re-entrancy key for `slug` in *this* store -- see the class docstring for why root
        identity, not `id(self)`, is what has to decide it. `self._root_key` is resolved once, at
        construction (`__init__`), not recomputed here -- see that comment for why."""
        return (self._root_key, slug)

    # ── everything below was a free function; each docstring is the original, unchanged, and each
    # body is unchanged except that it now reads its root off `self` -----------------------------

    def ensure_store_dir(self, path: Path) -> Path:
        """`mkdir(parents=True, exist_ok=True)` for anything under `.requivo/`, writing the privacy
        `.gitignore` on the call that brings the store root into existence.

        **Every directory creation under the store goes through here, and that is the point** (#211).
        `.requivo/` lands in the caller's *workspace*, which defaults to cwd — for the Claude Code plugin
        that is the user's project repository by construction — and `create_session` writes the client's
        request there verbatim. A routine `git add .` then publishes confidential requirements to whatever
        remote that project pushes to, silently, against the local-first confidentiality this product
        states as its wedge. This repository's own `.gitignore` covers `.requivo/`, so the maintainer was
        the one person who could not experience it.

        The issue proposed writing the file at "the one place `.requivo` is first created". There is no
        such place: every call site creates it as a `parents=True` ancestor — the lock directory, the
        session root in `create_session`, `write_meta`, `save_revision`, `save_session_artifact`,
        `write_artifact_file`, and `session import`. Whichever of them a given workspace happens to reach
        first is the one that creates the root, so guarding one guards nothing. Hence a single ensure
        function rather than a single call site, with
        `test_no_store_directory_is_created_outside_ensure_store_dir` failing on one that goes around it.

        **Written once, on creation, and never recreated.** A user who deletes it to commit sessions
        deliberately stays committed, and a user who edits it keeps their edit. Pinned by
        `test_the_privacy_gitignore_is_written_once_and_never_restored`.

        **The trigger is `mkdir` winning, not `exists()` answering, and that is the whole of #320.** This
        first read `not root.exists()` before creating anything, which broke in two directions at once.
        `Path.exists()` re-raises `EACCES` rather than swallowing it — invariant 15's #80, one function
        along — and `PermissionError` is not a `RequivoError`, so the first command run in a workspace
        whose parent denies stat ended in a traceback instead of a refusal. And the answer it gave was
        the wrong question: *does the root exist* is not *did I create the root*, so a marker write that
        failed once left `.requivo/` present and unignored, after which every later call read
        `fresh = False` and never tried again. One transient error switched the confidentiality
        guarantee off for the life of that workspace, silently, and left the result indistinguishable
        from a user who had deleted the file on purpose — the one state this design means to be
        irreversible.

        `mkdir(parents=True)` with **no** `exist_ok` answers the real question atomically and probes
        nothing: it either creates the root or raises `FileExistsError`, and only the winner writes.
        Pinned by `test_a_failed_marker_write_leaves_no_root_behind_to_suppress_the_next_attempt`.

        **All-or-nothing, so a failure is retryable.** If the marker cannot be written, the root this
        call just made is removed again before the error surfaces. That keeps the store's two possible
        states to "root and marker" or "neither" — the alternative is the silent hole above. `rmdir`
        only removes an empty directory, so a concurrent creator's work is never destroyed; if it cannot
        be removed the error still surfaces, because a visible failure is the point.
        """
        root = self.store_root()
        fresh = True
        try:
            root.mkdir(parents=True)
        except FileExistsError:
            # Somebody else owns the root — this process, an earlier run, or a concurrent creator whose
            # marker decision already stands. Losing this race is success.
            fresh = False
        except OSError as e:
            raise SessionUnreadableError(
                f"could not create the session store at {root}: {e}", details={"path": str(root)}) from e
        if fresh:
            marker = root / ".gitignore"
            try:
                # `x` rather than a plain write: the loser of a race must not truncate the winner's file.
                with open(marker, "x", encoding="utf-8") as fh:
                    fh.write(_STORE_GITIGNORE)
            except FileExistsError:
                pass
            except OSError as e:
                with suppress(OSError):
                    root.rmdir()
                raise SessionUnreadableError(
                    f"could not write the privacy marker at {marker}: {e}",
                    details={"path": str(marker)}) from e
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SessionUnreadableError(
                f"could not create {path}: {e}", details={"path": str(path)}) from e
        return path

    def no_session_message(self, ref: str, *, what: str = "session") -> str:
        """The one sentence for "there is no such session" — every CLI-facing site builds it here (#243).

        Three facts, and the two that were missing are the ones that end the trap. **Where Requivo
        looked**, because the way a session actually goes missing is a user running from a different
        directory — the plugin README calls it a failure with no visible symptom — and a root printed at
        the moment of the refusal is what makes that visible. **How to see what is really there**, so a
        typo'd slug is a step rather than a dead end. The third is the reference itself, which was the
        only one all five previous wordings carried.

        A method rather than a constant because the session root is workspace-dependent and must be
        read when the refusal happens, not at import — and, since #272, dependent on *which* store is
        asking, not only on the ambient process root.

        `what` exists for the one caller whose absence is genuinely wider: `_resolve_ref` accepts a
        *path* to a `model.json` as well as a slug, so "no session" would name half of what it looked
        for. Everything else takes the default.

        The word `canonical` is deliberately gone. It distinguished this layout from the retired `out/`
        one — a fact about the store's history that a user cannot act on, and it appeared only in the
        three sites reachable from none of the main verbs, so the jargon and the missing help arrived
        together.

        `display_token` for the same reason every other render of an untrusted string calls it (#40):
        the reference is raw argv, a newline in it ends the line, and everything after that point reads
        as a sentence Requivo is saying. **On every current CLI route it cannot fire**, because
        `validate_slug` refuses a control character first — it is here as the second line of defence
        invariant 14 asks for, since this function is public and an external consumer calls this layer
        rather than a careful surface. Pinned as such, against the builder, by
        `test_the_shared_builder_escapes_a_reference_it_could_be_handed_directly`; a test routed through
        a verb would have been green whether or not this call escaped anything.

        The whole set is pinned by `tests/test_session_not_found.py`, which sweeps the *verbs* rather
        than the sites, because the builder this replaced was itself correct and reached by nothing a
        user runs.
        """
        return (f"no {what} named {display_token(ref)} under {self.session_root()}. "
                "`requivo session list` shows the sessions in this workspace; a different --workspace "
                "(or REQUIVO_WORKSPACE) changes where Requivo looks.")

    def _no_session(self, slug: str) -> SessionNotFoundError:
        """The one refusal for "there is no such session", so the lock and the metadata read cannot drift
        into telling a caller two different stories about the same absence."""
        return SessionNotFoundError(self.no_session_message(slug), details={"slug": slug})

    def lock_path(self, slug: str) -> Path:
        """The write lock for `slug`: `<workspace>/.requivo/locks/<slug>.lock`.

        **Outside the session directory, which is the whole of #113's fix.** `lock_root()` carries why;
        the short version is that a lock inside a directory `session import --force` renames is a claim
        on an inode that every writer under it has already stopped agreeing with.

        Validated exactly as `canonical_dir` and `artifact_path` validate theirs, and for the same
        reason: the slug reaches here from `session_lock`, whose callers include the service layer and
        therefore, under invariant 14, an external consumer. The pattern already makes a separator or a
        dot segment unrepresentable; `is_contained` is the belt to that pair of braces, and it is the
        one shared statement of that rule rather than a fourth local one."""
        root = self.lock_root()
        slug = _slug_shape(slug)
        # Checked against the *session* root, never against `root` above (#372). A lock file is not
        # itself a session, and what decides whether #221's refusal still applies is whether a session
        # already claims this name -- not whether a `<slug>.lock` file happens to, which it never does on
        # a first lock. Without this, taking the read-consistency lock `session export` holds would be
        # the one thing standing between an already-on-disk reserved name and the data filed under it,
        # even though locking creates nothing under that name.
        #
        # `<slug>.lock` is itself a reserved-stem-shaped name on Windows -- `con.lock` matches the same
        # before-the-first-dot rule `validate_filename` enforces for artifact names (raised in review).
        # Not a live gap: the precondition for reaching this line at all is a session already occupying
        # `slug` on disk, and Windows's own `CreateDirectory` refuses to *materialize* a directory named
        # `con` in the first place -- independent of anything this file does, and true before #221 ever
        # shipped. So a reserved-named session cannot exist on a real Windows filesystem for this branch
        # to be reached from, which is also why the sibling tests that build one are POSIX-only.
        _refuse_new_reserved_slug(slug, self.session_root() / slug)
        p = root / (slug + ".lock")
        if not is_contained(p, root):
            raise InvalidSlugError(f"slug {slug!r} does not resolve to a lock file inside {root}",
                                   details={"slug": slug})
        return p

    @contextmanager
    def session_lock(self, slug: str) -> Iterator[None]:
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

        **The check before the open stays, and is deliberately not authoritative.** It buys one thing
        and decides nothing: a slug with no session refuses without leaving an empty lock file behind.
        A reserved Windows device name (`con`, `nul`, `lpt1`) never reaches this far when nothing already
        exists under it — `session_exists` resolves through `canonical_dir`, which refuses a genuinely
        *new* one exactly as before (#221, `test_reserved_windows_device_names_are_refused_as_slugs`).
        One that already exists on disk *does* reach this far, deliberately (#372): `session_exists`
        answers True for it, and `lock_path` applies the identical existing-session exception, so a verb
        like `session export` — which locks for read-consistency rather than to write anything new — can
        still take this lock for data that predates #221's refusal rather than being blocked by it. It
        can be wrong in exactly one direction — `_swap_in` holds this lock across two renames and
        `<root>/<slug>` does not exist for the microseconds between them, so a caller sampling that
        instant refuses `session_not_found` about a session that is merely being replaced. That window is
        the one the pre-#113 code already had at its `os.open`, and a refusal is the safe direction: this
        check can decline a lock, never grant one.

        Neither the lock file nor a session directory is ever removed here. Unlinking a lock file a
        concurrent process may be holding is legal on POSIX and silently breaks mutual exclusion — the
        repair #22 rejected, and the same reason `_swap_in` could not be written as a contents swap.

        **Legacy `.lock` files inside existing session directories are inert.** Nothing opens them now,
        `session export` already skips every dot-prefixed entry, and `check_session_dir` does not look
        for unexpected files. They cost a few empty bytes and are safe to delete.

        **Re-entrancy is keyed by root identity plus slug, not by slug alone** (#272) — see the class
        docstring for why: the ambient module-level wrapper builds a fresh `Store` per call, so keying
        on `id(self)` would break re-entrancy across two ambient calls of the same workspace, and keying
        on `slug` alone would wrongly treat two *different* workspaces sharing a slug name as one lock.
        """
        depths: dict[tuple[str, str], int] = getattr(_held_locks, "depths", None) or {}
        _held_locks.depths = depths
        key = self._lock_key(slug)
        if depths.get(key):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return

        if not self.session_exists(slug):     # cheap, non-authoritative — see above
            raise self._no_session(slug)
        p = self.lock_path(slug)
        # Outside the `try` on purpose (#320). That handler says "could not open the write lock", and
        # `ensure_store_dir` fails about the store root or the privacy marker — reporting one operation's
        # failure under the other's name sends the reader to the wrong file. It raises a structured error
        # of its own, so nothing is swallowed by moving it out.
        self.ensure_store_dir(p.parent)
        try:
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
            if not self.session_exists(slug):
                raise self._no_session(slug)
            depths[key] = 1
            yield
        finally:
            depths[key] = 0
            try:
                if acquired:
                    _release(fd)
            finally:
                os.close(fd)

    def canonical_dir(self, slug: str) -> Path:
        """The canonical session directory `<workspace>/.requivo/sessions/<slug>/`."""
        return _child_of(self.session_root(), slug)

    def legacy_dir(self, slug: str) -> Path:
        """The legacy `out/<slug>/` directory — read-only, and migrated only by an explicit
        `requivo session migrate`, never on a read or a first write (see `migrate_legacy`)."""
        return _child_of(self.output_root(), slug)

    def artifact_path(self, slug: str, filename: str) -> Path:
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
        deliberately cannot resolve, and it does *not* carry over here: `check_session_dir` puts every
        recorded filename through `validate_filename` and `is_contained`, and `session import` refuses
        the whole archive when either fails — reproduced, both for a traversal and for a merely wrong
        name. Read as covering both fields, this would claim a vector that is shut and quietly drop the
        one that is open.

        **Since #260 that is the whole of what a filename is pinned to when the artifact *type* is one
        this build does not know**, because there is then no `ARTIFACT_FILENAMES` value to pin it against
        — an unknown type is a note rather than a refusal, so `session import` accepts the entry. This
        paragraph said "pins every recorded filename to its `ARTIFACT_FILENAMES` value" and would have
        read as a stronger claim than the code makes. The claim that matters is unchanged and is the one
        stated above: the name is a bare file inside `artifacts/` or the archive is refused, whether or
        not anything here recognises the type it is filed under. `artifact_filename_mismatch` still
        refuses a *known* type stored under the wrong name.

        Coming through here also means such a name cannot forge a line in the terminal it is printed to:
        `_FILENAME_RE` is anchored at end-of-string and admits no line break (#40).

        A target that is not there is not an error here. `is_contained` does stat it — `exists()` is a
        stat — and answers True for what it cannot find rather than raising, so routing a display site
        through this does not turn a session with nothing generated into a refusal. Absence and refusal
        stay the two different answers `read_artifact_file` keeps them as."""
        d = self.canonical_dir(slug) / "artifacts"
        p = d / validate_filename(filename)
        if not is_contained(p, d):
            raise InvalidFilenameError(
                f"artifact filename {filename!r} does not resolve to a path inside {d}",
                details={"slug": slug, "filename": filename})
        return p

    def session_exists(self, slug: str) -> bool:
        return _probe(self.canonical_dir(slug) / "session.json", slug)

    def legacy_exists(self, slug: str) -> bool:
        return _probe(self.legacy_dir(slug) / "model.json", slug)

    def write_meta(self, slug: str, meta: SessionMeta) -> Path:
        d = self.canonical_dir(slug)
        self.ensure_store_dir(d)
        return _atomic_write(d / "session.json", meta.model_dump_json(indent=2))

    def read_meta(self, slug: str) -> SessionMeta:
        p = self.canonical_dir(slug) / "session.json"
        # Through `_probe`, not a bare `p.exists()` (#264): `Path.exists()` re-raises `EACCES`, and this
        # check used to sit outside the `try` below that wraps `OSError`, so a session.json the process
        # cannot stat escaped as a raw `PermissionError` instead of `SessionUnreadableError` -- the
        # identical unguarded probe #80 removed from `_scan_session_root` and #97 removed from
        # `session_exists`, a third time here.
        if not _probe(p, slug):
            raise self._no_session(slug)
        try:
            return migrate_session(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            raise SessionUnreadableError(f"session '{slug}' has an unreadable session.json: {e}",
                                         details={"slug": slug}) from e

    def create_session(self, slug: str, request: str, *, provider: str | None = None,
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
        d = self.canonical_dir(slug)
        self.ensure_store_dir(d.parent)
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

    def delete_session(self, slug: str) -> None:
        """Irreversibly remove a session: its directory and its lock file (#238).

        **Ordering, and why.** The directory removal is the write a concurrent writer has to
        serialise against (invariant 9), so it runs entirely inside `session_lock`'s own critical
        section: `session_lock` already refuses a missing slug with `session_not_found`, both before
        taking the lock (cheap, non-authoritative) and again just after (authoritative -- see its own
        docstring), so a writer that races this delete either finishes first and has its result
        removed, or blocks on the same lock and, once granted, meets that second check and refuses
        cleanly rather than writing into a directory this method had only partly torn down.
        `test_a_writer_racing_an_in_flight_delete_is_refused_rather_than_writing_into_a_half_removed_directory`
        and `test_delete_waits_for_a_concurrent_writer_then_removes_what_it_wrote` pin both halves.

        **The lock file is unlinked *after* the lock is released, not while still held.** Unlinking a
        file this same process still has open is legal on POSIX, but what a Windows handle opened by
        `os.open` with no explicit share-delete flag does to a self-referential unlink mid-hold is not
        something this store can verify without a Windows machine to observe it on -- so the safer
        ordering is: finish the locked work and fully release first, then remove the now-inert file
        as a second step. Nothing can race that second step in a way that matters: any caller reaching
        this slug after the lock is released and the directory is gone gets a clean refusal from
        `session_lock`'s own cheap existence check before it ever touches the lock file, so a leftover
        (or a `PermissionError` unlinking it, on a platform this store cannot exercise) is harmless
        best-effort residue rather than a correctness gap -- `create_session`'s rename is still the
        only real claim on a slug (invariant 11), and a stale lock file never contests it.
        """
        with self.session_lock(slug):
            shutil.rmtree(self.canonical_dir(slug))
        try:
            self.lock_path(slug).unlink(missing_ok=True)
        except OSError:
            pass  # best-effort cleanup; see the docstring above

    def save_revision(self, slug: str, model: EngineOutput, *, expected_revision: int | None = None,
                      provenance: dict | None = None) -> tuple[int, SessionMeta]:
        """Persist a new model revision: freeze revisions/NNNN-model.json, replace model.json with the
        same payload (the prior model is already frozen in an earlier revision file), record the
        revision's provenance, then bump current_revision + updated_at. Returns (new_revision,
        updated_meta). The order of those writes is a guarantee rather than a detail; see the comment on
        the two `_atomic_write` calls below.

        `expected_revision` is an optimistic-locking precondition: when given, the write fails with
        `RevisionConflictError` unless the session is still at that revision — so two updates racing from
        the same base can't both land silently. The single-user CLI omits it (last-writer-wins is fine
        locally); a concurrent Web service passes the revision the client read. `provenance` carries the
        surface-supplied fields (provider / model_name / surface / prompt_version) for the revision log.

        The precondition and every write it guards run under `session_lock`, because a check that is not
        held across the writes it authorises is not a precondition — two writers could both read revision
        N, both pass the check, and both write revision N+1."""
        with self.session_lock(slug):
            meta = self.read_meta(slug)  # raises SessionNotFoundError if the session isn't there
            if expected_revision is not None and meta.current_revision != expected_revision:
                raise RevisionConflictError(
                    f"session '{slug}' is at revision {meta.current_revision}, not the expected "
                    f"{expected_revision} — reload the current model and re-apply",
                    details={"slug": slug, "expected": expected_revision,
                             "actual": meta.current_revision})
            d = self.canonical_dir(slug)
            self.ensure_store_dir(d / "revisions")
            rev = meta.current_revision + 1
            payload = model.model_dump_json(indent=2)
            # Frozen revision file first, then model.json. Three writes and no transaction, so the order
            # decides what a crash between two of them leaves: reversed, a death here served every reader
            # content no revision records while session.json still named the previous one. Pinned by
            # `test_a_crash_after_the_first_payload_write_still_reads_as_the_recorded_revision` and, on
            # the revision 0 -> 1 arm that reports different codes,
            # `test_a_crash_in_the_very_first_apply_leaves_a_session_still_at_revision_zero`. The window
            # this does *not* close is pinned beside them by
            # `test_a_crash_after_both_payload_writes_is_still_reported_as_inconsistent`.
            _atomic_write(d / "revisions" / f"{rev:04d}-model.json", payload)
            _atomic_write(d / "model.json", payload)
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
                usage_input_tokens=prov.get("usage_input_tokens"),
                usage_output_tokens=prov.get("usage_output_tokens"),
                usage_cache_read_tokens=prov.get("usage_cache_read_tokens"),
                usage_cache_write_tokens=prov.get("usage_cache_write_tokens"),
                usage_rate_per_mtok=prov.get("usage_rate_per_mtok"),
                usage_priced_as_of=prov.get("usage_priced_as_of"),
            ))
            meta.current_revision = rev
            meta.updated_at = _now()
            self.write_meta(slug, meta)
            return rev, meta

    def load_session_model(self, slug: str) -> EngineOutput:
        """The current model of a canonical session."""
        p = self.canonical_dir(slug) / "model.json"
        if not p.exists():
            raise SessionNotFoundError(
                f"session '{slug}' has no model yet (apply a proposal first)", details={"slug": slug})
        return _read_model(p, slug=slug)

    def load_revision_model(self, slug: str, revision: int) -> EngineOutput:
        """A historical model revision — the basis for `impact` since a given point."""
        p = self.canonical_dir(slug) / "revisions" / f"{revision:04d}-model.json"
        if not p.exists():
            raise SessionNotFoundError(
                f"session '{slug}' has no revision {revision}", details={"slug": slug, "revision": revision})
        return _read_model(p, slug=slug, revision=revision)

    def session_request(self, slug: str) -> str:
        p = self.canonical_dir(slug) / "request.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def save_session_artifact(self, slug: str, artifact_type: str, filename: str, content: str,
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
        path = self.artifact_path(slug, filename)   # refuse a bad target before taking the lock
        with self.session_lock(slug):
            meta = self.read_meta(slug)
            if not 1 <= source_revision <= meta.current_revision:
                raise ArtifactRevisionOutOfRangeError(
                    f"cannot record {artifact_type!r} against revision {source_revision}: session '{slug}' "
                    f"has revisions 1..{meta.current_revision or 0}",
                    details={"slug": slug, "source_revision": source_revision,
                             "current_revision": meta.current_revision})
            self.ensure_store_dir(path.parent)
            _atomic_write(path, content)
            st = ArtifactStatus(revision=source_revision, filename=filename, updated_at=_now(), stale=stale)
            meta.artifact_status[artifact_type] = st
            meta.updated_at = _now()
            self.write_meta(slug, meta)
            return st

    def write_artifact_file(self, slug: str, filename: str, content: str) -> Path:
        """Write a raw file into a session's artifacts/ directory (no status tracking) — for the neutral
        epic exports (epic.json / epic.github.json / …) that are extra views of one generated artifact.

        Both halves of the target go through `artifact_path`: the mutating route validated its slug and
        not the filename beside it, so `write_artifact_file(slug, '../../../x.md', …)` wrote outside the
        session entirely."""
        path = self.artifact_path(slug, filename)
        self.ensure_store_dir(path.parent)
        return _atomic_write(path, content)

    def read_artifact_file(self, slug: str, filename: str) -> Optional[str]:
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
        p = self.artifact_path(slug, filename)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def _scan_session_root(self) -> tuple[list[str], list[Path], list[UnexaminableEntry]]:
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
        root = self.session_root()
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

    def list_session_slugs(self) -> list[str]:
        """Slugs of all canonical sessions, sorted — the backbone of `session list`.

        **Names known to be sessions, and this contract does not widen.** `doctor`, `session verify` and
        every read path reason over what comes back here, so an entry the partition could not examine is
        deliberately not in it — see `list_unexaminable_entries`, which is where it goes instead."""
        return self._scan_session_root()[0]

    def scan_session_root(self) -> tuple[list[str], list[NonSessionEntry], list[UnexaminableEntry]]:
        """All three parts of the session root from **one** listing — and the only way to reach the
        second one, since #300 (see below).

        `list_session_slugs` and `list_unexaminable_entries` each scan on their own, which is right when
        only one question is being asked and wrong when more than one is. `doctor` asks all three, and
        two scans are two instants: a `session.json` appearing between them puts a name in *neither*
        answer, which is the invisible state #67 is about, reintroduced by the report meant to close it;
        one disappearing puts it in both. Transient and diagnostic-only, and still not something to
        leave in the one verb whose job is to say whether anything is wrong. Found by review.

        **The second part is what nothing could see before #67**, and the reason it is worth returning
        at all is not in this module's output — it is at the next `create_session` on that name.
        `list_session_slugs` skips such an entry for want of a `session.json`, so `doctor` and
        `session verify` never reach one; `check_session` answers about a directory it is handed, which
        nobody can hand it a name for. The rename that *is* the claim on a slug (invariant 11) then
        loses to a directory that is already there, and `SessionService` falls through to its
        `<slug>-<identity hash>` candidate — so the user gets a session under a name they did not ask
        for, with nothing anywhere explaining why the one they asked for was unavailable.

        **A report, not a repair.** This reads; it never deletes, moves or rewrites. #22 stopped
        `session_lock` producing these, and clearing one on sight would be the same mistake pointing the
        other way: unlinking a `.lock` a concurrent process is holding is legal on POSIX and silently
        breaks mutual exclusion, and nothing in the directory tells a ghost from a half-extracted
        archive.

        Second of three since #80, not the other half of two: an entry whose examination *raised* is
        neither a session nor established to be one of these, and is the third part instead. Folding it
        into the second would hide it from `session list` for want of a `session.json` nobody could look
        for, which is that part's own defect class.

        It lives in Core beside `list_session_slugs` because that function owns the store layout and the
        answers come out of one predicate. Core reading a directory is not a boundary crossing:
        invariant 7 forbids importing a provider and touching argv, the streams, the environment and
        process exit — not IO, which this module is made of.

        The describe step is here rather than in `_scan_session_root` so that `list_session_slugs` — on
        every one of its call paths, `session list` included — keeps paying nothing for it: a stray
        directory holding ten thousand files is one `iterdir` this function makes and that one does not.
        The third part carries no describe step at all: whatever we would ask it, we have just failed to
        ask it once."""
        slugs, others, unexaminable = self._scan_session_root()
        return slugs, [_describe_non_session(p) for p in others], unexaminable

    def list_unexaminable_entries(self) -> list[UnexaminableEntry]:
        """Names under the session root whose examination raised — the partition's third answer (#80).

        Neither `list_session_slugs` nor `scan_session_root`'s second part returns one, and that is the
        point: calling it a session claims what the failed probe did not establish, and calling it a
        non-session hides it from `session list`, which is #67's defect one function along. It reaches a
        surface as a fact of its own — a degraded row on `session list`, its own line under `doctor`'s
        sessions check.

        **A report, not a repair**, on the same terms `scan_session_root` states for the second part:
        Requivo reads a workspace and does not chmod anything in it. What is here is a name and the
        reason the probe failed.

        A caller that wants the other parts too should take `scan_session_root()` instead: this one scans
        on its own, and two scans are two instants."""
        return self.scan_session_root()[2]

    def scan_lock_root(self) -> tuple[list[str], list[str], list[UnexaminableEntry]]:
        """Partition `lock_root()` three ways, for `doctor`'s lock-residue check (#180): the slugs a
        `<slug>.lock` file names, the entries that are neither that nor a recognised
        `<slug>.discovering` guard file (#209, #391), and the entries whose examination raised. The
        session-root sibling of `_scan_session_root`, one root over.

        **Two regular-file shapes are what this store writes here, and both are recognised.**
        `lock_path` joins `lock_root()` with a validated `<slug>.lock` -- pattern and length always,
        and the reserved-device-name refusal only when nothing already occupies the matching *session*
        name (`_refuse_new_reserved_slug`, #372; see `lock_path`'s own docstring for why that check is
        against `session_root()`, not this root). `services.discovery._discovery_guard_path` writes the
        second shape, `<slug>.discovering` -- deliberately never unlinked (#209), on the identical
        POSIX reasoning that leaves a deleted session's `.lock` file behind, so it outlives every
        discovery it ever served.

        **This function was written the release before the second shape shipped, and #209 never came
        back to teach it** (#391): every `.discovering` file read as `unexpected`, reported as "not a
        lock file Requivo recognises" about a file this store's own code had just written, on the very
        first ordinary discovery a workspace ever ran. A well-formed instance of *either* shape -- a
        regular file, not a symlink, with a stem either writer could have been given (`_is_lock_stem`,
        shape alone since #409) -- is recognised now and excluded from `unexpected`. Anything else
        under this root — a stray file with a different name, a directory, a symlink at a `.lock` or
        `.discovering` name, a stem neither writer could have been given — is not a shape either writer
        ever produces, and is still reported exactly as before.
        Not followed if it is a symlink, on the same terms as `_describe_non_session`: reporting a
        symlink's target would read another file into a report about this workspace.

        **The stem question is `_is_lock_stem`'s, not `validate_slug`'s, and it is shape alone** (#401,
        corrected by #409). It was `is_slug` for a release, which is `validate_slug`'s unconditional
        creation-time refusal, and reported a reserved-name session's own lock and guard files as
        residue nobody recognises. It was then the read-time rule (`_slug_shape` plus
        `_refuse_new_reserved_slug` against `session_root()`) for a release, which fixed that and broke
        the opposite case: a lock file's classification changed when the session it was written for was
        later deleted, because that rule reads the *session* root, a resource this function's own answer
        must not depend on (see the paragraph below). `_is_lock_stem` carries the full argument now.

        **What a matching slug means is left to the caller, deliberately.** This function answers only
        *is there a `<slug>.lock` file*, never *is `slug` still a session* — that needs the session
        root's own listing, a second read a moment apart, and conflating the two here would make this
        function's own answer depend on an argument it does not take. `doctor._lock_health` is where the
        two lists meet.

        Three outcomes on each entry, because the same predicate that decides *session or not* in
        `_scan_session_root` can fail here too: `p.is_symlink()` / `p.is_file()` raise on the errnos
        `Path.exists()` does not swallow (EACCES chief among them), and a name that failed that probe is
        neither a lock nor confirmed to be something else — it lands in `unexaminable`, on the same
        reasoning `_scan_session_root` gives for its own third bucket (#80).

        **That bucket had a second source from #401 to #409, and it is gone by design.** `_is_lock_stem`
        used to stat `session_root() / stem` through `_probe` for a reserved stem, which could raise on
        EACCES -- so an entry could reach `unexaminable` with the file-type probe above it having
        answered perfectly well. #409 removed that stat entirely (`_is_lock_stem` is shape alone now),
        so this bucket's only source is the file-type probe immediately above it in the loop. Pinned by
        `test_a_reserved_lock_stem_no_longer_probes_the_session_root`, which replaced the test that used
        to pin the removed source.

        A root that does not exist is an empty lock directory and returns nothing, matching
        `_scan_session_root`'s own empty-workspace answer. A root that cannot be *listed* is not the same
        claim and is left to raise, for the caller to report as `readable: False` rather than as a clean
        scan of nothing — the whole-root-versus-per-entry distinction `_scan_session_root`'s docstring
        already makes."""
        root = self.lock_root()
        if not root.exists():
            return [], [], []
        lock_slugs: list[str] = []
        unexpected: list[str] = []
        unexaminable: list[UnexaminableEntry] = []
        for p in sorted(root.iterdir(), key=lambda p: p.name):
            lock_slug = p.name[: -len(".lock")] if p.name.endswith(".lock") else None
            # `_discovery_guard_path` (services/discovery.py, #209) writes this second shape and never
            # unlinks it -- recognised and excluded from `unexpected`, not folded into `lock_slugs`:
            # it is not a `<slug>.lock` file and answers a different question (#391).
            guard_slug = p.name[: -len(".discovering")] if p.name.endswith(".discovering") else None
            try:
                is_ordinary_file = p.is_file() and not p.is_symlink()
                # `_is_lock_stem`, not `is_slug` (#401), and shape alone -- not a read of the session
                # root (#409). This is a classification, not a creation: it asks whether a writer
                # *here* could have produced this file, which is a fact about the file's own name and
                # nothing else. Asked the creation-time question, a reserved-name session's own lock and
                # guard files read as residue nobody recognises (#391's defect one predicate over).
                # Asked the read-time question against the *session* root instead, the answer for a
                # fixed file flipped when that unrelated directory was later deleted (#409).
                if is_ordinary_file and lock_slug and _is_lock_stem(lock_slug):
                    lock_slugs.append(lock_slug)
                    continue
                if is_ordinary_file and guard_slug and _is_lock_stem(guard_slug):
                    continue
            except Exception as e:  # noqa: BLE001 - the third outcome, not a failure of the listing
                unexaminable.append(UnexaminableEntry(p.name, str(e)))
                continue
            unexpected.append(p.name)
        return lock_slugs, unexpected, unexaminable

    def migrate_legacy(self, slug: str) -> SessionMeta:
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

        src = self.legacy_dir(slug)
        if not (src / "model.json").exists():
            raise SessionNotFoundError(f"no legacy session '{slug}' under {self.output_root()}",
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
        #
        # Through `_read_model` like the other three, and it is the fourth door rather than an extra:
        # #204 named `load_model` and `load_revision_model`, and this site reads a model exactly the same
        # way. Wrapping three of four would be the defect the helper exists to prevent, one file later.
        # `slug=` is deliberately not passed: the legacy `out/<slug>/` layout has no `revisions/`, so the
        # recovery remedy would be a sentence about a directory that is not there. The path names the
        # session by itself.
        model = _read_model(src / "model.json")

        if request:
            req_hash = content_hash(request)
        else:
            # Fall back to the legacy session.json's hash, normalising a bare hex digest to "sha256:…".
            legacy_hash = str(old.get("request_sha256", ""))
            req_hash = legacy_hash if legacy_hash.startswith("sha256:") or not legacy_hash else "sha256:" + legacy_hash

        # The claim. Raises SessionExistsError if a canonical session already occupies the slug.
        self.create_session(slug, request, provider=old.get("provider"), model_name=old.get("model_name"),
                            context_cards=old.get("context_cards"))

        with self.session_lock(slug):
            # The three fields `create_session` cannot know, because they belong to the *legacy* session:
            # its original creation date, the request hash a migration may have to recover from the old
            # metadata when no request file survived, and an id derived from the slug so re-reading a
            # migrated session finds the identity a previous migration of it would have given.
            meta = self.read_meta(slug)
            meta.session_id = uuid.uuid5(uuid.NAMESPACE_URL, f"requivo:legacy:{slug}").hex
            meta.created_at = old.get("created_at", meta.created_at)
            meta.request_hash = req_hash
            self.write_meta(slug, meta)

            rev, _ = self.save_revision(slug, model, expected_revision=0)  # existing model → revision 1

            filename_to_type = {fn: t for t, fn in ARTIFACT_FILES.items() if fn}
            for fn, atype in filename_to_type.items():
                legacy_file = src / fn
                if legacy_file.exists():
                    content = legacy_file.read_text(encoding="utf-8")
                    self.save_session_artifact(slug, atype, fn, content, source_revision=rev)
            return self.read_meta(slug)


def _default_store() -> Store:
    """A fresh `Store` resolved from the ambient workspace root, built again on every call. This is
    what keeps every module-level function below behaving byte-identically to before this class
    existed: `workspace_root()` reads `REQUIVO_WORKSPACE`/cwd afresh each time, so a CLI `--workspace`
    env mutation mid-process (`cli.py`) is picked up by the very next call, exactly as it was when
    these functions read the root directly. See `Store`'s own docstring and
    `docs/cloud-boundary.md` (§3.1)."""
    return Store(workspace_root())


# Ambient-default wrappers over `Store`'s own root methods, kept for the same reason every other
# function above is: before #272 this module imported these four names straight from `paths.py`
# (`from requivo.paths import lock_root, output_root, session_root, store_root`), which made them
# reachable as `persistence.session_root()` etc. -- a convenience the whole test suite relies on
# (`store.session_root()`, `store.lock_root()`, ...). Three of these four -- `store_root`,
# `session_root`, `lock_root` (and `debug_root` beside them) -- are `Store` computing the identical
# path from an explicit root instead of reading `paths.py` ambiently, so a bare re-import would
# silently diverge from `Store`'s own math the moment one of the two was edited without the other;
# wrapping `Store` via `_default_store()`, like every other function in this file, keeps there being
# exactly one definition of what these paths are. `output_root` is the one exception -- see
# `Store.output_root`'s own docstring for why it deliberately reads `paths.output_root()` rather
# than `self.root`, on every `Store` alike.
def store_root() -> Path:
    """Ambient-default wrapper (#272) -- see `Store.store_root`."""
    return _default_store().store_root()


def session_root() -> Path:
    """Ambient-default wrapper (#272) -- see `Store.session_root`."""
    return _default_store().session_root()


def lock_root() -> Path:
    """Ambient-default wrapper (#272) -- see `Store.lock_root`."""
    return _default_store().lock_root()


def debug_root() -> Path:
    """Ambient-default wrapper (#272) -- see `Store.debug_root`."""
    return _default_store().debug_root()


def output_root() -> Path:
    """Ambient-default wrapper (#272) -- see `Store.output_root`."""
    return _default_store().output_root()


# What `.requivo/.gitignore` is written with. `*` ignores the directory's whole contents including
# the ignore file itself -- the self-ignoring pattern `uv` writes into `.venv/` and terraform into
# `.terraform/`, chosen so nothing has to be added to the *user's* `.gitignore`, which is a file
# Requivo has no business editing.
_STORE_GITIGNORE = """\
# Written by Requivo the first time this directory was created, and never rewritten.
# Sessions hold your request text verbatim -- for most users that is client-confidential
# material sitting inside a git repository. Delete this file to commit sessions deliberately;
# it will not come back. To share one session instead, use `requivo session export`.
*
"""


def ensure_store_dir(path: Path) -> Path:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh, from
    `paths.workspace_root()`, on every call. Full contract on `Store.ensure_store_dir`, which this
    delegates to; see `Store`'s own docstring and `docs/cloud-boundary.md` (§3.1) for why the root
    is resolved this way rather than read off `self`."""
    return _default_store().ensure_store_dir(path)


def no_session_message(ref: str, *, what: str = "session") -> str:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store.no_session_message`, which this delegates to."""
    return _default_store().no_session_message(ref, what=what)


def lock_path(slug: str) -> Path:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store.lock_path`, which this delegates to."""
    return _default_store().lock_path(slug)


@contextmanager
def session_lock(slug: str) -> Iterator[None]:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh, from
    `paths.workspace_root()`, on every call. Full contract, including the re-entrancy keying, on
    `Store.session_lock`, which this delegates to."""
    with _default_store().session_lock(slug):
        yield


# A slug becomes a directory name, so it is bounded by what the filesystem accepts (~255 bytes on ext4
# and APFS, and the whole *path* on Windows). 80 leaves generous room for the session subtree beneath
# it. `derive_slug()` stays under the smaller base ceiling so a uniqueness suffix still fits inside the cap.
MAX_SLUG_LENGTH = 80
_SLUG_BASE_LENGTH = 64


# Latin letters NFKD cannot decompose, spelled out before the fold runs. NFKD splits a letter into a
# base plus a combining mark and the ASCII fold then drops the mark; a letter carrying no mark
# decomposes to *itself*, so the fold has nothing to do but delete it — 'Straßenverkehr' arrived as
# `stra-enverkehr`, which is the same mid-word mangling as `syst-me` one letter along. Lower-case
# only, because the fold runs after `.lower()`. Pinned by
# `test_folding_expands_a_latin_letter_that_carries_no_combining_mark`.
_LATIN_EXPANSIONS = str.maketrans({
    "ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i",
})

# Function words dropped before the five tokens are taken (#245). English plus the three other Latin
# languages this project's users actually write requests in — the slug is a handle in whatever
# language the request arrived in, and folding accents without also dropping `nous`/`un`/`des` just
# moves the junk one character along.
#
# Two rules kept this list from becoming a general-purpose stoplist. A word is in it only if it is a
# function word in *some* in-scope language and not a content word in *any* of them — which is why
# `son`, `hay`, `sin`, `man`, `war`, `bin` and `hat` are deliberately absent despite being ordinary
# function words in French, Spanish or German. And nothing is here for being *common*: `system`,
# `data`, `report` and `user` open a great many requests and are exactly what the handle should say.
#
# The exclusion rule is prose, so it has a guard rather than a promise:
# `test_the_stopword_list_keeps_the_words_its_own_comment_promises_to_keep` asserts those seven are
# absent. `son` was in the Spanish half anyway, two lines under the paragraph saying it was not.
#
# Two accepted costs, stated rather than discovered. **`die`** is the German article and an English
# verb, so "the service must not die quietly" loses a word it would have liked; the German article is
# far the more frequent of the two in a request opening, so the trade is taken knowingly. And
# **matching is case-folded ASCII, so a short function word collides with an acronym** — `er` eats
# the ER in "an ER diagram", and `im`, `am`, `us`, `et`, `est` and `par` are the same shape. That is
# the real residual limit of packing four languages into one flat set, and it is not fixable by
# pruning: dropping `er` costs German requests far more often than "ER diagram" costs English ones.
# The fallback below is what keeps it survivable — a request eaten down to fewer than two survivors
# uses its words as typed — and an explicit `--slug` is the way past it when it matters.
_SLUG_STOPWORDS = frozenset("""
    a an and are as at be been being but by can could d did do does for from had has have i if in
    into is it its like ll m me my need needed needs of on or our ours ourselves please re s should
    so some t that the their them then there these they this those to us ve want wanted wants was
    way ways we were what when where which who whose will with would you your
    au aux avec avoir avons besoin ce ces cet cette dans de des du elle elles en est et etaient
    etait ete etre faut ils je la le les leur leurs ne nos notre nous ou par pas plus pour qui quoi
    sa se ses sommes sont sur tu un une vos votre vous y aimerions aimerait souhaitons souhaiterions
    voudrais voudrions voulons
    al como con del el ella ellos es esta estas este esto estos la las lo los mi necesita necesitamos
    necesito nuestra nuestro para podemos podria podriamos por que queremos quiero se ser su sus
    tiene tenemos un una unas unos deberiamos
    aber alle als am auch auf aus bei benotigen benotigt brauche brauchen braucht das dass dem den
    der des die dies diese ein eine einem einen einer eines er es fur haben ich ihr ihre im ist kein
    keine mit mochte mochten nach nicht oder sein sich sie sind uber um und von vor wenn wie wir
    wollen wurde wurden zu zum zur
""".split())


def derive_slug(text: str) -> str:
    """Derive a session directory name from arbitrary text — the one producer of the canonical shape.

    Public because it is consumed outside this module: `SessionService.slug_hint` is the surface's
    route to it, and `validate_slug` below is written against exactly what this emits.

    Three steps, and the order between the first two is load-bearing (#245). **Fold, then filter,
    then take five.** The slug is the handle a user retypes into `answer`, `status`, `brief` and
    `prd`, and taking five tokens verbatim off the front of a request produced handles that named
    the greeting rather than the subject — `we-need-a-way-to`, from "We need a way to track vendor
    invoices" — so two unrelated requests differed only by the collision hash. Filtering first and
    folding after would leave `syst`/`me` in the token stream as two words neither list can match.

    Below two survivors the *unfiltered* words are used. An all-function-word request is a real
    shape ("We need it"), and an empty token list falls through to `discovery`, which is the
    indistinguishable-handle problem this function exists to reduce, reintroduced by its own fix.

    **The residual limit, documented because it is not fixed here:** the ASCII fold romanizes Latin
    scripts and deletes everything else, so a Japanese or Cyrillic request still leaves no tokens and
    still lands on `discovery`, then `discovery-<hash>`. Transliteration needs a dependency this
    package does not carry. Pinned by
    `test_a_non_latin_request_still_derives_the_documented_discovery_fallback`.

    Nothing re-derives a slug for a session that already exists — `slug_hint` is reached only from
    `create_session` and from `discover`'s filename hint — so a session on disk keeps the name it was
    created with. What *did* change is idempotent re-discovery: re-running the same request under a
    newer Requivo derives a different base and creates a second session. `docs/compatibility.md`
    carries that, where the other "two versions, one workspace" promises live.

    Emits the same alphabet as before (`[a-z0-9-]`), so `validate_slug`, `_SLUG_RE` and every
    existing session stay valid.
    """
    folded = unicodedata.normalize(
        "NFKD", text.lower().translate(_LATIN_EXPANSIONS)).encode("ascii", "ignore").decode("ascii")
    tokens = re.findall(r"[a-z0-9]+", folded)
    content = [w for w in tokens if w not in _SLUG_STOPWORDS]
    words = (content if len(content) >= 2 else tokens)[:5]
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
    return _read_model(path)


def _read_model(path: Path, *, slug: Optional[str] = None, revision: Optional[int] = None) -> EngineOutput:
    """Read and validate a persisted model, turning every way that can fail into one structured error.

    One helper rather than three call sites, and that is the point rather than tidiness: `load_model`,
    `load_session_model` and `load_revision_model` each read a model the same way, so a guard added at
    two of the three is a guard the third quietly does without -- and which of the three a given verb
    reaches is not something a reader can see from the verb. `status`, `impact` and `model show` came
    in by two different doors.

    What was there before was the bare `model_validate_json`, which meant a truncated `model.json`
    reached the operator as a raw pydantic traceback from three CLI verbs and a generic
    "Something went wrong on the server" 500 from the web session page -- `ValidationError` is not a
    `RequivoError`, so it sailed past the handler that already had a vocabulary for a malformed
    session (#204). The remedy was on disk the whole time, in `revisions/`, and nothing said so.

    `OSError` is caught alongside the parse failures because "the file is there but unreadable" is
    the same fact about the store as "the file is there but unparseable"; a missing file is decided
    by the callers above, which raise `SessionNotFoundError` instead, because that is a different
    situation with a different remedy. Pinned by
    `test_a_corrupt_model_is_a_structured_error_from_every_door`.
    """
    try:
        return PersistedEngineOutput.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError) as e:
        details: dict = {"path": str(path)}
        if slug is not None:
            details["slug"] = slug
        if revision is not None:
            details["revision"] = revision
        what = f"revision {revision} of session '{slug}'" if revision is not None else (
            f"the model of session '{slug}'" if slug is not None else "the model file")
        remedy = (
            f" Run `requivo session verify {slug}` for the full picture; the session's `revisions/` "
            "directory holds every model that was applied, so an earlier one can be recovered from "
            "there." if slug is not None else ""
        )
        raise ModelUnreadableError(
            f"Could not read {what}: {path} is truncated, mis-encoded, or not a valid model "
            f"({type(e).__name__}).{remedy}",
            details=details,
        ) from e


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
    # Token/rate provenance for a provider-backed apply (#292) — absent for a deterministic apply
    # (session import, a hand-authored `model apply`, a Claude Code turn, which spends no API tokens)
    # and for any revision written before this field existed. Never zero-filled: invariant 6 says
    # provenance is real or absent, and a revision that genuinely spent 0 tokens does not exist.
    usage_input_tokens: Optional[int] = None
    usage_output_tokens: Optional[int] = None
    usage_cache_read_tokens: Optional[int] = None
    usage_cache_write_tokens: Optional[int] = None
    # The rate this revision's calls were actually billed at, `(input, output)` USD per million
    # tokens — stamped rather than looked up again at render time, so a later price-table edit
    # cannot retroactively change what an old revision is reported to have cost (`usage.py`'s own
    # "cost is arithmetic here and nowhere else"). `None` when the calls behind this revision did not
    # all agree on one rate — a genuine disagreement is refused rather than guessed at.
    usage_rate_per_mtok: Optional[tuple[float, float]] = None
    usage_priced_as_of: Optional[str] = None   # the rate table's own date, alongside the rate itself


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

# Windows refuses to create a file or directory named one of these, case-insensitively, whether bare
# or with any extension (`CON`, `con.txt` and `con.tar.gz` are all reserved -- the OS matches on the
# component *before the first dot*, not the whole name). `_SLUG_RE` and `_FILENAME_RE` both admit
# them, so without this a session slugged 'con' is fully valid on macOS/Linux, exports fine, and then
# cannot be materialized by `session import` on a colleague's Windows machine -- a portability hole
# the session format's own promise never mentions (#221).
#
# Refused on *every* platform, not only Windows: refusing only there would still let a POSIX user
# create an archive Windows can never open, which is the defect this closes rather than relocates.
# The cost is a POSIX user losing a legal name they will almost never want; the alternative is a
# session that is portable everywhere except where compatibility.md promises it is.
#
# `com0`/`lpt0` and a bare `com`/`lpt`/`console` etc. are deliberately absent -- only `com1`-`com9`
# and `lpt1`-`lpt9` are reserved devices, and a check wider than the real set is the kind of guard
# that refuses a name nobody needed refused.
#
# This refuses *creation* -- a name nothing yet occupies. A session already on disk under a reserved
# name (from before this shipped, or from a platform that never refused it) is data rather than a
# request to make anything, and reading it is a narrower, conditional exception -- see
# `_refuse_new_reserved_slug` (#372).
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)


def _reserved_stem(name: str) -> bool:
    """Is the component of `name` before its first dot a Windows reserved device name?

    Shared by `validate_slug` (whose "stem" is the whole slug -- a slug never carries a dot) and
    `validate_filename` (whose stem is genuinely the part before the first `.`, so `con.tar.gz` is
    caught on `con` and not on `con.tar`)."""
    stem = name.split(".", 1)[0]
    return stem.lower() in _RESERVED_DEVICE_NAMES


def _raise_reserved_slug(slug: str) -> None:
    """The one wording for "this slug is a reserved Windows device name" -- shared by `validate_slug`
    (which raises it unconditionally) and `_refuse_new_reserved_slug` (which raises it only when
    nothing already claims the name, #372), so the two paths cannot drift into two different
    sentences for what is, from the caller's side, the identical refusal."""
    raise InvalidSlugError(
        f"invalid session slug {slug!r}: {slug.lower()!r} is a reserved Windows device name and "
        "cannot be created as a directory there",
        details={"slug": slug})


def _slug_shape(slug: str) -> str:
    """Pattern and length -- the parts of slug validity that hold regardless of what is already on
    disk. `validate_slug` layers the reserved-device-name refusal on top of this unconditionally;
    `_child_of` and `lock_path` layer a *conditional* version of it instead (#372, see
    `_refuse_new_reserved_slug`)."""
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


def _refuse_new_reserved_slug(slug: str, existing_check: Path) -> None:
    """Refuse a reserved Windows device name (#221) unless something already occupies
    `existing_check` -- the creation/read split #372 draws. `canonical_dir` reaches this through
    `_child_of` with `existing_check` set to the very directory a caller is naming, so a genuinely
    *new* reserved slug is refused exactly as strictly as before: nothing is there yet, `_probe`
    answers False, and the refusal fires -- #221's guarantee that a fresh 'con' directory is never
    materialized, on any platform, is unweakened.

    What changes is a name a session already occupies on disk: created before #221 shipped, carried
    over from a platform that never refused it, or simply read again on the machine that made it. That
    is data, not a request to create anything, and letting the read through is what stops it being
    stranded behind the very guard meant to keep it *portable* -- exactly what #221's own changelog
    and `docs/compatibility.md` already claimed was the behaviour (#372).

    Routed through `_probe` rather than a bare `.exists()` so the third answer stays a third answer:
    a stat this cannot make (`EACCES`) surfaces as `SessionUnreadableError` -- the real problem --
    instead of this function silently picking a side of a question nobody could actually decide."""
    if _reserved_stem(slug) and not _probe(existing_check, slug):
        _raise_reserved_slug(slug)


def validate_slug(slug: str) -> str:
    """Return `slug` if it is a safe session identifier, else raise `InvalidSlugError`. Lives in Core
    so every surface (CLI, provider, a future web service) inherits the same directory-traversal guard,
    not just FastAPI. Belt-and-suspenders: callers additionally confirm the resolved path stays under
    the root, but the pattern alone already makes a separator or dot segment unrepresentable.

    **The reserved-device-name refusal here is unconditional, on purpose** (#372). This is the
    strict, creation-time check: a caller who *names* a slug directly -- an explicit `--slug`, a web
    form field, the slug `session import` derives from an archive's own top-level directory -- is
    asking to create or address one deliberately, and gets the same refusal whether or not anything
    already exists on disk under that name. `_child_of` (via `canonical_dir`) and `lock_path` are
    where an *existing* session earns a narrower, read-only exception instead; see
    `_refuse_new_reserved_slug`. Widening this function would also widen every one of those creation
    paths, which is exactly what must not happen."""
    slug = _slug_shape(slug)
    # A slug never carries a dot (the pattern above forbids it), so this is a whole-slug check --
    # see `_reserved_stem` and #221.
    if _reserved_stem(slug):
        _raise_reserved_slug(slug)
    return slug


def is_slug(name: str) -> bool:
    """Whether `name` is a slug that could be *created* right now — the same question `validate_slug`
    answers, as a predicate: the unconditional, creation-time form, which refuses a reserved
    Windows device name whether or not anything already occupies it.

    Deliberately implemented by *calling* it rather than by re-testing `_SLUG_RE`. Validity is the
    pattern **and** the length, and a second statement of that drifts: an earlier `slug_shaped` was
    written against the pattern alone and marked an 81-character kebab-case directory as a name a
    session would silently lose, when `canonical_dir` refuses such a name outright and loudly. One
    rule, one place, found by review.

    **Has no caller in this codebase as of #408, and that is correct rather than dead weight.**
    `NonSessionEntry.slug_shaped` used to be this function, on the reasoning that its one caller was
    asking the creation-time question -- and it was not: the caller is describing an entry that
    already exists on disk, so the question is whether *that* directory's rename would collide, which
    is `_shape_only` (#408, see `_describe_non_session`), not this. `is_slug` stays as the creation-
    time predicate `validate_slug` is missing a bool form of -- the same relationship `_shape_only`
    has to `_slug_shape` -- for a caller that genuinely does ask about creating a fresh name, the way
    `_is_lock_stem` (#401) and `_describe_non_session` (#408) both learned the hard way that they do
    not."""
    try:
        validate_slug(name)
    except InvalidSlugError:
        return False
    return True


def _shape_only(name: str) -> bool:
    """Whether `name` matches `_slug_shape` -- pattern and length -- with no reserved-device-name
    question asked at all, as a bool.

    The read-time predicate for a caller that already knows, by construction, that something
    occupies the path it would ask `_refuse_new_reserved_slug` about -- so that conditional check
    could only ever answer "does not refuse" and asking it anyway would be a filesystem read with no
    possible other outcome. `_is_lock_stem` (#409) and `_describe_non_session`'s own `slug_shaped`
    (#408) are both this now, one for a lock-root entry classifying itself, one for a session-root
    entry doing the same -- see each for why its own "something occupies the path" holds.

    Not `_slug_shape` bare, which raises rather than returning a bool -- a caller wanting a
    predicate wants this, the same relationship `is_slug` has to `validate_slug`."""
    try:
        _slug_shape(name)
    except InvalidSlugError:
        return False
    return True


def _is_lock_stem(stem: str) -> bool:
    """Whether a `<stem>.lock` or `<stem>.discovering` under `lock_root()` is one this store could
    have written -- the **stem** half of what `lock_path` and
    `services.discovery._discovery_guard_path` each validate before joining their own suffix.

    **Shape alone, since #409 -- not `_slug_shape` plus a read of `session_root()`, which is what
    this was from #401 until #409 corrected it.** The reserved-device-name half of that read-time
    rule (`_refuse_new_reserved_slug`) asks whether a *session* currently occupies
    `session_root() / stem` -- a different resource, in a different root, from the lock-root entry
    being classified. `session_lock` and `_discovery_guard_path` can only ever produce a reserved-
    stem lock file for a session that existed *at write time* (#372's conditional refusal), so the
    file's provenance is fixed the moment it is written. Asking the read-time question anyway made
    the classification of a fixed fact depend on whether that *other* directory still exists *now*
    -- so `nul.lock`, written while a `nul` session was open, read as "not a lock file Requivo
    recognises" the moment that session was deleted, exactly invariant 17's shape: a verdict
    decided by a resource the answer does not name. `scan_lock_root`'s own docstring already
    forbade this ("never *is `slug` still a session*"); this function had come to violate it.
    Whether a session still matches a recognised stem is `_lock_health`'s question
    (`locks.unmatched`), asked separately and a moment apart -- never folded back in here. Pinned by
    `test_a_reserved_lock_stems_classification_survives_the_session_being_deleted` and the renamed
    `test_a_lock_file_for_a_reserved_name_with_no_session_on_disk_is_recognised_not_residue`, which
    used to pin the opposite answer on purpose (see its own docstring for why that assumption did
    not hold).

    **Deliberately *not* `lock_path(stem)` in a `try`, which is what this was first written as**
    (found in review of #401, before it shipped). That reads as the tidier "one rule, one place",
    and it imports a third check that is about a *path* rather than about a stem: `lock_path` ends
    with `is_contained(root / (stem + ".lock"), root)`. Asked the `.discovering` question it
    therefore answered about a **different file**, so an unrelated symlink at `<stem>.lock` --
    itself already reported, and pointing anywhere outside the root -- flipped a real
    `<stem>.discovering` file this store wrote into `unexpected`. That is invariant 17's shape one
    layer down: a verdict about one entry decided by a sibling entry's state. Pinned by
    `test_a_symlink_at_the_lock_name_does_not_sink_the_guard_file_beside_it`.

    Containment is not missing from this predicate, it is inapplicable: `scan_lock_root`'s entries
    come out of `iterdir(root)` and are children of it by construction, `_slug_shape` makes a
    separator or a dot segment unrepresentable in a stem, and a symlink at the entry *itself* is
    already excluded before this is called.

    **Not `is_slug` either.** `is_slug` is `validate_slug`, whose reserved-device-name refusal is
    unconditional because it guards *creation*; asking the creation-time question here reported a
    reserved-name session's own lock and guard files as residue nobody recognises, which is #391's
    defect one predicate over (#401). Pinned by
    `test_a_reserved_name_sessions_own_lock_and_guard_files_are_not_reported_as_residue`.

    **No longer probes the filesystem at all, and that is itself pinned** -- `_shape_only` never
    touches `session_root()`, so the `SessionUnreadableError` this used to be able to raise (through
    `_refuse_new_reserved_slug`'s own `_probe`) cannot happen here any more; that source of
    `scan_lock_root`'s `unexaminable` bucket is gone by design. Pinned by
    `test_a_reserved_lock_stem_no_longer_probes_the_session_root`."""
    return _shape_only(stem)


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
    # The stem before the first dot, not the whole filename: `con.tar.gz` is reserved on the `con`
    # component alone, and Windows refuses it regardless of what follows (#221, see `_reserved_stem`).
    if _reserved_stem(filename):
        stem = filename.split(".", 1)[0]
        raise InvalidFilenameError(
            f"invalid artifact filename {filename!r}: {stem.lower()!r} is a reserved Windows device "
            "name and cannot be created as a file there",
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
    the reasoning for both halves of that confirmation, and for why it is one function.

    **The reserved-device-name refusal is conditional here, and `validate_slug` itself stays
    unconditional** (#372). `canonical_dir` — built on this function — is exactly what
    `create_session` calls, so a genuinely new reserved slug is refused exactly as strictly as
    before: `_refuse_new_reserved_slug` probes the very directory about to be created, finds
    nothing there, and refuses. What changes is a name a session already occupies: it reads through
    instead of being refused a second time on every access after the one that (rightly) refused its
    creation."""
    slug = _slug_shape(slug)
    d = root / slug
    _refuse_new_reserved_slug(slug, d)
    if not is_contained(d, root):
        raise InvalidSlugError(f"slug {slug!r} does not resolve to a path inside the session root",
                               details={"slug": slug})
    return d


def canonical_dir(slug: str) -> Path:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store.canonical_dir`, which this delegates to."""
    return _default_store().canonical_dir(slug)


def legacy_dir(slug: str) -> Path:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store.legacy_dir`, which this delegates to."""
    return _default_store().legacy_dir(slug)


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
    deliberately cannot resolve, and it does *not* carry over here: `check_session_dir` puts every
    recorded filename through `validate_filename` and `is_contained`, and `session import` refuses
    the whole archive when either fails — reproduced, both for a traversal and for a merely wrong
    name. Read as covering both fields, this would claim a vector that is shut and quietly drop the
    one that is open.

    **Since #260 that is the whole of what a filename is pinned to when the artifact *type* is one
    this build does not know**, because there is then no `ARTIFACT_FILENAMES` value to pin it against
    — an unknown type is a note rather than a refusal, so `session import` accepts the entry. This
    paragraph said "pins every recorded filename to its `ARTIFACT_FILENAMES` value" and would have
    read as a stronger claim than the code makes. The claim that matters is unchanged and is the one
    stated above: the name is a bare file inside `artifacts/` or the archive is refused, whether or
    not anything here recognises the type it is filed under. `artifact_filename_mismatch` still
    refuses a *known* type stored under the wrong name.

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
    """Ambient-default wrapper (#272) -- see `Store.session_exists`."""
    return _default_store().session_exists(slug)


def legacy_exists(slug: str) -> bool:
    """Ambient-default wrapper (#272) -- see `Store.legacy_exists`."""
    return _default_store().legacy_exists(slug)


def write_meta(slug: str, meta: SessionMeta) -> Path:
    """Ambient-default wrapper (#272) -- see `Store.write_meta`."""
    return _default_store().write_meta(slug, meta)


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
    """Ambient-default wrapper (#272) -- see `Store.read_meta`."""
    return _default_store().read_meta(slug)


def create_session(slug: str, request: str, *, provider: str | None = None,
                   model_name: str | None = None, context_cards: list[str] | None = None) -> SessionMeta:
    """Ambient-default wrapper (#272) -- see `Store.create_session`."""
    return _default_store().create_session(
        slug, request, provider=provider, model_name=model_name, context_cards=context_cards)


def delete_session(slug: str) -> None:
    """Ambient-default wrapper (#272) -- see `Store.delete_session`."""
    return _default_store().delete_session(slug)


def save_revision(slug: str, model: EngineOutput, *, expected_revision: int | None = None,
                  provenance: dict | None = None) -> tuple[int, SessionMeta]:
    """Ambient-default wrapper (#272) -- see `Store.save_revision`."""
    return _default_store().save_revision(
        slug, model, expected_revision=expected_revision, provenance=provenance)


def load_session_model(slug: str) -> EngineOutput:
    """Ambient-default wrapper (#272) -- see `Store.load_session_model`."""
    return _default_store().load_session_model(slug)


def load_revision_model(slug: str, revision: int) -> EngineOutput:
    """Ambient-default wrapper (#272) -- see `Store.load_revision_model`."""
    return _default_store().load_revision_model(slug, revision)


def session_request(slug: str) -> str:
    """Ambient-default wrapper (#272) -- see `Store.session_request`."""
    return _default_store().session_request(slug)


def save_session_artifact(slug: str, artifact_type: str, filename: str, content: str,
                          source_revision: int, *, stale: bool = False) -> ArtifactStatus:
    """Ambient-default wrapper (#272) -- see `Store.save_session_artifact`."""
    return _default_store().save_session_artifact(
        slug, artifact_type, filename, content, source_revision, stale=stale)


def write_artifact_file(slug: str, filename: str, content: str) -> Path:
    """Ambient-default wrapper (#272) -- see `Store.write_artifact_file`."""
    return _default_store().write_artifact_file(slug, filename, content)


def read_artifact_file(slug: str, filename: str) -> Optional[str]:
    """Ambient-default wrapper (#272) -- see `Store.read_artifact_file`."""
    return _default_store().read_artifact_file(slug, filename)


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
    the entry's history: whether `create_session`'s rename would reach this directory and collide
    with it, which is what decides whether this entry costs anybody anything. A name that is not
    slug-shaped is unreachable — `canonical_dir` refuses it before any rename is attempted, loudly —
    so it is reported and carries no consequence. It is answered by `_shape_only`, pattern *and*
    length, because testing the pattern alone once marked an 81-character name as one a session
    would silently lose (found by review).

    **Not `is_slug` (#408).** `is_slug` answers whether `create_session` could be *asked* for the
    name from nothing, which for a reserved Windows device name (`con`, `nul`, `lpt1`, ...) is
    unconditionally no -- and this entry is not "from nothing": the directory already exists, so
    `canonical_dir` reads straight through the very same conditional rule `_shape_only`'s siblings
    apply (#372), and the rename that follows loses to it rather than being refused. Asking `is_slug`
    here read a taken reserved name as unreachable and left `doctor`'s `[name taken]` hint silent
    about the one directory it exists to name. `_describe_non_session` explains why the reserved-
    device half of that read-time rule can be skipped rather than asked, for this caller specifically.

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
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store._scan_session_root`, which this delegates to."""
    return _default_store()._scan_session_root()


def list_session_slugs() -> list[str]:
    """Ambient-default wrapper (#272) -- see `Store.list_session_slugs`."""
    return _default_store().list_session_slugs()


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
    it could not be constructed here to be ruled out either way.

    **`slug_shaped` is `_shape_only(p.name)`, not the full read-time rule, and not `is_slug`** (#408).
    `p` is an entry `_scan_session_root`'s own `iterdir()` just found under `session_root()`, so
    `p.name` is already known to occupy `session_root() / p.name` -- the one path
    `_refuse_new_reserved_slug` would be asked to probe if this called the full rule
    (`_slug_shape` plus that conditional check) the way `_child_of` and `lock_path` do. Given
    something already occupies it, that probe can only ever answer "does not refuse", so calling it
    would be a filesystem read -- one this "never raise" function would then have to guard against
    failing -- for an answer already implied by the fact that `p` exists. `_shape_only` gives the
    identical bool without the read: shape is what decides whether `create_session` refuses this name
    outright; existence is what decides whether it collides instead, and existence is already
    established here. `is_slug` asked the *unconditional* creation-time question instead, which
    refuses a reserved Windows device name regardless of what already occupies it -- so a `con`
    directory with no `session.json` read `slug_shaped: False` and `doctor` never named the one
    directory its `[name taken]` hint exists for, even though `create_session('con', ...)` reads
    straight through it and loses its rename. Pinned by
    `test_a_reserved_name_directory_that_is_not_a_session_is_reported_as_taken`."""
    slug_shaped = _shape_only(p.name)
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
    """Ambient-default wrapper (#272) -- see `Store.scan_session_root`."""
    return _default_store().scan_session_root()


def list_unexaminable_entries() -> list[UnexaminableEntry]:
    """Ambient-default wrapper (#272) -- see `Store.list_unexaminable_entries`."""
    return _default_store().list_unexaminable_entries()


def scan_lock_root() -> tuple[list[str], list[str], list[UnexaminableEntry]]:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store.scan_lock_root`, which this delegates to."""
    return _default_store().scan_lock_root()


def migrate_legacy(slug: str) -> SessionMeta:
    """Ambient-default wrapper (#272) -- resolves the workspace root fresh on every call. Full
    contract on `Store.migrate_legacy`, which this delegates to."""
    return _default_store().migrate_legacy(slug)
