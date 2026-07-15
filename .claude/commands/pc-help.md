---
description: What Product Copilot is and how to drive it from Claude Code
allowed-tools: Bash(.venv/bin/python pc.py:*)
---

Explain Product Copilot to the user, concisely:

- **The product is the engine, not these commands.** Product Copilot is a Python
  requirements engine that turns a vague client request into a structured
  *solution model* (`out/<slug>/model.json`), then generates deliverables from
  it. Claude Code is only one interface over that engine — the same core also
  runs from a terminal (`pc …` / `python pc.py …`) and, later, an API. These
  slash commands are thin wrappers; they never do the reasoning themselves.
- **The flow:** `/pc-discover <request>` → a saved model → `/pc-status <model>`
  to inspect it → `/pc-generate <model> <artifact…>` to produce a PRD, user
  stories, an estimate, acceptance criteria, a delivery epic (+ GitHub/GitLab
  plans), release notes, or the solution assessment (brief).

Then run `.venv/bin/python pc.py --help` and show the live list of CLI commands.
