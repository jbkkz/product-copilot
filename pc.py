#!/usr/bin/env python
"""Deprecated launcher alias — kept temporarily for backward compatibility.

`python pc.py <command>` is equivalent to `python requivo.py <command>`. The project was
renamed from Product Copilot to Requivo; this alias (like the `pc` console script) is kept
so existing callers — including the Claude Code slash commands — keep working, and may be
removed in a future major version. Prefer `requivo.py` or the installed `requivo` command.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from requivo.cli import app  # noqa: E402

if __name__ == "__main__":
    app()
