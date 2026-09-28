# Changelog

Nothing has been released. There are no tags and no published versions, and
this file does not pretend otherwise. What follows is what has changed on
`main` and on the review branch, so that a first release has notes to start
from.

## Unreleased

Changes from the September 2026 review, on the branch `review/wave-a`. Not
merged, not pushed, not released.

### Changes you will notice

- **The server asks a browser to pair** before it shows anything. The pairing
  code is printed in the terminal where the server runs.
- **Start the server with `python -m api`.** It listens on this machine only.
- **With no provider configured, nothing runs**, and the page says what to
  configure. Before, an unconfigured install produced canned output that
  looked like real output.
- **Demo mode is a choice**: `SCRIVIO_DEMO=1`. It is labelled on every page
  and keeps its work in a folder of its own. In demo mode the server makes no
  outbound request, whatever keys are configured, and voice is off.
- **A tailored resume with a `[METRIC]` still in it cannot be downloaded as
  finished.** A copy marked DRAFT can.
- **Terminal recordings are off.** They ran commands written by a model on
  your machine. Diagrams need `python scripts/setup_renderers.py` once.
- **Python 3.12 or 3.13.** The pinned dependencies do not install on 3.10.

### Resume

- One guard runs after every path by which a model writes to a resume:
  tailoring, instructed edits, the coach, and the summary condenser.
- Numbers are tied to the claim and the employer they came from. A real
  figure moved to another bullet is treated as invented.
- Jobs and degrees are matched by who they are, so two roles at one employer
  keep their own titles and dates.
- Figures written as `12,5 %`, `2.000.000`, or with no-break spaces are read
  whole. They were being split and half replaced.
- A figure that stays on its line has to go on counting the same thing.
- A number in your instruction that you were refusing is not taken as yours.
- A line in which the model names something new is put back as it was. It
  used to be kept with a note that did not have to be answered.
- A sample resume and posting, both invented, for a first look.

### Security

- Requests are checked for host, origin, and a paired session.
- Generated articles are rendered through an allowlist, with a content
  security policy, and diagrams render in strict mode.
- Fetching a posting or a source cannot reach addresses inside your network,
  follows redirects one at a time with the same check, and stops at 3 MB.
- Text from web search is filtered before any model sees it.
- Settings are validated and written atomically with owner-only permissions.
- Uploads, request bodies, archives, and provider calls have limits. Every
  request that reaches a provider is admitted through one gate, and a provider
  call ends within 300 seconds counting retries.
- A renderer or assistant that is stopped, or that exits leaving something
  running, takes what it started with it.

### Reliability

- An article run is kept as a numbered record of events. A reader that
  reconnects picks up where it left off, and a second reader sees the same
  stream.
- Work that was running when the server stopped is marked interrupted. It is
  never started again without being asked, because every run costs money.
- A change that arrives while another is still being applied to the same
  resume is refused, and so is an export from a page that has gone stale.
  Before, the later write won and the earlier one was lost.

### Your data

- Settings has a count of what is stored, an export, and a delete.
- `python -m api.data` backs up and restores from the command line. It reads
  the same settings file as the server and prints the folder it resolved.

### Operating it

- `/ready`, `/diagnostics`, a request id on every response, and errors that
  give a reference and not an exception's text.
- `python -m api.doctor` checks an install without calling a model.

### For contributors

- `requirements.txt` holds ranges and `constraints.txt` holds exact versions.
- A CI workflow: tests on two Python versions, the interface on two Node
  versions, browser tests, and an install from a clean checkout. It has been
  written and has not yet run, because the branch has not been pushed.
- Browser tests drive the real interface, and restart tests kill a real
  server.
- A versioned evaluation corpus under `evals/corpus/v1`.

### Known limits

- One person per install. Everyone who pairs sees the same data.
- The guard checks facts and does not judge wording. It matches a number by
  the words beside it. Its evaluation lists four kinds of invention it does
  not catch.
- There is nowhere to record that you confirmed a claim, so a name the model
  adds is removed for you to add, not kept for you to approve.
- Nothing here has been run against a real model provider.
- Interview grading has not been measured against human interviewers.
- The article pipeline loses to a single prompt on prose in its own
  evaluation.

## Before the review

`main` at `7840810`. Resume studio, job prep, interview practice with voice,
coding rounds, the article pipeline, and the article matchup evaluation.
