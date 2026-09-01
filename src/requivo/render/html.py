"""The saved artifact Markdown, rendered as a document for the web's artifact page (#235).

The decision brief is this product's stated primary deliverable, and the web served it as literal
`# Decision Brief` and `**Objective:**` in a monospace code block — Markdown source handed to the one
audience the web vocabulary exists for, at the exact moment the product delivers its value.

**A closed dialect, not a Markdown parser.** These documents are written by `render/markdown.py`, in
this repository: three heading levels, a blockquote, bullets one level deep, ordered items, a pipe
table, and `**bold**` / `_italic_` / `` `code` `` inline. That is the whole vocabulary, and it is
enumerated below.

A checkbox marker is deliberately **not** in that list, and saying so is the point rather than an
omission: `render/markdown.py` really does emit `- [ ] …` bullets and `### [ ] …` headings, and they
render as an ordinary bullet or heading whose text happens to begin with two brackets. That reads
correctly and it keeps this renderer's one structural promise — it emits no attribute anywhere, which
is what the artifact template's `| safe` leans on, and a real checkbox is an `<input>` with at least
two. Pinned by `test_a_checkbox_marker_renders_as_text_and_not_as_an_input`.

A general library would parse a superset of it, over text a language model wrote
and a user can edit on disk — a larger attack surface and a new runtime dependency, bought to render
constructs nothing here emits. Anything outside the dialect degrades to escaped text, which is
exactly what the `<pre>` block did and never worse.

**Escaping is structural.** Every tag in the output is one this module wrote as a literal; every byte
that came from the document went through `html.escape` first, before any inline markup is applied to
it. So there is no sanitizer to keep in step with a parser, and no blocklist to be outrun: the
question "did we remember to escape this construct" is answered by `_inline`, which every text run
goes through, rather than per construct. `test_markup_smuggled_inside_every_construct_is_still_escaped`
is the guard, and it counts the surviving runs rather than looking for one — a count short means a
construct forgot one, which is the failure a single-arm test misses.

Nothing here emits a `style` attribute or lands document text inside any attribute at all. Both are
deliberate and both are pinned (`test_no_inline_style_is_emitted`,
`test_an_attribute_break_out_cannot_reach_an_attribute`): the app's CSP is `style-src 'self'`, and a
page that logs a violation on every view is a CSP nobody reads.

No new dependency. `html.escape` is the standard library, and this module holds no vendor knowledge,
so it sits in `render/` beside its Markdown sibling rather than under `web/`.
"""

from __future__ import annotations

import re
from html import escape

_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+\.\s+(.*)$")
_TABLE_RULE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

# The inline markers, as **one** alternation matched in a single left-to-right pass. Three separate
# `sub` calls would run over each other's output: a code span survives the first pass and then has
# its contents marked up by the second, so `` `**x**` `` — a backtick span being the one place those
# markers are meant to be *shown* — came out as bold. One pass consumes each match whole, which makes
# the code-first ordering below mean what it says. Pinned by
# `test_a_marker_inside_a_code_span_is_shown_rather_than_obeyed`.
#
# Applied to text that is *already* escaped, so `<` is `&lt;` by the time any of these look at it and
# none can produce a tag from document bytes.
#
# The underscore arm only opens emphasis at a word boundary. `business_rules` and `config_vs_custom`
# are this project's own slot ids and appear in its own prose; reading their underscores as emphasis
# swallows the rest of the paragraph into an `<em>` and drops two characters the author wrote.
# Pinned by `test_an_underscore_inside_a_word_is_not_emphasis`.
_INLINE_MARKUP = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|\*\*(?P<bold>\S(?:[^*]*\S)?)\*\*"
    r"|(?<![\w`])_(?P<italic>\S(?:[^_]*\S)?)_(?![\w`])"
)
_INLINE_TAGS = {"code": "code", "bold": "strong", "italic": "em"}

