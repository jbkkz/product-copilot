# Appending a required status check to `main`

**Slug:** `appending-a-required-check`

## Context

`main` is protected and requires a list of status checks by their exact names. The list grows: it was
9, then 13 when the four platform legs landed (#55), then 14 with `Plugin manifests (validate
--strict)`, and two more are pending at the time of writing (`Dependency floor`, `Types (pyright)`).

Adding a leg to `.github/workflows/ci.yml` does **not** add it to that list. The two are separate,
and the second is an API call a maintainer makes by hand.

This lived as a 33-line comment block inside `ci.yml`'s `test-platforms` job, which said of itself
that it was "history rather than an instruction — there is nothing to run". It is here because no
test in this repository can go red for it: it is a fact about a GitHub endpoint, not about this code.

## Decision

Append with the `contexts` sub-resource, and verify by reading the list back:

```sh
gh api -X POST repos/jbkkz/requivo/branches/main/protection/required_status_checks/contexts \
  -f 'contexts[]=Test (py3.9, macos-latest)'   -f 'contexts[]=Test (py3.13, macos-latest)' \
  -f 'contexts[]=Test (py3.9, windows-latest)' -f 'contexts[]=Test (py3.13, windows-latest)'

gh api repos/jbkkz/requivo/branches/main/protection/required_status_checks --jq '.contexts | length'
```

Read the count either side of the call and compare. **An endpoint whose failure mode is a silent 200
is one you verify by reading, not by checking an exit code.**

The line continuations are single backslashes. The form that used to sit in `ci.yml` carried doubled
ones, which bash reads as a literal backslash and a broken command.

## What breaking it cost

Nothing yet, and that is the point of writing it down. The command originally recorded here was
`PATCH .../required_status_checks` with `contexts[]`, which **replaces** the list rather than
extending it. Run as written, it would have cut the required checks from 13 to the 4 it named —
dropping `Lint (ruff)`, `Gitleaks`, `fragment`, `Wheel install (no clone)` and all five Python legs.

The API answers 200 either way, so nothing would have said so. It would have surfaced weeks later as
a pull request merging green over a check that no longer ran.

## Alternatives rejected

- **Delete the block rather than keep it.** Considered and rejected: the wrong verb is the one the
  API docs lead you to, so a maintainer reconstructing this from scratch reaches `PATCH` again. The
  record exists to spend the mistake once.
- **Automate it from the workflow file.** A job that reconciles the required-check list with the
  jobs declared in `ci.yml` would need write access to branch protection from CI, which is a strictly
  worse thing to hold than a two-line manual step taken a few times a year.
- **Fold the platform legs into the `test` matrix** instead of a second job. Rejected for a different
  reason, and it is recorded in `ci.yml` at the job it concerns: `main` requires the five `Test
  (pyX.Y)` checks by exact name, so adding an `os` axis renames all five, none of the required checks
  ever reports again, and no pull request can merge.
