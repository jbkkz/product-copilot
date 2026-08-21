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

## Interactive requests

A block that also carries `answer.<slot>:` lines is captured differently: instead of one discovery
call, it drives the **interactive** shape — `DiscoveryService.draft_turn`, the loop behind
`requivo discover` — for up to `GOLDEN_TURNS` turns (5 by default), answering the engine's questions
off those lines. It exists because a single-pass capture cannot see what #77 changed: from turn 3 the
loop is grounded on the carried model alone, where the old one re-sent the whole transcript. Turns 1
and 2 are byte-identical between the two shapes, so only a capture that runs deep says anything.

Each `answer.<slot>:` line is one **layer** — the next thing this client has to say when the engine
comes back to that slot. Layers are handed out in order and then run out, which is what keeps the
conversation moving instead of looping on the same reply; a question the sheet cannot answer is
skipped, exactly as a user pressing Enter skips it, and a turn that answers nothing ends the capture.

**Cost:** an interactive request is `K × GOLDEN_TURNS` calls (15 at the defaults) where a single-pass
one is `K`. Capture it on its own, not as part of a full-set run.

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

### training-budget
form: allocate-scarce-pool
card: b2b-platform
request: We need to hand out a yearly training budget across departments, with rules for who gets priority when it runs out.
answer.problem: Department heads fight over the budget by email today and the loudest one wins, and finance only discovers the overspend in March.
answer.problem: The real cost is not the money, it is that mandatory certifications get bumped by discretionary courses and we then fail the audit.
answer.current_process: One shared spreadsheet per department, consolidated by HR twice a year, and the two consolidations never match.
answer.current_process: There is no reservation step at all: a manager books with the vendor first and tells HR afterwards.
answer.success_metrics: Zero certification lapses, and finance seeing committed spend within a week of the booking rather than at year end.
answer.success_metrics: We would also count it a success if HR stopped spending two weeks each January reconciling the spreadsheets.
answer.actors: Department heads request, HR validates eligibility, finance owns the envelope, and the employee books the course.
answer.actors: For the three regulated entities a compliance officer has to countersign anything that is a certification renewal.
answer.business_objects: A request carries the employee, the course, the vendor quote, the department and the fiscal year.
answer.business_objects: The envelope is an object too: an amount, a period and a scope, and it can be split by cost centre.
answer.business_rules: Certifications outrank everything, then seniority within the department, then first come first served.
answer.business_rules: Unused budget carries over one year for the regulated entities and is lost everywhere else.
answer.business_rules: A mid-year joiner gets a pro-rata entitlement, and a leaver's committed but unspent amount returns to the envelope.
answer.workflow: Request, eligibility check, budget reservation, approval, booking, then invoice reconciliation.
answer.workflow: A reservation has to be able to expire: if nobody has booked within thirty days the money goes back to the pool.
answer.permissions: A department head sees only their own envelope; HR and finance see all of them.
answer.permissions: The compliance officer sees certification requests across every entity but must not see amounts.
answer.integrations: Bookings come back from the vendor portal as a CSV, and the invoices land in the accounting system.
answer.integrations: We have not decided whether the tool pushes to accounting or accounting pulls from it.
answer.constraints: The fiscal year is not the calendar year for two of the entities, and the tool has to close within five working days of year end.
answer.constraints: Everything has to work for a department head on a phone, because half of them are never at a desk.
answer.config_vs_custom: Every client we roll this out to orders the priorities differently, so that has to be configurable rather than coded.
answer.edge_cases: A vendor can cancel a course after the money is committed, and the refund can land in the next fiscal year.
answer.edge_cases: Two department heads can request the last remaining seat within the same minute.
answer.reporting: Finance needs committed versus spent versus remaining per envelope, and an audit trail of who overrode a priority.
answer.reporting: The auditor asks for the state of an envelope as it stood on a given date, not just as it stands now.
answer.acceptance: It is accepted when a full year can be replayed from the audit trail and matches the accounting system to the cent.
answer.risks: The main risk is that department heads keep booking with the vendor first, and the tool then records fiction.
answer.risks: The second is that we roll it out mid-year and nobody can say what the opening balances should be.
