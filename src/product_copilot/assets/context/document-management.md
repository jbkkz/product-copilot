# Context card — Document Management (GED)

## Who
- Business domain: document / records management — capture, approval, publication, retention of
  business documents (contracts, policies, reports, deliverables, correspondence)
- Product type: **configurable multi-client platform** → `config_vs_custom` slot ACTIVE
- Typical users / roles: author, reviewer, approver, records/compliance officer, reader, administrator

## The product / module
- What it does: manages a document through its whole life — draft, review, approval, publication,
  supersession and archival — with controlled access and a defensible audit trail
- Main business objects: Document, Version, Approval, Signatory, Folder/Category, Classification
  (confidentiality level), Retention rule
- Key concepts: **two independent lifecycles** (approval/signature *and* publication/distribution),
  content immutability after approval, classification-based access, legal retention

## Domain notes (per entity — helps estimate impact)
- **Document vs Version**: a Document is a stable identity; the **Version** is the mutable unit. Once a
  version is approved or signed it is **frozen** — a later "edit" is a **new version**, never an
  in-place change. The prior version is retained and stays referenceable (links, citations, prior
  signatures point at a *specific* version). "Edit the document" almost always means "supersede it
  with a new version", which is a different build from patching a field. (This mirrors the
  contract/amendment pattern, generalised to any document.)
- **Two lifecycles that must not be conflated**:
  - *approval / signature*: draft → in-review → approved/signed. Governs whether the content is valid.
  - *publication / distribution*: unpublished → published → superseded → archived. Governs who can see
    it and which version is the "current" one people act on.
  A document can be **approved but not yet published**, or **published then superseded** (a newer
  version is now current, the old one still exists but is stamped superseded). Confusing "approved"
  with "live/current" is a classic source of people acting on the wrong version.
- **Classification (confidentiality level)**: access is driven by the document's **classification**
  (public / internal / confidential / restricted), not only by folder or role. A single reclassify
  action can change who may read, download, or even see the existence of a document — it ripples
  across search results, listings, notifications and export.
- **Retention rule**: a document has a **retention/disposition** policy (keep N years, then
  archive or destroy). "Delete" is almost never a hard delete — it is **archival** with a retention
  clock, and destruction before the clock expires is itself a controlled, logged act. Legal hold can
  freeze disposition regardless of the normal clock.
- **Approval circuit**: approval is a **multi-step circuit** (sequential and/or parallel reviewers,
  quorum, delegation when an approver is away), not a boolean flag — same shape as any approval
  workflow, and the identity of the re-approver after an edit is rarely the same as the original.

## The existing surface (already built)
- Major features: document capture & versioning, approval circuits, classification & access control,
  publication, retention/archival, full-text search
- Sensitive modules / areas: access-control and classification are cross-cutting — one rule change
  touches search, listings, download, notifications and export at once; the audit trail is relied on
  for compliance and must never have gaps

## Sensitivities & constraints
- Regulatory: retention obligations (legal/sector-specific), GDPR (right to erasure vs. legal
  retention can conflict), auditability of who accessed/changed what; electronic-signature validity
- Recurring traps:
  - "edit a signed/approved document" hides **supersession**: a new version, the old one retained,
    prior signatures invalidated or re-collected, and anything pointing at the old version reconciled
  - "the current document" is ambiguous — **latest version**, **latest *approved* version**, or
    **latest *published* version** are three different things and users mean different ones
  - "who can see it" is driven by **classification**, not just folder/role — reclassifying is a
    high-blast-radius change (visibility, search, notifications, export all shift)
  - "delete" almost always means **archive under a retention rule**; true destruction is a separate,
    logged, sometimes legally-blocked act (legal hold)
  - "approved" ≠ "published/current" — a document can be valid content that is not yet the live one,
    or a superseded one that is still valid but no longer current
  - an **audit trail** (who did what, when, previous vs new value, who accessed) is a hard requirement,
    not a nice-to-have — retrofitting it late is expensive
  - "notify on a document" implies **which event** (submitted, approved, published, superseded,
    expiring retention) and **which role/classification** may even be told it exists
  - templates / generated documents carry their **source of truth** elsewhere — regenerating differs
    from editing, and a template change can ripple across every document produced from it

## Configurability
- Standard for all: the Document/Version identity, the frozen-after-approval rule, the audit trail
- Client-specific: approval circuits, classification scheme & access rules, retention/disposition
  policies, publication targets, labels and notification routing
