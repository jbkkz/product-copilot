---
description: Show a Product Copilot model's understanding checklist and readiness
argument-hint: <path to out/<slug>/model.json>
allowed-tools: Bash(.venv/bin/python pc.py:*)
---

Thin wrapper — only run the CLI; never compute or summarize status yourself.

1. If `$1` is empty, ask for the model path and stop.
2. Run: `.venv/bin/python pc.py status "$1"`
3. Show the output verbatim.
4. Then, based on that output, suggest a next step: if it reports blockers
   ("Not ready" / "Nearly ready"), suggest confirming the flagged item or
   refining with `/pc-discover`; if "Ready", suggest
   `/pc-generate "$1" prd`.
