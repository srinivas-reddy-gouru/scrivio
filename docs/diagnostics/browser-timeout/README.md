# One browser test that timed out once

**Open. The cause has not been established.**

## What happened

On 28 September 2026, on macOS, one full run of the suite stopped at

    tests/browser/test_workflows.py::test_resume_import_review_edit_and_export[dev]

with `Page.goto: Timeout 30000ms exceeded`, loading `/#/desk` from the Vite
development server. The same test against the built interface had passed
moments before. Run alone it passed, and it has passed in every full run
since, on macOS and on Linux.

The log of that run is kept as it was, with local paths removed:
[2026-09-28-the-failure.txt](2026-09-28-the-failure.txt). It says a page was
being waited for. It does not say what the page was waiting for, because
nothing recorded that.

## What has been established

Measured on 29 September with [measure.py](measure.py), which starts the same
two servers the tests start and records every request the page makes.

| Condition | Page loaded in | Record |
| --- | --- | --- |
| Dependency cache warm, font host answering | 0.18 to 0.28 s, three loads | [warm-as-is](2026-09-29-measured-warm-as-is.json) |
| Dependency cache removed first | 0.21 and 0.24 s. Vite rebuilt the cache in 1.2 s | [cold-as-is](2026-09-29-measured-cold-as-is.json) |
| Font host refusing at once, as when offline | 0.18 s | [warm-refused](2026-09-29-measured-warm-refused.json) |
| **Font host reached and never answering** | **Did not load. `Page.goto: Timeout 30000ms exceeded` at 30.05 s** | [warm-never](2026-09-29-measured-warm-never.json) |

1. **A cold dependency cache is not the cause.** This was my first guess, and
   I recorded it in the ledger as a guess. It is wrong. Rebuilding the cache
   takes about a second, and the page loads in a quarter of one afterwards.

2. **A font host that does not answer produces exactly this failure.** The
   interface loads its fonts from Google Fonts through a stylesheet in the
   head of the page. A browser will not draw a page until the stylesheets in
   its head have been answered. With that one request held, all 28 requests to
   this machine finished in under 0.05 s, the page never drew, and loading it
   timed out with the same message, at the same 30 seconds.

## What has not been established

**That this is what happened on 28 September.** The second finding shows a
cause that is sufficient. It does not show that it was the cause. The failure
left no record of its requests, so there is nothing to compare with.

What is against it being the whole story: the same run's other browser tests
load the same stylesheet, and they passed, before and after. A font host that
was slow for one request and not for its neighbours is possible. It is not
demonstrated.

What else it could be, and has not been ruled out:

- The development server being slow to answer under the load of a full run.
  Measured only when idle.
- Something in the server or the fixtures that is slow some of the time. In a
  later full run the teardown of one browser test took 26 seconds and the
  setup of another 11, against the same 30 second limit.

## What was done, and what was not

**Done.** Every page a browser test opens now keeps an account of its
requests. When a browser test fails, the account is written to
`.scrivio/test-diagnostics/`: for each page, every host it asked, what it was
still waiting for and for how long, what failed, and the end of the API
server's log and the Vite server's log, which used to be thrown away. CI keeps
that folder when the browser job fails. **If it happens again it will say
what it was waiting for.**

**Not done.** No test retries. No limit was raised. The tests still load the
fonts from Google Fonts, as the interface does, so they are not yet hermetic
in that one respect. Blocking that request in the tests would make them
hermetic and would also remove the one cause that has been shown to be
sufficient, which is to say it would hide the question before it has been
answered. That is a decision for the owner, and it is A12 in the ledger.

## A defect in the product, found on the way

Separate from the test, and true whatever caused the timeout: **a font host
that is slow holds up the whole interface.** On a network where Google Fonts
is reachable and slow, or is held by a filter that neither answers nor
refuses, Scrivio shows a blank page until the browser gives up. Offline it is
fine, because the request fails at once.

It is recorded as a test that is expected to fail,
`test_the_interface_is_drawn_without_waiting_for_the_font_host` in
`tests/browser/test_external_resources.py`, and that test will report when it
starts passing. The fix is to serve the fonts from this machine, or to load
them without blocking the page. Both are the owner's to choose.
