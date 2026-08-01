# Open-source strategy

This document explains how Requivo is distributed: what is open source today, what may become a
hosted service later, and what is deliberately kept private. It is a **product and distribution**
document, not a legal one — the code license is [MIT](../LICENSE), and the trademark boundary is in
[TRADEMARKS.md](../TRADEMARKS.md).

## Why open source

Requivo Core, the CLI, the Claude Code integration and the local Web interface are open source on
purpose. The goal is to make the requirements *model* portable, inspectable and usable locally —
without forcing anyone into a hosted service to get value from it. Open source serves adoption, trust,
local and self-hosted use, use inside Claude Code, contributions, and integration into other workflows.

The main risk for a project at this stage is not that a competitor copies the code. The code is
generic; the reasoning lives in prompts and context that anyone could rewrite. The main risk is that
**no one actually uses the product**. Openness is the cheapest way to reduce that risk.

The durable proprietary value, if any, is expected to come not from the generic engine but from
**hosting, collaboration, operation, and the learnings drawn from real-world usage** — see
*Requivo Lab* and *Requivo Cloud* below.

## The surfaces

Requivo is one engine behind several surfaces. The layering is described in
[architecture.md](architecture.md); this section maps each surface to how it is distributed.

### Requivo Core — open source

The deterministic requirements-reasoning engine (`requivo.core`). No LLM call, no I/O, no argv. It
holds:

- the model schema and typed slots;
- validation and the slot vocabulary;
- the session model and its versioned, deterministic on-disk format;
- readiness and completeness computation;
- design decisions and their provenance;
- the dependency graph, impact propagation, and diff;
- local persistence and revision history.

### Requivo CLI — open source

The local `requivo` / `pc` command. Initialise a session, validate a model, apply an update, compute
impact, manage and regenerate artifacts, import and export sessions. Runs entirely on the user's
machine.

### Requivo for Claude Code — open source

The plugin and skills under `plugins/claude-code/`. They drive the engine using **the user's own
Claude** inside Claude Code. This surface does **not** require a separate Anthropic API key: Claude
does the reasoning, the deterministic CLI applies the result.

### Requivo Web — open source, self-hostable (shipping)

The local web interface (`requivo web`, the optional `[web]` extra), under `src/requivo/web/`. Submit a
request, answer the engine's questions, review the model, generate a solution assessment or a PRD,
view and export artifacts. It is a thin FastAPI + HTMX layer over the same Core and services as every
other surface — **local, single-user, filesystem-backed, no authentication, no database, no remote
storage.** That boundary is exactly what keeps it distinct from Requivo Cloud (below). See
[web.md](web.md).

### Requivo Cloud — future, potentially proprietary, not in this repository

A future *hosted* offering that may provide managed storage, teams and organisations, governance,
collaboration, LLM credits, operational security, administration and enterprise features. **No
commercial hosted offering is currently promised or available.** None of the Cloud backend is built
in this repository, and the roadmap mentioning Requivo Cloud should not be read as a commitment to
publish that backend here.

### Requivo Lab — private

A private environment for evaluation data, experiments, real-world cases, proprietary learnings and
internal metrics. This is where the accumulated understanding of *what a good discovery run looks
like* lives. Nothing in Lab is published automatically (see the data boundary below).

## The community / cloud / lab boundary

| Concern | Community (public, MIT) | Cloud (future, private) | Lab (private) |
|---|---|---|---|
| Reasoning engine, schema, validation | ✅ | — | — |
| CLI, Claude Code plugin, local Web, generic providers | ✅ | — | — |
| Generic prompts & generic context cards | ✅ | — | — |
| Renderers, GitHub/GitLab exports | ✅ | — | — |
| Anonymised examples, golden harness | ✅ | — | — |
| Auth, accounts, multi-tenant, roles | — | ✅ | — |
| Billing, LLM credits, managed storage | — | ✅ | — |
| Collaboration, comments, sync, admin | — | ✅ | — |
| Rate limiting, anti-abuse, prod observability | — | ✅ | — |
| Real user requests & customer data | — | — | ✅ |
| Company-specific context cards | — | — | ✅ |
| Full evaluation corpus, human annotations | — | — | ✅ |
| Prompt experiments tied to confidential data | — | — | ✅ |
| Internal product metrics & learnings | — | — | ✅ |

Fully synthetic or properly anonymised examples may stay public to document and test the product.

## Data: what may be public, what stays private

**May be public** — synthetic cases, generalised cases, strongly anonymised requests, aggregated
results, minimal tests, generic failures, benchmarks reproducible without customer data.

**Must stay private** — a client's original text, identifiable content, a full user session, an
organisation's context card, a company's decision history, account-linked metrics, internal
annotations, data used to train or evaluate the system without authorisation, and experimental
prompts associated with confidential data.

## Repository layout

```text
requivo/          public repository (this one) — Core, CLI, Claude Code, local Web, generic assets
requivo-cloud/    private repository — hosted service backend
requivo-lab/      private repository — evaluation data, experiments, learnings
```

The `requivo-cloud` and `requivo-lab` repositories exist and are private; both are currently empty
placeholders that materialise the open-core boundary — no code has been split out of the public
repository yet. No Git submodules link them, and no GitHub organisation is used.

## License and trademark

The code is MIT ([LICENSE](../LICENSE)) and stays MIT — including the local Web interface. A move to a
different license (e.g. AGPL) would be a separate, explicit decision, never automatic; none is planned.
The **Requivo name and identity** are separate from the code license; see
[TRADEMARKS.md](../TRADEMARKS.md).
