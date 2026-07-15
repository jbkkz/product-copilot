---
description: Run Product Copilot discovery on a client request (produces a model.json)
argument-hint: <request text | path to a request file>
allowed-tools: Bash(.venv/bin/python pc.py:*)
---

You are a **thin wrapper** around the Product Copilot engine. Do NOT do discovery,
ask requirements questions, or invent any model content yourself — the Python
engine is the single source of truth. Your only job: run the CLI, surface its
output, and propose the next step.

1. If `$ARGUMENTS` is empty, ask the user for the request (a sentence, or a path
   to a request file) and stop.
2. Run discovery through the real CLI:
   `.venv/bin/python pc.py discover "$ARGUMENTS"`
   (Run this way it does a single pass and saves the model; the full interactive
   refinement loop only runs when a human drives it in a real terminal.)
3. From the CLI output, report the **saved model path** (`out/<slug>/model.json`)
   and the readiness line, verbatim.
4. Propose next steps: `/pc-status out/<slug>/model.json` to inspect it, or
   `/pc-generate out/<slug>/model.json prd` (also: stories, estimate, criteria,
   epic, release, brief) to produce deliverables.
