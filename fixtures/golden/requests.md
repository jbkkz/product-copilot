# Golden requests

A fixed set of discovery inputs used to watch the engine for regressions. Each block below is one
request: `golden_run.py` reads this file, runs a single-pass discovery **K times** per block (K=3 by
default) and saves all K models to `fixtures/golden/<slug>.runs.json`. `golden_diff.py` then compares
a fresh K-run capture against the committed one and reports a slot as *moved* only when the change
clears the measured noise floor — the engine is non-deterministic and the model family exposes no
sampling controls, so a single capture can't be pinned, only sampled. See `scripts/golden_lib.py`.

The set is deliberately small and diverse: **one request per problem *form***, so a change to a prompt
or a context card that shifts how the engine reasons about a form shows up on the request that
exercises it. The `card:` line is documentary — it names the context card that *should* shape this
request. It is not a loading switch: `load_context()` concatenates every non-`_` card in `context/`,
so every run sees every card. The mapping tells you which card a diff on this request is likely
attributable to.

Format (parsed by `golden_run.py`): each run is a `### <slug>` heading followed by `key: value`
lines. `request:` holds the single-line discovery input; `form:` and `card:` are metadata.

### leave-approval
form: approval
card: b2b-platform
request: We'd like managers to approve employee leave requests, with an escalation if the manager is away.

### invoice-on-signature
form: auto-create-on-event
card: b2b-platform
request: When a contract is signed, we want an invoice to be created automatically.

### notify-mission-end
form: notify
card: b2b-platform
request: We want to notify the right people when a freelancer's mission is about to end.

### export-financials
form: export-report
card: financial-reporting
request: Let users edit the reported totals and export the figures for the finance team.

### event-checkin
form: one-shot-app
card: event-ops
request: We need an app for staff to check attendees in at the venue entrance on the event day.

### doc-reapproval
form: mutate-signed-artifact
card: document-management
request: We'd like managers to edit and re-approve documents after they've already been signed.
