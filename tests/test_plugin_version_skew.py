"""Plugin/CLI version-skew detection for the shared preflight (#251).

The CLI updates via `pip`/`uv`; the plugin updates via a marketplace pin the project's own notes
record as bumped roughly monthly (see the memory note this repo keeps on it). Nothing before this
compared the two at runtime -- `tests/test_plugin.py` compares the plugin to `src/requivo/` in the
SAME checkout, which is always in step by construction and proves nothing about a real install
where the two artifacts were built from different commits.

`plugins/claude-code/scripts/version_skew.py` is the tested reference for that comparison. It is
also a standalone diagnostic a human or CI can run directly (`python3
plugins/claude-code/scripts/version_skew.py`), but its primary job here is to be the ground truth
against which `plugins/claude-code/REASONING.md`'s prose preflight is written -- REASONING.md asks
*Claude* to do the comparison at runtime (reading the doctor JSON it already has, plus this
plugin's own manifest via the `Read` tool every skill already carries), because widening every
skill's `allowed-tools` from `Bash(requivo:*), Read` to permit shelling out to a second script is a
bigger, unverified permission change than this issue asks for -- see the module docstring for the
argument in full.

Three states, and the third is the point, same shape as `tests/test_version_sites.py` next door:
`in step` (CLI >= the version this plugin was tested against, say nothing), `behind` (CLI older,
warn and continue -- never refuse, see the issue: the plugin is keyless and most verbs still work
across a minor), and `could not tell` (the doctor JSON did not parse, carried no `requivo_version`,
or the manifest could not be read) -- which must never render as `in step`. A preflight that reports
"in step" because its own inputs were unreadable is exactly the silent-absence class this project
exists to close (CLAUDE.md invariant 15's argument, one layer up from a listing).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_SCRIPTS = ROOT / "plugins" / "claude-code" / "scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from version_skew import BEHIND, COULD_NOT_LOOK, IN_STEP, check, compare  # noqa: E402
from version_skew import tested_against_version as read_tested_against_version  # noqa: E402

MANIFEST = ROOT / "plugins" / "claude-code" / ".claude-plugin" / "plugin.json"
REASONING = ROOT / "plugins" / "claude-code" / "REASONING.md"


def _doctor_json(version: str) -> str:
    return json.dumps({"requivo_version": version, "python_version": "3.12.0"})


# -- the comparison itself, both directions (the must-fire / must-not-fire pair) -------------


def test_a_newer_cli_is_in_step_and_silent():
    result = compare("1.4.0", "1.3.0")
    assert result.state == IN_STEP
    assert "1.3.0" in result.message or "1.4.0" in result.message


def test_an_equal_cli_is_in_step():
    result = compare("1.3.0", "1.3.0")
    assert result.state == IN_STEP


def test_an_older_cli_is_behind_and_warns():
    """The positive control for the case above -- without this, `IN_STEP` could be returned no
    matter what the code does, and the two tests above would still pass."""
    result = compare("1.2.0", "1.3.0")
    assert result.state == BEHIND
    assert "1.2.0" in result.message and "1.3.0" in result.message


def test_behind_never_recommends_refusing():
    result = compare("1.0.0", "1.3.0")
    assert "refuse" not in result.message.lower()
    assert "stop" not in result.message.lower()


# -- the could-not-look arm: never the same as in-step ----------------------------------------


def test_doctor_call_that_failed_outright_is_could_not_look():
    result = check(None, "the `requivo` command was not found on PATH")
    assert result.state == COULD_NOT_LOOK


def test_empty_doctor_output_is_could_not_look():
    result = check("", None)
    assert result.state == COULD_NOT_LOOK


def test_unparseable_doctor_json_is_could_not_look():
    result = check("not json at all {{{", None)
    assert result.state == COULD_NOT_LOOK


def test_doctor_json_missing_requivo_version_is_could_not_look():
    result = check(json.dumps({"python_version": "3.12.0"}), None)
    assert result.state == COULD_NOT_LOOK


def test_could_not_look_never_reads_as_in_step():
    """The bar from the brief: could-not-read must never render as 'versions match'."""
    for result in (
        check(None, "not found"),
        check("", None),
        check("{broken", None),
        check(json.dumps({}), None),
    ):
        assert result.state != IN_STEP
        assert "not" in result.message.lower() or "could" in result.message.lower()


def test_a_readable_doctor_report_is_not_could_not_look():
    """The positive control on the arm above: a guard that reported could-not-look for everything
    would pass every assertion above it and say nothing true about a healthy install."""
    result = check(_doctor_json("1.3.0"), None)
    assert result.state != COULD_NOT_LOOK


# -- reading the plugin's own declared version, live, never a second literal ------------------


def test_tested_against_version_reads_the_real_manifest():
    version = read_tested_against_version(MANIFEST)
    manifest_version = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]
    assert version == manifest_version


def test_tested_against_version_is_could_not_look_shaped_when_the_manifest_is_bad(tmp_path):
    bad = tmp_path / "plugin.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        read_tested_against_version(bad)


def test_check_reads_the_manifest_end_to_end(monkeypatch):
    """The whole flow: doctor output in, manifest read live, a verdict out -- no CLI code changes,
    no stamped literal (see the module docstring)."""
    result = check(_doctor_json("0.1.0"), None, manifest_path=MANIFEST)
    assert result.state == BEHIND


# -- the prose preflight must not duplicate the version as a literal --------------------------


def test_reasoning_md_names_the_skew_check_without_hardcoding_a_version():
    """REASONING.md is what Claude actually reads at runtime. It must describe the comparison and
    must NOT paste a version number into prose -- a copy there is a second site that can drift from
    the manifest the day someone bumps it and forgets the second one, which is the exact defect
    `tests/test_version_sites.py` exists to catch for the other version sites. This guards the
    *shape* of the fix (derive, don't duplicate), not merely that some words are present."""
    text = REASONING.read_text(encoding="utf-8")
    assert "version_skew.py" in text or "requivo_version" in text, (
        "REASONING.md's preflight does not mention the version-skew comparison at all"
    )
    assert ".claude-plugin/plugin.json" in text, (
        "REASONING.md must point at the manifest as the source of the tested-against version, "
        "not restate a number"
    )
    import re
    # A bare X.Y.Z anywhere in this file's prose is exactly the duplicated literal this test exists
    # to catch -- a real semantic version, not a doc-format string like the schema's `format_version:
    # 1` or a slot's `completeness: 0-100`, both of which have no third dotted component.
    stray_versions = re.findall(r"(?<![\w.])\d+\.\d+\.\d+(?![\w.])", text)
    assert not stray_versions, (
        f"REASONING.md hardcodes what looks like a version number: {stray_versions} -- read it "
        f"from the manifest instead, or this drifts the day the manifest is bumped"
    )
