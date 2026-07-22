"""Shared logic for the golden regression harness (K-run consensus).

The engine is non-deterministic and the model family in use (Claude 5 / Opus 4.8) exposes no sampling
controls — `temperature`/`top_p`/`top_k` are removed and 400 if sent — so a single capture can't be
pinned. At n=1 the run-to-run noise drowns the signal a prompt or context-card change actually causes.

The answer is statistical: capture each request K times and reason about the *consensus*. A slot's
impact or confidence is only trustworthy as a signal if it is stable across the K runs; a dimension
that flickers run-to-run is noise and can't be used to detect change. This module computes that
consensus and the per-request stability (the empirical noise floor); `golden_run` captures the K runs,
`golden_diff` compares two K-run baselines through it.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from product_copilot.core.analysis import _label, _state_of  # noqa: E402
from product_copilot.core.contracts import EngineOutput  # noqa: E402

GOLDEN = REPO / "fixtures" / "golden"
REQUESTS = GOLDEN / "requests.md"
K = int(os.getenv("GOLDEN_K", "3"))  # runs per request; 3 is the approved default


def parse_requests(path: Path) -> list[dict]:
    """Parse requests.md into ``[{slug, form, card, request}, …]`` (see the file's own header)."""
    runs: list[dict] = []
    current: dict | None = None
    for line in path.read_text().splitlines():
        if line.startswith("### "):
            current = {"slug": line[4:].strip(), "form": "", "card": "", "request": ""}
            runs.append(current)
        elif current is not None and ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            if key.strip() in ("form", "card", "request"):
                current[key.strip()] = value.strip()
    return [r for r in runs if r["request"]]


def runs_path(slug: str) -> Path:
    return GOLDEN / f"{slug}.runs.json"


def dump_runs(slug: str, request: str, models: list[EngineOutput]) -> Path:
    """Persist the K captured models for one request as a single JSON envelope."""
    import json
    payload = {"request": request, "runs": [m.model_dump() for m in models]}
    path = runs_path(slug)
    path.write_text(json.dumps(payload, indent=2))
    return path


def load_runs(text: str) -> list[EngineOutput]:
    """Parse a `.runs.json` envelope (from disk or `git show`) into its list of models."""
    import json
    payload = json.loads(text)
    return [EngineOutput.model_validate(r) for r in payload["runs"]]


def _mode(values: list) -> tuple[object, int]:
    """Most common value and how many of the K runs agree on it."""
    return Counter(values).most_common(1)[0]


def consensus(models: list[EngineOutput]) -> dict:
    """Per-slot consensus over K runs: for each of `impact` and `state`, the modal value and the
    agreement count (how many of K runs share it). Agreement == K means unanimous — the only case a
    later change can be attributed to a real cause rather than sampling noise."""
    n = len(models)
    slot_ids = list(models[0].model.keys())
    out = {"n": n, "slots": {}, "themes": _stable_themes(models)}
    for sid in slot_ids:
        impacts = [str(getattr(m.model[sid].impact, "value", m.model[sid].impact))
                   for m in models if sid in m.model]
        states = [_state_of(m.model[sid]) for m in models if sid in m.model]
        out["slots"][sid] = {
            "impact": _mode(impacts),
            "state": _mode(states),
        }
    return out


def _stable_themes(models: list[EngineOutput]) -> set[str]:
    """Question-target labels that appear in a majority of the K runs — the stable focus of the
    engine on this request, as opposed to a theme that showed up in a single noisy run."""
    n = len(models)
    counts: Counter = Counter()
    for m in models:
        for lab in {_label(q.slot) for q in m.questions}:
            counts[lab] += 1
    return {lab for lab, c in counts.items() if c > n / 2}


def stability(models: list[EngineOutput]) -> dict:
    """The empirical noise floor for one request: how much of the model is stable enough to diff on.
    Returns counts of unanimous vs jittery slots per dimension, plus the stable question themes."""
    con = consensus(models)
    n = con["n"]
    unan = {"impact": 0, "state": 0}
    jitter = {"impact": 0, "state": 0}
    for meta in con["slots"].values():
        for dim in ("impact", "state"):
            if meta[dim][1] == n:
                unan[dim] += 1
            else:
                jitter[dim] += 1
    return {"n": n, "unanimous": unan, "jitter": jitter,
            "themes": sorted(con["themes"]), "total_slots": len(con["slots"])}


def movements(old: list[EngineOutput], new: list[EngineOutput]) -> dict:
    """Changes between two K-run baselines that clear the noise floor. A slot dimension is reported
    only if the OLD baseline was unanimous on it (so it's a reliable reference) and the NEW consensus
    clearly moved to a different value (majority of new runs). Everything else is noise and stays
    silent. Also reports stable question themes that appeared or disappeared."""
    co, cn = consensus(old), consensus(new)
    n_new = cn["n"]
    majority = n_new // 2 + 1
    moved = []
    for sid, ometa in co["slots"].items():
        nmeta = cn["slots"].get(sid)
        if not nmeta:
            continue
        for dim in ("impact", "state"):
            o_val, o_agree = ometa[dim]
            n_val, n_agree = nmeta[dim]
            reliable = o_agree == co["n"]           # old baseline rock-stable on this dimension
            clear = n_val != o_val and n_agree >= majority
            if reliable and clear:
                moved.append({
                    "slot": _label(sid), "dim": dim,
                    "from": o_val, "to": n_val,
                    "old_agree": o_agree, "new_agree": n_agree, "n": n_new,
                })
    return {
        "moved": moved,
        "themes_added": sorted(cn["themes"] - co["themes"]),
        "themes_removed": sorted(co["themes"] - cn["themes"]),
    }
