# Decision: the Claude Code plugin does not bundle the CLI

> Spike [#94](https://github.com/jbkkz/requivo/issues/94). Measured against Claude Code **2.1.238**
> and the plugin documentation as published on **2026-08-21**. Verdict: **no** — the second install
> step cannot be removed today without breaking one of the constraints that made the question worth
> asking. The preflight in [#93](https://github.com/jbkkz/requivo/issues/93) is the answer.

## The question

The plugin ships skills and a manifest. The `requivo` CLI those skills call is a separate PyPI
install, deliberately not in the plugin — so a user meets four steps where they expected one:

```
install plugin → install Python or uv → install requivo → fix PATH → use plugin
```

The spike asked whether the current plugin spec offers a way to collapse that, and what it costs.
The spec moves, so nothing below is recalled; each claim names how it was established.

## What the plugin spec measurably supports today

| Claim | How it was established |
| :--- | :--- |
| A plugin-root `bin/` puts executables on the Bash tool's `PATH` | Documented (*Plugins*, file-locations table: "Executables added to the Bash tool's `PATH`. Files here are invokable as bare commands in any Bash tool call while the plugin is enabled") **and observed**: five `…/plugins/cache/<mkt>/<plugin>/<version>/bin` entries were on `PATH` inside a Bash tool call on this machine |
| The `bin` entry is added whether or not the directory exists | Observed: two of those five plugins ship no `bin/` at all, and their entry is on `PATH` regardless |
| Plugin `bin` entries come **last** | Observed on macOS, and **not documented anywhere**. This is the property that would keep a shim from shadowing a real install, and it is the one property with no written guarantee behind it |
| `claude plugin validate` accepts a plugin carrying `bin/` and an executable | Ran it on a minimal probe plugin: `✔ Validation passed` |
| The Bash tool environment does **not** carry `CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA` or `CLAUDE_PROJECT_DIR` | Observed: 34 variables in a Bash tool call, none of the three. Corroborated by the reference, which exports them "to hook processes and to MCP and LSP server subprocesses" and lists the Bash tool nowhere |
| `${CLAUDE_PLUGIN_DATA}` is the blessed home for a bootstrap | Documented, and it names our case: "Use it for **Python dependencies**, dependencies locked with Yarn or pnpm, and packages whose lifecycle scripts must run" |
| `${CLAUDE_PLUGIN_ROOT}` is version-scoped and must not hold state | Documented: "treat it as ephemeral and don't write state there". Observed: the cache is `<plugin>/<version>/`, and two plugins here have two version directories side by side. Orphans are swept "roughly 14 days later" |
| `SessionStart` is the only automatic bootstrap point, and it cannot ask | Documented: `SessionStart` is "Context only… **No blocking or decision control**", and "runs on every session, so keep these hooks fast" |
| A skill's Bash call cannot prompt either | Observed: stdin is not a TTY in a Bash tool call |
| A `SessionStart` hook can hand values to later Bash calls | Documented: `CLAUDE_ENV_FILE`, a file the hook appends `export` lines to |
| Submissions are screened | Documented: "The review pipeline runs the same check on every submission, along with **automated safety screening**." The criteria themselves are not published |
| On Windows without Git Bash, **there is no Bash tool** | Documented: "On Windows without Git Bash, the tool is enabled automatically and Claude Code doesn't register the Bash tool at all" — the PowerShell tool takes over. `bin/` is specified against the Bash tool only |

What the released wheel needs at runtime, measured with `pip download requivo` and a clean venv:

- Python **3.9** floor; exercised end-to-end on 3.9.6 (`requivo doctor --json` returns `schema.ok`
  and `context.ok`).
- **7 wheels, 2.7 MB** downloaded: `requivo`, `pydantic`, `pydantic-core`, `typing-extensions`,
  `typing-inspection`, `annotated-types`, `python-dotenv`.
- **17 MB** on disk as a venv; **6.6 s** cold with pip's cache disabled, on a fast link.

That is small. Size was never what made this hard.

## What was tried

**A — a `bin/requivo` shim that repairs only `PATH`.** No network. Written and exercised: it
excludes its own directory from the `PATH` scan, defers to any real `requivo` found ahead of it, and
otherwise looks for an interpreter that can `import requivo` and execs `python -m requivo`
(`src/requivo/__main__.py` exists, so that entry point is real and was confirmed to work). Two
prototype runs settled the precedence question: with a real install earlier on `PATH`, `requivo`
resolves to the real one and the shim never executes; with none, the shim is what a bare `requivo`
resolves to. So a shim genuinely does not compromise `pip install requivo`.

**B — a first-run bootstrap into a plugin-local virtualenv.** A `SessionStart` hook comparing a
bundled marker against a copy in `${CLAUDE_PLUGIN_DATA}` — the pattern the reference documents for
exactly this — creating a venv there, `pip install requivo` into it, and exporting its path through
`CLAUDE_ENV_FILE` so the shim from A can find it. Every mechanism this needs exists. It was not
built past the shim, because it fails a constraint before it fails a mechanism.

**C — vendoring the source tree.** Excluded by the issue, and rightly: two copies of the engine is a
worse problem than one install step.

## The decision

**Neither A nor B ships. The plugin stays inert and the second install step stays.**

Three reasons, in the order that decided it.

**A bootstrap that is both automatic and refusable does not exist in the spec.** This is the whole
verdict; the rest is detail. The constraint was that a bootstrap "must say what it is doing and be
refusable". `SessionStart` is the only hook that fires without the user doing anything, and it is
documented as having no decision control: it cannot block, cannot prompt, and its stdout becomes
context for the model rather than a question for the person. Stdin is not a TTY. So an automatic
bootstrap is a silent network install by construction — the exact thing the constraint forbids. Make
it opt-in instead, behind an environment variable or a marker file, and the first run fails with
instructions, which *is* #93's preflight with 17 MB of machinery bolted to it.

**On Windows the mechanism may not be there at all.** `bin/` is specified against the Bash tool, and
on a Windows machine without Git Bash the Bash tool is not registered. Nothing in the documentation
extends `bin/` to the PowerShell tool. So the one platform where "fix your `PATH`" hurts most is the
platform where the fix is least reliable. (This is worth knowing beyond this spike: the skills'
`allowed-tools: Bash(requivo:*)` rests on the same assumption. Out of scope here.)

**The shim cannot find its own data directory.** `CLAUDE_PLUGIN_DATA` is absent from the Bash tool
environment. A shim can derive its plugin root from `$0`, but that root is version-scoped and
documented as ephemeral, so a venv placed there is rebuilt on every plugin update and each orphaned
copy lingers about two weeks. Routing the real path through `CLAUDE_ENV_FILE` from a `SessionStart`
hook works, and it means adding a hook to a plugin whose main virtue is having none — while doing
nothing about the first reason.

Option A survives all three, and still loses. It removes one of the four steps — the `PATH` one —
and leaves the other three. To buy that it puts the first executable into a plugin that currently
ships none, and rests its safety on an undocumented `PATH` ordering. #93 fixes the same failure with
prose. *Prefer the simple maintainable answer over the clever one* was in the constraint list, and
it points at #93.

## What each option costs

| | macOS | Linux | Windows | Screening profile |
| :--- | :--- | :--- | :--- | :--- |
| **Ship nothing (#93 preflight)** | Unchanged: one `pip install` | Unchanged | Unchanged; still the platform where `PATH` breaks most often | **Unchanged.** 9 files, 52 KB, six skills and a manifest, no executable, no hook, no MCP server, no network call at any point in the plugin's life |
| **A — `PATH`-repair shim** | Works. Needs an interpreter that can `import requivo`; `python3` is present, `python` and `py` are not | Same as macOS | Undefined without Git Bash, where the Bash tool is not registered. With Git Bash, needs an extensionless shebang script to be found and executed from `PATH`, plus a `.cmd` twin for any PowerShell route | **Changed.** First executable in the plugin. No network and no hook, so a reviewer's question is "what does this script do" rather than "what does it fetch" |
| **B — first-run bootstrap** | 2.7 MB fetched, 17 MB venv, ~7 s cold on a fast link; rebuilt per plugin version unless routed to `${CLAUDE_PLUGIN_DATA}` | Same, plus distributions that ship Python without `ensurepip`, where `python -m venv` fails and the failure has to be explained rather than retried | Everything above, plus interpreter discovery: `python3` is typically absent, `python` may be the Microsoft Store stub that opens the Store instead of running | **Changed materially.** A hook running a shell command at every session start, an executable on `PATH`, and a network install of a package resolved at runtime. The screening criteria are not published, so the outcome cannot be measured here; the direction of the change is not in doubt |

## What would change the answer

- A plugin-manifest declaration of a Python dependency, installed by Claude Code under the same
  constraints it already applies to npm dependencies — frozen resolution, no lifecycle scripts, a
  bounded timeout. That is the shape the answer wants, and it does not exist today.
- A hook event that can genuinely ask the user, so a first-run install can be consented to rather
  than announced.
- `bin/` specified against the PowerShell tool as well as the Bash tool.
- Documented `PATH` precedence for plugin `bin` entries, so a shim's safety rests on a promise
  rather than an observation.

Any one of the first two would be enough to reopen this.

## Recommendation

Close #94. Ship the #93 preflight and make it good: the cause named, one command, an immediate
retry, and the statement that nothing was half-created. That is the product for as long as this
answer holds.
