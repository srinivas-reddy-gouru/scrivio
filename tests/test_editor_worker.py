import asyncio
from types import SimpleNamespace

from pipeline.schemas.models import (
    ArticlePlan,
    ArticleRequest,
    ArticleSection,
    Claim,
    DraftPackage,
    DraftSection,
    EditorReport,
    EvidenceSpan,
    SectionRevision,
    StoryBrief,
)
from pipeline.workers import drafting_worker, editor_worker


def _build_plan_and_draft() -> tuple[ArticlePlan, DraftPackage, list[EvidenceSpan]]:
    span = EvidenceSpan(
        source_url="https://example.com",
        content="Concurrent writes serialize through MVCC.",
    )
    claim = Claim(text="MVCC serializes writes.", source_ids=[str(span.span_id)])
    sections = [
        ArticleSection(title="Hook", claim_ids=[str(claim.claim_id)]),
        ArticleSection(title="Body", claim_ids=[str(claim.claim_id)]),
        ArticleSection(title="Close", claim_ids=[str(claim.claim_id)]),
    ]
    brief = StoryBrief(
        thesis="MVCC quietly governs every concurrent Postgres write you make.",
        angle="deep-dive",
        reader_pain_point="Engineers hit confusing transaction conflicts without knowing why.",
        key_insight="MVCC trades storage for non-blocking reads.",
        hook_seed="Two transactions update the same row. Neither blocks. How?",
        suggested_title="The Hidden Engine Behind Every Postgres Write",
    )
    plan = ArticlePlan(
        request=ArticleRequest(topic="MVCC"),
        brief=brief,
        sections=sections,
        claims=[claim],
        visual_intents=[],
        evidence_span_ids=[str(span.span_id)],
    )
    draft = DraftPackage(
        plan=plan,
        sections=[
            DraftSection(title="Hook", content="Original hook content. [src:s1]", citation_ids=[]),
            DraftSection(title="Body", content="Original body content. [src:s1]", citation_ids=[]),
            DraftSection(title="Close", content="Original close content. [src:s1]", citation_ids=[]),
        ],
        raw_markdown="",
    )
    return plan, draft, [span]


def test_revise_draft_only_redrafts_flagged_sections(monkeypatch) -> None:
    plan, draft, spans = _build_plan_and_draft()
    editor_report = EditorReport(
        approved=False,
        overall_assessment="Hook is weak; rewrite.",
        revisions=[
            SectionRevision(
                section_title="Hook",
                issues=["Opens with a generic statement."],
                instruction="Open with the concrete two-transaction scenario from the hook seed.",
            )
        ],
    )
    redraft_calls = []

    async def fake_draft_section(section, plan_arg, spans_arg, client, **kwargs):
        redraft_calls.append((section.title, kwargs.get("revision_note")))
        return DraftSection(
            title=section.title,
            content=f"REWRITTEN {section.title}",
            citation_ids=[],
        )

    monkeypatch.setattr(editor_worker, "draft_section", fake_draft_section)

    new_draft = asyncio.run(
        editor_worker.revise_draft(plan, draft, spans, editor_report, client=None)
    )

    assert [call[0] for call in redraft_calls] == ["Hook"]
    assert redraft_calls[0][1] == editor_report.revisions[0].instruction
    assert new_draft.sections[0].content == "REWRITTEN Hook"
    assert new_draft.sections[1].content == "Original body content. [src:s1]"
    assert new_draft.sections[2].content == "Original close content. [src:s1]"
    assert "REWRITTEN Hook" in new_draft.raw_markdown


def test_revise_draft_passes_through_when_no_revisions() -> None:
    plan, draft, spans = _build_plan_and_draft()
    editor_report = EditorReport(approved=True, overall_assessment="Good.", revisions=[])

    new_draft = asyncio.run(
        editor_worker.revise_draft(plan, draft, spans, editor_report, client=None)
    )

    assert new_draft is draft


def test_run_editor_review_invokes_anthropic_with_tool_use() -> None:
    plan, draft, _ = _build_plan_and_draft()
    expected_report = EditorReport(
        approved=True,
        overall_assessment="Reads cleanly. Voice is consistent.",
        revisions=[],
    )
    calls = []

    class MockMessages:
        async def create(self, **kwargs):
            calls.append(kwargs)
            tool_use = SimpleNamespace(type="tool_use", input=expected_report.model_dump())
            return SimpleNamespace(content=[tool_use])

    client = SimpleNamespace(messages=MockMessages())

    report = asyncio.run(editor_worker.run_editor_review(plan, draft, client))

    assert report == expected_report
    assert calls[0]["model"] == "claude-sonnet-4-6"
    assert calls[0]["tool_choice"]["name"] == "submit_editor_report"
    user_content = calls[0]["messages"][0]["content"]
    assert "article_thesis:" in user_content
    assert "Hook" in user_content


