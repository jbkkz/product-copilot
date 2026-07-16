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
- **The flow:** `/pc-discover <request>` runs discovery as a **turn-by-turn
  conversation** — the engine asks the high-value questions, you answer here, and
  it refines the model each turn until it converges → a saved model →
  `/pc-status <model>` to inspect it → `/pc-generate <model> <artifact…>` to
  produce a PRD, user stories, an estimate, acceptance criteria, a delivery epic
  (+ GitHub/GitLab plans), release notes, or the solution assessment (brief).
- **The model is a graph, not a flat file.** `pc impact <model> [slots…]` is a pure
  offline query (no API call): it shows what a change would invalidate — the design
  decisions to re-validate and the artifacts that go stale. And a discovery turn that
  moves the model automatically warns which already-generated files no longer match it.

Then run `.venv/bin/python pc.py --help` and show the live list of CLI commands.
