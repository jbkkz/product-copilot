---
title: "How work lands: branch, PR, squash — never a push to main"
description: "Branch protection forbids merge commits and refuses a force-push, so a local merge is a bypass that cannot be undone."
tool: Bash
match: ~(git-push|gh-pr-merge|(^|[;&|[:space:]])git[[:space:]]+(push|merge))
mode: once, remind
---

**Every change goes through a pull request, squash-merged.** Branch, push the
branch, open the PR, let the required checks go green, then squash.

**Do not push to `main` directly unless JB asks.** Pushing a *branch* for its PR
needs no permission. Branch protection forbids merge commits and refuses a
force-push, so a local merge is a bypass and **cannot be undone afterwards**.

The one exception: the release commit, `chore(release): X.Y.Z`, goes straight to
`main` and carries no `(#N)`. Everything else carries one.

**Never quote the number of required checks — measure it.**

```
gh api repos/jbkkz/requivo/branches/main/protection
```

A PR shows more legs than are required; those are two different questions. The
same rule covers the version, the test count, and any number in prose that no test
can falsify: it buys one release and then lies.

**`gh-prs` with no filter answers a different question.** It lists what is *open*.
On a repo whose PRs are all merged it prints `No PRs match`, which is not *there is
no PR practice*. Use `gh-prs:state=all`.

**Re-running a failed check can bury a passing one.** GitHub resolves a required
check by the **most recent** run of that name, and a re-run replays the original
event payload — so a label added afterwards is still invisible to it.
