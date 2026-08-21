"""Requivo — the requirements engine.

The model is the product; every interface — the terminal CLI, the Claude Code
plugin, Requivo Web — is a thin layer over the same core, reaching it through the
shared services. Business logic, prompts, context cards, Pydantic contracts and
model.json are the single source of truth; see CLAUDE.md for the architecture and
the invariants a change must not break.
"""

__version__ = "1.1.0"
