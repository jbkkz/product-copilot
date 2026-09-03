---
title: "Read the issue's comments before dispatching a lane"
description: "An empty assignee field is not evidence an issue is free — outside contributors cannot self-assign on this repo."
tool: Agent
match: oss:developer
mode: remind
---

Before this dispatch, read the issue in full — **comments included**:

```
./supertool 'gh-issue:<N>:full'
```

`gh-issues` prints labels and assignees. It does **not** print comments, and a
reservation lives in a comment.

**An unassigned issue is not a free issue.** An outside contributor cannot
self-assign on this repository, so an empty assignee field and a deliberately
held issue render identically on the board.

On 2026-09-02 a maintainer tick read the board correctly, saw no assignee, and
dispatched a lane onto an issue held open as an on-ramp for an external
contributor. Its read was right and its conclusion was wrong.

Two other shapes not to dispatch, both visible in the issue body:

| Shape | What it looks like | Do instead |
| --- | --- | --- |
| Reserved | a comment saying so, often `good first issue` | leave it; say so in the report |
| Maintainer's decision | label `question`, or "Decide whether…" / "What would settle it: either (a) … or (b) …" with no chosen branch | ask JB which branch, then dispatch |

A lane brief must name the branch when the issue offers two. An agent that picks
one itself has made a product decision nobody asked it to make.
