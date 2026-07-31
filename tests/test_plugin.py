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
EXPECTED_SKILLS = {
    "requivo-discover", "requivo-answer", "requivo-status",
    "requivo-brief", "requivo-prd", "requivo-impact",
}


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
    for name in ("requivo-discover", "requivo-answer"):
        text = (SKILLS / name / "SKILL.md").read_text()
        assert "model apply" in text, f"{name}: must apply via the CLI"
        assert "model validate" in text, f"{name}: must validate before applying"
        assert "/tmp/requivo-proposal.json" in text, f"{name}: must write a temp proposal file"
    # No skill should instruct writing/editing model.json directly.
    for p in _skill_files():
        assert not re.search(r"(edit|write)\s+[^\n]*model\.json", p.read_text(), re.IGNORECASE), \
            f"{p.parent.name}: must not hand-edit model.json"


def test_artifact_saving_skills_use_the_cli():
    for name in ("requivo-brief", "requivo-prd"):
        text = (SKILLS / name / "SKILL.md").read_text()
        assert "artifact save" in text, f"{name}: must save via `requivo artifact save`"
