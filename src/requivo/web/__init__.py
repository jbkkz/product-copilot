"""Requivo Web — the local, single-user, self-hostable browser interface.

A thin FastAPI + Jinja2 + HTMX layer over the *same* application services as the CLI and Claude Code
(`SessionService`, `ArtifactService`, `DiscoveryService`). It owns no business logic: readiness,
validation, revisioning and staleness all live in the Core and services. It is deliberately local and
single-user — no auth, no accounts, no database, no remote storage. That boundary is what keeps it
distinct from the future private **Requivo Cloud**; see `docs/web.md`.

Nothing here binds a port at import time: `create_app()` is a factory, invoked by `requivo web`.
"""
