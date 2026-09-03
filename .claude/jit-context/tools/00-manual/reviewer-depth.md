---
title: "One reviewer per change, two only where it pays"
description: "Measured 2026-08-20: 12 per-change review passes returned ~3 cosmetic findings and zero correctness bugs."
tool: Agent
match: oss:auditor
mode: remind
---

**Spawn a second reviewer only if the diff touches one of these:**

- `src/requivo/core/`
- `src/requivo/services/`
- anything that persists (`core/persistence.py`, session or artifact writes, a lock)
- the **shape** of a public payload — a `--json` key, `session.json`, an export envelope

Everything else gets **one**. A docs-only, CI-only or scripts-only diff gets one.

Measured cutting 1.0.0 on 2026-08-20: **14 independent passes over one delta.**
Twelve were per-change — six issues, two reviewers each — and returned about three
findings, **all cosmetic** (a docs link to the wrong file, a claim in a changelog
fragment). **Zero correctness bugs in shipped code.** The two findings that mattered
came from the release-level passes, which see composition and no per-PR review can.

On a diff that adds a dict literal, a cross-platform auditor spends four defect
classes to conclude that nothing touches a path or a separator.

**This is not "review less".** Release-level audit rounds stay, both of them. The
saving is at the per-change layer, where the second pass was measured and did not pay.
