import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, UUID4


ExplanationLevel = Literal["basic", "intermediate", "advanced"]
ArticleAngle = Literal["tutorial", "deep-dive", "comparison", "war-story", "contrarian", "explainer"]
ModelPreset = Literal["balanced", "best", "fast"]
LLMProvider = Literal["auto", "anthropic", "openai", "claude-cli"]


class ArticleRequest(BaseModel):
    # Allow fields named model_* without Pydantic complaining about the
    # protected "model_" namespace (model_preset is ours, not Pydantic's).
    model_config = ConfigDict(protected_namespaces=())

    topic: str
    explanation_level: ExplanationLevel = "intermediate"
    audience_role: str = "software engineer"
    # "balanced" = Haiku for routing, Sonnet for writing (default).
    # "best"     = Sonnet everywhere.
    # "fast"     = Haiku everywhere except the core drafter + polish.
    model_preset: ModelPreset = "balanced"
    # "relay"  = draft each section, then editor, revision, and polish.
    # "single" = one call writes the whole article from verified evidence.
    # The relay is the default until the matchup evals say otherwise; this
    # flag exists so that comparison is a measurement, not an argument.
    generation_mode: Literal["relay", "single"] = "relay"
    # Per-request provider pin. "auto" (default) keeps the key-based
    # resolution (single key → that provider; both keys → LLM_PROVIDER env
    # preference). "anthropic"/"openai" forces that provider for the writing
    # stages when its key is present; if the key is missing the pin is
    # ignored and resolution falls back to auto.
    llm_provider: LLMProvider = "auto"
    web_search: bool = True
    max_source_age_days: int = 365
    include_gifs: bool = False
    include_diagrams: bool = True
    extra_context: str = ""
    # ── Sprint 2: clarification + user steering ──────────────────────
    # Explicit "must cover these sub-topics" — overrides the brief's freedom
    # to pick its own scope.
    must_cover: list[str] = []
    # Answers the user gave to the clarification questions, keyed by
    # ClarificationQuestion.id. Composed into the user-facing extra_context
    # by the API layer before the brief runs.
    clarification_answers: dict[str, str] = {}
    # When true, broad topics skip the clarification step and run with the
    # broad-topic default angle. Used by clients that want "just generate
    # something reasonable, don't ask me."
    skip_clarification: bool = False
    # Set when this request was started from an existing article's "Re-run"
    # action: the id (output directory name) of the article it re-runs.
    # Persisted in meta.json, letting the library group runs of the same
    # article into a version chain instead of listing near-duplicates.
    rerun_of: str | None = None


# ── Clarification flow models ────────────────────────────────────────
# Used by the /clarify and /generate endpoints to ask the user 2-4 targeted
# questions when their topic is broad and unsteered. Returned in a single
# round-trip; the user answers all at once and resubmits.

class ClarificationQuestion(BaseModel):
    id: str = Field(
        description="Stable slug like 'scope' or 'depth'. Used as the key in clarification_answers."
    )
    question: str = Field(description="The question text shown to the user.")
    options: list[str] = Field(
        default_factory=list,
        description="Suggested answers. Empty list means free-text only.",
    )


class ClarificationQuestions(BaseModel):
    questions: list[ClarificationQuestion]
    default_if_skipped: str = Field(
        default="",
        description="One-sentence description of the article that will be generated if the user skips clarification — so they know what they're agreeing to.",
    )


CriticCategory = Literal[
    "title", "opening", "citations", "diagrams",
    "consistency", "voice", "structure", "code",
]
CriticSeverity = Literal["minor", "moderate", "blocking"]


