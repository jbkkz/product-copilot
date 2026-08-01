# Requivo

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> Turn vague requests into validated product decisions.

Requivo separates facts, assumptions and unknowns, asks the questions that could change the solution,
and keeps the resulting decisions traceable — before generating product documentation.

**The model is the product. Documents are views of that model.**

Built for Product Managers, Solutions Engineers and Business Analysts working on complex, configurable
B2B products.

---

## Why Requivo

An LLM will happily turn a half-understood request into a polished PRD. Clean documentation is not the
same as a correct understanding — and the expensive mistakes come from the question nobody thought to
ask, the one that turns a "small feature" into a three-month build.

Requivo asks a question only when the answer would **materially change the solution**. The rest, it
infers and marks as an assumption to confirm. You spend discovery time where it moves the needle.

```text
Request:
  "We'd like a leave approval system."

Requivo asks only what changes the build:
  - Is there a balance/quota check, and does it vary by contract or country?
  - Is the approval circuit multi-level, and configurable per client?
  - What happens when the approver is unavailable — escalate or wait?
  - How are requests handled today (paper, email, another tool)?
```

It leaves the low-stakes questions (reporting, cosmetics) alone. See a full run in
[`examples/leave-approval/`](examples/leave-approval/), or a harder, messy one in
[`examples/event-checkin-reconciliation/`](examples/event-checkin-reconciliation/).

---

## Choose your interface

Requivo is one engine with three interfaces over the same local session format. A session created by
any of them is readable and editable by the others.

| Interface | Best for | LLM usage |
|---|---|---|
| **Claude Code** | Interactive discovery in a session you already have | Uses your Claude Code session — **no extra API key** |
| **Web** | A local browser interface for non-terminal users | Anthropic provider (optional; key only for LLM actions) |
| **CLI** | Automation, scripting, deterministic session operations | Anthropic provider (optional; key only for LLM actions) |

**New to Requivo? Start with Claude Code or the local Web interface.**

---

## Quickstart

### Try it with no key, no setup

`requivo demo` replays a real run from saved output — the messy request, the questions it raised, the
assessment it produced. No API key, no network.

```bash
git clone https://github.com/jbkkz/requivo && cd requivo
uv run requivo demo
```

### Claude Code

Reason with the Claude session you already have — no Anthropic API key needed. In Claude Code:

```text
/plugin marketplace add jbkkz/requivo
/plugin install requivo@requivo

/requivo:discover  We'd like a leave approval system.
/requivo:status    <slug>
/requivo:brief     <slug>
```

See the [plugin README](plugins/claude-code/) for the full skill list and the from-a-checkout install.

### Web

A local, single-user browser interface. Sessions stay on your machine; the server binds to localhost.

```bash
uv tool install "requivo[web,anthropic]"   # or just [web] to review sessions without a provider
export ANTHROPIC_API_KEY="…"               # only needed for discovery / generation
requivo web                                # opens http://127.0.0.1:8765
```

Requivo Web is **not** Requivo Cloud — no accounts, no database, no remote storage. See
[`docs/web.md`](docs/web.md).

### CLI

```bash
uv run --extra anthropic requivo discover "We'd like a leave approval system."
uv run requivo status <slug>               # understanding + readiness (no network)
uv run requivo prd    <slug>               # a PRD from the saved model
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

A **solution assessment** (a senior-PM judgment that pushes back on the request, not a recap), plus
generated artifacts from the same model, without redoing discovery:

- PRD
- User stories
- Acceptance criteria (Given/When/Then)
- Uncertainty-aware estimate
- Delivery epic + tool-neutral export (GitHub / GitLab issue plans)
- Release notes

See [`docs/cli.md`](docs/cli.md) for how each is generated.

---

## Core concepts

- **Explicit fact** — stated directly by the client.
- **Inferred assumption** — assumed from context; confirm before building.
- **Unknown** — not yet known; may need a question.
- **Evidence vs coverage** — *how we know* a slot (explicit / inferred) is separate from *how fully*
  it's covered.
- **Decision & challenge** — a settled choice with its trade-off; a contested premise worth weighing.
- **Dependency** — a decision records the slots it's derived from; an artifact records the slots it
  consumes.
- **Readiness** — whether a high-impact gap still blocks the build.
- **Stale artifact** — a generated file the model has moved past. `requivo impact` shows a change's
  blast radius.

Details: [`docs/requirements-model.md`](docs/requirements-model.md).

---

## Architecture

```text
   Claude Code        Web        CLI / API
         \             |             /
                  Requivo Core
          validated, versioned model
```

- The **Core** is provider-independent — no LLM, no network. It validates, versions, computes
  readiness, and tracks staleness.
- The **Anthropic provider** is optional; it powers automated discovery and generation.
- Every interface uses the **same session format** and the same validated apply path — there is no fork.

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
| [Claude Code](plugins/claude-code/) | The plugin: skills, workflow, install |
| [Web](docs/web.md) | The local browser interface |
| [CLI reference](docs/cli.md) | Every command and flag |
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
