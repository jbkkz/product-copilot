"""Every image a document references resolves to a file in this repository.

A broken image is the one docs defect that is invisible to the person who introduced it: the README
renders on GitHub *and* on PyPI, both from URLs, and a missing file shows as an empty frame on a page
the author is not looking at. There is no import to fail and no link checker in the required CI legs.

Two forms, because the README and `docs/` cannot use the same one. `pyproject` sets
`readme = README.md`, so PyPI renders that file and does not rewrite relative hrefs -- every image
there has to be an absolute `raw.githubusercontent.com` URL, which is unverifiable as a URL and
entirely verifiable as the repository path inside it. Pages under `docs/` are only ever read on
GitHub, so they use relative paths and are checked as paths (#224).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RAW = "https://raw.githubusercontent.com/jbkkz/requivo/main/"
# `![alt](path)` and the `[ref]: url` definition an `![alt][ref]` resolves through.
INLINE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
REFDEF = re.compile(r"^\[([^\]]+)\]:\s*(\S+)\s*$", re.MULTILINE)
IMAGE_SUFFIXES = {".webp", ".png", ".jpg", ".jpeg", ".gif", ".svg"}


def _doc_pages():
    return [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))]


def test_every_readme_image_hosted_from_this_repo_names_a_file_that_exists():
    """Only the repository's own images. A shields.io badge and the Actions status SVG are images
    too, and neither is a file anyone here can check."""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    urls = [u for _, u in REFDEF.findall(text) if Path(u).suffix.lower() in IMAGE_SUFFIXES]
    urls += INLINE.findall(text)
    ours = [u for u in urls if u.startswith(RAW)]
    assert ours, "the README references none of this repository's own images"
    for u in ours:
        assert (REPO / u[len(RAW):]).is_file(), f"{u} names no file in this repository"


def test_no_readme_image_is_relative():
    """`pyproject` sets `readme = README.md`, so PyPI renders this file verbatim and does not rewrite
    relative hrefs. A relative image renders on GitHub and 404s on the project page -- half the
    audience, and the half deciding whether to install."""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    candidates = [u for _, u in REFDEF.findall(text) if Path(u).suffix.lower() in IMAGE_SUFFIXES]
    candidates += INLINE.findall(text)
    for u in candidates:
        assert u.startswith("http"), f"README image {u!r} is relative and will 404 on PyPI"


@pytest.mark.parametrize("page", _doc_pages(), ids=lambda p: p.name)
def test_every_relative_image_path_in_a_doc_page_resolves(page):
    text = page.read_text(encoding="utf-8")
    for rel in INLINE.findall(text):
        if rel.startswith("http") or rel.startswith("#"):
            continue
        assert (page.parent / rel).is_file(), f"{page.name} references {rel}, which does not exist"
