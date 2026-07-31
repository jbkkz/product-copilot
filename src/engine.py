"""Backward-compat shim.

The engine lives in the product_copilot package now; this module re-exports its
public surface so `python src/engine.py ...` and `from src.engine import ...` keep
working. New code should import from product_copilot directly.
"""

from dotenv import load_dotenv

load_dotenv()

from product_copilot.cli import MAX_TURNS, _flag_value, converse, main
from product_copilot.core.adapters import (
    EPIC_EXPORT_FORMAT,
    EPIC_EXPORT_VERSION,
    epic_export,
    epic_export_json,
    to_github,
    to_github_json,
    to_gitlab,
    to_gitlab_json,
)
from product_copilot.core.analysis import (
    _is_deferred,
    _label,
    _readiness_blockers,
    _slot_meta,
    _state_of,
    estimate_confidence,
    soft_slots,
)
from product_copilot.core.contracts import (
    PRD,
    SOFT_COMPLETENESS,
    AcceptanceCriteria,
    Brief,
    Challenge,
    Complexity,
    Confidence,
    DesignDecision,
    EngineOutput,
    Epic,
    EpicIssue,
    EstimateDraft,
    EstimateItem,
    Feature,
    Impact,
    Level,
    Leverage,
    Opportunity,
    Priority,
    Question,
    ReleaseNotes,
    Requirement,
    Scenario,
    ScenarioKind,
    Slot,
    Stories,
    Story,
    Summary,
)
from product_copilot.core.discovery import run
from product_copilot.core.generators import (
    advise,
    derive_stories,
    estimate,
    generate_criteria,
    generate_epic,
    generate_prd,
    generate_release,
)
from product_copilot.core.llm import _complete, _extract_json, _response_text, build_prompt, load_context
from product_copilot.core.persistence import _slug, load_model, save_model, write_artifact
from product_copilot.render.markdown import _KIND_TAG, criteria_markdown, epic_markdown, prd_markdown, release_markdown
from product_copilot.render.terminal import (
    STATE_ROWS,
    _bullet,
    _labeled,
    _wrap,
    render_brief,
    render_estimate,
    render_readiness,
    render_stories,
    render_turn,
    render_understanding,
)

if __name__ == "__main__":
    main()