class CriticIssue(BaseModel):
    """One issue flagged by the post-polish critic agent.

    The critic reads the FINAL polished article and identifies the kinds
    of problems a senior editor would notice on a first read but that
    the existing pipeline stages don't catch — title clichés, broken
    diagrams, missing citations on big claims, unit inconsistency,
    contrarian voice creeping back in.
    """
    category: CriticCategory = Field(
        description=(
            "Which axis the issue lives on. 'title' = the article title; "
            "'opening' = the first paragraph or section opener; 'citations' "
            "= sourcing density or authority; 'diagrams' = mermaid quality "
            "or placement; 'consistency' = units, language, conventions; "
            "'voice' = tone, accusatory phrasing, punchline patterns; "
            "'structure' = section ordering, missing pieces."
        )
    )
    severity: CriticSeverity = Field(
        description=(
            "blocking = the article should not ship without this fix. "
            "moderate = worth fixing if possible. "
            "minor = nice-to-have polish, log-only."
        )
    )
    location: str = Field(
        description=(
            "Where in the article this issue lives. Use the section title "
            "for section issues, '(title)' for the article title, "
            "'(opening)' for the first paragraph, or '(global)' for things "
            "spanning the whole article."
        )
    )
    issue: str = Field(description="One-sentence description of what's wrong.")
    fix: str = Field(
        description=(
            "Specific actionable instruction for the polish pass. Not "
            "'make it better' — 'rewrite the title without the (And It's "
            "Not X) parenthetical' or 'add a citation after the 1.2M RPS "
            "claim in section 2'."
        )
    )


class CriticVerdict(BaseModel):
    """Output of the critic agent — the final quality gate before publish."""
    approved: bool = Field(
        description=(
            "True if the article is ready to publish as-is (no blocking issues). "
            "Even if there are minor or moderate issues, approved can be true "
            "if none of them rise to blocking severity."
        )
    )
    issues: list[CriticIssue] = Field(
        default_factory=list,
        description="Every issue found, with severity. Can be empty.",
    )
    overall_assessment: str = Field(
        default="",
        description=(
            "One- or two-sentence summary of the article's overall quality "
            "for the user-facing debug pane and logs."
        ),
    )

    def has_blocking_issues(self) -> bool:
        return any(i.severity == "blocking" for i in self.issues)


class RelevanceCheck(BaseModel):
    """Output of the relevance-checker agent that runs right after the brief.

    Catches topic drift before the expensive search/plan/draft stages: if the
    brief invented a thesis that doesn't actually serve the user's request,
    the pipeline regenerates the brief once with `missing_aspects` injected
    into extra_context.
    """
    aligned: bool = Field(
        description=(
            "True if the brief's thesis and angle would produce an article a "
            "reasonable reader expecting an answer to `request.topic` would "
            "find on-target."
        )
    )
    missing_aspects: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete aspects the brief should have covered but doesn't, given "
            "the user's topic and any extra_context. Empty when aligned=true."
        ),
    )
    suggested_thesis_adjustment: str = Field(
        default="",
        description=(
            "One-sentence proposed rewording of the brief's thesis that would "
            "bring it back on target. Empty when aligned=true."
        ),
    )
    reasoning: str = Field(
        default="",
        description="Short justification, for logs and debugging.",
    )


class ClarificationState(BaseModel):
    original_prompt: str
    filled_request: ArticleRequest | None = None
    questions_asked: list[str] = []
    is_complete: bool = False


class StoryBrief(BaseModel):
    thesis: str
    angle: ArticleAngle
    reader_pain_point: str
    key_insight: str
    hook_seed: str
    suggested_title: str


class EvidenceSpan(BaseModel):
    span_id: UUID4 = Field(default_factory=uuid.uuid4)
    source_url: str
    source_title: str = ""
    content: str
    published_at: datetime | None = None
    trust_score: float = Field(0.8, ge=0.0, le=1.0)
    was_filtered: bool = False


SupportStatus = Literal["supported", "weak", "unsupported"]
RelevanceStatus = Literal["relevant", "tangential", "off_topic"]


class Claim(BaseModel):
    claim_id: UUID4 = Field(default_factory=uuid.uuid4)
    text: str
    source_ids: list[str]
    support_status: SupportStatus = "supported"
    # Whether the claim is on-topic for the user's request. Set by the
    # verifier. `off_topic` claims are dropped before drafting even when
    # they are factually supported.
    relevance_status: RelevanceStatus = "relevant"
    freshness_sensitive: bool = False
    corrective_attempts: int = 0


class VisualIntent(BaseModel):
    intent_id: UUID4 = Field(default_factory=uuid.uuid4)
    description: str
    format: Literal["mermaid", "graphviz", "vhs"] = "mermaid"
    rationale: str
    # Title of the section this diagram belongs in. The drafter inserts a
    # <!-- DIAGRAM:{intent_id} --> placeholder in that section; the compiler
    # later replaces the placeholder with a rendered mermaid block. When None,
    # the diagram is unattached and won't appear in the article.
    section_title: str | None = None


