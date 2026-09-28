# What to count

**There are no numbers yet.** This sheet says what to count, where each
number comes from, and what it would tell you.

Scrivio collects nothing. It has no analytics and sends nothing about its use
to anyone, so most of what is below is counted by hand or read from GitHub,
and some of it cannot be known at all. Where that is so, the sheet says so.

## The sheet

| | Count | Where from | Period | Number |
| --- | --- | --- | --- | --- |
| 1 | **Qualified visits**: unique visitors to the repository | GitHub, Insights, Traffic. Kept for 14 days only, so copy it down weekly | week | |
| 2 | **Where they came from** | The same page, under referring sites | week | |
| 3 | **Trial starts**: unique cloners | The same page, under clones | week | |
| 4 | **Setup completed** | Cannot be seen. Ten-user sessions, and issues that mention a working install | | |
| 5 | **Usable export**: someone got a resume they would send | Cannot be seen. Ten-user sessions | | |
| 6 | **Completed interview** | Cannot be seen. Ten-user sessions | | |
| 7 | **Return within seven days** | Cannot be seen. The follow-up question in the ten-user protocol | | |
| 8 | **Referral**: someone says they were sent by someone | Issues and discussions that say so. Ask in the ten-user follow-up | | |
| 9 | Stars, forks, issues opened by others | GitHub | week | |

Clones are inflated by CI and by bots. Treat row 3 as a ceiling.

## Reading it

The point is to tell apart three different problems that look the same from
the outside, which is no stars.

| What you see | What it means | What to do |
| --- | --- | --- |
| Few visits (row 1) | **Reach.** Nobody has seen it. Nothing can be concluded about the product. | Tell people it exists. Do not change the product on this evidence. |
| Visits, but few clones (3 against 1) | **The pitch.** People looked and did not try. | The README's first screen, the pictures, the description. |
| Clones, but setup fails (4) | **Onboarding.** | The install. Watch someone do it. |
| Setup works, no usable export (5) | **Usefulness, or the workflow.** | Ten-user sessions, steps 2 to 5. |
| They export once and do not return (7) | May be fine. A job search ends. | Ask why, before assuming a fault. |

Do not move down a row until the row above has a number in it. Until row 1
has been counted, whether the project is wanted is not known.

## If usage were ever measured in the product

It is not, and adding it is a decision for the owner. If it were, these are
the conditions:

- **Off unless switched on**, by a person, in Settings, with the full list of
  what is sent shown before they agree.
- **Counts of events only**: install completed, resume analysed, export
  downloaded, interview completed. A random identifier made on that machine
  and deletable there.
- **Never**: resume text, job descriptions, answers, transcripts, file names,
  company names, role titles, scores, keys, IP addresses kept beyond the
  request.
- **First party.** Sent to a server the project runs, not to an analytics
  company.
- **Visible.** A page in Settings showing exactly what has been sent.
- The README section "Your data" changes on the same day.

## Sharing

Nothing in Scrivio shares anything. If a way to share a resume or a scorecard
is added, it happens only when the person presses a button to do it, it
produces a file on their machine and not a public link, and nothing is
shared by default.
