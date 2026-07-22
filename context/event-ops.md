# Context card — Event Operations

## Who
- Business domain: on-site event operations — accreditation, check-in, access control and attendance
  tracking for a physical event (conference, convention, gala, roadshow, internal seminar)
- Product type: **event-scoped app** — built or reconfigured per event, with a short life and a fixed
  date → `config_vs_custom` slot ACTIVE, but the question is different: *is this a throwaway app for
  one event, or the first instance of a reusable event kit?* That answer changes almost every build
  decision below, and clients rarely state it.
- Typical users / roles: on-site staff / hostess (the primary operator), event manager, security,
  exhibitor, attendee, client stakeholder watching the numbers

## The product / module
- What it does: holds the expected-attendee list, identifies a person at the door, records their
  arrival, controls what they may access, and produces the attendance record afterwards
- Main business objects: Event, Session / Zone, Attendee (with a Category), Registration,
  Accreditation / Badge, Check-in, Access Right, Staff Device
- Key concepts: **a hard immovable date**, **degraded connectivity**, **peak throughput**, an
  attendee list that is never final, and attendance data that becomes the post-event deliverable

## Domain notes (per entity — helps estimate impact)
- **The event date is a hard deadline with no second chance.** Unlike a platform feature, there is no
  "we'll fix it next sprint" — the app is used for a few hours and the failure mode is a queue at the
  door in front of the client. This inverts the usual trade-offs: a manual fallback for every
  automated path is worth more than an extra feature, and a rehearsal on real hardware in the real
  venue is part of the scope, not a nicety.
- **Connectivity is the defining constraint**, and it is usually assumed away in the request. Venues
  have basements, thick walls, saturated wifi and shared 4G. "The staff scans a badge" needs an
  answer to: does the device work **offline**? If yes, it holds a **local snapshot** of the attendee
  list (how fresh?) and check-ins **sync later** (conflict resolution, ordering, a visible pending
  state). Offline-capable and online-only are two different builds, and the choice drives the
  architecture, not a setting.
- **Identification has to degrade gracefully.** QR / badge scanning fails in real conditions — dim
  phone screens, crumpled printouts, someone who never opened the email, a name spelled differently
  than at registration. A **manual name search with fuzzy matching** and an **on-the-spot
  registration** path are not edge cases; they are a meaningful share of real arrivals.
- **Check-in is not a boolean.** A second scan can mean a duplicate (block it), a re-entry after
  stepping out (allow it), or access to a *different* zone or session (record it separately). One
  attendee routinely produces several check-in events across zones and sessions. Which of these the
  client means changes the data model, not just a rule.
- **Concurrency across entrances**: several staff scan simultaneously at different doors on
  independent devices. Two devices can process the same attendee within seconds — "already checked
  in" must be a defined outcome (warning? refusal? silently fine?), and it is the first thing that
  breaks in a naive build.
- **Throughput is a functional requirement.** Arrivals are not spread evenly — most of the room shows
  up in the twenty minutes before the keynote. Seconds per attendee, measured on the actual device,
  is a spec: a three-second flow and a fifteen-second flow are different products at the door.
- **Attendee Category** (VIP, press, speaker, exhibitor, staff, standard) usually drives access
  rights, badge appearance, routing and what the greeter is told to do. Categories arrive late and
  change late.
- **The list is never final.** Walk-ins, late registrations, cancellations, name corrections and
  substitutions ("my colleague is coming instead") continue up to and during the event day. An app
  that assumes a frozen import will be edited by hand at the door.
- **Attendance data is the deliverable.** After the event the client, and often their sponsors, want
  who actually came, when, and where — for invoicing, for lead follow-up, for a report. Accuracy and
  export are part of the feature, not a post-processing afterthought.

## The existing surface (already built)
- Major features: attendee import / registration, badge & accreditation generation, on-site check-in,
  zone/session access, live attendance dashboard, post-event export
- Sensitive modules / areas: the check-in path is the one thing that must not fail on the day — it is
  the most conservative, most rehearsed part of the build; the sync layer between devices is where
  correctness problems concentrate

## Sensitivities & constraints
- Regulatory: GDPR on attendee personal data (consent, retention after the event, what sponsors may
  legitimately receive), badge photos and identity checks, safety/headcount obligations for the venue
- Recurring traps:
  - **offline capability** is assumed by the client and almost never stated — it is an architecture
    decision (local snapshot + sync + conflict rules), not a checkbox
  - a **manual fallback** must exist for every scan path, because scanning will fail on the day
  - "check in an attendee" hides **duplicate vs. re-entry vs. a different zone/session** — three
    different meanings, three different data models
  - **multi-device concurrency** at several entrances needs a defined outcome for the same attendee
    processed twice within seconds
  - **peak throughput** is a functional spec (seconds per attendee at the door), not a nice-to-have
  - the **attendee list keeps moving** up to and during the event — walk-ins, substitutions and name
    corrections need an on-site path, not an import
  - **staff are not trained users**: the operator is a hostess seeing the app for the first time that
    morning, often one-handed, in bad light — training-free UI is a requirement
  - the **device and the venue** are part of the spec (which phones/tablets, battery for a full day,
    where the network actually reaches) and are only discoverable on site
  - **the date cannot move**, so scope must be cut against a fixed line and a rehearsal has to fit
    inside it
  - **post-event data** (who came, when, which zone) is a client deliverable with its own accuracy and
    export requirements
  - **one event or many?** a throwaway build and a reusable event kit differ in configuration,
    multi-event data model and cost — worth settling before anything else

## Configurability
- Standard across events: the check-in flow, the offline/sync model, the attendee/category model
- Event-specific: branding, zones and sessions, attendee categories and their access rights, badge
  layout, the registration fields, the export format expected by the client
