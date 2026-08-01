# Examples

These examples show Requivo end to end: a request → a saved `model.json` → the artifacts generated
from it (assessment, PRD, acceptance criteria, epic, exports, release notes). They double as
documentation and as inputs the tests and golden harness rely on.

## What's here

- **`leave-approval/`** — a one-line request taken through the full artifact chain.
- **`event-checkin-reconciliation/`** — a messy, multi-feature client-style email, and the
  assessment that refuses to conflate its distinct problems (the engine's core differentiator).
- **`case1_leave.md` … `case6_freelancer_payment.md`** — short standalone request prompts.

## Every public example must be synthetic or properly anonymised

These are illustrative, invented scenarios. Anything committed here **must** be free of:

- real client or company names;
- real email addresses, identifiers, or account references;
- confidential business rules, pricing, or commercial data;
- secrets, API keys, or credentials;
- personal data of real individuals.

**Do not commit a real user session, request, or context card** — even one that "looks generic".
Real-world material belongs in a private workspace (or, for cards, `REQUIVO_CONTEXT_DIR`), never in
this repository. See the data boundary in
[../docs/open-source-strategy.md](../docs/open-source-strategy.md#data-what-may-be-public-what-stays-private).

If you want to *report* how the engine did on a real request, use the **Real-world discovery
feedback** issue template, which asks only for anonymised information.

## Contributing an example safely

1. Invent a scenario, or fully anonymise a real one — change names, numbers, domains, and any detail
   that could identify a real party. Removing the name is not enough if the surrounding facts still
   identify the source.
2. Keep it small and focused on one requirements *form* (the golden request set follows the same
   rule).
3. If it includes a generated model or artifacts, generate them from the anonymised request so
   nothing confidential leaks through the model.
4. Open a PR and confirm in the description that the example contains no confidential data.