# A pipe that is not backslash-escaped ends a table cell. `render/markdown.py`'s `_cell()` writes a
# literal pipe as a backslash-pipe for exactly that reason, so the split has to honour the escape and
# then undo it — the escape is the writer's, and a reader who never typed it should not see it.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def _inline(text: str) -> str:
    """One run of document text, escaped and then marked up.

    **The order is the security property.** `escape` runs first, so every `<`, `>`, `&` and `"` the
    document carried is already an entity by the time the patterns below look at the string — none of
    them can build a tag out of document bytes, and none of them needs to know what a dangerous tag
    is. Every text run in this module goes through here, which is what makes "did we escape that
    construct" one question rather than one question per block type.
    """
    def tag(match: re.Match) -> str:
        name = match.lastgroup
        # Every alternation branch of `_INLINE_MARKUP` is a *named* group, so exactly one of them
        # participates in any match reaching here and `lastgroup` is never None. That fact lives in
        # the pattern above, not here, which is what makes an assert worth its line: an edit adding
        # an unnamed branch would otherwise turn this into a `KeyError` out of a renderer whose
        # module promises that anything outside the dialect degrades to escaped text and never
        # worse (#393).
        #
        # **Two different things hold this, and saying "pinned by" once would overstate one of
        # them** -- found in review of this change, which is the whole reason it is spelled out.
        # `test_every_inline_markup_branch_is_a_named_group_with_a_tag` pins the *invariant*: it
        # compares the pattern's own `groups` against `groupindex`, so it goes red on the unnamed
        # branch and would go red with or without this line. What holds *this line* is the pyright
        # leg -- `render/` is inside `[tool.pyright]`'s `include` since #393, and deleting the
        # assert puts the four `str | None` diagnostics back. Neither guard covers `python -O`,
        # where an assert is compiled out; nothing here runs under it, and the pre-#393 baseline
        # had no runtime check at all, so `-O` is that baseline rather than a regression.
        #
        # This and the one in `_list_items` are the **first two `assert`s in `src/requivo/`** —
        # measured at the base commit, not assumed. Said out loud because it is a precedent rather
        # than a local choice: #393 asked for a narrowing that *documents* the invariant it rests
        # on, and an assert is the only option that survives an edit to the pattern, where a
        # `# type: ignore` preserves nothing.
        assert name is not None, f"an unnamed _INLINE_MARKUP branch matched {match.group(0)!r}"
        return f"<{_INLINE_TAGS[name]}>{match.group(name)}</{_INLINE_TAGS[name]}>"

    return _INLINE_MARKUP.sub(tag, escape(text, quote=True))


def _cells(line: str) -> list[str]:
    """One table row's cells, with the writer's pipe escape undone.

    Pinned by `test_an_escaped_pipe_comes_back_as_a_pipe`.
    """
    body = line.strip().strip("|")
    return [part.strip().replace("\\|", "|") for part in _UNESCAPED_PIPE.split(body)]


def _table(rows: list[str]) -> list[str]:
    """A pipe table: a header row, the rule row under it, then the body.

    **Positional, not content-matched, and both halves of that were wrong on their own.** This used
    to filter every rule-shaped line out of the block and unpack the rest, which failed in two
    opposite directions at once. A block whose lines were *all* rule-shaped left nothing to unpack
    and raised `ValueError` — out of a function whose module promises that anything outside the
    dialect degrades to escaped text and never worse, and straight past every handler `create_app`
    registers, since `ValueError` is not a `RequivoError`. The artifact page became a bare 500, which
    is the one outcome worse than the code block this renderer replaced. And in the other direction,
    a genuine *body* row whose cells held only dashes, colons and spaces matched the same pattern and
    was silently dropped: a row gone from a requirements table with nothing raised and nothing said.

    Reading the rule by position fixes both. The caller has already established `rows[1]` is the rule
    row, so this unpack is total — there is no input that reaches here and cannot be destructured.

    Pinned by `test_a_pipe_block_with_no_header_degrades_instead_of_crashing` and
    `test_a_body_row_that_looks_like_a_rule_is_still_a_row`.
    """
    head, _rule, *body = rows
    out = ["<table>", "<thead><tr>"]
    out += [f"<th>{_inline(c)}</th>" for c in _cells(head)]
    out += ["</tr></thead>", "<tbody>"]
    for row in body:
        out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in _cells(row)) + "</tr>")
    out += ["</tbody>", "</table>"]
    return out


