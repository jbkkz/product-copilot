"""Public test infrastructure -- part of the declared Python import seam (docs/compatibility.md,
#423), not internal plumbing. `requivo.testing.repository_conformance.SessionRepositoryConformance`
is the one export today: a pytest mixin an out-of-repo `SessionRepository` implementation (a
Postgres backing, most concretely) can subclass to prove it honours the semantics `SessionService`
and `ArtifactService` assume, without reaching into this repository's own tests, fixtures or
`conftest.py` (#424).
"""

from requivo.testing.repository_conformance import SessionRepositoryConformance

__all__ = ["SessionRepositoryConformance"]
