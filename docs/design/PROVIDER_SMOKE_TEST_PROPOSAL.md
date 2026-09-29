# A smoke test against a real provider

**A proposal, for approval. Nothing here has been run, and no provider has
been called.**

Written 2026-09-29.

## What it is for

Every model call in the test suite is answered by a canned client. That shows
the application works around a model. It does not show that it works with
one: that a real response parses, that the guard holds on what a real model
writes, that errors from a real provider come through as they should.

This run is for that and for nothing more. It is eight calls on one invented
resume. It cannot say how good the tailoring is, how accurate the grading is,
or how often a model invents something. Those need the evaluation in
`evals/`, run live, which is a separate and larger request.

## What is asked for

| | |
| --- | --- |
| Provider | Anthropic, through the API, with a key of the owner's |
| Models | `claude-sonnet-4-6` and `claude-haiku-4-5-20251001`. These are what the code selects today under the default preset, and the point is to test what the product runs |
| Requests planned | 8 |
| **Requests at most, counting retries** | **10** |
| Retries | 0. The SDK's own retries are turned off for the run, so that a request is a request |
| Estimated cost | About $0.15 to $0.25. At most $0.57 for the planned calls, and $0.85 for ten, if every call used every token it is allowed |
| **Spending bound** | **$2**, set by the owner at the provider, on a workspace and key made for this run |
| Time | 120 seconds a call, 10 minutes in all |
| Input | The invented sample resume, and one public-domain posting from `evals/corpus/postings-v1` |

If the owner's settings name other models, through `ANTHROPIC_STRONG_MODEL` or
`ANTHROPIC_LIGHT_MODEL`, the run stops before its first call and says so. What
is approved is these two models, and a run on others would be a different
run.

If the owner uses OpenAI, or a subscription through a command-line assistant,
this proposal does not apply as written. See "Other providers" at the end.

## The calls

| | Step | Calls | Model | Most output |
| --- | --- | --- | --- | --- |
| 1 | Read the resume: extract its structure | 1 | Haiku 4.5 | 8,192 tokens |
| 2 | Read the resume: review it | 1 | Sonnet 4.6 | 3,072 |
| 3 | Tailor it to the posting | 1 | Sonnet 4.6 | 8,192 |
| 4 | Shorten the summary, if it came back over 60 words | 0 or 1 | Sonnet 4.6 | 400 |
| 5 | One instructed edit | 1 | Sonnet 4.6 | 8,192 |
| 6 | Analyse the fit for one job target | 1 | Sonnet 4.6 | 2,048 |
| 7 | Write three interview questions on one topic | 1 | Sonnet 4.6 | 4,096 |
| 8 | Grade one answer | 1 | Sonnet 4.6 | 2,048 |
| | **Planned** | **7 or 8** | | |

Not in the run: articles (dozens of calls each), voice, web search, fetching a
posting from an address, the coach, the job interview, the study plan.

## The estimate

Prices per million tokens, from the reference table bundled with the assistant
that wrote this, dated 2026-06-24. **They are to be checked against
Anthropic's pricing page on the day, and the estimate redone if they differ.**

| Model | Input | Output |
| --- | --- | --- |
| Claude Sonnet 4.6 | $3.00 | $15.00 |
| Claude Haiku 4.5 | $1.00 | $5.00 |

At most, with 6,000 tokens in to every call and every call using all the
output it is allowed:

| Step | In | Out | Cost at most |
| --- | --- | --- | --- |
| 1, Haiku | 6,000 | 8,192 | $0.047 |
| 2 | 6,000 | 3,072 | $0.064 |
| 3 | 6,000 | 8,192 | $0.141 |
| 4 | 3,000 | 400 | $0.015 |
| 5 | 6,000 | 8,192 | $0.141 |
| 6 | 6,000 | 2,048 | $0.049 |
| 7 | 3,000 | 4,096 | $0.070 |
| 8 | 3,000 | 2,048 | $0.040 |
| **Planned calls, at most** | | | **$0.57** |
| Two more calls at the largest size, to reach the ceiling of 10 | | | $0.28 |
| **Ten calls, at most** | | | **$0.85** |