class ArticleSection(BaseModel):
    title: str
    claim_ids: list[str]
    notes: str = ""
    narrative_note: str = ""


class ArticlePlan(BaseModel):
    request: ArticleRequest
    brief: StoryBrief | None = None
    sections: list[ArticleSection]
    claims: list[Claim]
    visual_intents: list[VisualIntent]
    evidence_span_ids: list[str]


class DraftSection(BaseModel):
    title: str
    content: str
    citation_ids: list[str]


class DraftPackage(BaseModel):
    plan: ArticlePlan
    sections: list[DraftSection]
    raw_markdown: str = ""


class RenderAsset(BaseModel):
    asset_id: UUID4 = Field(default_factory=uuid.uuid4)
    intent: VisualIntent
    spec: str
    output_path: str = ""
    qa_passed: bool = False


class VerificationReport(BaseModel):
    claim_id: str
    support_status: SupportStatus
    # Whether the claim — independent of whether it is factually supported —
    # is relevant to the user's request. Two-axis verdict means we drop a
    # factually-supported-but-off-topic claim (e.g., "PostgreSQL uses MVCC"
    # in a Spring Boot article) instead of letting it leak through.
    relevance_status: RelevanceStatus = "relevant"
    verifier_note: str = ""


class SectionRevision(BaseModel):
    section_title: str
    issues: list[str]
    instruction: str


class StructuralHint(BaseModel):
    """A structural improvement suggestion from the editor that does not rise
    to the level of a blocking revision but would make the article more useful.
    Examples: add a comparison table, add a quick-reference summary, split a
    wall of prose into a labelled list. Hints are injected as an addendum to
    the drafter's revision_note so the rewritten section includes the
    enhancement naturally rather than having it bolted on afterward."""
    section_title: str = Field(
        description=(
            "Exact section title from the draft that should be enhanced. "
            "Must match one of the draft section titles verbatim."
        )
    )
    hint: str = Field(
        description=(
            "One sentence describing the structural improvement. Be specific: "
            "'Summarise the three mechanisms as a Markdown table with columns: "
            "mechanism, state model, filter class, use case' is good. "
            "'Make it clearer' is not."
        )
    )


class EditorReport(BaseModel):
    approved: bool
    overall_assessment: str
    revisions: list[SectionRevision] = []
    structural_hints: list[StructuralHint] = Field(
        default_factory=list,
        description=(
            "Structural enhancements the drafter should apply when rewriting. "
            "These do NOT affect the approved flag or revision count — they are "
            "additive improvements (tables, summaries, labelled lists) that make "
            "the article more scannable or complete."
        ),
    )


class PublishedArticle(BaseModel):
    request: ArticleRequest
    title: str
    markdown: str
    assets: list[RenderAsset] = []
    verification_reports: list[VerificationReport] = []
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ── Interview practice models ────────────────────────────────────────
# The practice flow: generate interview questions (with a hidden rubric and
# model answer written BEFORE any user answer — the anti-sycophancy anchor),
# evaluate free-text answers against that rubric, optionally drill down with
# ONE follow-up question, and persist the whole session on disk for history.

class InterviewQuestion(BaseModel):
    # model_answer is ours, not Pydantic's — silence the namespace warning.
    model_config = ConfigDict(protected_namespaces=())

    id: str = Field(
        description="Stable slug 'q1'..'qN' in the order questions are asked."
    )
    question: str = Field(
        description=(
            "One free-text interview question. Open-ended — never answerable "
            "with yes/no or a single word."
        )
    )
    difficulty: ExplanationLevel = Field(
        description="Must match the requested level of this practice session."
    )
    section_anchor: str = Field(
        default="",
        description=(
            "When an article is provided: the EXACT heading text (without # "
            "marks) of the article section this question tests, copied "
            "verbatim from the outline. Empty string for topic-only sessions."
        ),
    )
    rubric_key_points: list[str] = Field(
        min_length=2,
        description=(
            "3-5 key points a strong answer must contain, written BEFORE "
            "seeing any candidate answer. Never shown to the candidate while "
            "the question is open."
        ),
    )
    model_answer: str = Field(
        description=(
            "A concise ideal answer (80-200 words), written BEFORE seeing any "
            "candidate answer. Revealed to the candidate only after they "
            "finish the question."
        )
    )


