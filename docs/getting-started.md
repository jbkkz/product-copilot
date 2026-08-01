# Getting started

> Install Requivo and run a first discovery on each interface. For orientation, read the
> [main README](../README.md) first.

Requivo is one engine with three interfaces over the same local session format. Sessions live in
`.requivo/sessions/<slug>/` in your workspace. Pick the interface that fits; a session created by one
opens in the others.

## Try it with no key, no setup

`requivo demo` replays a real run from bundled output — no API key, no network.

```bash
git clone https://github.com/jbkkz/requivo && cd requivo
uv run requivo demo
```

## Claude Code

Reason with the Claude session you already have — **no Anthropic API key needed**. The deterministic CLI
validates and applies what Claude proposes.

1. Install the plugin from [`plugins/claude-code/`](../plugins/claude-code/).
2. In Claude Code:

   ```text
   /requivo-discover  We'd like a leave approval system.
   /requivo-answer    <slug>  <your answers>
   /requivo-status    <slug>
   /requivo-brief     <slug>
   ```

See the [plugin README](../plugins/claude-code/) for the full skill list and workflow.

## Web

A local, single-user browser interface. The server binds to localhost; the Anthropic key is read from
the server environment and only needed for discovery / generation.

```bash
uv tool install "requivo[web,anthropic]"   # or just [web] to review sessions without a provider
export ANTHROPIC_API_KEY="…"
requivo web                                # opens http://127.0.0.1:8765
```

Details and security notes: [web.md](web.md).

## CLI

With [uv](https://docs.astral.sh/uv/) — no virtualenv to manage. Discovery calls the Anthropic API, so
pull in the `anthropic` extra and set a key:

```bash
cp .env.example .env                       # set ANTHROPIC_API_KEY
uv run --extra anthropic requivo discover examples/case1_leave.md
```

<details><summary>Classic pip + venv install</summary>

```bash
python -m venv .venv && source .venv/bin/activate
pip install -U pip setuptools   # a fresh venv may ship a pip too old for editable installs
pip install -e '.[anthropic]'   # deps + the anthropic SDK + the `requivo` command
cp .env.example .env
requivo discover examples/case1_leave.md
```

</details>

Discovery runs an interactive loop, then writes the session to `.requivo/sessions/<slug>/`. Every verb
takes the session **slug** (or a `model.json` path); regenerate any artifact without redoing discovery:

```bash
requivo prd    <slug>                      # also: stories · estimate · criteria · release · brief
requivo epic   <slug> --github --gitlab    # + a tool-neutral epic.json and tracker issue plans
requivo impact <slug> permissions          # what rests on a slot
```

Full reference: [cli.md](cli.md).
