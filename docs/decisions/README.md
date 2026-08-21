# Decision records

The narrow set of things that belong here, and why the set is narrow.

This project's normal home for a bug narrative is the code, and after that the test that goes red
when the guard is removed. `CLAUDE.md` states the rule under *Where a bug narrative lives*: a
paragraph recounting a past bug must be backed by a red test, and if it is, it belongs **in that
test** with one line and the test's name left at the call site. An external review proposed moving
all of it here instead; that was rejected, because the person about to simplify a subtlety away is in
the editor and a pointer they will not follow is worse than the paragraph it replaced.

So a record here is for **what no test can reach**. In practice that is three shapes:

- **A fact about something outside the repository** — an API's behaviour, a platform's, a service's.
  Nothing here can exercise it, so nothing here can go red for it.
- **A rejected alternative.** Nothing goes red when a path is *not* taken.
- **A cost tradeoff with a threshold** — an argument, not a guard.

If you are about to write a record for anything else, the honest answer is usually a missing test.

## Shape

Four headings, in this order. Keep them; a record that argues in a different order is one nobody can
scan against its siblings.

```markdown
# <Title>

**Slug:** `<stable-kebab-slug>`

## Context
What was true, and what question came up.

## Decision
What was decided, stated so it can be disagreed with.

## What breaking it cost
The concrete failure — the one that makes this worth a file. If there is none yet, say so plainly
rather than inventing one.

## Alternatives rejected
Each with the reason. This is usually the half a reader actually needs.
```

## Referencing one

**By slug, never by path.** Paths in this repository move: the package was renamed once, a module
became a package, and a 2147-line test file became seven, all inside a fortnight. A slug is greppable
and survives every one of those. Write it as `` `decision: <slug>` `` at the line that rests on it, on
one line — a wrap makes it unfindable, which is the failure
`tests/test_narrative_references.py` exists to catch for test names.

The filename carries a number for ordering and the slug for meaning. The number is not the reference.
