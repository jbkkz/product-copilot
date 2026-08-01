#!/usr/bin/env python
"""Install-free launcher.

`python scripts/requivo_cli.py <command>` runs the exact same app() as the installed `requivo`
console script — it just puts src/ on sys.path first, so no `pip install` is needed. It lives under
scripts/ (not the repo root) on purpose: a root-level `requivo.py` would shadow the `requivo` package
on `import requivo` from the checkout. Prefer `uv run requivo` (builds the env on first run) or the
installed `requivo` command; use this only from a bare clone with nothing installed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from requivo.cli import app  # noqa: E402

if __name__ == "__main__":
    app()
