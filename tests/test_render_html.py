"""Markdown → HTML for the web's artifact page: what it renders, and what it refuses to (#235).

The decision brief is this product's stated primary deliverable, and the web served it as literal
`# Decision Brief` and `**Objective:**` inside a monospace code block — Markdown source handed to the
one audience the web vocabulary exists for, at the exact moment the product delivers its value.

Its own file rather than a section of `test_render.py`, because half of what is asserted here is a
*security* property and the other half is formatting, and among eight tests about Markdown output an
injection test that stops being collected looks exactly like one that passes.

**The dialect is closed on purpose.** These documents are written by `render/markdown.py`, in this
repository, so the constructs are known: three heading levels, a blockquote, bullets one level deep,
ordered items, a pipe table, and `**bold**` / `_italic_` / `` `code` `` inline. A general Markdown
library would parse a superset — and would parse it over text a language model wrote and a user can
edit on disk. Anything outside the dialect is rendered as escaped text, which is the same thing the
`<pre>` block did and no worse than where this started.

A checkbox marker is deliberately outside that list even though the writers emit one; `render/html.py`
says why, and `test_a_checkbox_marker_renders_as_text_and_not_as_an_input` is where that decision is
held. This file used to list "checkbox items" as part of the dialect while the module it tests did
not and never implemented one — the two-docstrings-disagreeing form of the same defect class
everything else here is about, written into the very commit that added both.

**Escaping is structural, not a scrub.** Every tag in the output is one this module constructed; every
byte that came from the document went through `html.escape` first. That is why there is no separate
sanitizer to keep up to date, and why the injection tests below are about a property rather than
about a blocklist.
"""

from __future__ import annotations

import re

from requivo.render.html import markdown_to_html

# Anything a browser would run, fetch or lay out from bytes it did not choose.
_LIVE_MARKUP = re.compile(r"<\s*(script|iframe|object|embed|style|img|svg)\b", re.I)


def md(*lines: str) -> str:
    """A document as its lines, so the fixtures read like the Markdown they stand for."""
    return "\n".join(lines) + "\n"


# ── the dialect the generators emit ───────────────────────────────────────────

def test_headings_become_headings():
    html = markdown_to_html(md("# Decision Brief", "", "## Main risks", "", "### A challenge"))
    assert "<h1>Decision Brief</h1>" in html
    assert "<h2>Main risks</h2>" in html
    assert "<h3>A challenge</h3>" in html
    assert "#" not in html, "a heading marker reached the reader"


def test_inline_emphasis_becomes_emphasis():
    html = markdown_to_html(md("**Objective:** ship it, _eventually_, via `requivo prd`"))
    assert "<strong>Objective:</strong>" in html
    assert "<em>eventually</em>" in html
    assert "<code>requivo prd</code>" in html
    assert "**" not in html and "`" not in html


def test_a_marker_inside_a_code_span_is_shown_rather_than_obeyed():
    """A backtick span is the one place these markers are meant to be seen, so the passes must not run
    over each other's output.

    Three separate substitutions did: the code pass wrapped the span, and the bold pass then marked up
    what was inside it, so the reader was shown emphasis where the author had written the characters.
    One left-to-right pass over an alternation consumes each match whole, which is what makes
    "code first" mean anything.
    """
    html = markdown_to_html(md("Write `**not bold**` and `_not italic_` literally."))
    assert "<code>**not bold**</code>" in html
    assert "<code>_not italic_</code>" in html
    assert "<strong>" not in html and "<em>" not in html


def test_an_underscore_inside_a_word_is_not_emphasis():
    """`business_rules` and `config_vs_custom` appear in this product's own prose. Reading that
    underscore as emphasis would swallow the rest of the paragraph into an `<em>` and silently drop
    two characters the reader wrote."""
    html = markdown_to_html(md("The slot business_rules feeds config_vs_custom."))
    assert "business_rules" in html and "config_vs_custom" in html
    assert "<em>" not in html


def test_bullets_nest_one_level():
    html = markdown_to_html(md("- **A decision** — because",
                               "  - _Alternative weighed:_ the other one"))
    assert html.count("<ul>") == 2 and html.count("</ul>") == 2
    assert "<em>Alternative weighed:</em>" in html


def test_a_line_break_inside_a_paragraph_is_kept():
    """These documents never wrap, so consecutive lines are consecutive facts.

    A general Markdown renderer folds a soft break into a space, and that is right for prose flowed
    across a column. `render/markdown.py` emits one source line per value, so folding turned the
    decision brief's opening block — objective, problem, solution, complexity, cost driver — into one
    run-on paragraph, on the most-read part of the primary deliverable, where the code block this
    replaced had shown five lines.
    """
    html = markdown_to_html(md("**Objective:** ship it",
                               "**Problem:** it is not shipped",
                               "**Complexity:** medium"))
    assert html.count("<p>") == 1, "the lines are one paragraph, not three"
    assert html.count("<br>") == 2
    assert "ship it<br><strong>Problem:</strong>" in html, (
        "the break has to sit between the facts, not be dropped: " + html)


