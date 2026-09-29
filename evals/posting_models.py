"""The shape of the posting corpus: public postings, invented resumes.

Loaded through these so that a malformed record fails when it is read.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pipeline.schemas.models import StructuredResume

Split = Literal["tuning", "held_out"]
Kind = Literal["tech", "standard", "credential", "method", "field", "term", "proper", "code"]
# A claim of skill, as against the name of a place or an agency.
CLAIMS: tuple[str, ...] = ("tech", "standard", "credential", "method", "field", "term")


class Posting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    source: str
    source_url: str = Field(pattern=r"^https://")
    collected_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    licence: str
    licence_basis: str
    publisher: str
    title: str
    sections_kept: list[str]
    sections_left_out: list[str]
    text: str = Field(min_length=500)
    split: Split
    personal_details_found: int = 0


class Name(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    also_a_word: bool = False


class Annotations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    annotated_by: str
    what_is_annotated: str
    kinds: dict[str, str]
    grade_codes: str
    names: dict[str, Name]


class Rewording(BaseModel):
    model_config = ConfigDict(extra="forbid")

    where: str
    line: str


class SyntheticResume(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    synthetic: str
    resume: StructuredResume
    honest_rewordings: list[Rewording]


class Outcome(BaseModel):
    """One constructed rewrite, and what the guard did with it."""
    posting: str
    split: Split
    family: Literal["unsupported", "supported", "ordinary_word", "honest_rewording"]
    form: str
    when: Literal["first tailoring", "later edit"]
    what: str
    kind: str = ""
    also_a_word: bool = False
    wrong: bool


class Tally(BaseModel):
    cases: int = 0
    wrong: int = 0

    @property
    def rate(self) -> float:
        return self.wrong / self.cases if self.cases else 0.0


class SplitReport(BaseModel):
    split: Split
    postings: int
    names_annotated: int
    names_missed: int
    ordinary_words: int
    ordinary_words_taken_for_names: int
    # Which ones. Filled in for the tuning postings and left empty for
    # the held-out ones, whose totals are all that is shown.
    which_names_were_missed: list[str] = Field(default_factory=list)
    which_words_were_mistaken: list[str] = Field(default_factory=list)
    which_claims_were_retained: list[str] = Field(default_factory=list)
    which_rewordings_were_reverted: list[str] = Field(default_factory=list)
    unsupported_retained: Tally
    unsupported_retained_by_form: dict[str, Tally]
    unsupported_retained_names_that_are_also_words: Tally
    supported_reverted: Tally
    ordinary_word_reverted: Tally
    honest_rewording_reverted: Tally


class Report(BaseModel):
    corpus: str
    guard_commit: str
    what_this_is: str
    splits: list[SplitReport]
