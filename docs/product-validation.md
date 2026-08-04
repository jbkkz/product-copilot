# Product validation

Requivo is well tested and under-validated. The test suite answers *does it do what it says*; it
cannot answer *is what it says worth doing*. That second question has one honest method — run it
against the alternative on real requests and write down what happened.

This is a manual protocol. Deliberately no analytics and no telemetry ship in the open-source
application: a product-usefulness question answered by instrumenting users is a different question,
and a worse one. Run this yourself, on your own past requests.

## The claim under test

> Requivo surfaces the decisions that would change the scope, keeps them traceable, and tells you what
> a changed answer costs — better than a strong prompt to a capable model does.

Notice what is *not* claimed. Not "asks better questions" alone: a good model asks good questions. The
claim is about what survives the conversation.

## The baseline

Not a weak strawman. Give the model the same request and a genuinely good prompt:

```text
You are a senior product manager. Here is a client request.

1. Ask me the questions whose answers would materially change what gets built. Skip the ones that
   wouldn't. Explain why each one matters.
2. Challenge any premise in the request that looks expensive or risky.
3. Once I've answered, write a specification I could take into an estimate.
```

Use the same model Requivo is configured with, so the comparison is about the product and not the
provider. Run the baseline in a fresh chat.

## The requests

At least three, from your own past work, anonymised. Cover different shapes:

1. **Multi-country leave management** — one domain, heavy configurability.
2. **Field technician offline operations** — one domain, hard technical constraint.
3. **Approval workflow with an external integration** — two systems, ownership questions.

A request you already know the outcome of is the most useful kind: you can tell whether a question was
prescient or merely plausible.

## What to record

For each request, in both the baseline and Requivo:

| | Baseline | Requivo |
|---|---|---|
| Questions asked | | |
| …of those, genuinely useful | | |
| …of those, that **changed the scope** | | |
| Generic or filler questions | | |
| Time to a usable brief | | |
| Would you send the brief to a client as-is? | | |

The third row is the one that matters. A question is scope-changing if the answer would have moved
the estimate, the architecture, or what you agreed to deliver. Count it honestly — the temptation is
to credit anything that sounds insightful.

## Then the two things a chat cannot do

These are the actual bet. Test them separately, because they are where the product either earns the
Core's complexity or does not.

**Resumption.** Come back two days later. Open the session. How long before you can act — and how much
of what you knew survived? Do the same with the baseline chat.

**Change impact.** Change one answer you already gave. In Requivo, record what it reports: which topics
moved, which decisions need re-validating, which documents need updating. In the baseline, ask the
model the same question and check its answer against what you know to be true. Requivo's answer is
computed from the dependency graph; the baseline's is generated. Note where they differ, and which one
was right.

If Requivo's advantage does not show up here, it does not show up. Everything upstream — the slots,
the readiness rules, the revisions — exists to make these two moments work.

## Recording the result

Write it down as you go, in a scratch file, not from memory afterwards. Memory rounds toward whatever
you hoped would happen.

Three outcomes are all worth having:

- **It wins on change impact and resumption.** Then the product hypothesis holds, and the next work is
  making that visible earlier in the flow.
- **It wins on questions but not on the two moments.** Then Requivo is a very good prompt with a
  session store attached, and the Core is over-built for what it delivers.
- **It loses.** Then the interesting question is *why* — usually context. Requivo's questions are only
  as sharp as the [context cards](context-cards.md) it reads. A loss with no cards loaded is a
  different result from a loss with a good one.

## What this is not

Do not fold this into the [golden harness](evaluations.md). That harness measures whether an asset
edit moved the engine's behaviour above the noise floor — a real, narrow, mechanical question. Scoring
subjective usefulness on the same scale would make a judgment look like a measurement, and the number
would then be quoted without its caveats. Keep them apart.
