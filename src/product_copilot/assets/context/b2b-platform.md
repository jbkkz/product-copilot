# Context card — B2B Platform (example)

## Who
- Business domain: enterprise management (HR, projects, invoicing, contractors)
- Product type: **configurable multi-client platform** → `config_vs_custom` slot ACTIVE
- Typical users / roles: employee, manager, HR, administrator, external client

## The product / module
- What it does: centralizes leave, contracts, freelance missions, invoicing and project tracking
- Main business objects: Employee, Leave/Absence, Contract, Invoice, Mission, Freelancer, Client, Contact
- Key concepts: approval circuits, balances/quotas, state-based lifecycles, role-based notifications

## Domain notes (per entity — helps estimate impact)
- **Contract**: has a lifecycle (draft → signed → active → ended). Signature is an **event** other
  modules subscribe to; it is a common trigger for downstream automation.
- **Amendment** (avenant to a contract): has its own **draft → issued** lifecycle, distinct from the
  contract's. Editing a *draft* amendment is free; changing an *issued* one is a new versioned document,
  not an edit — it re-opens signature and can re-trigger downstream automation. "Modify the contract"
  almost always means "issue an amendment", which is a different build from patching a field.
- **Invoice**: carries amount source, numbering sequence, payment terms, VAT and currency, plus its
  own status lifecycle (draft → issued → paid). An invoice is rarely "just created" — the amount, the
  numbering and the client's billing rules all have to come from somewhere. A contract is **one-to-many**
  with its invoices (milestones, recurring/subscription, partial billing): "invoice the contract" is
  rarely a single document, and a billing-rule change ripples across every invoice the contract emits.
- **Mission** (freelance): linked to a Freelancer and a Client, has a start and an end. End-of-mission
  is an **event** that can trigger notifications, final invoicing and validation.
- **Client / Contact**: a Client already has Contacts, each with a **role** (billing, HR, operational).
  Notification routing and permissions frequently depend on the contact's role, not just the client.

## The existing surface (already built)
- Major features: absence management, contracts, invoicing, client/contact directory
- Sensitive modules / areas: approval circuits and permission rules are shared across modules —
  a rule change often touches several screens

## Sensitivities & constraints
- Regulatory: French labor law (paid leave, RTT), GDPR on HR data; invoicing follows French VAT and
  legal numbering rules
- Recurring traps:
  - "approval" almost always hides a **balance/quota check** + a **multi-level circuit**
  - "create X automatically" hides: **which exact trigger**, **idempotency** (don't create twice),
    what happens if the **source data is incomplete**, and who can then see/edit the result
  - a trigger is **synchronous or asynchronous** — inline & blocking (the user waits, the action can
    fail in their face and must be rolled back) vs. queued & eventual (retries, ordering, a visible
    "pending" state, and a window where the two systems disagree). The two are different builds; "when
    X happens, do Y" rarely says which, and it changes error handling, UX and cost
  - "notify" implies specifying **who** (which role/contact), **when** (which event), and **which channel**
  - a business rule can vary by **client**, **contract** or **country**
  - a **one → many** change (e.g. one client, several contacts) ripples into UI, permissions,
    notification routing and existing-data migration
  - permissions are forgotten until acceptance testing

## Configurability
- Standard for all: the core of entities and lifecycles
- Client-specific: approval circuits, business rules, labels, rights, notification routing
