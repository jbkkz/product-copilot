from __future__ import annotations

import hashlib
import re
from pathlib import Path

from product_copilot.core.contracts import EngineOutput
from product_copilot.paths import ROOT


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:5]
    return "-".join(words) or "discovery"


def resolve_slug(base: str, request: str) -> str:
    """A collision-safe slug for a fresh discovery. The 5-word slug is friendly but not unique — two
    different requests can share it and silently overwrite each other's out/<slug>/. Keep the clean
    slug when it's free or belongs to the *same* request (a re-run), and only fall back to a short,
    deterministic hash suffix when a *different* request already owns it: leave-approval-a3f82c."""
    folder = ROOT / "out" / base
    if not folder.exists():
        return base
    existing = load_request(folder / "model.json")
    if existing.strip() == request.strip():
        return base  # same discovery re-run — reuse the folder, don't proliferate
    return f"{base}-{hashlib.sha1(request.encode('utf-8')).hexdigest()[:6]}"


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


def save_request(slug: str, request: str) -> Path:
    """Persist the original request beside the model so a discovery turn can resume statelessly."""
    return write_artifact(slug, "request.txt", request)


def load_request(model_path: Path) -> str:
    """The original request saved next to a model (empty string if none)."""
    p = model_path.parent / "request.txt"
    return p.read_text() if p.exists() else ""


def present_artifacts(slug: str) -> set[str]:
    """Filenames currently in out/<slug>/ — so change-detection only flags artifacts that were
    actually generated, not the whole theoretical blast radius."""
    folder = ROOT / "out" / slug
    return {p.name for p in folder.iterdir() if p.is_file()} if folder.exists() else set()
