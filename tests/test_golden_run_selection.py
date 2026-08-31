"""A bare `golden_run.py` must not fold an interactive request into a full-set capture (#276).

CLAUDE.md's own golden-harness cost guidance says an interactive request -- K × `GOLDEN_TURNS`
calls, 15 at the defaults, where a single-pass request is K -- should be captured on its own rather
than as part of a full-set run. `golden_run.py`'s own documented workflow (its docstring, step 3
after any prompt edit) is the bare invocation, and until this fix `main()` filtered only on
explicitly named slugs: `is_interactive(r)` was consulted only for the printed cost ceiling, never
for inclusion, so anyone following the script's own instructions after editing `engine.md` paid the
full single-pass cost plus 15 calls per interactive request in `requests.md`, silently.

`select_runs()` is the selection logic pulled out of `main()`: which parsed requests to actually
capture, and which interactive ones were skipped and why. It is a pure function over parsed request
dicts -- no client, no network, no disk write -- so the selection is provable without spending
anything.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import golden_run  # noqa: E402
from golden_run import select_runs  # noqa: E402


def _req(slug: str, answers: dict | None = None) -> dict:
    return {"slug": slug, "request": f"request for {slug}", "answers": answers or {}}


_SINGLE = _req("single-pass-a")
_INTERACTIVE = _req("interactive-a", answers={"problem": ["layer one"]})


# ── must-fire / must-not-fire pairs ─────────────────────────────────────────────────────────────

def test_a_bare_invocation_skips_every_interactive_request():
    """must fire -- the defect this issue exists to close: a bare run used to include everything."""
    selected, skipped = select_runs([_SINGLE, _INTERACTIVE], wanted=set(), capture_all=False)
    assert selected == [_SINGLE]
    assert skipped == [_INTERACTIVE]


def test_a_bare_invocation_with_no_interactive_requests_captures_everything():
    """must not fire -- the positive control: a set with nothing interactive in it must not lose a
    single-pass request to the same filter."""
    selected, skipped = select_runs([_SINGLE], wanted=set(), capture_all=False)
    assert selected == [_SINGLE]
    assert skipped == []


def test_naming_an_interactive_slug_explicitly_still_captures_it():
    """must not fire -- acceptance criterion: naming the slug is still today's behaviour."""
    selected, skipped = select_runs([_SINGLE, _INTERACTIVE], wanted={"interactive-a"},
                                    capture_all=False)
    assert selected == [_INTERACTIVE]
    assert skipped == []


def test_all_flag_captures_every_interactive_request_in_a_full_set_run():
    """must not fire, given --all -- the documented opt-in for a full-set run that really does want
    everything, cost and all."""
    selected, skipped = select_runs([_SINGLE, _INTERACTIVE], wanted=set(), capture_all=True)
    assert selected == [_SINGLE, _INTERACTIVE]
    assert skipped == []


def test_naming_an_explicit_slug_ignores_the_all_flag_and_the_interactive_skip_alike():
    """An explicit slug list is exhaustive on its own -- neither the default skip nor --all changes
    what an explicitly named request set selects."""
    selected, skipped = select_runs([_SINGLE, _INTERACTIVE], wanted={"single-pass-a"},
                                    capture_all=True)
    assert selected == [_SINGLE]
    assert skipped == []


# ── main() actually uses the selection and reports what it skipped ─────────────────────────────

def test_main_prints_which_interactive_requests_it_skipped_and_how_to_capture_them(
        tmp_path, monkeypatch, capsys):
    """`main()` with the client and capture loop stubbed out -- no key, no call, no write -- proves
    the skip is reported, not silent: a run that skipped a request and a run where that request does
    not exist must not print the same thing (see #276's own three-state rule)."""
    captured_slugs: list[str] = []
    monkeypatch.setattr(golden_run, "GOLDEN", tmp_path)
    monkeypatch.setattr(golden_run, "REPO", tmp_path.parent)
    monkeypatch.setattr(golden_run, "REQUESTS", tmp_path / "requests.md")
    (tmp_path / "requests.md").write_text("stub", encoding="utf-8")
    monkeypatch.setattr(golden_run, "Anthropic", lambda *a, **k: object())
    monkeypatch.setattr(golden_run, "parse_requests", lambda _path: [_SINGLE, _INTERACTIVE])
    monkeypatch.setattr(golden_run, "capture",
                        lambda _client, req, _with_brief: captured_slugs.append(req["slug"]))

    rc = golden_run.main([])

    assert rc == 0
    assert captured_slugs == ["single-pass-a"]
    err = capsys.readouterr().err
    assert "interactive-a" in err
    assert "golden_run.py interactive-a" in err, (
        "the skip line should name the exact command to capture the skipped request alone"
    )
