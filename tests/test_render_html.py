"""Markdown → HTML for the web's artifact page: what it renders, and what it refuses to (#235).

The decision brief is this product's stated primary deliverable, and the web served it as literal
`# Decision Brief` and `**Objective:**` inside a monospace code block — Markdown source handed to the
one audience the web vocabulary exists for, at the exact moment the product delivers its value.

Its own file rather than a section of `test_render.py`, because half of what is asserted here is a
*security* property and the other half is formatting, and among eight tests about Markdown output an
injection test that stops being collected looks exactly like one that passes.

**The dialect is closed on purpose.** These documents are written by `render/markdown.py`, in this
repository, so the constructs are known: three heading levels, a blockquote, bullets one level deep,
ordered items, checkbox items, a pipe table, and `**bold**` / `_italic_` / `` `code` `` inline. A
general Markdown library would parse a superset — and would parse it over text a language model
wrote and a user can edit on disk. Anything outside the dialect is rendered as escaped text, which is
the same thing the `<pre>` block did and no worse than where this started.

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
    did, and never worse. What it must not do is consume everything after it."""
    html = markdown_to_html(md("# Title", "", "```", "some code", "", "## Still rendered"))
    assert "<h1>Title</h1>" in html
    assert "<h2>Still rendered</h2>" in html