class InterviewQuestionSet(BaseModel):
    """Tool schema for submit_interview_questions."""
    questions: list[InterviewQuestion] = Field(min_length=1)


AnswerVerdict = Literal["incorrect", "shallow", "adequate", "strong"]


class AnswerEvaluation(BaseModel):
    """Tool schema for submit_answer_evaluation."""
    score: int = Field(
        ge=0, le=10,
        description=(
            "Strictly rubric-based: 9-10 = every key point covered accurately; "
            "6-8 = most key points; 3-5 = some points but superficial; "
            "0-2 = wrong, empty, or 'I don't know'. Never inflated to be "
            "encouraging."
        ),
    )
    verdict: AnswerVerdict = Field(
        description="Band matching the score: 0-2 incorrect, 3-5 shallow, 6-8 adequate, 9-10 strong."
    )
    strengths: list[str] = Field(
        description=(
            "Specific things the answer got right, quoting or closely "
            "paraphrasing the candidate's own words. Empty if none — do not "
            "invent praise."
        )
    )
    gaps: list[str] = Field(
        description="Rubric key points that are missing or under-explained."
    )
    misconceptions: list[str] = Field(
        description=(
            "Candidate statements that contradict the reference material. "
            "Empty if none — do not invent misconceptions."
        )
    )
    suggestions: list[str] = Field(
        description=(
            "Concrete pointers for a better answer. In article mode each "
            "references an article section by its exact heading."
        )
    )
    section_pointers: list[str] = Field(
        description=(
            "Exact article headings worth re-reading (article mode), or "
            "short study pointers (topic mode)."
        )
    )
    needs_followup: bool = Field(
        description=(
            "True ONLY for a first answer that is shallow-but-salvageable "
            "(score 3-6). Never true when evaluating a follow-up answer."
        )
    )
    followup_question: str = Field(
        default="",
        description="Exactly one drill-down question when needs_followup, else empty.",
    )


# Persistence-only models below (never used as LLM tool schemas).

# practice: feedback after every answer, follow-ups allowed.
# simulation: realistic screen — no feedback until the end, then a debrief.
# drill: rapid-fire short questions, 60s each, compact feedback.
# job: resume+JD-targeted realistic screen — simulation semantics but
#      follow-ups allowed (real interviewers probe); ends in a scorecard.
# coding: one problem worked through in phases (clarify, approach, code,
#         defend) rather than a list of questions. Graded on whether the
#         thinking was legible, which is what actually fails candidates,
#         not only on whether the code is right.
InterviewMode = Literal["practice", "simulation", "drill", "job", "coding"]

CodingPhase = Literal["clarify", "approach", "code", "defend"]


class CodeCheck(BaseModel):
    """One deterministic finding about submitted code.

    Computed by parsing, never by executing: an interview-prep tool must
    not run a stranger's code, and the checks that matter most here (does
    it parse, does it define the function it was asked for, is it a stub)
    need no execution anyway. The grader receives these as facts so it
    cannot be talked out of them."""
    name: str
    passed: bool
    detail: str


class CodingProblem(BaseModel):
    # model_solution is ours, not Pydantic's.
    model_config = ConfigDict(protected_namespaces=())

    """Tool schema for submit_coding_problem. Everything below `signature`
    is sealed until the round ends, the same contract the spoken rounds
    use: the bar is written before the candidate starts."""
    title: str = Field(description="Short problem name, e.g. 'Merge overlapping intervals'.")
    statement: str = Field(
        description=(
            "The problem exactly as an interviewer would say it out loud: "
            "concrete, 3-6 sentences, with an example input and output. "
            "Deliberately leaves some constraints unstated so a strong "
            "candidate has something real to ask about."
        )
    )
    language: str = Field(default="python", description="Language the candidate writes in.")
    signature: str = Field(
        description="The exact function signature to implement, e.g. 'def merge(intervals: list[list[int]]) -> list[list[int]]:'."
    )
    stated_constraints: list[str] = Field(
        default_factory=list,
        description="Constraints given upfront, e.g. 'intervals fit in memory'.",
    )
    unstated_constraints: list[str] = Field(
        min_length=1,
        description=(
            "SEALED. The things a strong candidate should ASK about before "
            "coding: empty input, overlapping vs touching intervals, whether "
            "input is sorted, duplicates, integer overflow. Scoring the "
            "clarify phase reads this list."
        ),
    )
    optimal_complexity: str = Field(
        description="SEALED. e.g. 'O(n log n) time from the sort, O(n) space for the output'."
    )
    model_solution: str = Field(
        description="SEALED. A clean reference implementation, revealed only at the end."
    )


