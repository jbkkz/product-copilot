# Requivo

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> Turn vague requests into validated product decisions.

Requivo builds a structured and traceable model of what is **known**, **inferred** and still
**unknown** before generating product documentation — solution assessments, PRDs, user stories,
acceptance criteria, estimates and epics.

Built for Product Managers, Solutions Engineers and Business Analysts working on complex, configurable B2B products.

> **The model is the product. Documents are views of that model.**

---

## See it in one look

A real, rambling client email — a symptom, not a spec, with three features tangled together and a
constraint buried at the end:

> *"…we bring in freelancers to check guests in at the door, but nobody has a clear view of who's
> actually been approved to attend… afterwards finance spends weeks reconciling because the freelancer
> invoices never line up with the hours actually worked… We need something that ties this together…
> It has to work at the venue where the wifi basically doesn't. Event's in six weeks."*

**What Requivo made of it — before a line of spec was written:**

- **Two systems, not one.** It refused the "tie this together" framing: live door check-in and
  after-the-fact invoice reconciliation are separate builds, with separate data and separate owners.
- **A disguised-employment (*salariat déguisé*) exposure** nobody wrote down — freelancers on fixed
  hours doing core work — surfaced as a point for **legal review**, not a requirement.
- **An unresolved offline strategy** — the venue wifi "basically doesn't" work, which decides the
  whole architecture rather than being an edge case.
- **A six-week deadline** the two builds now have to be **sequenced** against.

See the whole run yourself — **no API key, no setup, no network:**

```bash
uv run requivo demo        # or, with nothing installed:  python scripts/requivo_cli.py demo
```

---

## Three surfaces, one engine

Requivo is a deterministic core with interchangeable reasoners on top. The **model** — the validated,
versioned session — is the single source of truth; every surface is a thin layer over the same core.

```
                    Requivo Core
               validated session model
                 /        |         \
                /         |          \
       Claude Code       CLI      Future Web
           |              |
   Claude reasoning   deterministic tools
```

- **Requivo Core** — validated requirements model + deterministic impact engine. No LLM, no provider,
  no network. It validates proposals, versions sessions, computes readiness, and detects what goes stale.
- **Requivo for Claude Code** — use your existing Claude Code session for the reasoning and dialogue.
  Claude proposes a model; the CLI validates and applies it. **You do not need an Anthropic API key to
  use Requivo inside Claude Code.** See [`plugins/claude-code/`](plugins/claude-code/).
- **Requivo CLI** — inspect, validate, version and export sessions locally: `requivo doctor`,
  `requivo session init`, `requivo model validate|apply|diff`, `requivo status`, `requivo impact`,
  `requivo artifact save`. All deterministic, all offline.
- **Anthropic provider** — the optional API-powered automation: `requivo discover request.md` runs
  discovery end-to-end against the Claude API. Install with `pip install 'requivo[anthropic]'` and set
  `ANTHROPIC_API_KEY`. Same core, same validation, same session format — just a different reasoner.

A session created by any surface is readable by the others (and by the future Web UI): they share one
model, one validation, one apply path — there is no fork.

Sessions are written to `.requivo/sessions/<slug>/` under your workspace (versioned metadata, the model,
its revision history, and generated artifacts). Legacy `out/<slug>/` sessions are read-only and migrated
into the new layout on first change (or in bulk with `requivo session migrate`).

---

```
                 Customer request
                         │
                         ▼
                   AI Discovery   ◀── product + client context
                         │
                         ▼
                 Structured model   ← the product (.requivo/sessions/<slug>/model.json)
                         │
    ┌─────────┬──────────┼──────────┬───────────────┐
    ▼         ▼          ▼          ▼               ▼
Solution     PRD    User stories  Estimate   More artifacts
assessment
```

---

## Why

Discovery tools either ask you *everything* — endless checklists no one finishes — or *nothing* — a
chat that nods along and hands back your own words. Neither helps you find the question you didn't
think to ask: the one that turns a "small feature" into a three-month build.

Requivo asks a question only when the answer would **materially change the solution**. The
rest, it infers and flags as an assumption. You spend your discovery time where it moves the needle.

### Why I built this

> After several years working on complex, configurable enterprise software, I realised that writing
> the specification was rarely the hardest part. The difficult part was building a shared
> understanding of the real problem before development started.
>
> Over time, I noticed the same reasoning pattern behind good discovery work: what do we actually
> know, what are we assuming, and what would materially change the solution? Requivo is my
> attempt to formalise that process.

---

## What it does

