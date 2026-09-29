# A1a: recording what the candidate has confirmed

**A design, for approval. Nothing here has been built, and no schema has been
changed.**

Written 2026-09-29, against the guard at `a3c1a9d`.

## What this is for

F03 asks that an unsupported addition to a resume needs the candidate's
explicit confirmation before it can be in a finished export. Today there is
nowhere to record a confirmation, so the guard does the only safe thing it
can: what a model adds without support is taken out, and the candidate puts
it in themselves.

That fallback works, and it has cost four rounds of fixes (F03a to F03d) to
make it hold. Each was a way in which state that mattered was kept somewhere
it could be lost: in a list the model could rewrite, at a position that could
move, in a form a model could imitate. This design puts that state in fields
of its own, owned by the server.

**Approving this does not close F03.** It gives confirmations somewhere to
live. Detection, migration, edit handling, export enforcement, and the
interface are each described below, and each still has to be built and
verified. F03 closes when they all have been.

## What it does not do

Said first, because it is the part most likely to be assumed.

**It finds no more unsupported claims than are found today.** A confirmation
can only be asked for a claim that was detected. Detection is the heuristic
in `pipeline/workers/resume_fact_guard.py`, and on the posting corpus it
retained 80 of 592 constructed unsupported claims on the tuning postings and
32 of 312 on the held-out ones. None of the following is changed by this
design:

| Gap | Why a confirmation field does not help |
| --- | --- |
| A name in lower case that the posting does not use, or writes in lower case | Nothing marks it as a name, so no item is raised |
| A name that is also a word (Go, React), in lower case | The same |
| A short name whose letters are inside another word (`AI` in "maintained") | It is taken to be on the resume already. This one is a fault in the comparison and can be fixed on its own |
| A stronger verb: "helped" to "led" | Wording is not checked at all |
| The top of a range, or "about 300" to "300+" | The figure is unchanged |
| A word made from a name, or a name spelled another way | Words are compared whole |
| A true rewording that shares no word with its source | This is the opposite mistake. A confirmation would let the candidate accept it, which is the one place the design helps with a detection fault |
| The original itself being untrue | Nothing checks the original |
| What the candidate types | It is theirs |

It is also not a permission system. Confirming that you ran Kubernetes at one
employer is not a licence for the model to write Kubernetes anywhere else.

## Principles

Each comes from a defect that was found.

1. **The server owns review state.** Nothing a model returns can raise,
   resolve, confirm, or remove an item. (F03a, F03c)
2. **A finding belongs to a claim, not to a position.** Positions are worked
   out when the document is read. (F03b)
3. **A confirmation belongs to one claim under one record.** It is not
   global. (The limit recorded under F03.)
4. **The saved resume never holds an unconfirmed claim.** What the model
   proposed is kept beside the resume, not in it. So a fault in export
   enforcement cannot put an unconfirmed claim in a finished file, because it
   was never in the text.
5. **Enforcement works things out again.** At export the server recomputes
   from the text. Stored flags are a convenience and are never the authority.
6. **What is ambiguous is refused.** A confirmation that might or might not
   cover a changed claim does not cover it.

Principle 4 is the important one, and it is a change of approach. The earlier
ledger proposal had the guard keep the model's line in the resume with an
open item against it. That makes the export gate the only thing between an
unconfirmed claim and a finished file, and the export gate is where F03a and
F03b were. Keeping the proposal out of the text removes that dependence.

## The data

### Claim identity

A claim is identified by where it is, what kind of thing it asserts, and what
it says.

```python
class RecordKey(BaseModel):
    """Which entry a claim is under. Not an index: entries move."""
    section: Literal["basics", "work", "projects", "education", "custom", "skills"]
    name: str = ""          # employer, project, institution, or section name, normalised
    title: str = ""         # position, or degree type
    start: str = ""
    end: str = ""

class ClaimKey(BaseModel):
    record: RecordKey
    kind: Literal["name", "number"]
    value: str              # the name, casefolded; or the figure as currency|magnitude|unit
    about: list[str]        # for a number: the stems that say what it counts. Empty for a name
    line_hash: str          # SHA-256 of the line, whitespace and case normalised
```

