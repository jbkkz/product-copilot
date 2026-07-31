"""Filesystem anchors — the single source of truth for path resolution.

Two roots, deliberately separate:

- **`ASSETS`** — read-only bundled data (prompts, framework, context, the demo payload). It lives
  *inside* the package (`src/requivo/assets/`), so it resolves identically from an editable
  checkout and from an installed wheel, and setuptools ships it as package data. Resolving it from
  this module's own location (not the repo root) is what makes a `pip install` work outside the clone.
- **`output_root()`** — where generated models/artifacts are written. This must never be inside the
  package (site-packages is often read-only, and writing there would be wrong anyway), so it defaults
  to `./out` under the current working directory and can be redirected with `REQUIVO_OUTPUT_DIR`.

Every module resolves assets and outputs through here, never through its own `__file__`.
"""

from __future__ import annotations

import os
from pathlib import Path

# src/requivo/paths.py → assets sit next to this file, inside the package. A normal wheel
# install unpacks the package to the filesystem, so a plain Path (with .glob/.read_text) works; we
# don't need importlib.resources' zip handling, and the wheel-install CI job proves this resolves.
ASSETS = Path(__file__).resolve().parent / "assets"

PROMPTS = ASSETS / "prompts"
FRAMEWORK = ASSETS / "framework"
CONTEXT = ASSETS / "context"
DEMO = ASSETS / "demo"


def workspace_root() -> Path:
    """The user's working area — where sessions are written. Defaults to the current working
    directory; override with `REQUIVO_WORKSPACE` (the CLI's `--workspace` sets this env for the run).
    Never inside the installed package. Evaluated per call so a `cd`/env change takes effect without
    reimporting. Distinct from `ASSETS` (read-only package data): assets are read, the workspace is
    written."""
    override = os.getenv("REQUIVO_WORKSPACE")
    return Path(override) if override else Path.cwd()


def session_root() -> Path:
    """Canonical home for sessions: `<workspace>/.requivo/sessions/`. Each session is a `<slug>/`
    directory under here (session.json + model.json + revisions/ + request.md + artifacts/). This is
    where all *new* data is written; the legacy `output_root()` (`./out`) is read-only and migrated on
    first mutation."""
    return workspace_root() / ".requivo" / "sessions"


def output_root() -> Path:
    """**Legacy** directory for generated models/artifacts (`./out`), from before the versioned
    `.requivo/sessions/` layout. Still read for backward compatibility and used by the pre-refactor
    provider CLI path; new sessions are written under `session_root()`. Override with
    `REQUIVO_OUTPUT_DIR`. Evaluated per call."""
    override = os.getenv("REQUIVO_OUTPUT_DIR")
    return Path(override) if override else Path.cwd() / "out"


def user_context_dir() -> Path:
    """Where a user drops their own context cards, so a pip-installed setup can be extended without a
    source checkout (the bundled cards in `CONTEXT` are inside the read-only package). Defaults to
    `~/.config/requivo/context`; override with `REQUIVO_CONTEXT_DIR`. May not exist — callers check.
    A user card whose stem matches a bundled one overrides it (see `load_context`)."""
    override = os.getenv("REQUIVO_CONTEXT_DIR")
    return Path(override) if override else Path.home() / ".config" / "requivo" / "context"
