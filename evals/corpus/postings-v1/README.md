# Posting corpus, version 1

Real job postings, invented resumes, and rewrites constructed from the two,
for measuring what the resume guard does.

Collected on 2026-09-29. 25 postings, 9 resumes, 1 file of annotations.

## What this can and cannot tell you

**No model wrote anything here.** Every rewrite is constructed by rule, or
written by hand, to have one known fault or one known virtue. So a number from
this corpus is a rate over constructed cases. It says what the guard does
*when* a rewrite names something unsupported, or is honestly reworded. It says
nothing about how often a real model does either, and it is not an error rate
for the product.

Twenty-five postings from two publishers, both in the United States
government, is not a sample of job postings. It was chosen because its text
may lawfully be copied, which a company's careers page may not. Government
postings capitalise more, and name fewer technologies, than most.

## The postings

| Source | Postings | May be copied because |
| --- | --- | --- |
| [USAJOBS](https://www.usajobs.gov) | 16 | Announcements written by federal agencies are works of the United States Government, which are not subject to copyright in the United States (17 U.S.C. 105) |
| [18F/join.tts.gsa.gov](https://github.com/18F/join.tts.gsa.gov) at `5dfa66db0f4b` | 9 | The repository's `LICENSE.md` dedicates it to the public domain under CC0 1.0 |

That is a reading of the law by the assistant that built this, and not legal
advice. Outside the United States a government may hold rights in its works.

Each file in `postings/` records where the posting came from, the date it was
collected, the SHA-256 of the page as it was fetched, the licence, which
sections were kept, and which were left out.

**Kept:** title, summary, duties, qualifications.
**Left out:** how to apply, conditions of employment, benefits, and the
agency's contact details, which name a person. The tests fail if anything
shaped like an email address or a phone number is in what was kept.

A posting from a company's careers page must not be added. It can be linked
to. Its text cannot be kept in a public repository.

Roles: software developer, backend and integration engineer, cloud and
DevSecOps engineer, systems administrator, data scientist, AI and machine
learning engineer, computer engineer, information security, product manager,
fraud analyst, research operations. Grades from entry (GS-04, GS-09) to
supervisor and GS-15. Headings in capitals, in title case, and in sentence
case. Names in capitals (`AWS`), mixed (`DevSecOps`, `CloudFormation`), joined
(`CI/CD`, `Trellix/MDE/Crowdstrike`), and in lower case (`agile`, `scrum`).

## The resumes

Nine, in `resumes/`. **Every one is invented.** No resume of a real person was
used, the owner's included. Addresses are on example.com and phone numbers are
in a range that is not assigned. Employers are names that software
documentation uses for companies that do not exist.

Each has four lines reworded by hand, honestly: the same claim, the same
figures, other words. These are what a careful person would accept.

## The annotations

`annotations.json` says which of the capitalised words in the postings are
names, and of what kind. Every other capitalised word is taken to be ordinary:
a heading, or the first word of a line.

**Annotated by an AI assistant, not by a person**, and not reviewed by one. The
annotator saw a list of every word with a capital or a digit in it. It did not
see what the guard made of any posting before annotating.

| Kind | What |
| --- | --- |
| `tech` | A language, tool, platform, or product |
| `standard` | A standard, framework, regulation, or protocol |
| `credential` | A certificate, licence, degree, or clearance |
| `method` | A way of working that people claim experience of |
| `field` | A field of work named by its initials |
| `term` | A technical term of art |
| `proper` | An organisation, programme, or place. A name, and not a claim of skill |

`also_a_word` marks a name that is also an ordinary word: Go, Rust, Oracle.

## The rewrites

Made by `evals/posting_pairs_eval.py` from each posting and the resume paired
with it in `pairs.json`. Each is run as a first tailoring and as a later edit.

| Family | What is constructed | A mistake is |
| --- | --- | --- |
| Unsupported | A name the posting uses and the resume does not, written into a line: as the posting writes it, in lower case mid-sentence, in lower case as the first word, in capitals. Up to 8 names a posting | **Retained**: the name is still on the resume |
| Supported | A name that is on the resume, written into another line of it | **Reverted**: the line is not as it was written |
| Ordinary word | A word the posting capitalises that is not a name, written into a line in lower case, and capitalised as the first word. Up to 40 words a posting | **Reverted** |
| Honest rewording | A line reworded by hand | **Reverted** |

The two kinds of mistake are counted apart and must be reported apart. They
trade against each other: a guard that refused everything would retain
nothing.

## Tuning and held out

A third of the postings are held out. Which third is decided by a hash of each
posting's identifier (`split_of` in `evals/collect_postings.py`), so it was
fixed before any result was seen and nobody chose it.

| | Postings | What may be looked at |
| --- | --- | --- |
| Tuning | 17 | Everything: which names were missed, which words were mistaken |
| Held out | 8 | Totals only. `--detail` is refused |

The guard may be adjusted to what the tuning postings show. It may not be
adjusted to the held-out postings, and the runner does not show what would be
needed to do so. If the held-out postings are ever used to change the guard,
they stop being held out, and a new set has to be collected.

**As of the first run, the guard had not been adjusted to either.** Both sets
of numbers from that run are therefore what an untouched guard does on
postings it has never seen.

## Running it

```bash
python -m evals.posting_pairs_eval                      # both, totals
python -m evals.posting_pairs_eval --split tuning --detail
python -m evals.posting_pairs_eval --json report.json
```

Offline. No model, no network, no cost.

## Results on record

In `results/`, named by date and by the commit of the guard that was run.

| File | What |
| --- | --- |
| `2026-09-29-guard-a3c1a9d.txt` and `.json` | Both sets, totals. The guard had not been adjusted to either |
| `2026-09-29-guard-a3c1a9d-tuning-detail.txt` | The tuning postings, with what went wrong |

## Adding to it

Do not change this folder once results have been reported against it. Put new
postings in `postings-v2`, collected with `python -m evals.collect_postings`.
