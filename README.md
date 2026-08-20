# Requivo

[![PyPI](https://img.shields.io/pypi/v/requivo)](https://pypi.org/project/requivo/)
[![Python](https://img.shields.io/pypi/pyversions/requivo)](https://pypi.org/project/requivo/)
[![CI](https://github.com/jbkkz/requivo/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/jbkkz/requivo/actions/workflows/ci.yml?query=branch%3Amain)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> Find what could change the solution before you commit to the scope.

Paste a client or stakeholder request. Requivo identifies the assumptions and missing decisions that
could change the workflow, integrations, permissions, timeline or effort — then produces one brief you
can review before estimating.

**The shared understanding is the source of truth. Every document is generated from it.**

Built for Product Managers, Solutions Engineers and Business Analysts working on complex, configurable
B2B products.

---

## Start here — Requivo Web

A local browser workspace, and the way to use Requivo. Paste a request, answer the few questions that
could change the solution, see what each answer moved, generate one decision brief.

```bash
uvx --from "requivo[web,anthropic]" requivo web    # opens http://127.0.0.1:8765
```

One command, nothing installed — [uv](https://docs.astral.sh/uv/) fetches Requivo into a temporary
environment and runs it (`curl -LsSf https://astral.sh/uv/install.sh | sh` if you don't have uv). To
keep it around, `uv tool install "requivo[web,anthropic]"` and then just `requivo web`; `pipx install`
works the same way. Prefer plain pip? [`docs/getting-started.md`](docs/getting-started.md) has the
virtualenv route — avoid `pip install --user`, which succeeds while leaving `requivo` off your PATH.

Set `ANTHROPIC_API_KEY` in your environment or a `.env` file to analyse and generate. Without it the
interface still opens and reads existing sessions, and tells you what is missing.

Sessions stay on your machine, the server binds to localhost, and nothing leaves your workspace — no
accounts, no database, no remote storage. See [`docs/web.md`](docs/web.md).

Two other ways in, on the same local sessions — nothing is locked to the interface you start in:

- **[Claude Code](plugins/claude-code/)** — an integration. `/plugin marketplace add jbkkz/requivo`,
  then `/requivo:discover <request>`. Reasoning goes through your own Claude session, so there is no
  extra API key.
- **[CLI](docs/cli.md)** — infrastructure. `requivo discover | status | brief …`, for automation and
  anything you drive from a script or a pipeline.

Install and first run in depth: [`docs/getting-started.md`](docs/getting-started.md).

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

**The canonical example is [`examples/leave-approval/`](examples/leave-approval/)** — one line of
request, taken through the questions, the brief, and a changed answer that moves the scope. A harder,
messy multi-feature one lives in
[`examples/event-checkin-reconciliation/`](examples/event-checkin-reconciliation/), and is what
`requivo demo` replays.

---

## How it works

1. **Bring a request** — a sentence or a rambling email; a symptom, not a spec.
2. **Clarify high-impact unknowns** — Requivo asks only what would change the solution, and infers the
   rest as assumptions to confirm.
3. **Build and validate the understanding** — a versioned, typed model that is the durable product.
4. **Generate traceable documents** — each is a view of the model, and knows which decisions it rests on.

The decision rule is **information value = uncertainty × impact**. Impact is estimated from the product
context you give it, so better context means sharper questions.

The main output is a **decision brief** — the smallest document a scope review can be run from: what is
confirmed, what is assumed, the decisions on record, the premises worth contesting, and what is still
open. It is not a PRD; it is what you read *before* writing one. A PRD, user stories, acceptance
criteria, an uncertainty-aware estimate, a delivery epic with GitHub/GitLab issue plans and release
notes all generate from the same understanding, without redoing the discovery.

The vocabulary — what we know, what we are assuming, open question, needs updating, are we ready — and
the model underneath it: [`docs/requirements-model.md`](docs/requirements-model.md).

---

## Architecture

```text
       Web          Claude Code        CLI / API
   (the product)   (an integration)  (infrastructure)
         \               |                /
                    Requivo Core
            validated, versioned understanding
```

Every interface uses the same session format and the same validated apply path — there is no fork, and
no interface holds business logic of its own. The three differ in weight, not in what they can reach.
The Core is provider-independent: no LLM, no network. More:
[`docs/architecture.md`](docs/architecture.md).

---

## Data and privacy

**Local by default.** Sessions live in `.requivo/sessions/` in your workspace. No telemetry, no
analytics — Requivo never phones home. When a provider is used, each turn sends your request, answers,
prompts, schema and loaded context cards to the Anthropic API, and to no other server; in Claude Code
mode, reasoning goes through your own Claude session. Treat anything you type as content sent to a
model provider. Full notes: [SECURITY.md](SECURITY.md).

---

## Documentation

| Doc | What it covers |
|---|---|
| [Getting started](docs/getting-started.md) | Install and first run for each interface |
| [Web](docs/web.md) | The primary interface — the local browser workspace |
| [CLI reference](docs/cli.md) | Every command and flag |
| [Requirements model](docs/requirements-model.md) | The vocabulary, readiness, dependencies |
| [Architecture](docs/architecture.md) | Core, services, surfaces |

Everything else — session format, providers, context cards, evaluations, product validation, roadmap,
open-source strategy — is indexed in [`docs/`](docs/README.md).

---

## Supported platforms

| Platform | Python | Tested in CI |
|---|---|---|
| Linux | 3.9 – 3.13 | every version, every push |
| macOS | 3.9 – 3.13 | 3.9 and 3.13 |
| Windows | 3.9 – 3.13 | 3.9 and 3.13 |

An untested platform and a supported platform look identical from outside, so this table says which
is which. The ends of the version range are tested on macOS and Windows rather than every minor
version, and the ends are the point: a platform's own standard library can behave differently at each
one. Windows on 3.9 cannot resolve a symlink whose target is missing, where Windows on 3.13 can —
which once left a path-containment guard holding on twelve of thirteen CI legs and not on the
thirteenth. Differences in the language itself show on the Linux axis, which runs all five.

Requivo reads and writes **UTF-8 everywhere**, regardless of the machine's locale or the console's
codepage. A session written on one machine reads back byte-identically on another. Where a console
cannot represent a character Requivo prints, the character is escaped rather than dropped and never
crashes the command — `requivo doctor` reports your console's encoding when there is something worth
saying about it. A file you pass in (`requivo discover ./brief.md`) must be UTF-8; one that is not is
refused by name rather than silently decoded into something that reads like prose and is wrong.

---

## Status

Actively developed, and stable at the interface. The Core, CLI, Claude Code plugin and local Web
interface are usable today. **1.0 is a promise about the interface, not a claim that the product is
finished**: from 1.0.0 onward a breaking change to the session format, the `--json` outputs, the
error-code vocabulary or the CLI surface costs a major version and is announced before it lands. What
is covered, what is deliberately *not*, and what is deprecated are all written down in
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
