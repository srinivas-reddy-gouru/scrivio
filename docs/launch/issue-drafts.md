# Issue drafts for newcomers

Drafts. **None has been filed.** Each is small, has a place to start, and
has a way to tell when it is done. All five describe things that are true of
the code today.

---

## 1. Read a range as a range in the resume number check

**Label:** good first issue, resume

An original bullet says "Led a team of 10-15 engineers". A rewrite that says
"Led a team of 15 engineers" is accepted, because 15 appears in the original.
The top of a range has been stated as the figure.

**Start in** `pipeline/workers/resume_fact_guard.py`, at `quantities_in`.

**Done when** the case `top-of-a-range` in
`evals/corpus/v1/resume_cases.json` passes with its `known_gap` cleared,
`python -m evals.resume_guard_eval` exits 0, and a range kept as a range
("10-15 engineers", "10 to 15 engineers") is still accepted.

---

## 2. Keep "about" and "over" attached to their figures

**Label:** good first issue, resume

"Supported about 300 users" rewritten as "Supported 300+ users" is accepted.
The figure is the same and the claim is larger.

**Start in** the same function. The words around a figure are already
collected as context.

**Done when** the case `approximation-made-a-floor` passes with its
`known_gap` cleared, and "about 300" rewritten as "roughly 300" is accepted.

---

## 3. Check the pages that open after a click for accessible names

**Label:** good first issue, accessibility, tests

`tests/browser/test_first_run.py` checks that everything operable has a name,
on each page as it opens and on the job target form. It does not look at what
appears later: the tailoring view, the send step, an interview in progress,
the settings panels.

**Start in** that file. `UNNAMED` is the check, and
`test_the_job_target_form_names_every_field` shows how to reach a view behind
a button.

**Done when** the same check runs on the tailoring view, the send step, and
an interview question, in demo mode, and anything it finds is fixed.

---

## 4. Recognise accented letters as letters in the number check

**Label:** good first issue, resume, internationalisation

The words around a figure say what it measures. The pattern that finds words
matches `A-Z` and `a-z` only, so "años" is read as "a" and "os", and Japanese
text yields no words at all. The check still fails safe, because a figure
with no matching context is refused. But a true figure in a reworded Spanish
or German bullet is refused more often than it should be.

**Start in** `_TOKEN_RE` in `pipeline/workers/resume_fact_guard.py`.

**Done when** new cases in a `corpus/v2` show a reworded bullet in Spanish,
German, and French keeping its figure, and every existing case still passes.

---

## 5. Split the resume routes out of `api/server.py`

**Label:** help wanted, refactor

`api/server.py` is about 3,800 lines and holds every route. The resume routes
are the most self-contained.

**Start by** moving the `/resumes` routes into `api/routes/resumes.py` as a
FastAPI `APIRouter`, with no change in behaviour.

**Done when** the full suite passes unchanged, including the browser tests,
and `tests/test_dev_proxy.py` still finds every route. No test should need
editing except for import paths. If one does, the move changed behaviour.
