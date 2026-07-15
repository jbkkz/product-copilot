#!/usr/bin/env python
"""Install-free launcher.

`python pc.py <command>` runs the exact same app() as the installed `pc` console
script — it just puts src/ on sys.path first, so no `pip install` is needed. Use
the installed `pc` command once the package is installed; use this from the repo
otherwise (the Claude Code commands call it).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from product_copilot.cli import app  # noqa: E402

if __name__ == "__main__":
    app()