class CodingRound(BaseModel):
    """Tool schema for submit_coding_round: the problem plus one sealed
    rubric per phase."""
    problem: CodingProblem
    phases: list["InterviewQuestion"] = Field(
        min_length=3,
        description=(
            "One entry per phase in order (clarify, approach, code, defend). "
            "Each carries its own rubric_key_points and model_answer, written "
            "before the candidate starts."
        ),
    )


class InterviewAnswerRecord(BaseModel):
    answer: str
    evaluation: AnswerEvaluation
    answered_at: datetime = Field(default_factory=datetime.utcnow)
    # Confidence calibration: the score the candidate predicted for
    # themselves BEFORE seeing feedback. None = didn't predict.
    predicted_score: int | None = Field(default=None, ge=0, le=10)


InterviewQuestionStatus = Literal[
    "pending", "awaiting_followup", "completed", "skipped"
]


class InterviewQuestionState(BaseModel):
    question: InterviewQuestion
    status: InterviewQuestionStatus = "pending"
    first: InterviewAnswerRecord | None = None
    # When a follow-up was asked, this record's evaluation is the FINAL
    # combined evaluation of both answers together.
    followup: InterviewAnswerRecord | None = None
    final_score: int | None = None  # None until completed; stays None if skipped


class InterviewSummary(BaseModel):
    total_questions: int
    answered: int
    skipped: int
    average_score: float | None  # None when every question was skipped
    per_question: list[dict] = []  # [{id, question, final_score, status}]
    # section_pointers ranked by frequency across final evaluations that
    # scored <= 6 — the "re-read these" list and the seed for future
    # weak-area tracking.
    weak_sections: list[str] = []
    top_gaps: list[str] = []
    # Mean |predicted - actual| over answers that carried a prediction.
    # None when nothing was predicted. Low = well-calibrated.
    calibration_gap: float | None = None
    # Simulation mode only: the interviewer's end-of-screen narrative
    # verdict (one extra LLM call; empty string when unavailable).
    debrief: str = ""
    # Job mode only: the full recruiter-grade scorecard. Quoted forward
    # reference — JobScorecard is defined below; InterviewSummary.model_rebuild()
    # runs after it.
    scorecard: "JobScorecard | None" = None


# ── Job-targeted interview models ────────────────────────────────────

EvidenceStrength = Literal["strong", "partial", "missing"]


class Competency(BaseModel):
    name: str = Field(description="Short competency name derived from the JD, e.g. 'Distributed systems design'.")
    why_it_matters: str = Field(description="One sentence tying this competency to the JD's own wording.")
    evidence_in_resume: EvidenceStrength = Field(
        description="How well the resume evidences this competency: strong / partial / missing."
    )
    probe_note: str = Field(
        description="One sentence on what an interviewer should probe (a resume claim to verify, or the gap to expose)."
    )


class JobAnalysis(BaseModel):
    """Tool schema for submit_job_analysis."""
    competencies: list[Competency] = Field(
        min_length=3,
        description="5-8 core competencies that define success in THIS role, derived from the JD (not generic).",
    )
    resume_highlights: list[str] = Field(
        description="Specific resume claims worth grilling in the interview, quoted or closely paraphrased."
    )
    gaps: list[str] = Field(
        description="Things the JD requires that the resume shows little or no evidence of."
    )
    company_context: str = Field(
        default="",
        description="1-2 sentences of company context relevant to interviewing, empty if company unknown.",
    )


