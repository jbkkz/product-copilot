"""Public test infrastructure -- part of the declared Python import seam (docs/compatibility.md,
#423), not internal plumbing. Two exports today:
`requivo.testing.repository_conformance.SessionRepositoryConformance` -- a pytest mixin an
out-of-repo `SessionRepository` implementation (a Postgres backing, most concretely) can subclass to
prove it honours the semantics `SessionService` and `ArtifactService` assume, without reaching into
this repository's own tests, fixtures or `conftest.py` (#424) -- and `full_model()`, the schema-
complete model builder the suite's own tests use, re-exported because a subclass extending or
overriding a test method needs the identical fixture the base class methods build.
"""

from requivo.testing.repository_conformance import SessionRepositoryConformance, full_model

__all__ = ["SessionRepositoryConformance", "full_model"]