def test_a_blank_line_still_starts_a_new_paragraph():
    """Must fire for the test above. Turning every break into a `<br>` would also be satisfied by a
    renderer that emitted one giant paragraph for the whole document."""
    html = markdown_to_html(md("First fact.", "", "Second fact."))
    assert html.count("<p>") == 2 and "<br>" not in html


def test_a_blockquote_and_a_paragraph_are_told_apart():
    html = markdown_to_html(md("> generated by Requivo", "", "A plain paragraph."))
    assert "<blockquote>" in html and "generated by Requivo" in html
    assert "<p>A plain paragraph.</p>" in html
    assert "&gt;" not in html, "the blockquote marker was escaped instead of understood"


def test_a_requirements_table_becomes_a_table():
    html = markdown_to_html(md("| ID | Requirement | Priority |",
                               "|----|-------------|----------|",
                               "| R-1 | Managers approve leave | Must |"))
    assert "<table>" in html and "<th>Requirement</th>" in html
    assert "<td>Managers approve leave</td>" in html
    assert "|" not in html, "a table delimiter reached the reader"


def test_an_escaped_pipe_comes_back_as_a_pipe():
    """`_cell()` writes a literal pipe as a backslash-pipe so it cannot close the cell. Rendering that
    verbatim would show the reader an escape they never wrote."""
    html = markdown_to_html(md("| ID | Requirement |", "|----|----|",
                               r"| R-1 | approve \| reject |"))
    assert "approve | reject" in html


def test_a_pipe_block_with_no_header_degrades_instead_of_crashing():
    """The dialect's floor is *escaped text*, never an exception.

    A block of nothing but rule-shaped lines — a lone `|---|---|`, which a hand-edited artifact file
    can easily carry — used to leave the table renderer with nothing to unpack, and the `ValueError`
    went straight past every handler `create_app` registers, because it is not a `RequivoError`. The
    artifact page answered a bare 500: the one outcome worse than the code block this replaced, on a
    module whose docstring promises it can never be worse.
    """
    html = markdown_to_html(md("Some notes.", "", "|---|---|", "", "More notes."))

    assert "<table>" not in html, "a block with no header is not a table"
    assert "|---|---|" in _unescape(html), (
        "the block has to survive as text — dropping it is the same absence one step quieter")
    # must fire: the surrounding document is still rendered, so this is not asserting about a
    # renderer that gave up on the whole file.
    assert "<p>Some notes.</p>" in html and "<p>More notes.</p>" in html


def test_a_body_row_that_looks_like_a_rule_is_still_a_row():
    """A row is dropped only for being in the rule's *position*, never for its contents.

    The separator used to be recognised by pattern anywhere in the block, so a genuine data row whose
    cells held only dashes — a placeholder, an "n/a" written as `--` — matched it and vanished from
    the rendered table with nothing raised and nothing said. A requirements table quietly one row
    short is exactly the silent wrong answer this project is careful about everywhere else.
    """
    html = markdown_to_html(md("| ID | Priority |", "|----|----------|",
                               "| R-1 | Must |", "|--|--|", "| R-3 | Should |"))

    assert html.count("<tr>") == 4, (
        "header plus three body rows; a row went missing: " + html)
    assert "R-1" in html and "R-3" in html
    assert "<td>--</td>" in html, "the placeholder row has to render as data: " + html


def _unescape(html: str) -> str:
    """Entities back to characters, so a test can ask what the reader sees rather than how it is
    spelled on the wire."""
    return html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')


def test_a_checkbox_marker_renders_as_text_and_not_as_an_input():
    """`render/markdown.py` emits `- [ ] …` and `### [ ] …`, and both stay text on purpose.

    Rendering a real checkbox would mean an `<input>`, which is an element carrying at least two
    attributes — and "this renderer emits no attribute anywhere" is the property the artifact
    template's `| safe` leans on and that
    `test_an_attribute_break_out_cannot_reach_an_attribute` pins. A prettier checkbox is not worth
    trading that for, so the decision is written down here rather than left for someone to
    "fix" later.
    """
    html = markdown_to_html(md("### [ ] SC-1 — Manager approves", "", "- [ ] the request is approved"))

    assert "<input" not in html and "=" not in html.split("<h3>")[1], (
        "a checkbox brought an attribute into a renderer that has none: " + html)
    assert "[ ] SC-1 — Manager approves" in html
    assert "<li>[ ] the request is approved</li>" in html


