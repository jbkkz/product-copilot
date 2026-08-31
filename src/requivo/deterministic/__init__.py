"""The deterministic CLI surface — every command here runs with no LLM and no API key.

These verbs (`doctor`, `session …`, `model …`, `artifact …`) are the offline half of Requivo: they
create and inspect sessions, validate and apply proposed models, and record artifacts, all through
the same `SessionService`/`ArtifactService` the provider path uses. Claude Code drives *these* — it
reasons with its own Claude, pipes the proposal in on stdin, and calls `model validate`/`model apply` — so no
`ANTHROPIC_API_KEY` is ever required in that mode.

**"No LLM" is true of every verb here and is not, on its own, the axis that put them here** (#296).
The actual split is *plumbing* (this package: session/model/artifact CRUD, install diagnostics) versus
*journey* (`cli.py`: demo, discover, refine, generate, web, in the order a user meets them). Three
journey verbs -- `status`, `demo`, `impact` -- happen to be no-LLM too, and stay in `cli.py` rather
than moving here, because they are reads over the same journey `discover`/`answer`/`brief` belong to,
not administration over a session's own directory the way everything in this package is. A contributor
who reads "no LLM" as the boundary and goes looking for `status` in here is reading the sentence that
used to make that the whole story; `cli.py`'s own tree entry in CLAUDE.md carries the other half.

`register(sub)` attaches the parsers to the main `requivo` argparse tree; each handler takes
`(args, client)` to match the CLI's uniform dispatch (the deterministic handlers ignore `client`).
Handlers raise structured `RequivoError`s; `cli.app()` turns them into a clean message or a JSON error
envelope (`--json`).

This was one 1541-line module until #73 split it along the axes that already changed independently:
`doctor` answers for the install, `sessions` for the session directory, `model` for the model,
`artifacts` for the views of it. `_shared` holds the surface primitives more than one of them needs,
and states its own membership rule so that it does not become a second `deterministic.py`.

**`register()` composes four `register_*` functions by name, and that is the deliberate choice.** The
alternative was a registry the modules populate as a side effect of being imported. That is prettier
and it fails silently: drop a module from the package and its verbs simply stop existing, with no
error, and a `--help` that is quietly one group shorter. Here a missing module is an `ImportError` at
startup. A verb group that cannot register must not be indistinguishable from one that never existed,
which is the same rule the rest of this surface applies to its own three-state checks.

The call order below is the order the parsers are added, and that is the order `--help` prints them
in. The help text is a public surface, so the order is not free to change.
"""

from __future__ import annotations

from requivo.deterministic._shared import EXIT_DEGRADED, read_user_text
from requivo.deterministic.artifacts import register_artifacts
from requivo.deterministic.doctor import register_doctor
from requivo.deterministic.model import register_model
from requivo.deterministic.sessions import register_sessions

# Re-exported because they are read from outside the package: `cli.py` imports `read_user_text`, and
# the suite imports `EXIT_DEGRADED` to assert the code a degraded run exits with.
#
# `docs/compatibility.md` publishes the **value 4**, never this name — the page lists
# `requivo.deterministic` among the internals that are explicitly not stable (#144), so nothing may
# import this symbol from outside. Publishing it was refused rather than left unchosen (#145): a
# promised Python name costs a major version to move and buys a consumer nothing the documented exit
# code does not, since a script gating on a degraded listing reads the process's status and not this
# namespace. Claiming otherwise invited both mistakes at once — importing it from outside, and
# reading a rename as a breaking change. Pinned by
# `test_the_degraded_exit_code_is_published_as_a_value_not_as_a_name`.
#
# Nothing private is re-exported, on purpose. A re-export is a *second* binding: rebinding it here
# would not reach the module global the code actually reads, so a test that patched
# `requivo.deterministic._validate_extracted` would go green having patched nothing. That is this
# repository's own defect class, and a compatibility shim is not worth installing one. A `_`-prefixed
# name is imported from the module that defines it.
__all__ = ["EXIT_DEGRADED", "read_user_text", "register"]


def register(sub) -> None:
    """Attach the deterministic verb groups to the main `requivo` subparser."""
    register_doctor(sub)
    register_sessions(sub)
    register_model(sub)
    register_artifacts(sub)
