# Context card — Financial Reporting

## Who
- Business domain: financial reporting — consolidating operational data into figures a finance team,
  an auditor or a regulator acts on (revenue, cost, margin, budget vs. actual, forecast)
- Product type: **configurable multi-client platform** → `config_vs_custom` slot ACTIVE
- Typical users / roles: operational contributor, controller / analyst, finance manager (approver),
  auditor (read-only), administrator

## The product / module
- What it does: collects figures produced by the operational modules, aggregates them along a
  reporting structure, lets finance review and adjust them, closes the period, and exports the result
  to the accounting system or the finance team
- Main business objects: Reporting Period, Line Item / Entry, Account (chart of accounts), Cost Center /
  Entity, Adjustment, Report, Export, Budget vs. Actual
- Key concepts: **figures are derived, not stored** — a total is the aggregation of underlying entries;
  **period close** (a period stops being mutable); **restatement** (correcting a closed figure);
  reconciliation with the source system; an audit trail on every figure that moved

## Domain notes (per entity — helps estimate impact)
- **A reported total is a derived value, not a field.** It is the sum of underlying entries along a
  dimension (period × account × entity). "Let users edit the total" is therefore never a simple write:
  either the edit is an **adjustment entry** (a new, attributed, reversible line that makes the total
  add up) or an **override** (the derived value is replaced and now silently disagrees with its own
  source data). These are two different builds with different consequences, and the request almost
  never says which. An override without a visible "this figure was overridden, by whom, why" marker is
  how a report quietly stops tying back to the system.
- **Reporting Period**: the unit of mutability. Open → in-review → **closed** → (re-opened). Once a
  period is closed, its figures are frozen and a correction is normally an **adjustment in the next
  open period**, not a mutation of the closed one — re-opening a closed period is a controlled,
  logged, sometimes forbidden act because it invalidates everything already exported or filed from it.
  "Edit the figures" means something different before and after close.
- **Cut-off**: which date puts an entry in a period (transaction date, service/delivery date, invoice
  date, payment date) is a business decision, not a technical one, and different accounts can use
  different ones. Getting it wrong moves money between periods — visible, and expensive to reconcile.
- **Adjustment**: carries an amount, an account, a period, an author, a reason and usually its own
  approval. It is the auditable way a figure changes. Adjustments must be separable from operational
  data in every view — "actuals" and "actuals including adjustments" are two different numbers finance
  will both ask for.
- **Export**: a **contract with a downstream consumer** (an accounting package, a bank, a tax filing,
  or a human's spreadsheet), not a file dump. It has an agreed format, an agreed column/field set, an
  encoding, a rounding convention and often a schema the consumer will reject on mismatch. It is also
  a **point-in-time snapshot**: once sent, the figures it carries must remain reproducible, so an
  export is normally versioned and retained, not regenerated on demand.
- **Restatement**: when a figure changes after it has been exported or published, every artifact
  produced from it is stale — prior exports, sent reports, dependent consolidations. Reporting the
  change is part of the feature, not an afterthought.
- **Currency**: multi-currency reporting carries an FX rate whose *date* is a business rule (rate at
  transaction date, at period end, or an average). The same underlying entries produce different
  totals under different conventions, and finance will notice.
- **Access**: financial figures are role-restricted and often scoped by entity/cost center — who may
  *see* a total, who may *adjust* it, and who may *close* the period are three different rights.

## The existing surface (already built)
- Major features: entry collection from the operational modules, aggregation along the reporting
  structure, period review & close, budget vs. actual, export to the accounting system
- Sensitive modules / areas: the aggregation layer is cross-cutting — a change to how a figure is
  derived (cut-off, FX, which entries are in scope) shifts every report, export and dashboard built on
  it at once; the audit trail is relied on at audit time and must not have gaps

## Sensitivities & constraints
- Regulatory: accounting standards and local filing rules, statutory retention of accounting records,
  auditability of who changed which figure and why, segregation of duties (the person who adjusts a
  figure should not be the one who approves the close)
- Recurring traps:
  - "edit the total" hides **adjustment vs. override** — a traceable entry that makes the sum work, or
    a replacement value that decouples the report from its own data
  - "the figures" is ambiguous — **raw actuals**, **actuals + adjustments**, **the closed/filed
    version**, or **budget/forecast** are different numbers, and stakeholders mean different ones
  - a **closed period** is not editable in the normal sense; the request usually needs a restatement
    or a next-period adjustment path, not an edit form
  - **export is a contract**, not a download: format, field set, rounding, encoding and the consumer's
    validation rules all have to come from somewhere, and a mismatch is rejected downstream
  - an export must be **reproducible after the fact** (versioned, retained) — "regenerate it" is not
    the same as "retrieve what we sent"
  - **rounding and aggregation don't commute** — the sum of rounded lines ≠ the rounded sum; the
    convention has to be stated or the report won't tie out to the penny finance is checking
  - **cut-off rules** decide which period an entry lands in, and are rarely stated in the request
  - **who may see vs. adjust vs. close** are distinct rights, usually scoped by entity/cost center
  - a change to a reported figure **invalidates prior exports and published reports** — propagation and
    notification are part of the feature
  - reconciliation ("does this report still tie back to the source?") is a recurring operational need,
    not a one-off check

## Configurability
- Standard for all: the derived-figure model, the period lifecycle and close, the audit trail
- Client-specific: the chart of accounts and reporting structure, cut-off and FX conventions,
  adjustment approval circuits, export formats and their field mappings, close calendar and rights
