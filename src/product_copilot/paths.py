"""Filesystem anchors — the single source of truth for path resolution.

Every module resolves repo assets (prompts, framework, context) and outputs
through `ROOT` here, never through its own `__file__`. That way moving a module
between packages never changes where files are read or written — the reason this
is the first thing extracted (it de-risks the whole split).
"""

from __future__ import annotations

from pathlib import Path

# src/product_copilot/paths.py → parents[2] is the repo root.
ROOT = Path(__file__).resolve().parents[2]