class JobProfile(BaseModel):
    profile_id: str
    role_title: str
    company: str = ""
    location: str = ""
    seniority: str = ""
    job_description: str
    resume_text: str
    extra_notes: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StudyResource(BaseModel):
    competency: str
    title: str
    url: str
    trust_score: float = 0.6


class CompetencyScore(BaseModel):
    name: str
    score: float | None = None     # None = never tested (all its questions skipped)
    band: Literal["strong", "adequate", "needs work", "not assessed"] = "not assessed"
    evidence: list[str] = []       # quotes from the candidate's own answers
    gaps: list[str] = []


class RequirementCoverage(BaseModel):
    requirement: str = Field(description="A concrete requirement quoted or paraphrased from the JD.")
    status: Literal["met", "partial", "missing"] = Field(
        description="Whether the candidate's answers demonstrated it: met / partial / missing."
    )


class JobDebrief(BaseModel):
    """Tool schema for submit_job_debrief."""
    hire_signal: Literal[
        "strong hire", "hire", "lean hire", "lean no-hire", "no-hire"
    ] = Field(description="Calibrated to THIS role and seniority, never inflated.")
    debrief: str = Field(
        description="3-5 sentence narrative: overall impression anchored to named moments, the most important gap, one genuine strength."
    )
    requirement_coverage: list[RequirementCoverage] = Field(
        description="The JD's 4-8 most important concrete requirements with demonstrated coverage."
    )


class JobScorecard(BaseModel):
    competency_scores: list[CompetencyScore] = []
    requirement_coverage: list[RequirementCoverage] = []
    hire_signal: str = ""
    debrief: str = ""
    study_plan: list[StudyResource] = []


InterviewSummary.model_rebuild()


# ── Resume studio models ─────────────────────────────────────────────
# The structured resume uses JSON Resume-standard section and field names
# (jsonresume.org) — the open format Reactive Resume and its ecosystem
# import/export. Everything downstream (ATS checks, tailoring, DOCX/MD/
# JSON rendering) operates on this structure, never on raw text.

class ResumeBasics(BaseModel):
    name: str = Field(default="", description="Full name exactly as written.")
    label: str = Field(default="", description="Professional headline, e.g. 'Senior Backend Engineer'.")
    email: str = ""
    phone: str = ""
    url: str = ""
    location: str = ""
    summary: str = Field(default="", description="Professional summary verbatim from the resume; empty if absent.")


class ResumeWorkItem(BaseModel):
    name: str = Field(default="", description="Employer name exactly as written.")
    position: str = Field(default="", description="Job title exactly as written.")
    startDate: str = Field(default="", description="As written, e.g. 'Jan 2022'. Empty if absent.")
    endDate: str = Field(default="", description="As written, e.g. 'Present'. Empty if absent.")
    summary: str = ""
    highlights: list[str] = Field(
        default_factory=list,
        description="Bullet points verbatim, one per entry.",
    )


class ResumeEducationItem(BaseModel):
    institution: str = ""
    area: str = Field(default="", description="Field of study.")
    studyType: str = Field(default="", description="Degree type, e.g. 'B.Tech'.")
    startDate: str = ""
    endDate: str = ""
    score: str = ""


class ResumeSkill(BaseModel):
    name: str = Field(default="", description="Skill group name, e.g. 'Languages'.")
    keywords: list[str] = Field(default_factory=list)


class ResumeProject(BaseModel):
    name: str = ""
    description: str = ""
    url: str = ""
    highlights: list[str] = Field(default_factory=list)


class StructuredResume(BaseModel):
    """Tool schema for submit_resume_extraction (JSON Resume shape)."""
    basics: ResumeBasics = Field(default_factory=ResumeBasics)
    work: list[ResumeWorkItem] = []
    education: list[ResumeEducationItem] = []
    skills: list[ResumeSkill] = []
    projects: list[ResumeProject] = []
    certificates: list[str] = []


class AtsCheck(BaseModel):
    """One deterministic, code-computed check — never LLM opinion."""
    id: str
    label: str
    passed: bool
    weight: int
    detail: str


class KeywordCoverage(BaseModel):
    found: list[str] = []
    missing: list[str] = []
    percent: int = 0


