# Getting started

> Install Requivo and run a first discovery on each interface. For orientation, read the
> [main README](../README.md) first.

Requivo is one engine with three interfaces over the same local session format. Sessions live in
`.requivo/sessions/<slug>/` in your workspace, and a session created by one interface opens in the
others — so nothing is locked to where you start.

**Start in the browser.** Use Claude Code or the CLI when they fit your workflow better.

## Try it with no key, no setup

`requivo demo` replays a real run from bundled output — no API key, no network.

```bash
git clone https://github.com/jbkkz/requivo && cd requivo
uv run requivo demo
```

## 1. Web — start here

A local, single-user browser workspace, and the shortest path from a request to something reviewable.
The server binds to localhost; the Anthropic key is read from the server environment and is only needed
to analyse and generate.

```bash
uv tool install "requivo[web,anthropic]"   # or just [web] to review sessions without a provider
export ANTHROPIC_API_KEY="…"
requivo web                                # opens http://127.0.0.1:8765
```

Then: paste a request on the home page → read what Requivo understood → answer the questions it raises
→ read what those answers moved → **Generate decision brief**. Come back later, change one answer, and
it will tell you what needs reviewing.

Details and security notes: [web.md](web.md).

## 2. Claude Code — an integration

Use the same sessions inside the Claude Code workflow you already have — reasoning goes through your
own Claude session, so **no Anthropic API key is needed**. The deterministic CLI validates and applies
what Claude proposes.

1. Install the plugin. In Claude Code:

   ```text
   /plugin marketplace add jbkkz/requivo
   /plugin install requivo@requivo
   /reload-plugins
   ```

   (From a checkout instead: `claude --plugin-dir ./plugins/claude-code`.)
2. Then:

   ```text
   /requivo:discover  We'd like a leave approval system.
   /requivo:answer    <slug>  <your answers>
   /requivo:status    <slug>
   /requivo:brief     <slug>
   ```

See the [plugin README](../plugins/claude-code/) for the full skill list and workflow.

## 3. CLI — inspect, automate, script

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
