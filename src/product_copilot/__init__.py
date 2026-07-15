"""Product Copilot — the requirements engine.

The engine is the product; every interface (terminal CLI, and later Claude Code,
API, web) is a thin layer over the same core. Business logic, prompts, context
cards, Pydantic contracts and model.json are the single source of truth — see
CLAUDE.md. This package is being carved out of the historical src/engine.py; that
module now re-exports from here for backward compatibility.
"""