`RecordKey` is what `match_work()` and `match_education()` already compare, so
a record is found the way the guard finds it now, by who it is. Two roles at
one employer are two records.

`line_hash` is deliberately strict. A reworded line is a different claim. The
cost is that a candidate who confirms a line and then has it reworded is
asked again. That is the conservative side of the choice, and it is what
"altering the claim invalidates the confirmation" means.

### What is stored

```python
class Proposal(BaseModel):
    """Something a model wrote that the guard did not let into the resume."""
    id: str                         # uuid4, made by the server
    claim: ClaimKey
    proposed_line: str              # what the model wrote
    kept_line: str                  # what is in the resume instead
    raised_at: datetime
    raised_by: Literal["tailoring", "instructed_edit", "migration"]
    state: Literal["open", "accepted", "declined", "superseded"] = "open"
    resolved_at: datetime | None = None

class Confirmation(BaseModel):
    """The candidate's own statement that a claim is true."""
    id: str
    claim: ClaimKey
    line: str                       # the line as it stood when confirmed
    how: Literal["accepted_proposal", "typed", "metric_fill", "added_entry",
                 "added_skill", "said_in_instruction"]
    confirmed_at: datetime
    withdrawn_at: datetime | None = None

# Additions to schemas that have tests. THIS IS WHAT NEEDS APPROVAL.
class ResumeDoc:
    schema_version: int = 1                          # absent in a saved file means 1
    proposals: list[Proposal] = []                   # new
    confirmations: list[Confirmation] = []           # new
```

Three fields on `ResumeDoc`, all with defaults. `TailoredResume` is **not**
changed, which matters because it is also the schema of the tool the model
fills in: a field there would be a field the model could write to.

Proposals and confirmations are on the document and not on the tailored
version, so undo, which replaces the tailored version, does not touch them.

### What is not stored

Open findings against text that is in the resume. Under principle 4 there are
none for any resume written after this lands. They exist only for resumes
migrated from earlier versions, and those are handled as proposals with
`raised_by: "migration"` (below).

## Behaviour

### When a model writes to a resume

1. The model's output is validated as now. Its warnings are its own account
   and are kept as such. Nothing in them is read for state.
2. For each line in which the guard finds an unsupported name or number, the
   line is put back, as now, and a `Proposal` is recorded with what the model
   wrote.
3. Before step 2, each finding is compared with the confirmations. A
   confirmation covers a finding only if **all** of these hold: same record,
   same kind, same value, and for a number, the words that say what it counts
   agree. If it is covered the line stands and no proposal is raised.
4. A finding the same as an open or declined proposal does not raise a second
   one. A declined proposal is not raised again for the same claim, so the
   candidate is not asked the same thing at every edit.

### Trusted confirmation actions

A confirmation is created only by the server, only in answer to a request
from a paired session, and only by one of these:

| Action | Endpoint | Creates |
| --- | --- | --- |
| Accept a proposal | `POST /resumes/{id}/proposals/{proposal}/accept` | The proposed line goes into the resume. A confirmation, `accepted_proposal` |
| Decline a proposal | `POST /resumes/{id}/proposals/{proposal}/decline` | Nothing. The proposal is closed |
| Type onto the resume | `POST /resumes/{id}/edit-tailored`, as now | A confirmation, `typed`, for each name or number in the typed line that the original does not support |
| Fill a placeholder | `POST /resumes/{id}/fill-metrics`, as now | A confirmation, `metric_fill` |
| Add an entry or a skill | `POST /resumes/{id}/add`, as now | It is written into the original, as now. No confirmation is needed: it is on the resume |
| Withdraw | `POST /resumes/{id}/confirmations/{confirmation}/withdraw` | The confirmation is marked withdrawn. It is not deleted |

