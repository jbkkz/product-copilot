"""A named test is a reference, so it has to resolve — and be findable (#75).

This repository answers "why is this line here?" by naming the test that enforces it, in `src/` and
in `CLAUDE.md`. Nobody decided that convention; it accreted, it is the right one, and
**nothing checked it**, which is this project's own recurring defect aimed at its own documentation.

Two things can go wrong and both were live when this guard was written:

* **The test is renamed or deleted.** The comment then names something that does not exist, and a
  reader who greps for it concludes the guard was removed — or, worse, that they cannot find it and
  gives up. A reference that does not resolve is worse than no reference: it spends the reader's
  trust and returns nothing.
* **The name is split by a line wrap.** `contracts.py` carried
  ``test_the_persisted_mirror_copies_every_`` at the end of one comment line and
  ``constraint_it_restates`` at the start of the next. The test exists. The reference is still
  useless, because the only way anyone uses one of these is to select it and grep, and the string
  they select is not in the repository. Two of sixteen were in that state.

Why a *name* and not a path, which is the question #75 left open: paths in this repository move.
The package was renamed once (`product_copilot` → `requivo`), `deterministic.py` became a package,
and `test_cli_deterministic.py` became seven files — all within a fortnight. A test name survives
every one of those and a path survives none, so the reference format is the name, for a test and for
a decision record alike.

What this guard deliberately does **not** do is decide whether a given paragraph *should* carry a
reference. That is a judgement, it is stated in CLAUDE.md as a rule for a person to apply, and a test
that tried to enforce it would be guessing at intent. This one only checks that the references that
exist are true and findable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "requivo"
TESTS = REPO_ROOT / "tests"

# Files that may carry a reference. `.md` is in here for CLAUDE.md, which names two tests in its
# invariants and is read more often than any module.
SUBJECT_SUFFIXES = (".py", ".html", ".md")
EXTRA_SUBJECTS = (REPO_ROOT / "CLAUDE.md",)

# A reference is a `test_`-prefixed identifier. The length floor keeps `test_x` in an example snippet
# from being read as a claim about the suite; every real reference in this repo is far longer.
_REFERENCE = re.compile(r"\btest_[a-z0-9_]{10,}\b")

# A decision record is referenced the same way and for the same reason — by a stable slug, never by
# a path. `docs/decisions/README.md` says why; this pattern is what makes the claim checkable, so the
# second convention does not ship unguarded the way the first one did.
_DECISION_REF = re.compile(r"`decision:\s*([a-z0-9-]+)`")
DECISIONS = REPO_ROOT / "docs" / "decisions"

# The wrap: an identifier that ends a line on a trailing underscore. A Python identifier never
# legitimately ends in `_` here — the suite has none — so this is the split, not a naming style.
_WRAPPED = re.compile(r"\btest_[a-z0-9_]*_$", re.MULTILINE)


def subjects() -> list[Path]:
    """Every file that may carry a reference.

    An empty result is an error rather than an answer, which is `test_boundaries.py`'s rule (#10) and
    it applies with more force here: this guard's whole job is a negative assertion, and a rename of
    `src/requivo` would turn it into a green test over nothing.
    """
    found = [p for p in sorted(SRC.rglob("*")) if p.suffix in SUBJECT_SUFFIXES and p.is_file()]
    found.extend(p for p in EXTRA_SUBJECTS if p.is_file())
    if not found:
        raise AssertionError(
            f"the narrative-reference guard found no files under {SRC}. This is 'could not look', "
            f"not 'looked and found nothing' — fix the path, never the assertion."
        )
    return found


def declared_test_names() -> set[str]:
    """Every test callable the suite defines, plus every test module's stem.

    Both forms are in use and both are legitimate: `services/artifacts.py` names
    `tests/test_artifact_provenance.py`, a whole file, because the claim it makes is the file's
    subject rather than one assertion. Accepting only function names would fail that reference and
    teach the next person to delete the check instead of the reference.
    """
    names: set[str] = set()
    for path in sorted(TESTS.rglob("*.py")):
        names.add(path.stem)
        for match in re.finditer(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)",
                                 path.read_text(encoding="utf-8"), re.MULTILINE):
            names.add(match.group(1))
    if not names:
        raise AssertionError(
            f"the narrative-reference guard found no tests under {TESTS} to resolve against — "
            f"every reference would 'resolve' against an empty set."
        )
    return names


def references(path: Path) -> set[str]:
    """Every test reference in one file."""
    return set(_REFERENCE.findall(path.read_text(encoding="utf-8")))


def test_the_guard_reads_the_real_tree():
    """Name what was scanned, because everything below is a negative assertion."""
    files = subjects()
    assert len(files) > 20, f"only {len(files)} subject files — the scan is not seeing the package"
    assert any(p.name == "contracts.py" for p in files)
    assert any(p.name == "CLAUDE.md" for p in files)
    assert len(declared_test_names()) > 100


def test_every_named_test_reference_resolves():
    """A reference that names nothing spends a reader's trust and returns nothing.

    The failure names the file and the dead reference, because "some reference is broken" costs the
    next person the same search this test just did.
    """
    known = declared_test_names()
    dangling = sorted(
        f"{path.relative_to(REPO_ROOT).as_posix()} -> {name}"
        for path in subjects()
        for name in references(path)
        if name not in known
    )
    assert not dangling, (
        "these references name a test that does not exist:\n  " + "\n  ".join(dangling) +
        "\nEither the test was renamed (update the reference) or it was deleted (delete the "
        "reference, and ask what is guarding the line it was attached to)."
    )


def test_no_reference_is_split_across_a_line():
    """The only way one of these is used is: select it, grep it. A name broken by a wrap is not in
    the repository as a string, so it fails at exactly the moment it is needed — and it fails
    silently, because the test it names really does exist.

    Two of sixteen were in that state when this was written (`contracts.py`,
    `deterministic/doctor.py`). Reflow the comment; never hyphenate or wrap an identifier.
    """
    split = sorted(
        f"{path.relative_to(REPO_ROOT).as_posix()}:{path.read_text(encoding='utf-8')[:m.start()].count(chr(10)) + 1} "
        f"-> {m.group(0)}…"
        for path in subjects()
        for m in _WRAPPED.finditer(path.read_text(encoding="utf-8"))
    )
    assert not split, (
        "these references are split by a line wrap and cannot be found by grep:\n  " +
        "\n  ".join(split) + "\nReflow the surrounding text so the identifier is on one line."
    )


def declared_slugs() -> set[str]:
    """Every slug a decision record declares, read from its `**Slug:**` line rather than from its
    filename — the filename carries an ordering number that is deliberately not the reference."""
    slugs: set[str] = set()
    for path in sorted(DECISIONS.glob("*.md")):
        if path.name == "README.md":
            continue
        m = re.search(r"^\*\*Slug:\*\*\s*`([a-z0-9-]+)`", path.read_text(encoding="utf-8"), re.MULTILINE)
        if not m:
            raise AssertionError(
                f"{path.relative_to(REPO_ROOT).as_posix()} declares no `**Slug:**` line. A record "
                f"nothing can reference by name is a record only its path can reach, which is the "
                f"one thing docs/decisions/README.md says not to build."
            )
        slugs.add(m.group(1))
    return slugs


def test_every_decision_reference_resolves_to_a_record():
    """The slug half of the same rule. A `decision:` reference naming no record is the dangling
    pointer above wearing a different prefix, and the second convention must not ship unguarded the
    way the first one did — sixteen references, two of them broken, and nothing looking."""
    if not DECISIONS.is_dir():
        pytest.skip("no docs/decisions/ yet — nothing declares a slug, so nothing can dangle")
    known = declared_slugs()
    dangling = sorted(
        f"{path.relative_to(REPO_ROOT).as_posix()} -> {slug}"
        for path in subjects()
        for slug in _DECISION_REF.findall(path.read_text(encoding="utf-8"))
        if slug not in known
    )
    assert not dangling, (
        "these `decision:` references name no record:\n  " + "\n  ".join(dangling) +
        f"\nDeclared slugs: {sorted(known)}"
    )


def test_the_decision_records_are_reachable_from_somewhere():
    """A record nothing points at is a document nobody opens, which is the failure mode the whole
    rule was written to avoid. Not a hard requirement of the convention and it is checked anyway,
    because an unreferenced record is a signal that a narrative was moved out of the code and the
    pointer was never left behind."""
    if not DECISIONS.is_dir():
        pytest.skip("no docs/decisions/ yet")
    referenced = {slug for path in subjects()
                  for slug in _DECISION_REF.findall(path.read_text(encoding="utf-8"))}
    # `.github/` is outside `src/`, and it is where the first record's referrer lives.
    for extra in sorted((REPO_ROOT / ".github").rglob("*.yml")):
        referenced.update(_DECISION_REF.findall(extra.read_text(encoding="utf-8")))
    orphans = sorted(declared_slugs() - referenced)
    assert not orphans, (
        f"these records are referenced from nowhere: {orphans}. Leave a `decision: <slug>` line at "
        f"whatever the record explains, or the move traded a paragraph for a file nobody opens."
    )


# ── controls: a guard that cannot fail is not a guard ──────────────────────────────────────────

@pytest.mark.parametrize("source, expected", [
    ("# see `test_the_persisted_contract_is_permissive_all_the_way_down` for why",
     {"test_the_persisted_contract_is_permissive_all_the_way_down"}),
    ("`tests/test_artifact_provenance.py` asserts it", {"test_artifact_provenance"}),
    ("def test_x(): pass", set()),                       # too short to be a reference
    ("no reference here at all", set()),
])
def test_the_extractor_sees_a_reference_and_only_a_reference(tmp_path, source, expected):
    p = tmp_path / "sample.py"
    p.write_text(source, encoding="utf-8")
    assert references(p) == expected


def test_the_wrap_detector_sees_the_shape_it_was_written_for(tmp_path):
    """The positive control, and the one that matters: this guard exists because two real references
    were in exactly this shape and every other check in the repository was blind to them."""
    p = tmp_path / "wrapped.py"
    p.write_text("# … the defect class #14 exists to remove. `test_the_persisted_mirror_copies_every_\n"
                 "# constraint_it_restates` pins the general property.\n", encoding="utf-8")
    assert _WRAPPED.search(p.read_text(encoding="utf-8"))


def test_the_wrap_detector_does_not_fire_on_an_intact_reference(tmp_path):
    """The must-not-fire half. A reference that ends a line *complete* is fine — what is forbidden is
    an identifier continued on the next line, not one that happens to sit at the margin."""
    p = tmp_path / "intact.py"
    p.write_text("# pinned by `test_the_persisted_mirror_copies_every_constraint_it_restates`\n"
                 "# which is why it cannot drift.\n", encoding="utf-8")
    assert not _WRAPPED.search(p.read_text(encoding="utf-8"))
