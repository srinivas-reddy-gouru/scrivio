# Scrivio

> **Learn it. Prove it. Get the job.**
> Honest ATS-aware resume tailoring, job-targeted mock interviews with recruiter-grade scorecards, voice-first interview practice graded against a rubric sealed before you speak, and deep-researched technical articles, all running on the AI subscription you already pay for.

Scrivio started as an article generator and grew into a job-search platform. It edits your resume like a writer with ethics (explainable ATS checks, tailoring that refuses to invent a single fact), gives feedback like a hiring panel (competency scorecards, hire signals, cited study plans), interviews like a senior engineer (spoken questions, a rubric written before you answer, real follow-ups), and researches like a journalist (live web sources, every claim fact-checked).

Two things here are unusual enough to be the reason to look: **the honesty guards**, which are deterministic Python that reverts any number or employer a model invents rather than prompt text asking it not to, and **the eval harness**, which is why the roadmap below is written from measurements instead of opinion, including where it says this project loses.

![Scrivio home](docs/home.png)

---

## The four studios

Ordered by how well the evidence supports them. The resume and interview
work is where this project's differentiated engineering lives; the article
studio is measured, honestly, as the weakest of the four (see
[the matchup evals](#measured-not-asserted-the-article-matchup-evals)).

### 📄 Resume studio

![Resume studio](docs/resume-studio.png)

Upload or paste your resume; get a report you can argue with, then a rewrite you can trust:

- **An explainable ATS score, not a magic number.** A weighted checklist computed in plain Python (contact info placement, standard headers, quantified bullets, date consistency, single-column parse safety, stuffing detection…), each row showing exactly why it passed or failed. With a JD attached (paste, URL, or a saved job target from Job prep), a deterministic **keyword-match** percentage joins the score: 70% checks + 30% coverage
- **Structured, not a text blob.** The resume is extracted once into the [JSON Resume](https://jsonresume.org) open standard (the architecture lesson borrowed from [Reactive Resume](https://github.com/AmruthPillai/Reactive-Resume)); every check, edit, and export operates on that structure. Import an existing `resume.json`, export to **PDF (what application portals want), Word (.docx), Markdown, or JSON Resume**, and the JSON round-trips into Reactive Resume and the rest of that ecosystem
- **Honest tailoring is the whole point.** The rewrite reorders, rephrases, and surfaces your *existing* experience in the JD's vocabulary. It will not invent employers, titles, dates, degrees, or skills: missing metrics become `[METRIC]` placeholders for you to fill, unclaimable keywords are listed as *"cannot honestly claim"*, and deterministic post-guards strip any invention a model sneaks through. Employers, titles, and dates must pass byte-identical, and **any number that came from neither your resume nor you is reverted**, with a note naming the figure it refused
- **The paper is the interface.** Findings are drawn *on* the resume, not in a report beside it. Teal marks are edits (hover for what changed and why); amber marks are honesty notes anchored to the exact line they question. Click one and answer it in your own words, and the line turns teal. Every pass reports its own score delta, so a fix that does not move the score says so
- **You can add what the extractor never saw.** The guards above bind the model, not you. Add a bullet under any job, add a job, project, or degree the parse missed, or create a section the JSON Resume standard has no field for (Publications, Volunteering, Patents), all directly on the paper. Anything you add is written into the *original* structure as well as the tailored copy, so the honesty guard treats it as fact rather than invention and the next tailoring pass keeps it. A section the tailor drops is restored; one the tailor invents is still removed
- **Nothing ships half-finished.** The packaging button stays disabled while `[METRIC]` placeholders or unsaved edits remain, and every change is one Undo away

---

### 💼 Job interview prep

![Job interview prep](docs/job-prep.png)

Upload your resume and a job description; interview for *that* job:

- **Fit analysis.** Five to eight competencies derived from the JD itself, each mapped against your resume's evidence (strong / partial / missing), with the gaps a sharp interviewer would probe
- **A realistic 30 to 45 minute screen.** Warm-up → resume deep-dive (it grills *your own claims* by name) → technical → behavioral (STAR required) → gap-probe → closing, using questions real interviewers ask for that role and company
- **Recruiter-grade scorecard.** A hire signal calibrated to the role's seniority, per-competency scores with quoted evidence from your answers, JD requirement coverage (met/partial/missing), panel notes, and a **study plan with trust-ranked citations** for every weak area

### 🎤 Topic practice

![Topic practice](docs/topic-practice.png)

Voice interviews on any topic, with grading you can trust:

- **The interviewer speaks, and sounds like one.** Questions are read by a steerable TTS voice told how to deliver them: unhurried, pausing at commas, lifting at the end of a question rather than landing it flat. A question read as a statement arrives as an interrogation, which is not what you are practicing for. Pick the voice from inside the room and preview it on the real question, since a list of adjectives tells you nothing about what you want to hear for twenty minutes
- **You answer out loud.** The mic records and transcribes through Whisper, which is accurate and punctuated and works in any browser with a microphone; the browser's own dictation is the fallback when no key is set. The transcript is yours to edit before you submit, and every failure says what went wrong instead of leaving a dead button
- **Manners, without a softer bar.** The first question of every mode opens with a courteous line before it, the way a real interviewer starts. The rubric and the difficulty are untouched
- **Hidden rubric.** The grading rubric and ideal answer are written *before* you answer and physically withheld from the client until the question closes; the grader scores against a fixed bar it cannot sweet-talk
- **Three modes.** Practice (feedback each answer + one drill-down follow-up), Simulation (silent grading, end-of-screen debrief with hire signal), Drill (60-second rapid fire seeded from your weak spots)
- **Progress that compounds.** Topic mastery (recent sessions weighted), daily streaks, badges, confidence calibration (predict your score before the verdict), and weak areas that automatically seed your next drill
![Coding round](docs/coding-round.png)

- **Coding rounds grade the interview, not the submission.** One problem worked through four phases, each with a rubric sealed before you start: clarify what you need to know before writing anything, state the approach and what it costs, implement, then defend it when the interviewer pushes. The problem withholds some constraints on purpose, and finding them is what the clarify phase scores, because candidates fail these rounds for not asking far more often than for not knowing. Your code is checked by parsing it, never by running it: whether it parses, defines the signature you were given, returns anything, and how deep its loops nest are handed to the grader as facts, so a confident explanation cannot cover a syntax error

### ✏️ Article studio

![Article studio](docs/article-studio.png)
Type a topic, get a sourced technical article. **Read the eval section
below before judging this one:** a single prompt to the same model beats
this pipeline on prose, and rebuilding it around one research-grounded
generation is the top item on the roadmap. What it does keep, and what the
baseline has no answer to, is the citation trail:

- **Docs-first research.** Scrivio resolves the official documentation domains for your topic (Kafka → kafka.apache.org) and ranks them above blogs and Q&A forums by trust tier
- **Verify before draft.** Every claim is checked against fetched evidence *before* a word of prose is written; unsupported claims are dropped, not published
- **Editorial pipeline.** Plan → draft with inline citations → editor review → targeted revision → level compilation (basic/intermediate/advanced) → voice polish → final critic gate
- **Citation integrity.** Versioned documentation is collapsed to one entry per page at its newest release, so a single doc cannot appear three times as three references, and a lone hit on an old release is re-fetched at the current one. User-hosted pages do not qualify as documentation, which keeps a personal blog from outranking the official reference
- **Mermaid diagrams**, numbered citations with a Sources section, resumable runs served from a stage cache

## Screenshots

The images above are captured from a running instance, so they stay honest
about what the app currently looks like:

```bash
python -m api                                  # in another shell
python scripts/capture_screenshots.py          # --light for light theme too
```

The resume shot is the exception, because a resume screenshot is exactly
where documentation leaks a real person: a contact line and an employment
history, at full resolution, forever. So it is taken against an invented
resume on a server pointed at an output directory of its own, where
nothing personal is loadable at all. The content is fiction; the before
and after scores are not, since they come from the same checks the
product runs.

```bash
python scripts/demo_resume.py /tmp/demo-output
ARTICLE_OUTPUT_DIR=/tmp/demo-output python -m uvicorn api.server:app --port 8897
python scripts/capture_screenshots.py --base http://localhost:8897 --only resume-studio
```

---

## Bring your own subscription (zero API cost)

Scrivio's LLM calls can route through **any local AI CLI** instead of a metered API. Which CLI is pure configuration (`LLM_CLI` in Settings), not code: each is described by a spec (invocation, output parsing, model tiers, auth quirks):

| `LLM_CLI` | Runs on | Notes |
| --------- | ------- | ----- |
| `claude` *(default)* | Claude Pro/Max via Claude Code | Reference implementation; also powers **subscription web search** |
| `codex` | ChatGPT Plus/Pro via Codex CLI | `codex exec`, read-only sandbox |
| `gemini` | Google account via Gemini CLI | JSON output mode |
| `qwen` | Qwen Code free tier | Gemini CLI fork |
| `ollama` | **Local models, no account at all** | `ollama run`, fully offline LLM |

The adapter strips API-key env vars from CLI subprocesses so your subscription login is actually used (an inherited `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` would silently win and bill the API). Structured output works on every CLI via JSON-forcing with a self-correcting retry. Trade-off, stated honestly: CLI calls are slower than the API (a process spawn per call) and bound by your plan's own limits. That is a good fit for interviews and a workable but slow one for batch article generation.

**Zero-key quickstart** (Claude subscription):

```bash
npm install -g @anthropic-ai/claude-code && claude login
```

Then select **"Local CLI"** as the provider in Scrivio's Settings. Articles (including live web search and fact-checking) and all interview modes now run at no marginal cost.

---

## Installation

You need **Python 3.12 or 3.13** and **Node.js 20 or newer**. Those are the
versions this has been installed and tested on. The pinned dependencies do not
install on Python 3.10, and 3.11 has not been tested.

```bash
git clone https://github.com/srinivas-reddy-gouru/scrivio.git
cd scrivio

# 1. Python, in its own environment
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -c constraints.txt

# 2. The interface. Without this step you get an older interface
#    that does not match the screenshots above.
(cd web && npm ci && npm run build)

# 3. Check the install. Calls no model, costs nothing.
python -m api.doctor
```

`python -m api.doctor` tells you what is ready and what is not: runtimes, the
interface build, whether a provider is configured, and the optional extras.

**To use your own resume you need a provider**, which is one of: an Anthropic
key, an OpenAI key, or a command-line assistant you are already signed in to
(no key, no API cost; see the next section). Copy `.env.example` to `.env` and
fill in what you use. Web search and voice are optional and explained in that
file.

Run the server and open **http://localhost:8899**:

```bash
python -m api
```

The server listens on loopback only, so it is reachable from your machine and
nowhere else. The first time you open it in a browser it asks for a **pairing
code**, which is printed in the terminal where the server is running. That is
the whole sign-in: your resumes and interview answers are on this machine, and
only a browser you have paired can read them. The code is single use, and a
paired browser stays paired for 30 days.

**No provider yet?** Scrivio will tell you so and refuse to run, rather than
show you made-up results. To look around first, start it in demo mode:

```bash
SCRIVIO_DEMO=1 python -m api
```

Demo mode runs on canned examples and never calls a model, even if keys are
configured. Everything it shows is labelled as a demo, and demo work is stored
in its own directory so it never mixes with your real job search.

This protects one person's local install. It is not multi-user isolation:
everyone who pairs sees the same data. Do not host this for several people
as it stands.

### Keys (all optional if a local CLI is signed in)

| Key | Purpose |
| --- | ------- |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | API-billed LLM providers (faster than CLI) |
| `TAVILY_API_KEY` (or Brave/Exa) | Live web search; without any, search falls back to the Claude CLI's WebSearch tool, then degrades gracefully |
| `OPENAI_API_KEY` (again) | Both directions of voice: the interviewer's TTS and Whisper transcription of your answers. Without it the browser's own voice and dictation take over, which is free but noticeably more robotic and unavailable in some browsers. `TTS_VOICE` sets the default voice (`sage`), overridable per listener in the room; `TTS_MODEL` defaults to `gpt-4o-mini-tts`, the tier that accepts delivery instructions |
| `JINA_API_KEY` | Fallback fetcher for scraper-blocking sites |

### Model selection is your call, not ours

Presets (Fast / Balanced / Best) decide which **tier** each pipeline stage uses. What model each tier *is* belongs to you, in Settings, scoped to your active provider:

- **Large tasks** (writing, editing, interview grading) and **Small tasks** (routing, checks, diagrams) each get a model dropdown with the current lineup plus an *Other…* free-text escape hatch
- Per-CLI knobs: `CLI_STRONG_MODEL` / `CLI_LIGHT_MODEL`, and `CLI_FORCE_MODEL` to pin every call to one model (the quota-saver switch)

---

## Your data

**Stored on your machine. Processed by your provider.** Those are two
different things and Scrivio does not blur them. Your resumes, job targets,
interview answers, and articles are files in `./output` on your own disk.
To analyse them, their text is sent to whichever model provider you
configured. Running Scrivio locally does not make the model local, unless the
provider you chose is one.

| What | Where it goes |
| --- | --- |
| Resume text and job descriptions | To your provider, each time a resume is analysed, tailored, edited by instruction, or discussed with the coach |
| Interview answers | To your provider, when they are graded |
| Spoken answers and the interviewer's voice | To OpenAI, only if an OpenAI key is set. Otherwise your browser does both |
| Search queries | To your search provider. For job prep these include the role and company |
| Keys | In the settings file on your machine. Each is sent only to the provider it belongs to |

What your provider keeps from those requests is governed by your agreement
with them, not by this application.

**Retention.** Scrivio keeps everything until you delete it and never deletes
on a schedule.

**Taking it out and removing it.** In Settings, under *Your data*: a count of
everything held and the folder it is in, **Export everything** (a zip of the
records exactly as stored), and **Delete everything**. Each resume, job target,
interview, and article can also be deleted one at a time. Deleting a job
target keeps the interviews you took for it unless you ask for those to go
too, and says how many there were.

Deleting cannot reach what your provider already received, backups you made,
or files you downloaded.

**Backup and restore.** An export is also the backup format:

```bash
python -m api.data show                      # what is stored, and where
python -m api.data backup  my-backup.zip     # contains your resumes: keep it private
python -m api.data restore my-backup.zip     # leaves existing records alone
python -m api.data restore my-backup.zip --replace
```

`--replace` overwrites records that already exist, and first saves what was
there as `_before-restore-<time>.zip`, so a restore can itself be undone.
Records saved by earlier versions restore and open without conversion.

**Logs.** Failures are logged by kind, without the text that caused them, so
a resume does not end up in a log file.

This describes what the software does. It is not a claim of compliance with
any regulation.

## How the interview grading stays honest

The pattern that runs through everything: **the bar is set before you speak.**

1. Question generation writes the rubric (three to five checkable points) and an ideal answer *first*
2. The server redacts both from every API response until the question closes, so you cannot peek and the grader cannot drift
3. Scoring is mechanically banded (9 to 10 means every rubric point; 0 to 2 means wrong or empty); length, confidence, and buzzwords earn nothing; a "strength" must quote your actual words
4. Summaries and scorecards are computed deterministically from stored evaluations, with no closing LLM call that could inflate the numbers (the narrative debrief is additive garnish, never the source of scores)

---

## CLI usage (articles)

```bash
python main.py --topic "How does Kafka handle backpressure?" --level intermediate
python main.py --topic "pytest best practices" --level basic --no-web --no-diagrams
```

Output lands in `./output/<timestamp>__<slug>__<id>/` with the article markdown and a `meta.json` of verification reports. Interview sessions persist under `output/interviews/`, job targets under `output/job_profiles/`, resume reports under `output/resumes/`.

## Claude Code skill

A standalone version of the article pipeline ships as a Claude Code skill that needs no server and no keys: [generate-article.skill](generate-article.skill). Drag it into a Claude Code chat, then `/generate-article "your topic"`.

---

## Development

```bash
pip install -r requirements-dev.txt -c constraints.txt
python -m pytest tests/ -q
```

The suite needs no network, no keys, and no provider. It runs on canned model
clients, and its fixtures give every test its own output folder, settings
file, session key, and stage cache, so it cannot read or write your real work
(see `tests/conftest.py`).

Three groups of tests do more than call functions:

- `tests/browser` drives a real browser through the real interface. It needs
  `web/dist` built and Chrome or Chromium available; without them it skips.
- `tests/live` starts a real server, puts work in flight, and kills it, to
  check what the next server makes of what was left.
- `tests/test_render_process.py` starts real child processes, to check that
  a renderer and everything it started are gone after a timeout.

### Upgrading dependencies

`requirements.txt` holds the direct dependencies as ranges. `constraints.txt`
holds the exact version of everything. To move to newer versions:

```bash
python3 -m venv /tmp/scrivio-upgrade && source /tmp/scrivio-upgrade/bin/activate
pip install -r requirements-dev.txt          # no -c: resolve afresh
python -m pytest tests/ -q                    # must pass before anything is pinned
pip freeze --exclude-editable | grep -v -E "^(pip|setuptools|wheel)==" > new-pins.txt
```

Replace the pins in `constraints.txt` with `new-pins.txt`, keeping the comment
at the top, and commit the two files together. If the suite fails on the new
versions, do not pin them: fix the code or narrow the range in
`requirements.txt` first.

### Temporal

`pipeline/orchestrator/` holds a Temporal workflow for article generation. The
web application does **not** use it: articles started from the interface run
in the API process. It is an optional, separate way to run the pipeline, its
dependencies are in `requirements-temporal.txt`, and it is covered by unit
tests only. Do not read its presence as meaning the API is durable.

## Measured, not asserted: the article matchup evals

`python -m evals.matchup` runs the question that matters, **is the pipeline actually better than one prompt to the same model?**, as a blind experiment: same topics, both arms on your own subscription, position-swapped pairwise judging (a win must hold in both orders) by two independent judges (Claude strong tier + GPT-4o), with measured wall-clock and true LLM-call counts.

The verdict so far (Aug 2026, 3 topics + 2 post-fix rematches): **the one-prompt baseline leads 6 overall verdicts to 0, with 4 ties**, at roughly one eighth the wall time and 1/35th the calls. Chasing the judges' reasons produced real fixes (a fence-blind whitespace cleanup in citation resolution was silently de-indenting every code block in every article; a formulaic zinger-per-section cadence; SEO-blog citations for facts official docs in evidence stated), and the rematches show the fixed axes moving from losses to ties. What remains is structural: independently generated stages drift into self-contradiction in ways a single coherent generation doesn't. The honest conclusion this harness forced: the pipeline's one measured edge is its citation trail, and the roadmap is now to rebuild the flow around a single research-grounded generation that keeps it, rather than polishing a relay race that loses to a solo run.

## Roadmap

- **Measure single-generation against the relay.** `generation_mode="single"` collapses draft, editor, revision and polish into one call over the same verified evidence, with a deterministic gate that falls back to the relay when the single pass truncates, drops sections, or ignores citations. The matchup decides which one survives
- **Rebuild the article flow around one research-grounded generation** (search + trust-ranked evidence + a single strong whole-article draft + verify pass + deterministic gates), re-matched against the same baseline until it wins or the studio is honestly re-scoped
- Live validation of the codex/gemini/qwen CLI specs against real binaries (specs follow their documented flags; drift is a one-line registry fix)

## Working on the interface

The interface is a Vite + React + TypeScript workspace in `web/`, served at
**/** by the API once it is built. Five pages: **Home**, **Resume**,
**Job prep**, **Interviews**, and **Articles**. `⌘K` jumps anywhere.

For development, run the API and the Vite dev server side by side:

```bash
python -m api                      # the API, on :8899
(cd web && npm run dev)            # the interface with hot reload, on :5180
```

Open **http://localhost:5180**. The dev server forwards every API route to
the backend, including progress streams, downloads, and audio. To point it at
an API on another port, set `SCRIVIO_BACKEND=http://localhost:PORT`.

`/studio` and `/desk` are aliases for old bookmarks. An older single-file
interface is kept at `/classic`. If the current interface has not been built,
the server serves that older one at `/` and says so, in the terminal and on
the page.

---

## License

MIT. See [LICENSE](LICENSE). Use it, fork it, ship it commercially: keep the
copyright notice and it is yours to build on.

The honesty guarantees are the point of this project, so if you fork it,
keep them working or drop the claim. A resume tool that invents a metric is
worse than no tool.

## Contributing

Issues and pull requests are welcome. Two house rules the codebase holds to:

- **Every honesty promise gets a deterministic guard, not a prompt.** Prompt
  discipline has been measured failing here repeatedly (ignored word caps,
  misfiled warnings, invented numbers). If a rule matters, enforce it in
  Python and cover it with a test.
- **Run the suite before opening a PR:** `python -m pytest -q`. It needs no
  API keys and no network; the mock provider covers every LLM path, so a
  green run on a laptop means the same thing it means in CI.