Accept and decline carry the proposal's `line_hash` and the document's
`updated_at`. If either does not match what the server holds, the request is
refused with 409, so a confirmation cannot be given to a line other than the
one that was on the screen.

**An instruction is not a confirmation of a flagged claim.** "Put the
Kubernetes bullet first" names Kubernetes and vouches for nothing. Today an
instruction that states a claim in the candidate's own words ("I ran those
clusters on Kubernetes") lets the model write it, and that stays, recorded as
`said_in_instruction` and bound to the line the model then writes. It cannot
resolve an existing proposal. Only accept can.

Open question 1 below asks whether `said_in_instruction` should exist at all.

### Reordering and moved entries

Nothing is stored by position. When a document is read, each proposal and
confirmation is placed by finding its record (by `RecordKey`) and then its
line (by `line_hash`, then by `kept_line`). A bullet that has moved, an entry
that has moved, and a bullet whose neighbours were deleted are all found
where they now are.

A claim whose record is no longer on the resume has nowhere to be shown. Its
proposal is marked `superseded`. Its confirmation is kept, because the record
may come back on undo or re-tailoring.

### Edits

| What happens | Proposal | Confirmation |
| --- | --- | --- |
| The model rewords a confirmed line and keeps the claim | None raised, if the confirmation covers it (step 3) | Kept. A second one is not made |
| The model rewords a confirmed line and changes what the figure counts | Raised. The line is put back to the confirmed wording | Kept, and does not cover the new line |
| The model moves a confirmed name to a different record | Raised | Does not cover it: the record differs |
| The candidate rewords a confirmed line themselves | None | A new `typed` confirmation for the new line. The old one is kept |
| The candidate deletes a confirmed line | None | Kept |

### Undo

Undo restores the tailored text. Proposals and confirmations are not restored
and not removed. After an undo each is placed again against the restored
text. A confirmation whose line is back covers it. A proposal accepted after
the restored version was saved has its line missing from the text, and is
shown as accepted and not applied, with the choice of applying it again.

Undoing does not undo a confirmation. They are separate acts, and the
candidate withdraws a confirmation by withdrawing it.

### Tailoring again

Tailoring again starts from the original. Today that loses every figure the
candidate typed onto the tailored copy, which is the unfinished part of R02.

With confirmations, the guard is given them as sources, each bound to its
record. A line the model writes that a confirmation covers is kept. So a
figure supplied once is not asked for twice.

The confirmations are given to the **guard**. Whether they are also given to
the **model**, so that it knows it may use them, is open question 2. If they
are, they are data in the prompt and must be fenced as such.

### Export

The finished export is refused while any of these is true, worked out from
the text at the time of the request:

1. A `[METRIC]` placeholder is in the tailored resume. (As now.)
2. The guard, run over the tailored resume against the original and the
   confirmations, finds a name or number that neither supports. (New. This
   is principle 5. It catches a resume written by a version with a fault in
   it, and one edited by hand on disk.)
3. A migrated proposal is open and its claim is in the text. (Legacy only.)

An open proposal whose claim is **not** in the text does not block the
export. It is an offer that has not been answered, and the resume is
truthful without it. The send step says how many there are.

A draft is always available, labelled as a draft, as now.

### Migration

`schema_version` is absent from every saved file today, and an absent value
is read as 1. Version 2 is this design.

On first load of a version 1 document:

| Found | Becomes |
| --- | --- |
| A note left by the guard, in the form `[path] New term: 'X' ...`, with X in the text and not on the original | A proposal, `raised_by: "migration"`, `state: "open"`, with `proposed_line` and `kept_line` both the line as it stands. The line stays in the text, because taking it out would change a saved resume without being asked. The export is refused until it is accepted or declined |
| The same note, with X no longer in the text or now on the original | Nothing. It was answered |
| A `[METRIC]` placeholder | Nothing. It is still a placeholder |
| A figure in the tailored copy that the original does not support | Nothing is assumed about who typed it. It is found by export rule 2 and the candidate is asked |

The notes are removed from the warnings once they have become proposals, and
the document is saved as version 2. Migration happens once, and a test loads
a migrated document a second time to show that nothing is raised twice.

