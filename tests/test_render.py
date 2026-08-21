"""The renderers: data → string, no side effects.

Split out of `test_engine.py` (#72). `render/` is the layer that turns a contract into Markdown or
terminal output, so every test here builds the contract by hand and reads the text back. No provider,
no session, no filesystem — the tracker adapters that share the `Epic` contract live next door in
`test_adapters.py`, because they transform the neutral export rather than render it.
"""
import io
from contextlib import redirect_stdout

from _fakes import out, slot

from requivo.core.contracts import (
    PRD,
    AcceptanceCriteria,
    Brief,
    Challenge,
    DesignDecision,
    Epic,
    Leverage,
    Opportunity,
    ReleaseNotes,
)
from requivo.render.markdown import criteria_markdown, epic_markdown, prd_markdown, release_markdown
from requivo.render.terminal import render_brief


def test_prd_markdown_renders_title_and_requirement_table():
    prd = PRD(
        title="Leave approval",
        problem="Approvals are lost in email.",
        requirements=[{"id": "FR-1", "requirement": "Submit a request", "priority": "must"}],
    )
    md = prd_markdown(prd)
    assert md.startswith("# Leave approval")
    assert "| FR-1 | Submit a request | Must |" in md


def test_prd_markdown_escapes_pipes_in_table_cells():
    # A requirement containing a literal | would otherwise split the Markdown table row.
    prd = PRD(title="X", problem="P", requirements=[
        {"id": "FR-1", "requirement": "Export as CSV | XLSX | PDF", "priority": "must"}])
    md = prd_markdown(prd)
    assert "| FR-1 | Export as CSV \\| XLSX \\| PDF | Must |" in md


def test_criteria_markdown_renders_gherkin_checklist():
    ac = AcceptanceCriteria(
        title="Leave approval",
        features=[
            {
                "name": "Submitting a request",
                "scenarios": [
                    {
                        "id": "AC-1",
                        "title": "Valid request is accepted",
                        "kind": "happy_path",
                        "given": ["the employee is logged in", "they have enough balance"],
                        "when": "they submit a 3-day request",
                        "then": ["the request is created", "the manager is notified"],
                    }
                ],
            }
        ],
        open_questions=["Can a manager approve their own request?"],
    )
    md = criteria_markdown(ac)
    assert md.startswith("# Leave approval")
    assert "### [ ] AC-1 — Valid request is accepted  _Happy path_" in md
    # First given is "Given", subsequent ones fold to "And"; likewise Then → And.
    assert "- **Given** the employee is logged in" in md
    assert "- **And** they have enough balance" in md
    assert "- **When** they submit a 3-day request" in md
    assert "- **Then** the request is created" in md
    assert "- **And** the manager is notified" in md
    assert "## Open questions" in md


def test_epic_markdown_renders_issues_with_labels_and_deps():
    epic = Epic(
        title="Leave approval",
        milestone="Pilot",
        goal="Let employees request leave and managers approve it.",
        issues=[
            {"id": "#1", "title": "Model the leave object", "description": "Fields and states.",
             "labels": ["backend"]},
            {"id": "#2", "title": "Build approval circuit", "description": "Route to manager.",
             "labels": ["feature", "backend"], "depends_on": ["#1"]},
        ],
        open_questions=["Half-day support?"],
    )
    md = epic_markdown(epic)
    assert md.startswith("# Epic: Leave approval")
    assert "**Milestone:** Pilot" in md
    assert "### [ ] #1 — Model the leave object" in md
    assert "**Labels:** `feature`, `backend` · **Depends on:** #1" in md
    assert "## Open questions" in md


def test_release_markdown_stamps_version_and_sections():
    rn = ReleaseNotes(
        title="Leave approval",
        version="v1.0",
        summary="Your team can now request and approve leave online.",
        highlights=["Submit a request in a few clicks"],
        known_limitations=["Payroll export is not included yet"],
        notes=["An administrator sets the approval circuit first"],
    )
    md = release_markdown(rn)
    assert md.startswith("# Leave approval — v1.0")
    assert "Your team can now request and approve leave online." in md
    assert "## What's new" in md
    assert "## Not included yet" in md
    assert "## Before you start" in md


def test_release_markdown_omits_version_when_empty():
    md = release_markdown(ReleaseNotes(title="Leave approval", highlights=["A"]))
    assert md.startswith("# Leave approval\n")
    assert "—" not in md.splitlines()[0]


def test_render_brief_titles_solution_assessment_and_shows_challenges():
    model = {"problem": slot(80, "explicit", "high")}
    brief = Brief(
        problem="P",
        solution="S",
        complexity="high",
        decisions=[
            DesignDecision(
                decision="Draft-first invoices reviewed before issuance",
                why="Finance sign-off is required.",
                alternative="Immediate issuance.",
                tradeoff="Extra step, lower compliance risk.",
            ),
            DesignDecision(decision="Amount sourced from the Contract"),  # bare fact, no fork
        ],
        challenges=[
            Challenge(
                headline="Invoice at signature",
                premise="Invoices are generated the moment a contract is signed.",
                alternative="Many teams invoice at the contract start date or on a billing schedule.",
                consequence="Signature-triggered invoicing multiplies credit-note handling.",
                recommendation="Validate the billing trigger with Finance first.",
            )
        ],
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_brief(out(model), brief)
    text = buf.getvalue()
    assert "SOLUTION ASSESSMENT" in text and "DISCOVERY BRIEF" not in text
    assert "CHALLENGES" in text
    # the top challenge surfaces in the executive summary, detail in the full analysis
    assert "Challenge Invoice at signature" in text
    assert "⚑ Invoice at signature" in text  # full-analysis section
    assert "Premise" in text and "Alternative" in text and "Recommend" in text
    # Design decisions: the forked one shows its reasoning, the bare fact stays a single line.
    assert "DESIGN DECISIONS" in text and "DECISION LOG" not in text
    assert "✓ Draft-first invoices reviewed before issuance" in text
    assert "Why" in text and "Tradeoff" in text
    assert "✓ Amount sourced from the Contract" in text


def test_render_brief_opportunity_names_reached_modules():
    model = {"problem": slot(80, "explicit", "high")}
    brief = Brief(
        problem="P",
        solution="S",
        complexity="high",
        opportunities=[
            Opportunity(
                text="Generalize the approval circuit.",
                leverage=Leverage.high,
                modules=["Absence", "Contracts", "Missions"],
            ),
            Opportunity(text="Add a dashboard later.", leverage=Leverage.future),  # no modules
        ],
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_brief(out(model), brief)
    text = buf.getvalue()
    # a grounded opportunity names the modules it reaches; an ungrounded one shows no ↳ line
    assert "↳ reaches: Absence, Contracts, Missions" in text
    assert "Add a dashboard later." in text
    assert text.count("↳ reaches:") == 1
