from __future__ import annotations

import re
from pathlib import Path

from product_copilot.core.contracts import EngineOutput
from product_copilot.paths import ROOT


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:5]
    return "-".join(words) or "discovery"


def save_model(out: EngineOutput, slug: str) -> Path:
    """Persist the model — the durable product. Every artifact is regenerated from this file."""
    folder = ROOT / "out" / slug
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "model.json"
    path.write_text(out.model_dump_json(indent=2))
    return path


def load_model(path: Path) -> EngineOutput:
    """Load a saved model so artifacts can be regenerated without redoing discovery."""
    return EngineOutput.model_validate_json(path.read_text())


def write_artifact(slug: str, filename: str, content: str) -> Path:
    """Write a generated artifact next to its model in out/<slug>/."""
    folder = ROOT / "out" / slug
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_text(content)
    return path