def test_run_editor_review_passes_user_topic_and_extra_context() -> None:
    """The editor must see the user's original topic + extra_context so it can
    perform the priority-0 request-alignment check defined in editor_v2.txt."""
    plan, draft, _ = _build_plan_and_draft()
    # Override the request so the assertions are unambiguous.
    plan = plan.model_copy(
        update={
            "request": ArticleRequest(
                topic="MVCC fundamentals",
                extra_context="Cover snapshot isolation and vacuum.",
            )
        }
    )
    calls = []

    class MockMessages:
        async def create(self, **kwargs):
            calls.append(kwargs)
            tool_use = SimpleNamespace(
                type="tool_use",
                input=EditorReport(approved=True, overall_assessment="ok").model_dump(),
            )
            return SimpleNamespace(content=[tool_use])

    client = SimpleNamespace(messages=MockMessages())
    asyncio.run(editor_worker.run_editor_review(plan, draft, client))

    user_content = calls[0]["messages"][0]["content"]
    assert "user_topic: MVCC fundamentals" in user_content
    assert "user_extra_context: Cover snapshot isolation and vacuum." in user_content


def test_run_editor_review_emits_none_marker_when_extra_context_blank() -> None:
    """When the user provides no extra_context the field must still be present
    in the user message — labeled '(none)' — so the editor never has to guess."""
    plan, draft, _ = _build_plan_and_draft()
    calls = []

    class MockMessages:
        async def create(self, **kwargs):
            calls.append(kwargs)
            tool_use = SimpleNamespace(
                type="tool_use",
                input=EditorReport(approved=True, overall_assessment="ok").model_dump(),
            )
            return SimpleNamespace(content=[tool_use])

    client = SimpleNamespace(messages=MockMessages())
    asyncio.run(editor_worker.run_editor_review(plan, draft, client))

    user_content = calls[0]["messages"][0]["content"]
    assert "user_extra_context: (none)" in user_content


# ── Citation density (mechanical check) ──────────────────────────────

def _density_section(title: str, content: str):
    from pipeline.schemas.models import DraftSection
    return DraftSection(title=title, content=content, citation_ids=[])


def test_density_flags_low_citation_section() -> None:
    from pipeline.workers.editor_worker import section_citation_density

    low = (
        "First factual paragraph without citation.\n\n"
        "Second factual paragraph, also uncited.\n\n"
        "Third paragraph with a citation [src:abc123].\n\n"
        "Fourth uncited paragraph.\n\n"
        "Fifth uncited paragraph."
    )
    density = section_citation_density(low)
    assert density is not None and density < 0.4


def test_density_passes_well_cited_section() -> None:
    from pipeline.workers.editor_worker import section_citation_density

    good = (
        "Claim one [src:a].\n\n"
        "Claim two backed by evidence [src:b].\n\n"
        "A narrative transition paragraph.\n\n"
        "Claim three [src:c]."
    )
    assert section_citation_density(good) >= 0.4


def test_density_exempts_code_walkthroughs_and_ignores_code_content() -> None:
    from pipeline.workers.editor_worker import section_citation_density

    walkthrough = (
        "Short intro.\n\n"
        "```java\n" + "\n".join(f"line{i}();" for i in range(30)) + "\n```"
    )
    assert section_citation_density(walkthrough) is None


def test_flag_low_citation_sections_appends_revision_and_unapproves() -> None:
    from pipeline.schemas.models import (
        ArticlePlan, ArticleRequest, DraftPackage, EditorReport,
    )
    from pipeline.workers.editor_worker import flag_low_citation_sections

    plan = ArticlePlan(
        request=ArticleRequest(topic="t"), sections=[], claims=[],
        visual_intents=[], evidence_span_ids=[],
    )
    uncited = "\n\n".join(f"Uncited factual paragraph {i}." for i in range(5))
    cited = "\n\n".join(f"Cited paragraph {i} [src:x]." for i in range(5))
    draft = DraftPackage(
        plan=plan,
        sections=[
            _density_section("Well cited", cited),
            _density_section("Citation desert", uncited),
        ],
        raw_markdown="",
    )
    report = EditorReport(approved=True, overall_assessment="ok", revisions=[])

    out = flag_low_citation_sections(draft, report)
    assert out.approved is False
    assert [r.section_title for r in out.revisions] == ["Citation desert"]
    assert "below the 40% floor" in out.revisions[0].issues[0]

    # A fully-cited draft leaves the report untouched.
    clean_draft = DraftPackage(
        plan=plan, sections=[_density_section("Well cited", cited)], raw_markdown="",
    )
    untouched = flag_low_citation_sections(clean_draft, report)
    assert untouched.approved is True and untouched.revisions == []
