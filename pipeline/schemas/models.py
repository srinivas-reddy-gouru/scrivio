import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, UUID4


ExplanationLevel = Literal["basic", "intermediate", "advanced"]
ArticleAngle = Literal["tutorial", "deep-dive", "comparison", "war-story", "contrarian", "explainer"]
ModelPreset = Literal["balanced", "best", "fast"]


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


ProgressEventType = Literal[
    "stage_started", "stage_completed", "complete", "error", "cancelled"
]


class ProgressEvent(BaseModel):
    type: ProgressEventType
    stage: str
    message: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    data: dict = {}
