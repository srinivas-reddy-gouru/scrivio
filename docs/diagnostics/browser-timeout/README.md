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
- The browser being slow to load a page for a reason of its own. The long
  closes and starts seen in later runs looked like evidence for this. The
  closes have since been explained, below, and turn out not to be evidence
  for it. It is still possible. Nothing measured so far supports it.

## What the new record showed, the first time it was used

On 29 September the browser tests were run as a group, on macOS, with the
record in place. Two things came of it.

**A different test failed, and the record said why.**
`test_the_sample_can_be_reached_and_started_from_the_keyboard` found the resume
field empty. The account written at the failure shows the page 13 thousandths
of a second into its first requests, three of them not yet answered. The test
had read the field the moment it appeared, and the sample is put into the
field a moment after that. It was a fault in the test, mine, and it is fixed:
the test now waits for the field to hold what it asserts. This is not the
failure of 28 September, which was a page that did not load.

**Closing the browser was slow.** At the end of four test files it took
between 7 and 29 seconds. I wrote here at first that this happened only
inside a longer run, that the same close took 0.06 seconds alone, and that
the load on the machine was the likely reason. That was wrong, and the next
section is what measuring it showed.

## Why closing the browser is slow, which is now established

`measure_close.py` starts the browser, opens one blank page, holds it for a
number of seconds, and closes the browser. Nothing of Scrivio's is involved.
The record is `2026-09-29-close-by-time-open.json`.

| Held open | Close took | Open and close together | Profile held |
| --- | --- | --- | --- |
| 2 s | 0.49 s | 2.5 s | 6.9 MB |
| 8 s | 0.11 s | 8.1 s | 7.0 MB |
| 15 s | 0.33 s | 15.3 s | 54.8 MB |
| 20 s | 26.45 s | 46.5 s | 54.8 MB |
| 25 s | 20.58 s | 45.6 s | 54.8 MB |
| 30 s | 15.98 s | 46.0 s | 54.7 MB |
| 35 s | 9.28 s | 44.3 s | 54.7 MB |
| 40 s | 4.31 s | 44.3 s | 54.7 MB |
| 50 s | 0.43 s | 50.4 s | 54.7 MB |
| 60 s | 0.41 s | 60.4 s | 54.7 MB |

The browser on this machine is the installed Google Chrome, version 154,
because the browser Playwright ships is not installed here. Playwright gives
it a new, empty profile each time it is started. Between 8 and 15 seconds
after it starts, that Chrome puts 48 MB into a folder of the profile named
`optimization_guide_model_store`. From some time after 15 seconds until about
45 seconds after it starts it is doing something that a close waits for. A
close asked for before or after is immediate. A close asked for in between
ends at about 45 seconds, whenever it was asked for. The pattern was the same
in three separate runs, with the load average between 2 and 6.

This accounts for what the test runs recorded. A test file whose tests take
20 to 40 seconds ends inside those seconds, and its close waits. The 11 tests
of `test_article_rendering.py`, run alone, took 25 seconds and the close took
9.9. In a full run, slower, the same close took 28.2.

What it is that Chrome is doing in those seconds has not been looked into.
It is Chrome's own, and it goes to Google's servers for it, which is a second
way these tests are not hermetic on this machine, beside the fonts.

**It does not explain the timeout.** `measure_close.py loads` loads Scrivio's
home page again and again for the first minute of a browser's life. The
record is `2026-09-29-loads-by-time-open.json`.

| Seconds after the browser started | Loads | Median | Slowest |
| --- | --- | --- | --- |
| 0 to 15 | 20 | 0.26 s | 0.45 s |
| 15 to 45 | 81 | 0.20 s | 0.37 s |
| 45 to 60 | 40 | 0.20 s | 0.31 s |

Pages load as fast during those seconds as outside them. So the slow close
is a cost in time, about 50 seconds in a full run, and it is not a reason for
a page to take 30 seconds to load. The timeout of 28 September is where it
was: one sufficient cause shown, the font host, and no cause established.

**Not explained:** starting the browser took 7 seconds at the beginning of
`test_article_rendering.py` in two runs, and under 1.5 seconds in every
measurement made outside the tests.

## Records that were lost, and what stands in for them

The first run with the record in place, on 29 September, wrote two files to
`.scrivio/test-diagnostics/`: the account of the keyboard test's failure, and
a list of slow steps with closes of 28.7, 26.0, 9.9 and 7.0 seconds. I
deleted that folder by mistake while clearing up before a later run. What is
said about them above was written from what the run had printed, before the
deletion. The files themselves cannot be shown.

`2026-09-29-slow-steps-full-run.jsonl` is the list from the next full run,
copied as it was written. It shows the same thing: closes of 28.2, 9.0 and
7.3 seconds, and starts of 7.1 and 5.7.

## What was done, and what was not

**Done.** Each step of starting and stopping a browser or a server is timed,
and one that takes more than 5 seconds is written to
`.scrivio/test-diagnostics/slow-steps.jsonl` with the time and the load on the
machine. Every page a browser test opens now keeps an account of its
requests. When a browser test fails, the account is written to
`.scrivio/test-diagnostics/`: for each page, every host it asked, what it was
still waiting for and for how long, what failed, and the end of the API
server's log and the Vite server's log, which used to be thrown away. CI keeps
that folder when the browser job fails. **If it happens again it will say
what it was waiting for.**

**Not done.** No test retries. No limit was raised. Nothing was done about
the slow close either: the tests wait for it, as before. The tests still load the
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