Requivo builds a **structured model of the solution** and refines it through a short,
targeted conversation. The chat is just the interface. **The product is the model** — and every
artifact (a solution assessment, a PRD, user stories, an estimate) is a view rendered from it.

The same discovery can later produce a PRD, a test plan, or a Jira export **without redoing the
conversation**.

---

## What you get

The deliverable is a **solution assessment** — a judgment on what you're about to build, not a recap
of what you said. It doesn't just organize the request; it **pushes back on it**, the way a senior PM
who has built this kind of system before would:

```
CHALLENGES
  ⚑ Immediate invoice on signature
      Premise      Invoices are generated the moment a contract is signed.
      Alternative  Many B2B contracts bill on a schedule — milestones, recurring
                   periods, usage — not a single lump sum at signature.
      Consequence  Signature-triggered invoicing multiplies cancellation and
                   credit-note handling when deals change before they start.
      Recommend    Validate the billing trigger with Finance before build.

DESIGN DECISIONS
  ✓ Draft-first invoices, reviewed by Finance before issuance
      Why          Finance sign-off is required for compliance.
      Alternative  Immediate issuance on the triggering event.
      Tradeoff     An extra approval step, in exchange for far lower compliance risk.
```

Above these sits a five-line **executive summary** (problem · solution · challenge · risks · next).
Below, the full analysis adds context-specific **risks**, ranked **opportunities**, a readiness
verdict with its single blocker — and the reasoning behind each. Every line is in a PM's language;
none of the engine's internals leak through.

---

## Example

```bash
requivo discover "We'd like to set up a leave approval system."
```

From that one sentence, on a platform whose context says *"approval usually hides a balance check
and a multi-level circuit"*, the engine asks the few questions that matter — the multi-level circuit,
the per-client variation, the balance rule — and leaves the low-stakes ones (reporting) alone. Each
answer refines the model until nothing high-value is left to ask, then the solution assessment is produced.

---

## See a complete example

Walk through a full discovery example, end to end, in
[`examples/leave-approval/`](examples/leave-approval/) — no install required:

| File | What it is |
|---|---|
| [`request.md`](examples/leave-approval/request.md) | The one-sentence input |
| [`model.json`](examples/leave-approval/model.json) | The structured model the discovery built |
| [`solution-assessment.md`](examples/leave-approval/solution-assessment.md) | The deliverable — challenges, design decisions, risks, next steps |
| [`prd.md`](examples/leave-approval/prd.md) | A PRD generated from the same model |
| [`epic.json`](examples/leave-approval/epic.json) | The same model as a GitHub/GitLab-importable epic |

Each of these — plus user stories, an estimate, acceptance criteria and release notes — is generated
from the same `model.json`. That's the whole idea:

```bash
requivo prd examples/leave-approval/model.json    # regenerate prd.md from the saved model
```

For a harder case — a rambling client email conflating three features, with a legal tripwire and a fixed
deadline buried in it — see
[`examples/event-checkin-reconciliation/`](examples/event-checkin-reconciliation/): the assessment refuses
the "tie this together" conflation, catches a disguised-employment (*salariat déguisé*) exposure nobody
wrote down, and sequences the two builds against the deadline.

---

## How it works

The solution model is a set of typed *slots* — the problem, actors, business rules, permissions and
edge cases — grouped into four areas: **Why / What / How / Validate**.

It decides what to ask with one rule: **information value = uncertainty × impact**. It never asks
just because something is unknown — it asks when an answer would change what you build. Impact is
estimated from the product context, so the engine is only as sharp as the context you give it.

The model is not a flat snapshot: its parts rest on each other. A design decision records the facts
it was **derived from**; each artifact records the slots it **consumes**. So a change knows its blast
radius — `requivo impact` shows what a revisited slot would invalidate, and a discovery turn that moves the
model warns you which already-generated files no longer match it.

---

## Quickstart

**See it first — no API key, no setup.** `requivo demo` replays a real run from saved output: the messy
client request, the questions the engine raised, the solution assessment it produced.

```bash
git clone https://github.com/jbkkz/requivo && cd requivo
uv run requivo demo        # or: python scripts/requivo_cli.py demo  (nothing installed) · requivo demo (after an install)
```

