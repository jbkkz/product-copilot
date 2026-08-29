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

from datetime import datetime, timezone

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


# ── when something last moved ─────────────────────────────────────────────────
# The store writes one timestamp format, `2026-08-25T12:36:48Z`, and that is the right vocabulary for
# `session.json` and for `--json`. On a screen it is the same mistake as printing a slot id: it asks a
# reader to parse a machine's spelling of a fact they only wanted the gist of. This is the translation,
# and it lives beside the others rather than in `templating.py`, because "3 days ago" is user-facing
# wording — the thing this module exists to keep in one place. `templating.py` registers it as a Jinja
# filter; the strings are decided here.
#
# Spelled out rather than taken from `strftime`. `%b` is locale-dependent, so a French-locale operator
# would get "janv." in an application that is English everywhere by contract, and the no-pad day is
# `%-d` on POSIX and `%#d` on Windows — one of the two raises on the other platform.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Past a week, "N days ago" stops being easier to read than the date it stands for.
_RELATIVE_DAYS = 7


def _instant(value: str) -> datetime | None:
    """One persisted timestamp as an aware datetime, or None when it cannot be read.

    `fromisoformat` does not accept a `Z` suffix before CPython 3.11 and this project's floor is 3.9,
    so the suffix is rewritten rather than relied on. A stamp carrying no zone is read as UTC, which
    is what the store writes; the alternative — reading it as local time — would make the same file
    say two different things on two machines.
    """
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def human_time(value: str | None, *, now: str | None = None) -> str:
    """When a session last moved, in the vocabulary of the screen it appears on.

    Three answers, and the two that are not a time are the point:

    * **a stamp it can read** — "just now", "7 minutes ago", "yesterday", "3 days ago", or the date
      itself once relative time stops helping;
    * **nothing to say** — an empty value, which is what a degraded row carries *on purpose*
      (invariant 15: we did not read a time, so we state none). Turning that absence into a plausible
      "just now" would be the quiet-wrong-answer form of the very bug that guard exists for;
    * **a stamp it could not read** — handed back unchanged. A hand-edited value, or one written by a
      newer Requivo, is a fact the row does have: inventing a time for it is a lie, and swallowing it
      deletes the only signal a reader has that something is odd about that session.

    `now` is injectable so the boundaries are assertable without a clock; production passes nothing.
    A stamp in the future (clock skew, or a hand-edit) is clamped to "just now" rather than rendered
    as a date — the exact value is still on the row's `title`, which is where a reader checks.

    Pinned by `test_human_time_translates_a_stamp_it_can_read`,
    `test_human_time_says_nothing_when_there_is_nothing_to_say` and
    `test_human_time_hands_back_a_stamp_it_could_not_read_rather_than_hiding_it`.
    """
    if not value:
        return ""
    stamp = _instant(value)
    if stamp is None:
        return value
    reference = _instant(now) if now else datetime.now(timezone.utc)
    if reference is None:
        return value

    seconds = max(0.0, (reference - stamp).total_seconds())
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    hours = int(seconds // 3600)
    if hours < 24:
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    days = int(seconds // 86400)
    if days == 1:
        return "yesterday"
    if days < _RELATIVE_DAYS:
        return f"{days} days ago"
    return f"{stamp.day} {_MONTHS[stamp.month - 1]} {stamp.year}"
