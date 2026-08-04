# Requivo

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> Find what could change the solution before you commit to the scope.

Paste a client or stakeholder request. Requivo identifies the assumptions and missing decisions that
could change the workflow, integrations, permissions, timeline or effort — then produces one brief you
can review before estimating.

**The shared understanding is the source of truth. Every document is generated from it.**

Built for Product Managers, Solutions Engineers and Business Analysts working on complex, configurable
B2B products.

---

## Why Requivo

An LLM will happily turn a half-understood request into a polished PRD. Clean documentation is not the
same as a correct understanding — and the expensive mistakes come from the question nobody thought to
ask, the one that turns a "small feature" into a three-month build.

Requivo asks a question only when the answer would **materially change the solution**. The rest, it
infers and marks as an assumption to confirm. You spend discovery time where it moves the needle.

And because it keeps the understanding rather than just the answer, it can tell you what a *changed*
answer costs:

```text
You change one answer:
  "The migration is one-time. After cutover, the legacy system is read-only."

Requivo:
  What changed        Integrations & notifications
  Needs review        the two-way sync decision · the reconciliation risk · the decision brief
```

That is the part a chat transcript cannot do.

```text
Request:
  "We'd like a leave approval system."

Requivo asks only what changes the build:
  - Is there a balance/quota check, and does it vary by contract or country?
  - Is the approval circuit multi-level, and configurable per client?
  - What happens when the approver is unavailable — escalate or wait?
  - How are requests handled today (paper, email, another tool)?
```

It leaves the low-stakes questions (reporting, cosmetics) alone.

**The canonical example is [`examples/leave-approval/`](examples/leave-approval/)** — one line of
request, taken through the questions, the brief, and a changed answer that moves the scope. Start
there. A harder, messy multi-feature one lives in
[`examples/event-checkin-reconciliation/`](examples/event-checkin-reconciliation/), and is what
`requivo demo` replays.

---

## Quickstart

Start in the browser. Use Claude Code or the CLI when they fit your workflow — all three read and
write the same local sessions, so nothing is locked to the one you start in.

### Try it with no key, no setup

`requivo demo` replays a real run from saved output — the messy request, the questions it raised, the
brief it produced. No API key, no network.

```bash
git clone https://github.com/jbkkz/requivo && cd requivo
uv run requivo demo
```

### 1. Requivo Web — the easiest way in

A local, single-user browser interface, and the one to start with. Paste a request, answer the few
questions that could change the solution, see what each answer moved, generate one decision brief.

```bash
uv tool install "requivo[web,anthropic]"   # or just [web] to review sessions without a provider
export ANTHROPIC_API_KEY="…"               # only needed to analyse and generate
requivo web                                # opens http://127.0.0.1:8765
```

Sessions stay on your machine; the server binds to localhost. Requivo Web is **not** Requivo Cloud —
no accounts, no database, no remote storage. See [`docs/web.md`](docs/web.md).

### 2. Requivo for Claude Code — an integration

Use the same Requivo sessions inside the Claude Code workflow you already have — reasoning goes
through your own Claude session, so there is **no extra API key**.

```text
/plugin marketplace add jbkkz/requivo
/plugin install requivo@requivo

/requivo:discover  We'd like a leave approval system.
/requivo:status    <slug>
/requivo:brief     <slug>
```

See the [plugin README](plugins/claude-code/) for the full skill list and the from-a-checkout install.

### 3. Requivo CLI — inspect, automate, script

The complete surface, for automation and for anything you want to drive from a script or a pipeline.

```bash
uv run --extra anthropic requivo discover "We'd like a leave approval system."
uv run requivo status <slug>               # the understanding, open questions and readiness (no network)
uv run requivo brief  <slug>               # a decision brief from the saved understanding
```

Full command reference: [`docs/cli.md`](docs/cli.md). Getting started in depth:
[`docs/getting-started.md`](docs/getting-started.md).

---

## How it works

1. **Bring a request** — a sentence or a rambling email; a symptom, not a spec.
2. **Clarify high-impact unknowns** — Requivo asks only what would change the solution, and infers the
   rest as assumptions to confirm.
3. **Build and validate the requirements model** — a versioned set of typed *slots* that is the durable
   product.
4. **Generate traceable artifacts** — each is a view of the model, and knows which decisions it rests on.

The decision rule is **information value = uncertainty × impact**. Impact is estimated from the product
context you give it, so better context means sharper questions. More:
[`docs/requirements-model.md`](docs/requirements-model.md).

---

## What Requivo produces