The last row is the uncomfortable one. A candidate who filled in a figure
under an earlier version will be asked to confirm it once. The alternative is
to assume every unsupported figure in an old document was typed by the
candidate, which is the assumption F02 was about.

### Interface

On the tailoring view:

- A line with an open proposal is marked, and opens to show what the rewrite
  proposed beside what is on the resume, with **Use this, it is true** and
  **Leave it out**.
- A confirmed line carries a small mark, and opens to show when it was
  confirmed and how, with **Withdraw**.

On the send step:

- Before any download is tried, a list of what stands in the way: each
  placeholder, each unsupported claim found by export rule 2, each migrated
  proposal. Each links to its line.
- A count of open proposals that do not stand in the way.

Each control is reachable by keyboard and has a name a screen reader
announces, and the browser tests hold it to that as they do the rest.

## How it would be verified

Each row is a test, or a set of them, written to fail before the code it
tests exists.

| Area | Tests |
| --- | --- |
| Claim identity | Two roles at one employer are two records. A reworded line is a different claim. A reordered line is the same claim |
| Model output | Warnings in any form raise, resolve, and confirm nothing. The cases of F03a and F03c, against proposals |
| Coverage | A confirmation covers the same claim reworded. It does not cover the figure counting something else, the name under another record, or the name in another line |
| Actions | Accept applies the line and confirms. Decline closes. A stale `line_hash` or `updated_at` is refused. An unpaired request is refused |
| Movement | The cases of F03b: reordered bullets, a deleted earlier bullet, a moved entry |
| Undo and re-tailoring | The table above, row by row. A figure supplied once survives tailoring again |
| Export | Rules 1 to 3, in four formats. A document edited on disk to hold an unsupported claim is refused. An open proposal not in the text does not block |
| Migration | Documents saved at `7840810`, `98eda46`, `bc6157e`, `fe22bc1`, and the commit before this lands: each loads, migrates once, exports, and saves. A second load raises nothing |
| Interface | Browser tests for accept, decline, withdraw, and the send step's list, by mouse and by keyboard |
| Evaluation | The posting corpus is rerun. Retained and reverted are reported apart, before and after. The design should not change retained. It should reduce reverted only where a candidate accepts |

## Order of work, if approved

1. Schema fields and `schema_version`, with migration and its tests. Nothing
   else changes behaviour yet.
2. Proposals recorded by the guard, alongside the present behaviour. The
   resume text is what it is today.
3. The actions, and coverage by confirmations.
4. Export rule 2.
5. The interface.
6. Tailoring again with confirmations.
7. The corpus rerun, and the ledger.

Each is a commit with its own reproduction and its own tests, and the suite
and the evaluation are run at each.

## Questions for the owner

1. **Should a claim stated in an instruction count as a confirmation at
   all?** It does today, in effect. It is convenient, and it rests on telling
   a statement from a mention, which is the same heuristic that reads "do not
   say 25" as a refusal. The strict answer is no: the model may propose, and
   only accept confirms. I recommend the strict answer.
2. **Should confirmations be shown to the model when tailoring again?** If
   not, the model will not know it may use them, and the candidate will be
   offered fewer of their own figures. If so, they are one more piece of data
   in a prompt. I recommend showing them, fenced and labelled.
3. **Should a migrated figure be assumed to be the candidate's?** The design
   says no, and asks once. The cost is one question for each such figure in
   each old resume.
4. **Is a withdrawn confirmation kept or deleted?** The design keeps it,
   marked. It is the candidate's data, and Settings can delete everything.

## What needs approval

Changing `ResumeDoc`, which has tests, by adding `schema_version`,
`proposals`, and `confirmations`, each with a default, and adding the models
`RecordKey`, `ClaimKey`, `Proposal`, and `Confirmation` to
`pipeline/schemas/models.py`. `TailoredResume` and `InterviewSession` are not
changed by this design. Provenance (A1b) is separate and is not asked for
here.
