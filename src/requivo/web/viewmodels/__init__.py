"""View models — adapt the service contracts to what a template needs, without moving any decision.

Templates must not re-derive readiness, staleness, or the understanding split (Core already computes
them). These helpers reshape `SessionService.status()` and the model into plain dicts a template can
render directly, so no logic leaks into Jinja.
"""
