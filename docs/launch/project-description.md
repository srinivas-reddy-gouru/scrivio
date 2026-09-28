# Project description

Three lengths. Each says only what the software does and what is tested.

## One line (for the repository description, under 120 characters)

Check a resume against a job posting, tailor it with every fact checked against the original, then practise the interview.

## One paragraph

Scrivio is for people applying to jobs. You give it your resume and a job
posting. It scores the match as a checklist with a reason for each row,
rewrites the resume for that posting, and then checks the rewrite against
your original in plain code: a number, employer, title, or date that was not
in your resume does not stay in the result. After that you can take a mock
interview for the role, graded against a rubric written before you answer. It
runs on your own machine, for one person, on your own API key or an AI
subscription you are already signed in to.

## What to say when asked how it differs

- The check on invented facts is code, with tests, and not an instruction to
  the model. The README has a table of exactly what is checked and a list of
  what is not.
- It publishes where it loses. The article pipeline was measured against a
  single prompt to the same model and lost on prose, 6 verdicts to 0 with 4
  ties. That is in the README with the reports.
- It has a demo mode that needs no key and no resume.

## What not to say

| Do not say | Because |
| --- | --- |
| It never invents anything | It checks figures, employers, titles, dates, degrees, and skills. It does not judge wording. |
| It gets you past applicant tracking systems | The score is a checklist. No tracking system was tested. |
| Recruiter-grade, or interviewer-grade | No recruiter or interviewer has assessed it. |
| It is free | It uses your key or your subscription's allowance. |
| It is private | Your resume is stored locally and sent to your model provider. |
| Accurate grading | Grading accuracy has not been measured. |
| Any number of users, stars, or testimonials | There are none to report. |

## Topics for the repository

`resume`, `job-search`, `interview-preparation`, `fastapi`, `react`,
`typescript`, `llm`, `local-first`
