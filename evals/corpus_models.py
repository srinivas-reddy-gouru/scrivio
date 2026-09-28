"""The shape of an evaluation case.

Cases are data, kept in versioned folders under evals/corpus/. They are
loaded through these models so that a malformed case fails when it is
read and not halfway through a run.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pipeline.schemas.models import StructuredResume, TailoredResume

ResumeCategory = Literal[
    "unsupported_fact", "missing_information", "promotion", "reordered_entries",
    "format_variation", "language_variation", "legitimate_edit",
]


class Record(BaseModel):
    """One job or one degree, as it must appear after the guard."""
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str = ""
    start: str = ""
    end: str = ""


class ResumeExpectation(BaseModel):
    """What must be true of the resume once the guard has run.

    Text is matched against every string in the guarded resume, joined.
    Nothing here asks whether the prose is good: factual preservation is
    scored on its own, apart from checklist score and writing quality."""
    model_config = ConfigDict(extra="forbid")

    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    warnings_must_mention: list[str] = Field(default_factory=list)
    placeholders: int | None = None
    work: list[Record] | None = None
    education: list[Record] | None = None


class ResumeCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    category: ResumeCategory
    probes: str = Field(min_length=20, description="What this case is for, in a sentence.")
    original: StructuredResume
    model_output: TailoredResume = Field(
        description="What a model is imagined to have returned. Written by "
                    "hand to be wrong in one specific way, or right.")
    user_text: str = ""
    jd_text: str = ""
    expect: ResumeExpectation
    known_gap: str = Field(
        default="",
        description="Set when the guard is known not to meet the expectation. "
                    "The case is then reported as a gap, and is required to "
                    "keep failing: one that starts passing must be promoted.")


class ResumeCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    written_by: str
    cases: list[ResumeCase]


AnswerKind = Literal[
    "complete", "partial", "wrong", "empty", "long_and_empty", "short_and_complete",
    "confident_and_wrong", "instructs_the_grader", "buzzwords", "hedged_and_correct",
]


class InterviewCase(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    kind: AnswerKind
    probes: str = Field(min_length=20)
    question: str
    level: Literal["basic", "intermediate", "advanced"]
    rubric_key_points: list[str] = Field(min_length=2)
    model_answer: str
    candidate_answer: str
    points_covered: list[int] = Field(
        description="Indexes into rubric_key_points that the answer covers "
                    "accurately, as judged by whoever wrote the case.")
    expected_score_min: int = Field(ge=0, le=10)
    expected_score_max: int = Field(ge=0, le=10)
    paired_with: str = Field(
        default="",
        description="Another case with the same points covered and a different "
                    "length or manner. The two should score within two points "
                    "of each other: the difference is style, not substance.")


class InterviewCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    written_by: str
    expected_scores_judged_by: str = Field(
        description="Who assigned the expected bands. Said plainly, because a "
                    "band assigned by a model is not a human judgment.")
    cases: list[InterviewCase]
