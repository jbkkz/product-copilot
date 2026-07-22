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
from product_copilot.core.contracts import Brief, EngineOutput  # noqa: E402

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


def dump_runs(slug: str, request: str, models: list[EngineOutput],
              briefs: list[Brief] | None = None) -> Path:
    """Persist the K captured models for one request as a single JSON envelope.

    ``briefs`` is optional and captured only for the requests we watch the *assessment* on — it costs
    a second API call per run, so it is opt-in rather than the default (see ``golden_run --brief``)."""
    import json
    payload = {"request": request, "runs": [m.model_dump() for m in models]}
    if briefs is not None:
        payload["briefs"] = [b.model_dump() for b in briefs]
    path = runs_path(slug)
    path.write_text(json.dumps(payload, indent=2))
    return path


def load_runs(text: str) -> list[EngineOutput]:
    """Parse a `.runs.json` envelope (from disk or `git show`) into its list of models."""
    import json
    payload = json.loads(text)
    return [EngineOutput.model_validate(r) for r in payload["runs"]]


def load_briefs(text: str) -> list[Brief]:
    """The assessments captured alongside the models, or [] if this request doesn't watch them."""
    import json
    payload = json.loads(text)
    return [Brief.model_validate(b) for b in payload.get("briefs", [])]


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


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# The assessment lens
#
# Discovery is watched through slots and question themes; the *assessment* is the deliverable, and it
# is mostly prose. Two things in it are comparable across runs: the `complexity` verdict (categorical,
# so consensus applies directly) and the **challenge headlines** — 3–6 words naming the premise being
# contested. Headlines are the assessment's equivalent of a question theme: they say what the engine
# chose to push back on, which is the differentiator we actually care about keeping sharp.
#
# Challenges never repeat verbatim across runs, so they have to be grouped rather than matched. They
# are grouped by the **slot ids they contest** (`Challenge.contests`) — the structural statement of
# what the challenge is about. Word overlap was tried first and is not good enough: the engine
# rephrases at the concept level, not just by reordering, and "Visibility of the superseded signed
# copy" / "Published-document blast radius ignored" are the same challenge with no word in common.
# Under word matching those read as one challenge lost and another gained, which is exactly the false
# alarm this lens exists to avoid. Word overlap survives only as a fallback for captures taken before
# `contests` existed.

_STOPWORDS = {"a", "an", "the", "as", "at", "in", "on", "of", "for", "to", "and", "or", "vs", "is",
              "be", "by", "with", "not", "no", "its", "it", "this", "that", "are", "may", "can"}


def _words(headline: str) -> frozenset[str]:
    """Content words of a headline, lowercased — the key a cluster is matched on."""
    raw = "".join(c.lower() if c.isalnum() or c.isspace() else " " for c in headline).split()
    return frozenset(w for w in raw if w not in _STOPWORDS and len(w) > 2)


def _cluster_headlines(per_run: list[list[str]], threshold: float = 0.4) -> dict[str, int]:
    """Fallback grouping for captures taken before `contests` existed: greedy single-pass clustering
    of headlines on Jaccard overlap of their content words, counting the runs each theme appeared in.

    It only catches rewordings that reuse the same vocabulary. That limit is why challenges are keyed
    on contested slots now — see `_challenge_themes`."""
    clusters: list[dict] = []   # {"words": frozenset, "label": str, "runs": set[int]}
    for run_idx, headlines in enumerate(per_run):
        for headline in headlines:
            words = _words(headline)
            if not words:
                continue
            best, best_score = None, 0.0
            for cluster in clusters:
                union = words | cluster["words"]
                score = len(words & cluster["words"]) / len(union) if union else 0.0
                if score > best_score:
                    best, best_score = cluster, score
            if best is not None and best_score >= threshold:
                best["runs"].add(run_idx)
                best["words"] = best["words"] | words   # absorb the variant so later runs match
            else:
                clusters.append({"words": words, "label": headline, "runs": {run_idx}})
    return {c["label"]: len(c["runs"]) for c in clusters}


def _challenge_themes(briefs: list[Brief]) -> dict[str, int]:
    """Group the K runs' challenges into themes and count the runs each appeared in.

    Keyed on the contested slot ids where the capture has them, which makes the grouping exact. Falls
    back to headline word overlap only when no run declared `contests` — i.e. a capture predating the
    field. A mixed set is treated as structural: a run that named its slots is not degraded because
    another run didn't."""
    if not any(c.contests for b in briefs for c in b.challenges):
        return _cluster_headlines([[c.headline for c in b.challenges] for b in briefs])

    # A theme is a contested slot, exactly as a question theme is a questioned slot on the discovery
    # side. No similarity threshold to tune, and it survives the engine naming a different set of
    # secondary slots each run — what stays constant across runs is the slot the challenge is really
    # about. Two distinct challenges contesting the same slot do merge; that is the same trade the
    # question-theme lens already makes, and it reads honestly: "in a majority of runs the engine
    # contested something about the workflow".
    runs_per_slot: dict[str, set[int]] = {}
    for run_idx, brief in enumerate(briefs):
        for challenge in brief.challenges:
            for slot_id in challenge.contests:
                runs_per_slot.setdefault(slot_id, set()).add(run_idx)
    return {_label(slot_id): len(runs) for slot_id, runs in runs_per_slot.items()}