The main output is a **decision brief** — the smallest document a scope review can be run from: what
is confirmed, what is being assumed, the decisions on record, the premises worth contesting, and what
is still open. It is not a PRD; it is what you read *before* writing one.

Everything else is generated from the same understanding, without redoing the discovery:

- PRD
- User stories
- Acceptance criteria (Given/When/Then)
- Uncertainty-aware estimate
- Delivery epic + tool-neutral export (GitHub / GitLab issue plans)
- Release notes

See [`docs/cli.md`](docs/cli.md) for how each is generated.

---

## Core concepts

- **What we know** — stated directly by the client.
- **What we are assuming** — inferred from context; confirm before building.
- **Open question** — not yet known, and worth asking when the answer would move the build.
- **How we know it vs how fully** — whether something was stated or inferred is separate from whether
  it has been covered in enough detail. Both have to hold before a topic stops blocking.
- **Decision and assumption to review** — a settled choice with its trade-off; a premise worth
  contesting before build.
- **What rests on what** — a decision records the topics it was derived from; a document records the
  topics it consumes. That graph is what makes "needs updating" an answer rather than a guess.
- **Are we ready?** — whether a high-impact topic is still unresolved.
- **Needs updating** — a document the understanding has moved past. `requivo impact` shows a change's
  blast radius before you make it.

These are the names the product uses. The engine's own vocabulary — slots, evidence, coverage,
artifacts, staleness, revisions — is the precise form of the same ideas, and it is what the technical
docs and `--json` speak: [`docs/requirements-model.md`](docs/requirements-model.md).

---

## Architecture

```text
       Web          Claude Code        CLI / API
   (the product)   (an integration)  (infrastructure)
         \               |                /
                    Requivo Core
            validated, versioned understanding
```

- The **Core** is provider-independent — no LLM, no network. It validates, versions, computes
  readiness, and decides what a change makes stale.
- The **Anthropic provider** is optional; it powers automated analysis and generation.
- Every interface uses the **same session format** and the same validated apply path — there is no
  fork, and no interface holds business logic of its own. The three differ in weight, not in what they
  can reach.

More: [`docs/architecture.md`](docs/architecture.md).

---

## Data and privacy

- **Local by default.** Sessions live in `.requivo/sessions/` in your workspace. No telemetry, no
  analytics — Requivo never phones home.
- **What leaves your machine.** When a provider is used, each turn sends your request, answers, prompts,
  schema, and loaded context cards to the Anthropic API — and to no other server. In Claude Code mode,
  reasoning goes through your own Claude session. Treat anything you type as content sent to a model
  provider; don't paste secrets or confidential data.

Full notes: [SECURITY.md](SECURITY.md).

---

## Documentation

| Doc | What it covers |
|---|---|
| [Getting started](docs/getting-started.md) | Install and first run for each interface |
| [Web](docs/web.md) | The primary interface — the local browser workspace |
| [Claude Code](plugins/claude-code/) | The integration: skills, workflow, install |
| [CLI reference](docs/cli.md) | Every command and flag |
| [Product validation](docs/product-validation.md) | How to test whether Requivo beats a strong prompt |
| [Architecture](docs/architecture.md) | Core, services, surfaces |
| [Requirements model](docs/requirements-model.md) | Slots, evidence/coverage, readiness, dependencies |
| [Session format](docs/session-format.md) | The `.requivo/` layout and revisions |
| [Providers](docs/providers.md) | The Anthropic provider, models, cost |
| [Context cards](docs/context-cards.md) | Teaching the engine your product |
| [Evaluations](docs/evaluations.md) | The golden harness for prompt/context changes |
| [Roadmap](docs/roadmap.md) | What exists and what's next |
| [Open-source strategy](docs/open-source-strategy.md) | Core / Web / Cloud boundary |

---

## Status

Actively developed, pre-1.0. The Core, CLI, Claude Code plugin and local Web interface are usable
today. The **session format is a published contract** — versioned, forward-compatible, and shared by
every interface; what is guaranteed and what is deprecated is written down in
[compatibility](docs/compatibility.md). Output is non-deterministic — treat the assessment as a senior
colleague's read, not an oracle, and get expert review for any legal/tax/compliance flag it raises.
See the [roadmap](docs/roadmap.md).

---

## Contributing and license

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The Core, CLI and Claude Code
integration are open source under [MIT](LICENSE). The Requivo **name and identity** are separate from
the code license — see [TRADEMARKS.md](TRADEMARKS.md).

[MIT](LICENSE) © jbkkz

> _Requivo was previously named Product Copilot._
