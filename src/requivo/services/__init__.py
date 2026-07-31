"""Application services — the orchestration seam every interface shares.

A service composes the deterministic core (validation, persistence, diff, impact, readiness) into the
operations an interface actually performs: create a session, apply a proposed model, save an artifact.
The CLI, the Anthropic provider path, the Claude Code skills (via the CLI), and the future Web layer
all call *these* — never the core primitives directly — so there is exactly one implementation of
"apply a model update" or "record an artifact", not one per interface.

Services never touch argv, stdout, or the network, and never call an LLM. They take data in and return
data (`SessionMeta`, `UpdateResult`, `ArtifactStatus`); rendering and I/O framing stay at the edges.
"""