def brief_consensus(briefs: list[Brief]) -> dict:
    """Per-request consensus over K assessments: the modal complexity verdict with its agreement
    count, the challenge themes that a majority of runs raised, and the shape of the output (how many
    challenges/risks/opportunities it tends to produce)."""
    n = len(briefs)
    complexities = [str(getattr(b.complexity, "value", b.complexity)) for b in briefs]
    themes = _challenge_themes(briefs)
    return {
        "n": n,
        "complexity": _mode(complexities),
        # Unanimity, not a majority. Each challenge names two or three contested slots and a run
        # raises about three challenges, so a majority bar marks nearly every slot as stable and the
        # readout saturates — a lost challenge changes nothing because its neighbours still cover the
        # same slots. Requiring every run to contest a slot is both more discriminating and the same
        # bar `movements()` applies to a strong slot move.
        "themes": {label for label, count in themes.items() if count == n},
        "all_themes": themes,
        "counts": {
            "challenges": [len(b.challenges) for b in briefs],
            "risks": [len(b.risks) for b in briefs],
            "opportunities": [len(b.opportunities) for b in briefs],
            "open_decisions": [len(b.open_decisions) for b in briefs],
        },
    }


def brief_movements(old: list[Brief], new: list[Brief]) -> dict:
    """What changed between two K-run assessment baselines. The complexity verdict is graded strong /
    weak on the same rule as a slot (strong = unanimous before *and* after). Challenge themes are
    reported as gained or lost — a theme the engine used to raise in a majority of runs and no longer
    does is the assessment's version of a regression, and the one this lens exists to catch."""
    co, cn = brief_consensus(old), brief_consensus(new)
    o_val, o_agree = co["complexity"]
    n_val, n_agree = cn["complexity"]
    verdict = None
    if o_agree == co["n"] and n_val != o_val and n_agree > cn["n"] / 2:
        verdict = {"from": o_val, "to": n_val, "old_agree": o_agree,
                   "new_agree": n_agree, "n": cn["n"], "strong": n_agree == cn["n"]}
    return {
        "complexity": verdict,
        "themes_added": sorted(cn["themes"] - co["themes"]),
        "themes_removed": sorted(co["themes"] - cn["themes"]),
        "old_counts": co["counts"], "new_counts": cn["counts"],
    }


def movements(old: list[EngineOutput], new: list[EngineOutput]) -> dict:
    """Changes between two K-run baselines that clear the noise floor, split by how much they can be
    trusted. Both tiers need the OLD baseline unanimous on that dimension (a reliable reference):

    - **strong**: the new consensus is *also* unanimous on a different value. Every run agrees, before
      and after — this cannot be one run's jitter.
    - **weak**: the new consensus is only a majority. At K=3 a majority is 2 of 3, so a single run
      flipping produces one of these. Informative in aggregate, not on its own.

    Reading only the strong tier is the default; the weak tier is worth watching when several land on
    the same slot or the same request. Also reports stable question themes that appeared or vanished.
    """
    co, cn = consensus(old), consensus(new)
    n_new = cn["n"]
    majority = n_new // 2 + 1
    strong, weak = [], []
    for sid, ometa in co["slots"].items():
        nmeta = cn["slots"].get(sid)
        if not nmeta:
            continue
        for dim in ("impact", "state"):
            o_val, o_agree = ometa[dim]
            n_val, n_agree = nmeta[dim]
            if o_agree != co["n"] or n_val == o_val:   # unreliable reference, or nothing moved
                continue
            if n_agree < majority:                     # the new runs don't even agree — pure noise
                continue
            entry = {"slot": _label(sid), "dim": dim, "from": o_val, "to": n_val,
                     "old_agree": o_agree, "new_agree": n_agree, "n": n_new}
            (strong if n_agree == n_new else weak).append(entry)
    return {
        "strong": strong,
        "weak": weak,
        "moved": strong + weak,   # kept for callers that want the union
        "themes_added": sorted(cn["themes"] - co["themes"]),
        "themes_removed": sorted(co["themes"] - cn["themes"]),
    }
