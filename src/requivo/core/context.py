"""Context cards + prompt assembly — deterministic, provider-free.

This is the string-assembly half of what used to live in `core/llm.py`: it reads the bundled prompt
files, the framework schema, and the context cards, and injects them into a prompt template. It makes
**no LLM call and imports no provider** — it only turns assets into a system-prompt string — so it is
safe to keep in `core`. The provider imports `build_prompt()` to feed a model; the CLI and `doctor`
import `available_cards()` to validate a `--context` selection, none of which needs the SDK.
"""

from __future__ import annotations

from pathlib import Path

from requivo.paths import CONTEXT, FRAMEWORK, PROMPTS, user_context_dir


def _card_paths() -> dict[str, Path]:
    """Loadable context cards keyed by stem: the bundled cards in the package, plus any the user drops
    in `user_context_dir()` (so a pip-installed setup is extensible without a source checkout). A user
    card whose stem matches a bundled one **overrides** it — you can tweak a built-in without editing
    the package. `_`-prefixed files are skipped. Emitted in sorted-stem order so the assembled system
    is deterministic and the prompt cache holds."""
    paths: dict[str, Path] = {}
    for directory in (CONTEXT, user_context_dir()):  # user dir second → its cards win on stem clash
        if not directory.exists():
            continue
        for p in sorted(directory.glob("*.md")):
            if not p.name.startswith("_"):
                paths[p.stem] = p
    return paths


def available_cards() -> list[str]:
    """Stems of the loadable context cards (bundled + user), sorted — the vocabulary of the
    `--context` selector."""
    return sorted(_card_paths())


def load_context(only: list[str] | None = None) -> str:
    """Concatenate the context cards. `only` (card stems) restricts the set — this is how a session
    trims irrelevant cards so they don't dilute impact estimation (every card is loaded otherwise).
    Selection is per-session, so the assembled system stays byte-identical across a run's calls and
    the prompt cache still holds."""
    keep = None if only is None else {c.lower() for c in only}
    paths = _card_paths()
    cards = [f"## {stem}\n{paths[stem].read_text()}"
             for stem in sorted(paths)
             if keep is None or stem.lower() in keep]
    return "\n\n".join(cards)


def build_prompt(name: str, only: list[str] | None = None) -> str:
    """Load a prompt file and inject the schema + product context (optionally a subset of cards)."""
    schema = (FRAMEWORK / "model_schema.json").read_text()
    text = (PROMPTS / name).read_text()
    return text.replace("{{SCHEMA}}", schema).replace("{{CONTEXT}}", load_context(only))
