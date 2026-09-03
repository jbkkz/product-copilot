"""#257's own guard: the measured per-card byte/token cost stated in `docs/context-cards.md` and
printed by the CLI's default-cards disclosure must agree with the actual bundled cards on disk.

A number in prose that no test can falsify buys one release and then lies (CLAUDE.md's own rule,
stated about a different count) -- this file recomputes the figure from the real files rather than
trusting a literal anyone could forget to update after adding or resizing a card.
"""
import re
from pathlib import Path

from requivo.core.context import available_cards
from requivo.paths import CONTEXT

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _bundled_card_sizes() -> dict[str, int]:
    return {
        p.stem: p.stat().st_size
        for p in sorted(CONTEXT.glob("*.md"))
        if not p.name.startswith("_")
    }


def test_the_docs_stated_bundled_card_byte_total_matches_the_files_on_disk():
    sizes = _bundled_card_sizes()
    assert sizes, "no bundled context cards found -- this test is not exercising anything"
    total = sum(sizes.values())
    doc = (_REPO_ROOT / "docs" / "context-cards.md").read_text(encoding="utf-8")
    m = re.search(r"([\d,]+) bytes, ~[\d.]+k tokens", doc)
    assert m, ("docs/context-cards.md no longer states a 'N bytes, ~Xk tokens' figure for the "
               "bundled cards -- update this test's pattern if the wording moved.")
    documented = int(m.group(1).replace(",", ""))
    assert documented == total, (
        f"docs/context-cards.md says {documented} bytes for the bundled cards; the real total is "
        f"{total} from {sizes}. A card was added, removed or resized -- re-measure and update the "
        "doc (and the CLI/web disclosure text, if the count of cards changed).")


def test_the_docs_stated_bundled_card_count_matches_available_cards():
    # `available_cards()` includes any user-installed cards too, so in an ordinary dev environment
    # (no REQUIVO_CONTEXT_DIR cards) it is exactly the bundled set -- the same set the CLI's default
    # disclosure enumerates. Must fire: an empty set would make every assertion above vacuous.
    cards = available_cards()
    sizes = _bundled_card_sizes()
    assert len(cards) >= len(sizes) >= 1
