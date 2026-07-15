---
description: Generate Product Copilot deliverables from a saved model (thin wrapper over the CLI)
argument-hint: <model.json> <prd|stories|estimate|criteria|epic|release|brief ...>
allowed-tools: Bash(.venv/bin/python pc.py:*)
---

You are a **thin wrapper** over the Product Copilot CLI. Never write a PRD, user
stories, an epic, or any artifact yourself — each is produced by the engine from
the model. Your job: map the requested artifacts to CLI subcommands, run them,
and report the produced file paths.

1. `$1` is the model path. If it is empty or the file does not exist, ask for a
   valid `out/<slug>/model.json` and stop.
2. The remaining arguments are the artifacts to generate, chosen from:
   `prd`, `stories`, `estimate`, `criteria`, `epic`, `release`, `brief`.
   If none are given, ask which one(s) — or suggest `prd`.
3. For each requested artifact, run the matching subcommand, for example:
   - `.venv/bin/python pc.py prd "$1"`
   - `.venv/bin/python pc.py criteria "$1"`
   - `.venv/bin/python pc.py epic "$1" --github --gitlab`  (epic takes optional --json/--github/--gitlab)
   - `.venv/bin/python pc.py release "$1" v1.0`            (release takes an optional version)
   - `.venv/bin/python pc.py stories "$1"` / `estimate "$1"` / `brief "$1"`
4. Report each **written file path** the CLI prints (e.g. `out/<slug>/prd.md`).
5. Propose a sensible next artifact (e.g. after `prd`, suggest `criteria` or `epic`).