**Then run your own — with [uv](https://docs.astral.sh/uv/):** no virtualenv to create or activate.
`uv run` builds the environment from `pyproject.toml` on first run, then runs the command. Discovery
calls the Claude API, so pull in the `anthropic` extra and set a key:

```bash
cp .env.example .env                       # set ANTHROPIC_API_KEY
uv run --extra anthropic requivo discover examples/case1_leave.md   # first run resolves deps; later runs are instant
```

<details><summary>Or the classic pip + venv install</summary>

```bash
git clone https://github.com/jbkkz/requivo && cd requivo
python -m venv .venv && source .venv/bin/activate
pip install -U pip setuptools   # a fresh venv may ship a pip too old for editable installs
pip install -e '.[anthropic]'   # deps + the anthropic SDK (discovery) + the `requivo` command (and `pc` alias)
cp .env.example .env            # set ANTHROPIC_API_KEY
requivo discover examples/case1_leave.md
```

</details>

It runs an interactive loop — showing what's understood, asking the priority questions, folding your
answers back in — then writes the session to `.requivo/sessions/<slug>/` and produces the solution
assessment. Every verb takes the session **slug** (or a `model.json` path); regenerate any deliverable
from the saved model without redoing discovery (prefix each with `uv run` if you use uv, or activate
the venv first):

```bash
requivo prd    <slug>                      # also: stories · estimate · criteria · release · brief
requivo epic   <slug> --github --gitlab    # + a tool-neutral epic.json and tracker issue plans
requivo impact <slug> permissions          # what rests on a slot: decisions + artifacts that go stale
```

### Two interfaces, one engine

The product is the engine; the interfaces are thin layers over the same `requivo` core.

- **Terminal** — `requivo <command>` (or `uv run requivo <command>` with no manual venv, or `python
  scripts/requivo_cli.py <command>` with nothing installed at all). The short alias `pc` still works.
- **Claude Code** — `/requivo-discover`, `/requivo-answer`, `/requivo-status`, `/requivo-brief`,
  `/requivo-prd`, `/requivo-impact` (the [plugin](plugins/claude-code/)) reason in your Claude session
  and drive the same CLI — no API key needed.

The legacy flag CLI (`python src/engine.py "…" --prd`, `--from out/<slug>/model.json`) still works
unchanged.

---

## Before you rely on it

- **What leaves your machine.** Each discovery or generation turn sends, as one Anthropic API call:
  your request text, the framework schema, and **every** context card — bundled plus any in your
  `REQUIVO_CONTEXT_DIR` — (the system prompt), to
  the Claude model named by `MODEL` (default `claude-sonnet-5`). Nothing else is transmitted; this
  project stores nothing beyond `out/` on your own disk, and has no telemetry. `requivo demo`, `requivo status`
  and `requivo impact` make **no** network call at all.
- **Cost.** A discovery is a few calls (one per turn, up to 8) plus one per generated artifact. The
  system prompt is prompt-cached across a session, so the repeated calls of a run are cheap. Every
  `requivo` command that hits the API prints its own footprint when it finishes — calls, tokens (with the
  cached share), latency, and an estimated cost — so you see the real number for *your* request
  rather than guessing. (Tokens are exact; the cost is a labelled estimate from a dated rate table.)
- **Models.** Developed and measured against `claude-sonnet-5`; any current Claude model works via the
  `MODEL` env var.
- **Known limits.** Output is **non-deterministic** — the golden harness measures change above a noise
  floor rather than asserting exact text. By default every context card is loaded for every request, so
  cards can dilute one another (see [Knowing whether a card helped](#knowing-whether-a-card-helped)) —
  scope a session to the relevant ones with `requivo discover --context b2b-platform,financial-reporting`.
  The model can simply be wrong.
- **Not professional advice.** When the engine flags a legal, tax, or regulatory exposure (e.g. the
  disguised-employment risk in the event example), that is a prompt to get **expert review** — never a
  substitute for it. Nothing it produces is legal, financial, or compliance advice.

---

## Add your product

The engine is domain-agnostic; the context makes it smart. The built-in cards live in the package at
`src/requivo/assets/context/`; working from a clone (or an editable `pip install -e .`), drop
a card there describing your product, its entities, and its recurring traps:

```
src/requivo/assets/context/
  hris.md        ← HR / people platforms
  crm.md         ← sales & pipeline tools
  erp.md         ← finance & operations suites
  my-product.md  ← yours
```

Better context → sharper impact estimates → better questions. Files prefixed with `_` are ignored.

**Installed via pip, no checkout?** Drop your cards in a user directory instead — no need to touch the
package:

```bash
export REQUIVO_CONTEXT_DIR=~/.config/requivo/context   # this is also the default location
mkdir -p "$REQUIVO_CONTEXT_DIR" && $EDITOR "$REQUIVO_CONTEXT_DIR/my-product.md"
```

User cards are merged with the built-in ones; a user card whose name matches a built-in **overrides**
it, so you can tweak a bundled card without editing the package.

### Knowing whether a card helped

Behavior here is tuned by editing Markdown, and the engine is non-deterministic — so "did that card
make the engine sharper?" is a real question, and one run can't answer it. A small harness does:

```bash
python scripts/golden_run.py <slug>          # capture a fixed request K times (K=3)
python scripts/golden_diff.py                # what moved, above the measured noise floor
python scripts/golden_diff.py <slug> --questions   # the questions and challenges themselves
```

A fixed request set (one per problem *form*) is captured K times and compared against the committed
baseline. A change is reported only when the runs agreed before *and* after — anything that flickers
run-to-run is noise and stays silent. `--brief` extends this to the assessment itself, tracking the
complexity verdict and which premises the engine chose to contest.

It measures movement, not improvement — the questions are what tell you the direction. When the
finance card landed, the engine stopped asking *"what exactly are these totals?"* and started asking
*"a traceable adjustment entry, or an override?"*. That's the read that matters.

---

## Roadmap

**Current**
- Discovery engine — priority questions, multi-turn refinement, solution assessment (with challenges)
- Artifact generators — PRD, user stories, uncertainty-aware estimate, acceptance criteria, delivery
  epic, release notes
- Tool-neutral epic export (`epic.json`) — importable into GitHub / GitLab issues
- Tracker adapters — idempotent, n8n-ready issue-creation plans for GitHub (`epic.github.json`) and
  GitLab (`epic.gitlab.json`, with structured issue links)
- The model as a durable product (`model.json`), regenerable via `--from`
- A dependency graph over the model — `requivo impact` shows a change's blast radius, and a discovery turn
  flags the already-generated artifacts a change makes stale
- Two interfaces over one presentation-free engine — a `requivo` subcommand CLI and Claude Code slash
  commands (`/requivo-discover`, `/requivo-status`, `/requivo-brief`, …), each a thin layer over the same core
- A regression harness for prompt and context changes — consensus over repeated runs, so a real effect
  is separable from sampling noise, on the discovery *and* on the assessment
- A self-contained wheel — prompts, schema and context cards ship inside the package, so `pip install`
  works outside the clone; sessions go to `.requivo/sessions/` in your working directory, never into the install
- A user-level context directory (`REQUIVO_CONTEXT_DIR`) — add or override product cards on a pip-installed
  setup without a source checkout; user cards merge with the built-ins

**Upcoming**
- An HTTP API / MCP façade — another thin layer over the same core (for n8n and future web UIs)
- Jira adapter, alongside GitHub and GitLab
- Delivery integrations — authenticated push (via n8n), Notion and Confluence
- Context tooling — validation and assisted generation of product context cards

**Vision**
- A full artifact chain from a single model — the reasoning layer beneath product delivery
- Multiple surfaces over one engine — **Requivo Core** (this engine), **Requivo for Claude Code**,
  **Requivo Web**, and eventually **Requivo Cloud**

---

## Open source

Requivo Core, the CLI and the Claude Code integration are open source (MIT). The goal is to make the
requirements *model* portable, inspectable and usable locally — without forcing anyone into a hosted
service to get value from it.

A future **Requivo Cloud** may provide managed storage, collaboration, team administration and other
operational features. That hosted service is **not** part of this repository, and no commercial
offering is currently promised or available. A future **Requivo Community Web** interface is intended
to be open source and self-hostable when it exists; it does not exist yet.

The full surface map — Core, CLI, Claude Code, Community Web, Cloud, and the private evaluation Lab —
is in **[docs/open-source-strategy.md](docs/open-source-strategy.md)**. See also
[CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [GOVERNANCE.md](GOVERNANCE.md), and
[TRADEMARKS.md](TRADEMARKS.md) (the code is MIT; the Requivo name and identity are separate).

## Data and privacy

- **Local by default.** Your sessions and generated artifacts (`.requivo/`, `out/`) stay in your
  workspace. Requivo has **no telemetry and no analytics** — it never phones home.
- **What leaves your machine.** In the API-powered CLI mode, each run sends your request, your
  refinement answers, the prompts, the schema, the loaded context cards, and (when regenerating) the
  saved model to the **Anthropic API** to do the reasoning — and to no other server. In Claude Code
  mode, the reasoning goes through your own Claude session. Either way, treat everything you type as
  content sent to a model provider: don't paste secrets or confidential customer data unless you
  understand that provider's data-handling policy.
- **Not in this repository.** Real customer requests and private evaluation datasets are never
  committed here; public examples are synthetic or anonymised. See [SECURITY.md](SECURITY.md) for the
  full data and prompt-injection notes.

## License

[MIT](LICENSE) © jbkkz

---

> _Requivo was previously named Product Copilot._