def test_an_ordered_step_list_becomes_an_ordered_list():
    html = markdown_to_html(md("1. Request leave", "2. Manager approves"))
    assert "<ol>" in html and "<li>Manager approves</li>" in html


# ── what it refuses to render ─────────────────────────────────────────────────
# The content is written by a language model and lives in a file the user can edit, so it is
# untrusted input by both routes this repo already recognises (invariant 14). Autoescape is off for
# this string in the template — it has to be, or the tags below would be shown rather than applied —
# so the escaping has to be complete *here*, and these are the tests that say it is.

def test_a_script_tag_in_the_content_is_shown_rather_than_run():
    html = markdown_to_html(md("## Risks", "", "<script>alert(1)</script>"))
    assert not _LIVE_MARKUP.search(html), "live markup survived into the rendered document"
    assert "&lt;script&gt;" in html, "the tag was dropped instead of shown — that loses evidence"


def test_markup_smuggled_inside_every_construct_is_still_escaped():
    """One arm per block type, because escaping is applied per text run and a construct that builds
    its own tags is exactly where a run gets forgotten. A single-arm version of this test passed
    against an implementation that escaped paragraphs and nothing else."""
    hostile = "<img src=x onerror=alert(1)>"
    document = md(
        "# " + hostile,
        "",
        "> " + hostile,
        "",
        "- " + hostile,
        "  - " + hostile,
        "",
        "1. " + hostile,
        "",
        "| ID | " + hostile + " |",
        "|----|----|",
        "| R-1 | " + hostile + " |",
        "",
        "**" + hostile + "** and `" + hostile + "` and _" + hostile + "_",
    )
    html = markdown_to_html(document)
    assert not _LIVE_MARKUP.search(html), (
        "a construct rendered attacker-chosen markup live: " + html)
    # Heading, blockquote, bullet, nested bullet, ordered item, table header cell, table body cell,
    # bold, code, italic.
    assert html.count("&lt;img") == 10, (
        "every one of the ten hostile runs has to survive as visible text — a count short means one "
        "construct dropped its run, which hides the tampering instead of showing it: " + html)


def test_an_attribute_break_out_cannot_reach_an_attribute():
    """There is no place in this output where document text lands inside an attribute, and this is
    what says so. A renderer that grew one — a heading anchor, a table `title` — would have to argue
    with this test rather than quietly open the hole."""
    html = markdown_to_html(md('# " onmouseover="alert(1)'))
    assert '="' not in html, (
        "this renderer emits no attribute anywhere, so document text has nowhere to break out of — "
        "a construct that grew one has to argue with this line: " + html)
    assert "&quot;" in html, "the quotes were dropped rather than escaped"


def test_no_inline_style_is_emitted():
    """The app's CSP is `style-src 'self'`, so an inline style is not merely untidy — it is blocked,
    and a page that logs a violation on every artifact view is a CSP nobody reads (the argument
    `base.html` already makes about htmx's indicator styles)."""
    html = markdown_to_html(md("# Title", "", "| a | b |", "|---|---|", "| 1 | 2 |",
                               "- one", "", "> quoted"))
    assert "style=" not in html


def test_an_unclosed_fence_does_not_swallow_the_document():
    """A construct outside the dialect degrades to escaped text — the same thing the `<pre>` block
    did, and never worse. What it must not do is consume everything after it.

    **No blank line before the heading, and that is the whole test.** The first version of this
    fixture had one, and a blank line already ends a paragraph on its own — so it passed identically
    with `_is_block_start` stubbed out to `False`, while citing that function as the thing it pinned.
    A reference that resolves and guards nothing is the defect CLAUDE.md names at invariant 13, and
    it was in a test written to demonstrate the opposite. `test_a_paragraph_stops_at_the_heading_that_follows_it`
    is the same claim on the case the docstring actually describes.
    """
    html = markdown_to_html(md("# Title", "", "```", "some code", "## Still rendered"))
    assert "<h1>Title</h1>" in html
    assert "<h2>Still rendered</h2>" in html, (
        "the heading was swallowed by the paragraph that ran up to it: " + html)


def test_a_paragraph_stops_at_the_heading_that_follows_it():
    """The hazard `_is_block_start` is actually written for: no blank line between prose and the
    block after it. Without the guard the paragraph loop runs on and the heading is rendered as words
    inside it, which is the failure mode nothing else in this file can see."""
    html = markdown_to_html(md("Some paragraph text.", "## A heading right after", "- and a bullet"))
    assert "<p>Some paragraph text.</p>" in html
    assert "<h2>A heading right after</h2>" in html
    assert "<li>and a bullet</li>" in html
