"""Static validation of the Claude Code plugin.

No Claude, no runtime — these assert the plugin's shape and that its skills honour the contract:
they drive the deterministic CLI, never require an API key, and never hand-edit model.json.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "claude-code"
SKILLS = PLUGIN / "skills"
# Claude Code namespaces plugin skills as `/<plugin>:<skill>`, so the directory name must NOT repeat
# the plugin name — `skills/requivo-discover/` in a plugin called `requivo` is invoked as
# `/requivo:requivo-discover`, which is not what any of the docs said.
EXPECTED_SKILLS = {"discover", "answer", "status", "brief", "prd", "impact"}


def _cli_commands() -> set[str]:
    """The real top-level `requivo` subcommands, from the argparse tree — the source of truth the
    skills' command references are checked against."""
    from requivo.cli import _build_parser
    parser = _build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md must start with a YAML frontmatter block"
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm


def _skill_files() -> list[Path]:
    return sorted(SKILLS.glob("*/SKILL.md"))


# ── manifest ─────────────────────────────────────────────────────────────────────


def test_manifest_present_and_valid():
    manifest = PLUGIN / ".claude-plugin" / "plugin.json"
    assert manifest.is_file()
    data = json.loads(manifest.read_text())
    assert data["name"] == "requivo"
    assert data["version"] and data["description"]


def test_readme_present():
    assert (PLUGIN / "README.md").is_file()
    assert (PLUGIN / "REASONING.md").is_file()


def test_repo_is_a_marketplace_pointing_at_this_plugin():
    # `/plugin marketplace add jbkkz/requivo` is the documented install path, and it only works if the
    # repo root carries a catalog whose `source` actually resolves to the plugin directory.
    catalog = PLUGIN.parents[1] / ".claude-plugin" / "marketplace.json"
    assert catalog.is_file(), "the repo root must carry a marketplace catalog"
    data = json.loads(catalog.read_text())
    entry = next(p for p in data["plugins"] if p["name"] == "requivo")
    assert (catalog.parent.parent / entry["source"]).resolve() == PLUGIN.resolve()
    # The catalog and the manifest are edited in different files; drift makes the install lie.
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert entry["version"] == manifest["version"]


def test_the_plugin_version_tracks_the_package_version():
    """Four files declare a version — pyproject, the package, the plugin manifest, the marketplace
    catalog — and each release edits them by hand. The plugin's had silently fallen a release behind,
    which matters because the skills call CLI verbs and the version is the only thing telling a user
    which CLI they were tested against. A hand-edited number needs a test, not a convention."""
    from requivo import __version__

    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["version"] == __version__
    # And the prose must not restate it: the copy in the README is exactly what drifted before.
    assert __version__ not in (PLUGIN / "README.md").read_text()


def test_documented_skill_invocations_are_namespaced():
    # Claude Code always namespaces plugin skills as `/<plugin>:<skill>`. The README documented
    # `/requivo-discover`, which no user could ever type successfully.
    readme = (PLUGIN / "README.md").read_text()
    for name in EXPECTED_SKILLS:
        assert f"/requivo:{name}" in readme, f"{name}: README must document the namespaced invocation"
    assert "/requivo-" not in readme


# ── skills ───────────────────────────────────────────────────────────────────────


def test_exactly_the_expected_skills_exist():
    found = {p.parent.name for p in _skill_files()}
    assert found == EXPECTED_SKILLS, f"skill set drifted: {found ^ EXPECTED_SKILLS}"


def test_each_skill_frontmatter_name_matches_dir():
    for p in _skill_files():
        fm = _frontmatter(p.read_text())
        assert fm.get("name") == p.parent.name, f"{p.parent.name}: frontmatter name mismatch"
        assert fm.get("description"), f"{p.parent.name}: missing description"
        assert "allowed-tools" in fm, f"{p.parent.name}: must declare allowed-tools"


def test_no_skill_requires_an_api_key_or_the_anthropic_provider():
    for p in _skill_files():
        text = p.read_text()
        assert "ANTHROPIC_API_KEY" not in text or "not need" in text.lower() or "no api key" in text.lower(), \
            f"{p.parent.name}: must not require an API key"
        assert "--provider anthropic" not in text, f"{p.parent.name}: must not call the Anthropic provider"


def test_skills_reference_only_real_cli_commands():
    commands = _cli_commands()
    assert commands, "could not introspect CLI commands"
    for p in _skill_files():
        for cmd in re.findall(r"requivo (\w[\w-]*)", p.read_text()):
            assert cmd in commands, f"{p.parent.name}: references unknown `requivo {cmd}`"


def test_mutating_skills_use_a_proposal_file_not_direct_edits():
    # discover/answer change the model — they MUST go through validate/apply on a temp proposal, never
    # by editing model.json directly.
    for name in ("discover", "answer"):
        text = (SKILLS / name / "SKILL.md").read_text()
        assert "model apply" in text, f"{name}: must apply via the CLI"
        assert "model validate" in text, f"{name}: must validate before applying"
        assert "/tmp/requivo-proposal.json" in text, f"{name}: must write a temp proposal file"
    # No skill should instruct writing/editing model.json directly.
    for p in _skill_files():
        assert not re.search(r"(edit|write)\s+[^\n]*model\.json", p.read_text(), re.IGNORECASE), \
            f"{p.parent.name}: must not hand-edit model.json"


def test_artifact_saving_skills_use_the_cli():
    for name in ("brief", "prd"):
        text = (SKILLS / name / "SKILL.md").read_text()
        assert "artifact save" in text, f"{name}: must save via `requivo artifact save`"
