# Compatibility and deprecations

> What Requivo promises not to break, what it may change, and what is on the way out.

Requivo is versioned with [SemVer](https://semver.org/). This page is the specific list of what that
covers.

## What 1.0 means, and what it does not

**1.0 is a statement about the interface, not about the code.** It does not claim the product is
finished, mature, or feature-complete. It says one thing:

> From 1.0.0 onward, a breaking change to the public surface costs a **major** version, and is
> announced here and in the changelog before it lands.

That is a *weaker* promise than the ones already on this page — "nothing is removed in a patch",
"anything deprecated keeps working for at least one minor version and names its replacement" — which
Requivo held throughout 0.x and continues to hold. Nothing on this page gets looser at 1.0. What
changes is the price of breaking it.

**The public surface is exactly four things**, each with its own section below:

| Surface | What is promised |
|---|---|
| The session format (`.requivo/sessions/`, `format_version` 1) | layout, forward and backward compatibility, an explicit migration frontier |
| `--json` outputs | key sets, and `null` meaning *could not read* rather than a value |
| The error-code vocabulary | one code, one fact, one `details` shape |
| CLI verbs, flags and exit codes | spelling, meaning, and a help text that describes what happens |

**Everything else is not covered, and the boundary is the point.** A promise that absorbs everything
is one nobody can keep, so [What is explicitly *not* stable](#what-is-explicitly-not-stable) is as
load-bearing as this section: Python internals (`requivo.core`, `requivo.services`,
`requivo.providers` are importable and documented, and are the engine's own structure, not an API),
prompt and context-card content, and terminal output layout. Those are tuned continuously and a
consumer that depends on them is deliberately tracking the repo.

**What a 2.0 would have to contain.** Renaming or repurposing a populated session field beyond what
`migrate_session()` can carry; moving a condition from one error code to another; removing a CLI verb
or flag, or changing what one already means; changing an exit code's meaning; removing a `--json` key
or changing its type. Adding to any of those is not breaking and does not wait for a major — the
whole design of the four contracts above is that they grow additively.

**1.0 arrived after the interface stopped moving, not after the features did.** The error-code
vocabulary moved twice in the release that carries this note — `cross_site_request` split into six
codes, and the `artifact save` refusal took a code of its own — and both were deliberate cleanups
taken *before* the freeze precisely because they cost nothing here and a major version afterwards.
That is what a 1.0 release is for.

## The session format is public

`.requivo/sessions/<slug>/` is the interface between the CLI, the Claude Code plugin, the Web app, and
anything you build on top. It is a **published contract**, not an implementation detail: the layout is
documented in [session-format.md](session-format.md), and a session written by one interface is
readable by all the others and by later versions.

Concretely, guaranteed:

- The directory layout: `session.json`, `request.md`, `model.json`, `revisions/NNNN-model.json`,
  `artifacts/`.
- `model.json` is a serialized model whose slot ids come from `framework/model_schema.json` — the same
  vocabulary `requivo schema` prints.
- `session.json` carries `format_version`. Today it is **1**.
- **A session written by an older Requivo keeps loading.** A field that has been retired is ignored
  rather than fatal; a field added since takes its default. Both are pinned by a test that loads a
  frozen 0.8.2 `session.json` verbatim.
- **A session written by a newer Requivo is refused, clearly** (`invalid_session`, "upgrade requivo")
  rather than half-understood.
- **An older Requivo preserves a field it does not understand.** Since 0.9.4, an unknown key in
  `session.json` survives a round-trip: the older reader loads the session, mutates it, writes it back,
  and the newer version's field is still there. This is stronger than "readers ignore what they do not
  know", and it is what makes adding a field genuinely safe — before 0.9.4 the unknown key was dropped
  the first time an older Requivo wrote the file, so a mixed-version workspace quietly destroyed it.
  Keys that Requivo has *retired* are the deliberate exception: those are dropped, in `migrate_session()`.
- **The same is now true of `model.json`, and was not before** (#14). This page said "adding a
  field, anywhere" from the start and only `session.json` delivered it. `model.json` and
  every `revisions/NNNN-model.json` were read through the same contract an LLM reply is validated
  against, which is `extra="forbid"` on purpose, so a key added by a later version did not merely get
  dropped on write — the session could not be opened at all, and the refusal arrived as a Pydantic
  traceback rather than as a message naming the upgrade. Reads now go through a permissive sibling
  contract; the provider boundary is unchanged and still refuses an invented field, because there the
  unknown key means a drifted prompt and there is a retry that can correct it. Preservation is the
  same promise `session.json` carries: the key survives a **load, a mutation and a write back**, so
  an ordinary refinement turn through an older Requivo keeps it rather than quietly destroying it.
  Two limits are worth stating rather than leaving to be discovered. An *apply* **replaces** the
  slots, the summary and the questions with the ones the proposal carries, so an unknown key inside
  one of those does not survive a turn — it is superseded by a value this version built, not dropped
  by a reader that could not hold it. The reasoning layer (`decisions`, `challenges`,
  `opportunities`) and any key at the top level are carried, because a turn that says nothing about
  them is not a turn that deleted them. And an unknown **slot id** is still refused — that is
  `schema_version`'s frontier, described below, and absorbing a future slot as an unknown key would
  route it around the clear refusal that frontier exists to give.

What may change without a `format_version` bump:

- **Adding** a field, anywhere. Readers ignore what they do not know, and preserve it on write.
- **Retiring** a field that was never populated (this is how the unused session-level
  `prompt_versions` map was removed in 0.9.2).
- Adding a slot to the schema, a new artifact type, or a new value in a provenance field.

What requires a `format_version` bump, an entry in the changelog, and a migration in
`migrate_session()`:

- Renaming, removing or changing the meaning of a *populated* field.
- Changing the directory layout or a file's role.
- Any change that would make an older session read *incorrectly* rather than merely incompletely.

Slot ids are a separate contract with its own `schema_version`, recorded on every session — and, since
0.9.5, actually enforced: a session authored against a *newer* slot schema is refused with the same
clarity as a newer `format_version`, instead of failing later as an `unknown_slot` error naming a slot
the user never typed. An older `schema_version` keeps loading.

## The `--json` outputs are public

`requivo status --json`, `model apply --json`, `model diff --json`, `artifact list --json`, `session
list --json`, `doctor --json` and the structured error envelope (`{code, message, path?, details?}`)
are what the Claude Code plugin drives. They follow the same rule as the session format: fields get
added, populated fields do not change meaning without a note in the changelog.

Error `code` values are stable identifiers — assert on the code, never on the message text.

**A code carries one fact, and one `details` shape.** That is what makes the advice above safe to
follow: matching a code and then reading a key out of `details` has to work for every payload
carrying it. Two changes in 0.10.0 were needed to make it true.

- **`empty_selector_token` split into two codes.** It carried two facts with two shapes: an empty
  *token inside* a selection (`{selector, position}`) and a selection that is *itself* empty
  (`{selector, tokens}`). A consumer matching the code and reading `details["position"]` got a
  `KeyError` from a payload that correctly carried the code it matched. Since 0.10.0:

  | Condition | Code | `details` |
  |---|---|---|
  | a stray comma — `--context "a,,b"` | `empty_selector_token` | `{selector, position}` |
  | a selection that selects nothing — `--context ""` | `empty_selection` | `{selector, tokens}` |

  **If you match `empty_selector_token`, match `empty_selection` alongside it.** The message is
  unchanged for both; only the code moved for the second case, and `position` is now guaranteed
  present on every `empty_selector_token` payload.

- **`no_context_cards` is new.** An install with no context cards at all now refuses rather than
  reasoning with an empty product context. It is distinct from `unknown_context_card` (the card you
  named is not there) and from `context_unreadable` (we could not look): here we looked, at every
  root, and there is nothing. `details` carries `{roots}` — the directories searched.

- **`no_context_cards` now also reaches session creation** (#41). It was wired into the calls that
  *read* the cards and not into `resolve_cards`, the validator every surface runs on the way in — so
  on a card-less install, naming a card at creation answered `unknown_context_card` (a name you
  typed wrongly) and the very next call answered `no_context_cards` (an install that is incomplete).
  One condition, two codes, and the misleading one arrived first. A caller matching
  `unknown_context_card` on a creation path should match `no_context_cards` alongside it; nothing
  changes on an install that has cards, where an unknown name is still `unknown_context_card`.
  Passing *no* selection at all is unaffected and still resolves to the every-card sentinel — the
  install is caught when the cards are read, as before.

- **`unsafe_selector_token` is new** (#40). A selector token — a context card name or a slot token —
  carrying a control character is refused rather than echoed back. `details` carries
  `{selector, position}`, the same shape as `empty_selector_token`. This can turn a *previously
  accepted* stored value into a refusal: a `session.json` whose `context_cards` holds a name with an
  embedded newline now reports `unsafe_selector_token` from `doctor` and `session verify` instead of
  printing it. No name Requivo itself writes is affected — `resolve_cards` has always resolved a
  selection against the installed cards, so such a value can only have arrived by import or by hand.

- **`doctor --json` gained `sessions.non_sessions`** (#67). Additive: a consumer reading only the
  existing `sessions` keys is unaffected, and on a workspace Requivo alone has written it is always
  `[]`. It carries what is under the session root and is *not* a session — one object per entry with
  `name`, `kind`, `entries`, `entry_count`, `error` and `slug_shaped`. See
  [Something here that is not a session](cli.md#something-here-that-is-not-a-session).

  **`null` and `[]` mean different things**, at both levels. The key itself is `null` when the session
  root could not be listed at all, matching `sessions.total`; `[]` means it was listed and holds
  nothing but sessions. Within an entry, `entries: null` with an `error` is a directory that could not
  be listed, and `entries: null` with no error is a `file` or an `other` — there is nothing to look
  inside. Branch on `error`, never on emptiness.

  Nothing is deleted, moved or rewritten as a result, and no field states a conclusion: there is no
  `is_lock_ghost`. `slug_shaped` is the one derived value and it is a property of the name — whether
  `create_session` can be asked for it — not a claim about where the entry came from.

- **`session list --json` gained two fields, and a row can now be degraded** (#62). Every row carries
  `readable` (a boolean) and `error` (the reason, or `null`) alongside `slug`, `revision`, `provider`
  and `updated_at`. Both are additive, so a consumer reading only the original four is unaffected on
  a workspace where every session loads.

  What is new is that the command **no longer fails for the whole set** when one session cannot be
  read — a `session.json` from a newer Requivo, or one left half-written by a crash. It used to exit
  1 with a single message and no payload at all; it now prints the complete listing and exits **4**.

  A degraded row **keeps the same key set** as a healthy one, with `null` in `revision`, `provider`
  and `updated_at` rather than a missing key or a plausible `0`: a consumer looping over the payload
  reading `row["revision"]` gets `None` from a row it was handed deliberately, not a `KeyError`.
  **Branch on `readable`**, and treat `null` in those three as *we could not read this*, never as a
  value. A session at revision 0 is `readable: true` with `revision: 0` — not analysed yet is a
  normal state and is not this one.

  The **terminal** output of the same command changed alongside it, in the same way `#40` changed
  `doctor`'s: a `slug`, `provider` or `updated_at` carrying a control character is now escaped rather
  than echoed, because all three are read back out of `session.json` and could otherwise write what
  reads as a second, authoritative row at column 0. A value that is already one safe line is returned
  byte-for-byte, so no session Requivo itself wrote is affected — such a value can only have arrived
  by import or by hand. `--json` is unchanged and was never affected: `json.dumps` escapes it.

- **`session show`'s terminal output escapes the same way, in eight fields** (#70). The identical
  defect, in the other verb. #62 found it while fixing the listing and reported it rather than riding
  it in on that diff, which is why the two land as separate changes. `slug`, `session_id`, `created_at`,
  `updated_at`, `provider`, `model_name`, each key of `artifact_status` and each artifact's
  `filename` are all bare strings in `session.json`'s body, and all eight reached the terminal
  unescaped. `current_revision`, an artifact's `revision` and its `stale` flag are untouched and need
  nothing — `read_meta` refuses a string in an `int` or a `bool` before the render runs.

  It is **eight** rather than the five the issue names: #62 listed the five that are `SessionMeta`
  scalars, which left out `slug` and the two fields that live on `ArtifactStatus` and its dict key.
  Recorded here because a count in a compatibility note is the thing a later reader checks their own
  work against.

  **`artifact list` renders two of the same fields and is fixed alongside it** (#70). It prints the
  `artifact_status` key and the `filename` at the same fixed column, off the same file, through
  `ArtifactService.list`. It is outside the issue's own footprint and is named here for that reason:
  fixing one verb's copy of a two-field render turns the rule from *a persisted value is escaped
  where it is shown* into *it is escaped where somebody looked*.

  Same rule as above, so the same guarantees: a value that is already one safe line comes back
  byte-for-byte and no session Requivo itself wrote changes, and `--json` is unaffected on all three
  verbs.

  **The reason `--json` is unaffected is narrower than #62 stated it**, and is corrected here rather
  than left standing. `_session_list_line`'s docstring, and #70's own issue text, both said
  `json.dumps` defaults to `ensure_ascii=True` and therefore escapes a control character — the bullet
  above says only that `json.dumps` escapes it, which is true and is why it needs no correction of its
  own. Measured, the flag is not
  what protects a newline: JSON's grammar forbids a literal control character below `U+0020` inside a
  string, so `\n` is escaped whether the flag is on or off. What the flag decides is the *non-ASCII*
  half of the guarded range, `U+007F`–`U+009F` — which carries `NEL` (`U+0085`), a line terminator
  `str.splitlines()` and some terminals honour, and `CSI` (`U+009B`), an escape introducer. So the
  default **is** load-bearing, for a different set of characters than was written down, and a test
  now probes both halves; one probing only with a newline is green either way and pins nothing.

- **`artifact save` without `--revision` is refused, and carries `unstated_source_revision`** (#6,
  #57). It used to succeed, filling the omission in with the session's current revision and recording
  `stale: false` — an answer that could not come out any other way, about a revision the caller never
  claimed to have read. Every documented invocation already passes the flag, so this only reaches a
  caller relying on the undocumented default, which was being handed a fabricated provenance. The
  condition itself is new rather than moved: nothing previously carried a code here, because nothing
  previously failed.

  **The code moved once before it shipped.** #6 raised the refusal under `invalid_session`; #57 gave
  it one of its own. Moving a condition to a new code is breaking under the rule at the foot of this
  section, and it is recorded as such — but no released version ever answered `invalid_session` here,
  because #6 and #57 land in the same release, so there is no consumer to break. What forced the
  inheritance was that a new code needs a row in the Web's status table and that file was held by
  another change in the same round; the precision sat in the exception *type* meanwhile, which a
  caller reading a serialized envelope cannot see. `UnstatedSourceRevisionError` is still an
  `InvalidSessionError` subclass, so catching the class is unaffected either way.

  `details` carries `{slug, type, source_revision, current_revision, cause}` — the same five keys as
  the refusal for a source revision that cannot be *read*, which kept `invalid_session`. Both are
  always present: here `source_revision` is `null` because none was stated and `cause` is `null`
  because no underlying failure occurred, and on the unreadable side `cause` names the exception type
  and its text. The shared shape **survives the split as a choice**, not an obligation: one code, one
  shape no longer binds two codes together, and narrowing this payload would break a consumer reading
  `details["cause"]` across both arms for no gain. `opaque_origin` and `origin_mismatch` below are the
  same answer to the same question. `tests/test_artifact_provenance.py` asserts that the two codes
  differ and that the two key sets still agree.

- **`cross_site_request` split into six codes** (#52). Requivo Web's cross-site guard raised one code
  for six distinct facts, and their `details` payloads had five different shapes between them — the
  exact condition this rule was written for. A consumer matching `cross_site_request` and reading
  `details["origin"]` got a `KeyError` from a payload that correctly carried the code it matched.

  | Condition | Code | `details` |
  |---|---|---|
  | no host could be read — absent, empty, or not an authority | `undetermined_host` | `{host_header_present, host_header, hint}` |
  | the host was read and is not one this server answers to | `host_not_allowed` | `{host, hint}` |
  | the browser's `Sec-Fetch-Site` says another site | `cross_site_fetch` | `{sec_fetch_site}` |
  | `Origin: null` | `opaque_origin` | `{origin, host}` |
  | the origin is not the host's trust domain | `origin_mismatch` | `{origin, host}` |
  | the request token was absent or wrong | `missing_request_token` | `{}` |

  **All six are still 403**, and all six are still `CrossSiteRequestError` subclasses, so a caller
  catching that class or matching on the status is unaffected. `cross_site_request` survives as the
  family base and keeps its status row, but **nothing raises it any more** — if you match that string,
  match the six above instead. `opaque_origin` and `origin_mismatch` share a `details` shape and are
  still two codes: a shared shape is not a shared meaning.

  This was inert before it was fixed, and deliberately noted anyway. Requivo Web does not serialize
  `details` — a refusal renders as HTML — so no consumer could observe the inconsistency, and an
  argued exception to the rule was the other defensible answer. What decided it is that the cost was
  already being paid: both #43 and #45 had to distinguish their new arm **by message**, which is the
  one handle this document tells you never to use.

Adding a code is not a breaking change under this policy, but *moving a condition to a new code* is,
so both are noted here and in the changelog rather than only in the latter.

### HTTP statuses in Requivo Web

The Web maps each code to a status, and **every code has an explicit mapping** — the table used to
default to 400, so a code added in one place reached the browser as "your request was bad" whatever
it actually meant. Consumers scripting against the Web should note the four that moved off that
default in 0.10.0, and the one that is new:

| Code | Was | Now | Why |
|---|---|---|---|
| `context_unreadable` | 400 | 500 | the server cannot read its own card directory |
| `no_context_cards` | — | 500 | the install shipped no cards; nothing the caller sent caused it |
| `provider_output_invalid` | 400 | 502 | upstream would not hold the contract, after every retry |
| `session_locked` | 400 | 503 | the write never started; retrying it unchanged is correct |
| `session_exists` | 400 | 409 | a conflict with the store's state, like `revision_conflict` |

An unrecognised code is now a 500 rather than a 400, for the same reason: "we could not classify
this" is not evidence that the caller erred.

`unstated_source_revision` is new in the Web's status map and is a **400** (#57). The request is
incomplete — the one fact only the caller holds was not stated — and nothing about the session is
wrong, so it sits with the other 4xx rows rather than with the 409 conflicts, which are about the
store's state. **No status moves for it**: the condition answered 400 under `invalid_session` before
the split, so what changed is the code the payload carries, not the number a client branches on.

No status in that table moves for #41, but one **condition** crosses it. `POST /sessions` naming a
context card on an install that has none used to answer **400** — the reader was told their request
was bad — and now answers **500**, because the code it raises changed from `unknown_context_card` to
`no_context_cards`. That is the split this table exists to hold: nothing the caller sent caused an
install to ship without cards, and the previous 400 was the same misattribution one call earlier
than the one #34 fixed. A client scripting session creation and branching on a 4xx should expect a
5xx for this condition.

One populated field has changed meaning under that rule, and the changelog carries the note:
`doctor --json`'s `sessions.total` is `null`, not `0`, when the session directory could not be read
at all. It reported `0` before, which was indistinguishable from an empty workspace — a reader
gating on `total == 0` was being told "you have no sessions" by a check that had not managed to look.
The sibling `sessions.readable` says which case you are in.

## What a proposal means

The JSON you hand to `model validate`, `model diff` and `model apply` is a **proposal**, and since
0.9.6 its shape is stated rather than implied:

- `model` is the complete slot set. An apply *replaces* the model — it does not merge — so a partial
  one is refused. Check a projection with `model validate --allow-partial`; that flag is gone from
  `apply` and `diff`, where it read as "apply a patch" and silently replaced a fifteen-slot model
  with whatever subset was sent.
- `summary.objective` must say something. A model of filled slots with nothing naming what it is for
  renders as a blank heading everywhere, so it is refused as incomplete (`invalid_model`).
- `decisions`, `challenges` and `opportunities` are **tri-state**: leaving the key out means "not
  speaking to it" and keeps what is established; `[]` deletes; a list replaces. Before 0.9.6 an
  omission was read as an empty list, so an ordinary refinement turn deleted the whole reasoning
  layer — and reported no change while doing it.

## Deprecations

| What | Status | Since | Removal | Instead |
|---|---|---|---|---|
| **`model apply --allow-partial`, `model diff --allow-partial`** | Removed — it merged nothing, it replaced | 0.9.6 | gone | `model validate --allow-partial` to check a projection; send the full slot set to apply |
| **Legacy flag CLI** (`python src/engine.py "…" --prd`) | **Removed** | deprecated 0.9.2 | 0.9.8 | `requivo discover` + `requivo prd` — see [cli.md](cli.md) |
| **`pc` command alias** | **Removed** | deprecated 0.7.0 (rename) | 0.9.8 | `requivo` |
| **Implicit `out/<slug>/` fallback** | **Removed** — migration is explicit | deprecated 0.8.0 | 0.9.8 | `requivo session migrate`, then `.requivo/sessions/` |
| **`/requivo-<skill>` plugin skill names** | Renamed | 0.9.2 | gone | `/requivo:<skill>` — Claude Code namespaces plugin skills |

The policy: anything deprecated keeps working for at least one minor version, says so when used where
that is possible, and names its replacement here. Nothing is removed in a patch release.

**The 0.9.8 removals.** These three were the last of the pre-store architecture, all deprecated for
several versions and none of them with a known user. They were carried on a "removal in 1.1.0" plan,
which would have meant maintaining them *through* 1.0 — the moment you least want
two answers to "where does a session live?". The public interface is unchanged: `requivo` and the
session store were already the only supported path. If you have `out/` sessions, `requivo session
migrate` still converts them, and it is now the only thing that reads that layout.

## What is explicitly *not* stable

- **Python internals.** `requivo.core`, `requivo.services` and `requivo.providers` are importable and
  documented, but they are the engine's own structure, not a published API. A refactor can move them.
  A downstream consumer that depends on them deliberately tracks the repo.
- **Prompt and context-card content.** These are tuned continuously — that is the point of the
  [golden harness](evaluations.md). Two versions can reason differently about the same request; the
  provenance recorded on each revision (model + prompt hash) is what makes that traceable.
- **Terminal output layout.** Parse `--json`, never the rendered view.
