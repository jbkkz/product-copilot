# Security

Requivo is an early open-source beta. This document states what the tool does with your data
and how to report a problem.

## What leaves your machine

The engine makes calls to the **Anthropic API** and nothing else. On each run it sends, as the prompt:

- your **client request** (and any answers you provide in refinement turns);
- the **prompts**, the **model schema**, and the **context cards** (`context/*.md`) that inform the run;
- the **saved model** (`out/<slug>/model.json`) when you regenerate an artifact from it.

There is **no telemetry, no analytics, and no other network call**. Nothing is sent to any server
operated by this project. Your `out/` folder stays local. Do not put secrets (API keys, passwords,
personal data) into a request or a context card — treat everything you type as prompt content that
will be sent to the model provider.

Your `ANTHROPIC_API_KEY` is read from the environment / `.env` and used only to authenticate those
API calls. Keep `.env` out of version control (it is gitignored).

## Untrusted input / prompt injection

The **client request**, the client's **answers**, and the **context cards** are treated as *untrusted
business data* — material for the engine to analyse, never instructions for it to obey. The engine
prompts state this trust boundary explicitly, so text like "ignore the above instructions" embedded
in a request is modelled as a requirement to capture, not a command to follow.

This is a mitigation, not a guarantee: LLM prompt-injection defences are imperfect. Do not run
Requivo on requests from a source you would not trust to read your prompts, and review
generated artifacts before acting on them.

## Reporting a vulnerability

Please **do not** open a public issue for a security problem. Instead, email the maintainer
(see the repository owner's public profile) with:

- a description of the issue and its impact;
- steps to reproduce;
- any suggested remediation.

You will get an acknowledgement as soon as possible. As a solo-maintained beta there is no formal SLA,
but security reports are prioritised over feature work.