class AtsReport(BaseModel):
    score: int  # 0-100; weighted checks, +30% keyword coverage when JD present
    checks: list[AtsCheck] = []
    keyword_coverage: KeywordCoverage | None = None


class ResumeIssue(BaseModel):
    category: Literal["impact", "clarity", "structure", "relevance", "red-flag"] = Field(
        description="impact = weak/unquantified achievement; clarity = hard to parse; structure = organization; relevance = misaligned with the JD; red-flag = something a recruiter would question."
    )
    detail: str = Field(description="The issue, quoting the resume's own words.")
    fix: str = Field(description="Concrete rewrite or action, not generic advice.")


class ResumeReview(BaseModel):
    """Tool schema for submit_resume_review."""
    strengths: list[str] = Field(description="What genuinely works, quoting the resume.")
    issues: list[ResumeIssue]
    missing_keywords: list[str] = Field(
        default_factory=list,
        description="JD requirements the resume does not evidence, including semantic gaps (JD says Kubernetes, resume only says 'containers'). Empty when no JD was given.",
    )
    summary: str = Field(description="2-3 sentence recruiter's first impression.")


ResumeChangeKind = Literal[
    "rephrased", "reordered", "added-keyword", "condensed", "placeholder"
]


class ResumeChange(BaseModel):
    kind: ResumeChangeKind
    where: str = Field(description="Path in the structure, e.g. 'work[0].highlights[2]' or 'basics.summary'.")
    what: str = Field(description="One sentence: what changed and why it serves the JD.")


class TailoredResume(BaseModel):
    """Tool schema for submit_tailored_resume."""
    resume: StructuredResume
    changes: list[ResumeChange] = []
    warnings: list[str] = Field(
        default_factory=list,
        description="Honesty notes: keywords that could NOT be claimed truthfully, and every [METRIC] placeholder needing a real number from the candidate.",
    )
    note: str = Field(
        default="",
        description="Status message to the candidate about THIS pass (e.g. why nothing changed, or what to supply next). Transient; never a durable note about the resume content — those belong in warnings.",
    )


class ResumeDoc(BaseModel):
    """Persisted resume-studio document (output/resumes/<id>.json).

    Analysis is two-phase: POST /resumes returns immediately with the
    deterministic report and status="analyzing"; a background task fills
    in the LLM parts (structure, review) and flips status to "ready".
    Old single-phase docs deserialize as "ready" via the default.
    """
    resume_id: str
    original_text: str
    status: Literal["analyzing", "ready", "error"] = "ready"
    error: str = ""
    tailor_status: Literal["idle", "tailoring", "error"] = "idle"
    tailor_error: str = ""
    structured: StructuredResume | None = None
    jd_text: str = ""
    jd_label: str = ""  # "role @ company" when sourced from a saved job target
    report: AtsReport | None = None
    review: ResumeReview | None = None
    tailored: TailoredResume | None = None
    tailored_report: AtsReport | None = None  # before/after comparison
    # Undo stack: the tailored version as it was before each mutation
    # (metric fill, manual edit, instructed edit, re-tailor). Capped at
    # 10; old docs deserialize with an empty stack.
    tailored_history: list[TailoredResume] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class InterviewSession(BaseModel):
    session_id: str
    article_id: str | None = None  # None = topic-only session
    topic: str
    level: str
    mode: InterviewMode = "practice"  # default keeps pre-mode session files valid
    job_profile_id: str | None = None  # set for mode="job"
    # Set for mode="coding": the one problem every phase works on. Sealed
    # fields are redacted from responses until the round ends.
    coding_problem: CodingProblem | None = None
    duration_minutes: int = 45         # job mode: the soft time budget
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    questions: list[InterviewQuestionState] = []
    summary: InterviewSummary | None = None  # set once all questions are terminal


ProgressEventType = Literal[
    # Emitted once at run start (before any stage) to name the model that
    # will execute each stage — see main._pipeline_models and the UI handler.
    "pipeline_info",
    "stage_started", "stage_completed", "complete", "error", "cancelled"
]


class ProgressEvent(BaseModel):
    type: ProgressEventType
    stage: str
    message: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    data: dict = {}