A tailored resume is about 1,500 tokens, not 8,192, so the expected cost is
about a quarter of the most. The spending bound of $2 is more than twice what
ten calls could cost, so reaching it would itself mean something was wrong.

## What keeps it inside those numbers

An estimate is not a bound. These are the bounds, and each is enforced by
something other than intention.

| Bound | Enforced by | Whose |
| --- | --- | --- |
| **$2 spent** | A workspace made for this run in the Anthropic Console, with a spend limit of $2, and a key made in that workspace. When the limit is reached the provider refuses further requests | The owner's. It is the only bound that holds if everything else fails, and I cannot set it or check it |
| **10 requests** | The harness puts a counter in front of the client. The eleventh request is refused before it is sent, and the run ends | The harness |
| **No retries** | The client for this run is built with `max_retries=0`. The product's clients are built with 2, which is why this is said and not assumed | The harness |
| **120 seconds a call** | The deadline already in `pipeline/providers/clients.py`, set to 120 for the run | The product |
| **10 minutes in all** | The harness stops the run | The harness |
| **Only this provider** | The run has one key in its environment. No OpenAI key, so no voice. No search key. `CLAUDE_CLI_PATH` points at nothing, so search cannot fall back to a command-line assistant and spend a subscription | The harness |
| **Only these models** | Checked before the first call, and the `model` in every response is checked against what was asked for | The harness |
| **Only invented data** | The harness takes no resume as an argument. It reads the sample and one posting from the repository | The harness |

The key is never shown to me and never passes through a chat. The owner puts
it in a settings file of its own, and gives the harness the path.

## When it stops

At the first of these. It does not continue past one to see what else
happens.

1. Any request fails: an error from the provider, a timeout, a response that
   does not parse.
2. A response names a model other than the one asked for.
3. The guard finds, in what it let through, a name or a number that is on
   neither the sample resume nor the instruction. This is the finding the run
   exists to look for, and one is enough.
4. The tenth request has been made.
5. Ten minutes have passed.
6. The provider refuses a request for having reached the spending limit.

## What is kept

Written to `evals/results/`, which is not kept in the repository.

- For each call: the step, the model asked for and the model that answered,
  how long it took, the tokens in and out **as the provider reported them**,
  and why it stopped.
- The cost, worked out from those token counts. Measured, not estimated.
- What the guard changed, and why.
- The tailored resume and the graded answer, which are about an invented
  person.

Not kept: the key, the request headers, anything from the settings file.

A summary with no model output in it is what would go in the ledger: the
provider, the models, the date, the number of calls, the tokens, the cost,
and whether each step passed.

## What it would and would not show

| It would show | It would not show |
| --- | --- |
| That real responses parse into the application's schemas | That they parse every time |
| That the guard runs on real output and what it does with it, once | How often a real model writes something unsupported |
| That a finished export is produced, or refused, correctly from real output | That the tailoring is good |
| That a real grade is returned for a real answer | That the grade is right |
| What eight calls cost | What a user's month costs |

One run is one run. A pass means nothing was found to be broken on that day
with that input. It is not evidence of readiness for production, and the
ledger would not describe it as such.

## Other providers

**OpenAI.** The same plan, with the models the code selects for OpenAI, its
prices, and a project with a budget in place of a workspace with a spend
limit. It would need its own estimate, and I have not made one, because I do
not have OpenAI's current prices from a source I can name.

**A subscription, through a command-line assistant.** Not recommended for
this. A subscription reports no token counts and has no spending limit that
can be set for one run, so the cost can be neither bounded nor measured, and
the bound would be the request count alone.

## The approval asked for

To run, once, the eight calls above, on the invented sample, against
Anthropic, with `claude-sonnet-4-6` and `claude-haiku-4-5-20251001`, with
retries off, stopping at ten requests, ten minutes, or the first failure,
using a key made by the owner in a workspace with a spend limit of $2.

Before running I would need from the owner:

1. That the provider and the two models are the ones to test.
2. That the workspace, its limit, and its key exist, and the path of the
   settings file that holds the key.
3. That the prices above are still the prices.

The harness would be written first and reviewed before it is run. It does not
exist yet.