def _list_items(lines: list[str], ordered: bool) -> list[str]:
    """A bullet or ordered list, nested at most one level deep — which is all the writers emit.

    Deeper indentation is folded into the second level rather than opening a third: a document that
    somehow carried one renders slightly flat, which is a formatting loss and not a correctness one.
    The alternative, a general nesting stack, is machinery for a case `render/markdown.py` cannot
    produce.
    """
    tag = "ol" if ordered else "ul"
    out = [f"<{tag}>"]
    nested = False
    for line in lines:
        bullet = _BULLET.match(line)
        if bullet:
            indent, text = len(bullet.group(1)), bullet.group(2)
        else:
            # `markdown_to_html` collects a line into `lines` only when `_BULLET` or `_ORDERED`
            # matched it, so a line that is not a bullet is an ordered item. The invariant is the
            # *caller's*, which is exactly why it is asserted rather than left implicit: this
            # function cannot see it, and a second caller handing it arbitrary lines got
            # `AttributeError: 'NoneType' object has no attribute 'group'`, naming neither the line
            # nor the rule it broke. Pinned by
            # `test_a_list_line_that_matches_neither_marker_is_refused_by_name`, which calls this
            # function directly and really does discriminate -- against the code before #393 it
            # gets the `AttributeError` rather than the named refusal. Compiled out under
            # `python -O` like any assert, which returns to the pre-#393 behaviour and does not go
            # below it (#393).
            item = _ORDERED.match(line)
            assert item is not None, f"a list item matched neither marker: {line!r}"
            indent, text = 0, item.group(1)
        if indent >= 2 and not nested:
            out.append(f"<{tag}>")
            nested = True
        elif indent < 2 and nested:
            out.append(f"</{tag}>")
            nested = False
        out.append(f"<li>{_inline(text)}</li>")
    if nested:
        out.append(f"</{tag}>")
    out.append(f"</{tag}>")
    return out


def markdown_to_html(text: str) -> str:
    """One saved artifact, as a document a reader can read.

    A block scanner over the closed dialect this module's docstring enumerates. Every branch either
    emits tags this function wrote as literals around text that went through `_inline`, or falls
    through to the paragraph arm, which does the same — so there is no path from document bytes to
    live markup, and no construct that has to be remembered separately.

    The output is a fragment, not a page: the template wraps it. It is passed through Jinja's `safe`,
    which is why the escaping above is not optional — `test_hostile_markup_in_a_saved_artifact_is_shown_not_executed`
    drives that whole path.
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            i += 1
            continue

        if line.lstrip().startswith("> "):
            quoted = []
            while i < len(lines) and lines[i].lstrip().startswith("> "):
                quoted.append(lines[i].lstrip()[2:])
                i += 1
            out.append("<blockquote>" + _inline(" ".join(quoted)) + "</blockquote>")
            continue

        if line.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            # A table is a header line, then a rule line, **in that order** — so the test is on
            # `rows[1]` and not on "is there a rule row anywhere in here". Asking the looser question
            # accepted a block with no header at all (a lone `|---|---|`, which a hand-edited file can
            # easily carry) and handed `_table` nothing to unpack. A block that is not shaped like a
            # table is prose that happens to open with a pipe, and prose degrades to escaped text —
            # which is what this module promises and what the code block it replaced already did.
            if len(rows) > 1 and _TABLE_RULE.match(rows[1]):
                out += _table(rows)
            else:
                out += [f"<p>{_inline(r)}</p>" for r in rows]
            continue

        if _BULLET.match(line) or _ORDERED.match(line):
            ordered = _ORDERED.match(line) is not None
            items = []
            while i < len(lines) and (
                    (_ORDERED.match(lines[i]) is not None) == ordered
                    and (_BULLET.match(lines[i]) or _ORDERED.match(lines[i]))):
                items.append(lines[i])
                i += 1
            out += _list_items(items, ordered)
            continue

        paragraph = []
        while i < len(lines) and lines[i].strip() and not _is_block_start(lines[i]):
            paragraph.append(lines[i].strip())
            i += 1
        # **A line break inside a paragraph is kept**, which is a deliberate departure from what a
        # general Markdown renderer does with a soft break — and the dialect is what justifies it.
        # `render/markdown.py` never wraps: it emits one source line per value, so consecutive lines
        # are consecutive *facts*, not one sentence flowed across a column. Joining them with a space
        # turned the decision brief's opening block — objective, problem, solution, complexity, main
        # cost driver — into a single run-on paragraph, on the most-read part of the primary
        # deliverable, where the code block it replaced had shown five lines. Measured on
        # `examples/leave-approval/`: one multi-line paragraph across all five generated documents,
        # and that is the one. Each line is escaped on its own, so the separator is a tag this
        # function wrote and never something a document can forge. Pinned by
        # `test_a_line_break_inside_a_paragraph_is_kept`.
        out.append("<p>" + "<br>".join(_inline(line) for line in paragraph) + "</p>")
    return "\n".join(out)


def _is_block_start(line: str) -> bool:
    """Whether a line opens a construct, so a paragraph stops before it rather than eating it.

    Without this a document with no blank line between a paragraph and the heading after it would
    lose the heading into the paragraph — and, on a fenced block, would swallow everything to the end
    of the file. Pinned by `test_an_unclosed_fence_does_not_swallow_the_document`.
    """
    stripped = line.lstrip()
    return bool(_HEADING.match(line) or _BULLET.match(line) or _ORDERED.match(line)
                or stripped.startswith(("> ", "|")))
