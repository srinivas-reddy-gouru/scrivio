# The Studio — design spec for the full React migration

One workplace, four rooms, one visual language. Everything Scrivio makes
is **paper on a desk**, and everything Scrivio thinks is **marks on that
paper**. The Desk (resume) proved the language; this spec extends it to
the whole app.

Research grounding (Aug 2026): interview tools won by quantifying the
candidate's own performance, not generating answers (Yoodli's visual
speech reports; category consensus "assist, not replace"); writing tools
converged on document-center-stage with AI around it (Canvas, Lex);
suite navigation standard is sidebar + command palette ("menus don't
scale"). Scrivio's honesty thesis is the category direction; the UI's
job is to make it physical.

## Tokens (shared, from the Desk)

| Role | Value | Meaning |
| ---- | ----- | ------- |
| desk | #0C1312 | the ground everything sits on |
| paper | #F7F4EC | every artifact the user keeps |
| ink | #212B29 | what is written on paper |
| teal / grad | #20B8CD → #4CC9F0 | Scrivio acting |
| honest amber | #E5B04C | Scrivio needs the user's truth |
| red pen | #E06A55 | the reviewer's objection |
| verdict green | #34D399 | earned, not given |

Type: Space Grotesk (display) · Inter (UI + paper) · JetBrains Mono
(scores, data, code). Signature primitives: **bar-tick** (the bar is set
before you speak), **margin marks**, **stations**, **sticky cta-panel**,
**paper-in settle**. Reduced-motion stills everything, focus-visible
everywhere.

## Shell

- **Left rail sidebar**, compact (icons + labels): Floor, Newsroom,
  Interview Room, Job Room, Desk, Back Office (settings) pinned bottom.
  Provider health dot lives under the brand.
- **⌘K command palette**: fuzzy jump across artifacts and actions
  ("open kafka article", "new report", "start drill", "settings: model").
  Every palette action also exists as a visible control somewhere.
- Routing: hash-based (#/room/id), no router dependency.

## Rooms

### 1. The Floor (home) — a workspace, not a landing page
The current home is marketing cards; users live here, so it becomes a
dashboard: **"On your desk"** (most recent artifacts as mini-papers:
last article, last report with score pill, last session with verdict),
continue-where-you-left-off as the primary CTA, a quiet stats strip
(streak flame, topics mastered, last hire signal), and one quick action
per room. Signature: the mini-papers ARE the navigation.

### 2. The Newsroom (article studio) — watch the press run
Compose: a brief card (topic, level, steering) styled as an assignment
slip. Generation is the room's signature — **the press run**: the
manuscript sits center-stage as paper and visibly assembles while the
stage rail (same rail grammar as the Desk) reports: sources found with
trust ticks, claims verified/dropped (red pen strikes a dropped claim),
sections drafting in, the editor's flags, the polish pass. Feed = the
existing SSE progress events; no new backend. Reading view: the paper
with **citation marks in the margin** (numbered teal ticks, hover =
source + trust). Library: a drawer of papers with spine labels.

### 3. The Interview Room (topic practice) — the rubric card flip
The room keeps the orb + mic + live transcript (they tested well), and
gains the desk language: the question sits on a **card on the table**;
the transcript writes onto a notepad paper. The signature moment: when
an answer closes, the rubric card — which visibly sat face-down on the
table the whole time (the bar, set before you spoke) — **flips over**
to show the checkable points, ticked green where the grader found them
in the answer, with the score counting up dial-style. Modes are table
setups: Practice (coach at the table), Simulation (panel, cards stay
down until the end), Drill (a stopwatch on the table, 60s ring).
Dashboard: the room's wall — streak flame, mastery shelves, badges as
plaques. Yoodli lesson, phased later: a speech strip under the
transcript (pace, filler count) — deterministic, from the transcript.

### 4. The Job Room (job prep) — the dossier
A job target is a **dossier folder**: resume + JD clipped together,
tabs for Fit / Questions / History. Fit analysis renders ON the dossier
(competency rows with strong/partial/missing marks in margin-mark
grammar). "Start the screen" walks into the Interview Room (same room,
job dressing). The scorecard is a **marked report card**: paper with
per-competency marks, the panel's notes in the pen's hand, requirement
coverage as ticked/crossed lines, the study plan as a paperclipped
reading list with trust ticks. Reuses Desk components heavily
(paper/rail/marks/stations).

### 5. The Back Office (settings)
Current structure (provider, models, connections) restyled with studio
tokens. No metaphor theatrics; it is the utility room.

## Migration order (each step ships alone)

1. Shell + Floor + ⌘K (reads existing list endpoints only)
2. Job Room (closest to Desk components: paper + rail + marks)
3. Interview Room (orb/mic/transcript port + rubric flip)
4. Newsroom (SSE press run + reading paper + library)
5. Back Office; then `/` redirects to the React app and ui/index.html
   retires; Vitest component tests land with each room

Rule for every room: findings on the artifact, one primary action per
state, honest progress with elapsed time, amber only when Scrivio needs
the user's truth.
