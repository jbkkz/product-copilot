from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from product_copilot import __version__
from product_copilot.core.contracts import EngineOutput
from product_copilot.paths import ROOT

SESSION_FORMAT_VERSION = 1


def _atomic_write(path: Path, content: str) -> Path:
    """Write via a temp file + atomic rename, so an interruption can never leave a half-written file
    where a good one was. model.json is the durable product — a truncated JSON would be unrecoverable,
    and `os.replace` (via Path.replace) is atomic on the same filesystem."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return path


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
    return _atomic_write(folder / "model.json", out.model_dump_json(indent=2))


def save_session(slug: str, *, request: str, model_name: str,
                 context_cards: list[str] | None) -> Path:
    """Provenance sidecar for a discovery: which engine version and Claude model produced this model,
    which context cards informed it, and a hash of the originating request. Kept separate from
    model.json (which stays a clean EngineOutput) so a run is reproducible and `pc answer` /
    generators can reuse the *same* card selection instead of silently widening to all cards.
    `context_cards` is None when all cards were loaded (the default)."""
    session = {
        "format_version": SESSION_FORMAT_VERSION,
        "product_copilot_version": __version__,
        "model_name": model_name,
        "context_cards": context_cards,
        "request_sha256": hashlib.sha256(request.encode("utf-8")).hexdigest(),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    folder = ROOT / "out" / slug
    folder.mkdir(parents=True, exist_ok=True)
    return _atomic_write(folder / "session.json", json.dumps(session, indent=2))


def load_session(model_path: Path) -> dict:
    """The session sidecar next to a model, or {} when absent/unreadable (pre-0.6.1 models have none,
    so every reader must tolerate its absence)."""
    p = model_path.parent / "session.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def session_cards(model_path: Path) -> list[str] | None:
    """The context-card selection recorded for a discovery — None means all cards (the pre-0.6.1
    default, and the value stored when no --context subset was given)."""
    return load_session(model_path).get("context_cards")


def load_model(path: Path) -> EngineOutput:
    """Load a saved model so artifacts can be regenerated without redoing discovery."""
    return EngineOutput.model_validate_json(path.read_text())


def write_artifact(slug: str, filename: str, content: str) -> Path:
    """Write a generated artifact next to its model in out/<slug>/."""
    folder = ROOT / "out" / slug
    folder.mkdir(parents=True, exist_ok=True)
    return _atomic_write(folder / filename, content)


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
