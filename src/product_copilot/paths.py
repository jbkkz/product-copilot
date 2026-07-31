"""Filesystem anchors — the single source of truth for path resolution.

Two roots, deliberately separate:

- **`ASSETS`** — read-only bundled data (prompts, framework, context, the demo payload). It lives
  *inside* the package (`src/product_copilot/assets/`), so it resolves identically from an editable
  checkout and from an installed wheel, and setuptools ships it as package data. Resolving it from
  this module's own location (not the repo root) is what makes a `pip install` work outside the clone.
- **`output_root()`** — where generated models/artifacts are written. This must never be inside the
  package (site-packages is often read-only, and writing there would be wrong anyway), so it defaults
  to `./out` under the current working directory and can be redirected with `PC_OUTPUT_DIR`.

Every module resolves assets and outputs through here, never through its own `__file__`.
"""

from __future__ import annotations

import os
from pathlib import Path

# src/product_copilot/paths.py → assets sit next to this file, inside the package. A normal wheel
# install unpacks the package to the filesystem, so a plain Path (with .glob/.read_text) works; we
# don't need importlib.resources' zip handling, and the wheel-install CI job proves this resolves.
ASSETS = Path(__file__).resolve().parent / "assets"

PROMPTS = ASSETS / "prompts"
FRAMEWORK = ASSETS / "framework"
CONTEXT = ASSETS / "context"
DEMO = ASSETS / "demo"


def output_root() -> Path:
    """Directory for generated models/artifacts. Defaults to `./out` under the caller's working
    directory (never inside the installed package); override with `PC_OUTPUT_DIR`. Evaluated per call
    so a `cd` or an env change takes effect without reimporting."""
    override = os.getenv("PC_OUTPUT_DIR")
    return Path(override) if override else Path.cwd() / "out"
