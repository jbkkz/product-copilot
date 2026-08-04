"""The user-facing vocabulary — one table, read by every template.

The engine's vocabulary is precise and internal: slots, coverage, evidence, artifacts, staleness,
revisions. It is the right vocabulary for `docs/` and for the CLI's `--json`, and the wrong one for
the first screen of a product: it asks a reader to learn the model before they can use it.

So the Web speaks a second, smaller vocabulary, defined here rather than spelled inline in each
template — a term that lives in six templates drifts in six directions. Nothing below changes what is
stored, computed or emitted by `--json`; this is a translation layer over the same values.

    requirements model  →  current understanding      artifact        →  document
    explicit evidence   →  what we know               stale artifact  →  needs updating
    inferred evidence   →  what we are assuming       revision        →  history
    unknown             →  open question              context card    →  product context
    challenge           →  assumption to review       provider        →  advanced setting
    readiness           →  are we ready?              slot            →  (never shown by default)
"""

from __future__ import annotations

# Artifact type → the name a reader sees. Deliberately wider than what the Web generates: a session
# created by the CLI can carry a `stories` artifact, and it still has to be listed under a name.
#
# `brief` is the one that changed name and not identity. On disk it is still `solution-assessment.md`,
# the CLI verb is still `requivo brief`, and the contract is still `Brief` — renaming any of those
# would break sessions, scripts and the plugin to change a caption. "Decision brief" says what the
# document is *for* (reviewing scope before committing) where "solution assessment" said what it is.
ARTIFACT_LABELS: dict[str, str] = {
    "brief": "Decision brief",
    "prd": "PRD",
    "stories": "User stories",
    "criteria": "Acceptance criteria",
    "epic": "Delivery epic",
    "release": "Release notes",
}

# The one document the primary flow leads to. Everything else is available, under "More documents".
PRIMARY_ARTIFACT = "brief"


def artifact_label(artifact_type: str) -> str:
    return ARTIFACT_LABELS.get(artifact_type, artifact_type)


def artifact_labels(types: list[str]) -> list[str]:
    return [artifact_label(t) for t in types]
