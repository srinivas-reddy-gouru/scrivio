import asyncio
from types import SimpleNamespace

from pipeline.schemas.models import (
    ArticleRequest,
    RelevanceCheck,
    StoryBrief,
)
from pipeline.workers.relevance_worker import (
    amend_request_with_missing_aspects,
    check_relevance,
)


class _MockMessages:
    def __init__(self, tool_input: dict) -> None:
        self.tool_input = tool_input
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    name="submit_relevance_check",
                    input=self.tool_input,
                )
            ]
        )


class _MockClient:
    def __init__(self, tool_input: dict) -> None:
        self.messages = _MockMessages(tool_input)


def _sample_brief() -> StoryBrief:
    return StoryBrief(
        thesis="Spring Boot's @ConditionalOnMissingBean silently overrides user configuration.",
        angle="deep-dive",
        reader_pain_point="Engineers debug for hours when their @Bean is ignored.",
        key_insight="Auto-config beans defer only to predefined override mechanisms.",
        hook_seed="The app starts cleanly but writes to the wrong database.",
        suggested_title="The Silent Override: When Your @Bean Loses",
    )


# ── check_relevance ─────────────────────────────────────────────────────

def test_check_relevance_parses_aligned_verdict() -> None:
    client = _MockClient({
        "aligned": True,
        "missing_aspects": [],
        "suggested_thesis_adjustment": "",
        "reasoning": "On target.",
    })
    request = ArticleRequest(topic="Spring Boot", extra_context="cover @ConditionalOnMissingBean")

    result = asyncio.run(check_relevance(request, _sample_brief(), client))

    assert isinstance(result, RelevanceCheck)
    assert result.aligned is True
    assert result.missing_aspects == []


def test_check_relevance_parses_misaligned_verdict() -> None:
    client = _MockClient({
        "aligned": False,
        "missing_aspects": ["bean lifecycle", "dependency injection"],
        "suggested_thesis_adjustment": "Spring Boot's productivity comes from bean lifecycle, DI, and auto-config working together.",
        "reasoning": "Brief drifted to one annotation pitfall for a broad topic.",
    })
    request = ArticleRequest(topic="Spring Boot")

    result = asyncio.run(check_relevance(request, _sample_brief(), client))

    assert result.aligned is False
    assert "bean lifecycle" in result.missing_aspects
    assert "dependency injection" in result.missing_aspects
    assert result.suggested_thesis_adjustment.startswith("Spring Boot's productivity")


def test_check_relevance_user_message_includes_topic_and_context() -> None:
    """The LLM needs to see user_topic, extra_context, must_cover, AND the brief
    so it can compare them. Asserting structure of the user message protects
    against accidental field-drop refactors."""
    client = _MockClient({
        "aligned": True, "missing_aspects": [], "suggested_thesis_adjustment": "",
        "reasoning": "ok",
    })
    request = ArticleRequest(
        topic="database",
        extra_context="cover ACID and indexes",
        must_cover=["MVCC", "query planner"],
    )

    asyncio.run(check_relevance(request, _sample_brief(), client))

    user_content = client.messages.calls[0]["messages"][0]["content"]
    assert "user_topic: database" in user_content
    assert "cover ACID and indexes" in user_content
    assert "MVCC" in user_content
    assert "query planner" in user_content
    # Brief content should also be in there.
    assert "@ConditionalOnMissingBean" in user_content


def test_check_relevance_emits_none_marker_for_empty_extra_context() -> None:
    """Like the editor, extra_context should be labeled '(none)' when blank
    so the LLM never has to guess whether it was omitted vs. forgotten."""
    client = _MockClient({
        "aligned": True, "missing_aspects": [], "suggested_thesis_adjustment": "",
        "reasoning": "ok",
    })
    request = ArticleRequest(topic="x")
    asyncio.run(check_relevance(request, _sample_brief(), client))
    user_content = client.messages.calls[0]["messages"][0]["content"]
    assert "user_extra_context: (none)" in user_content
    assert "user_must_cover: (none)" in user_content


def test_check_relevance_uses_tool_use_with_correct_name() -> None:
    client = _MockClient({
        "aligned": True, "missing_aspects": [], "suggested_thesis_adjustment": "",
        "reasoning": "ok",
    })
    asyncio.run(check_relevance(ArticleRequest(topic="x"), _sample_brief(), client))
    call = client.messages.calls[0]
    assert call["tool_choice"]["name"] == "submit_relevance_check"


# ── amend_request_with_missing_aspects ──────────────────────────────────

def test_amend_request_appends_missing_aspects_to_extra_context() -> None:
    request = ArticleRequest(
        topic="Spring Boot",
        extra_context="initial steering",
    )
    amended = amend_request_with_missing_aspects(
        request,
        ["bean lifecycle", "dependency injection"],
    )
    assert "initial steering" in amended.extra_context
    assert "bean lifecycle" in amended.extra_context
    assert "dependency injection" in amended.extra_context
    assert "MUST be covered" in amended.extra_context


def test_amend_request_handles_empty_extra_context() -> None:
    request = ArticleRequest(topic="Spring Boot")
    amended = amend_request_with_missing_aspects(request, ["bean lifecycle"])

    # No leading separator when there was no prior context.
    assert not amended.extra_context.startswith("|")
    assert "bean lifecycle" in amended.extra_context


def test_amend_request_is_noop_when_no_missing_aspects() -> None:
    """A relevance check that found no missing aspects must not amend
    anything — keeps the request immutable in the aligned path."""
    request = ArticleRequest(topic="Spring Boot", extra_context="original")
    amended = amend_request_with_missing_aspects(request, [])

    assert amended is request


def test_amend_request_preserves_other_fields() -> None:
    """Amending extra_context must not alter topic, must_cover, or any other
    request field — only extra_context changes."""
    request = ArticleRequest(
        topic="Spring Boot",
        explanation_level="advanced",
        audience_role="senior engineer",
        must_cover=["beans"],
        clarification_answers={"scope": "production"},
    )
    amended = amend_request_with_missing_aspects(request, ["lifecycle"])

    assert amended.topic == "Spring Boot"
    assert amended.explanation_level == "advanced"
    assert amended.audience_role == "senior engineer"
    assert amended.must_cover == ["beans"]
    assert amended.clarification_answers == {"scope": "production"}


# ── Integration: brief → relevance → retry flow inside generate_article ──

def test_generate_article_regenerates_brief_when_relevance_check_fails(
    monkeypatch, tmp_path,
) -> None:
    """Exercises the Sprint 3 wiring inside main.generate_article: a brief that
    fails relevance must trigger ONE regeneration with the amended extra_context.
    No second relevance check (retry capped at 1)."""
    import main
    from pipeline.schemas.models import (
        ArticlePlan,
        DraftPackage,
        EvidenceSpan,
        PublishedArticle,
    )

    request = ArticleRequest(topic="Spring Boot")

    # Track every brief call so we can assert (a) it was called twice and
    # (b) the second call's extra_context contained the missing aspects.
    brief_calls: list[ArticleRequest] = []

    async def fake_generate_brief(req, client):
        brief_calls.append(req)
        return _sample_brief()

    monkeypatch.setattr(main, "_generate_brief", fake_generate_brief)

    # The first relevance check fails with missing aspects; we should NOT see
    # a second relevance check because retries are capped at 1.
    relevance_calls: list[ArticleRequest] = []

    async def fake_check_relevance(req, brief, client):
        relevance_calls.append(req)
        return RelevanceCheck(
            aligned=False,
            missing_aspects=["bean lifecycle", "dependency injection"],
            suggested_thesis_adjustment="rewrite",
            reasoning="drift",
        )

    monkeypatch.setattr(main, "check_relevance", fake_check_relevance)

    # Stub out every downstream stage so the test stays focused on the
    # brief→relevance→retry flow without exercising the full pipeline.
    span = EvidenceSpan(source_url="https://example.com", content="x")
    empty_plan = ArticlePlan(
        request=request, sections=[], claims=[], visual_intents=[],
        evidence_span_ids=[],
    )
    empty_draft = DraftPackage(plan=empty_plan, sections=[], raw_markdown="")
    sample_article = PublishedArticle(
        request=request, title="t", markdown="md",
    )

    async def stub_collect(req, brief, client, official_domains=frozenset(), debug_info=None):
        return [span]

    async def stub_run_planner(req, spans, client, brief=None):
        return empty_plan

    async def stub_fill_gaps(plan, spans, client, **kwargs):
        return spans

    async def stub_verify(plan, spans, client):
        return plan, spans, []

    async def stub_draft(plan, spans, client):
        return empty_draft

    async def stub_assets(plan, client):
        return []

    async def stub_editor(plan, draft, client):
        from pipeline.schemas.models import EditorReport
        return EditorReport(approved=True, overall_assessment="ok")

    async def stub_polish(draft, plan, client, assets=None, **kwargs):
        return sample_article

    async def stub_critic(markdown, plan, client):
        from pipeline.schemas.models import CriticVerdict
        return CriticVerdict(approved=True, issues=[], overall_assessment="ok")

    monkeypatch.setattr(main, "_collect_evidence_spans", stub_collect)
    monkeypatch.setattr(main, "run_planner", stub_run_planner)
    monkeypatch.setattr(main, "_fill_evidence_gaps", stub_fill_gaps)
    monkeypatch.setattr(main, "run_verification_loop", stub_verify)
    monkeypatch.setattr(main, "draft_all_sections", stub_draft)
    monkeypatch.setattr(main, "_generate_assets", stub_assets)
    monkeypatch.setattr(main, "run_editor_review", stub_editor)
    monkeypatch.setattr(main, "polish_draft_to_article", stub_polish)
    monkeypatch.setattr(main, "critique_article", stub_critic)

    # Use an isolated cache directory so prior test runs can't cache-hit.
    monkeypatch.setenv("ARTICLE_CACHE", "0")

    asyncio.run(main.generate_article(request))

    # Brief must have been called twice — once initially, once after relevance
    # check flagged misalignment.
    assert len(brief_calls) == 2, f"expected 2 brief calls, got {len(brief_calls)}"
    # First call: original empty extra_context.
    assert brief_calls[0].extra_context == ""
    # Second call: amended with missing aspects.
    assert "bean lifecycle" in brief_calls[1].extra_context
    assert "dependency injection" in brief_calls[1].extra_context

    # Relevance check must have been called exactly once (no recursive retry).
    assert len(relevance_calls) == 1


def test_generate_article_does_not_regenerate_brief_when_aligned(
    monkeypatch,
) -> None:
    """When the relevance check says aligned=true, brief must NOT be
    regenerated. This is the hot path — must stay one brief call."""
    import main
    from pipeline.schemas.models import (
        ArticlePlan, DraftPackage, EditorReport, EvidenceSpan, PublishedArticle,
    )

    request = ArticleRequest(topic="Spring Boot")
    brief_calls: list[ArticleRequest] = []

    async def fake_generate_brief(req, client):
        brief_calls.append(req)
        return _sample_brief()

    async def fake_check_relevance(req, brief, client):
        return RelevanceCheck(aligned=True, missing_aspects=[])

    monkeypatch.setattr(main, "_generate_brief", fake_generate_brief)
    monkeypatch.setattr(main, "check_relevance", fake_check_relevance)

    # Minimal stubs for the rest of the pipeline.
    span = EvidenceSpan(source_url="https://example.com", content="x")
    empty_plan = ArticlePlan(
        request=request, sections=[], claims=[], visual_intents=[],
        evidence_span_ids=[],
    )
    empty_draft = DraftPackage(plan=empty_plan, sections=[], raw_markdown="")
    article = PublishedArticle(request=request, title="t", markdown="md")

    async def _none(*a, **kw): return None
    async def stub_collect2(*a, **kw):
        return [span]
    monkeypatch.setattr(main, "_collect_evidence_spans", stub_collect2)
    monkeypatch.setattr(main, "run_planner", lambda *a, **kw: _wrap(empty_plan))
    monkeypatch.setattr(main, "_fill_evidence_gaps", lambda *a, **kw: _wrap([span]))
    monkeypatch.setattr(main, "run_verification_loop",
                        lambda *a, **kw: _wrap((empty_plan, [span], [])))
    monkeypatch.setattr(main, "draft_all_sections",
                        lambda *a, **kw: _wrap(empty_draft))
    monkeypatch.setattr(main, "_generate_assets", lambda *a, **kw: _wrap([]))
    monkeypatch.setattr(main, "run_editor_review", lambda *a, **kw:
                        _wrap(EditorReport(approved=True, overall_assessment="ok")))
    monkeypatch.setattr(main, "polish_draft_to_article",
                        lambda *a, **kw: _wrap(article))
    from pipeline.schemas.models import CriticVerdict
    monkeypatch.setattr(main, "critique_article",
                        lambda *a, **kw: _wrap(CriticVerdict(
                            approved=True, issues=[], overall_assessment="ok")))
    monkeypatch.setenv("ARTICLE_CACHE", "0")

    asyncio.run(main.generate_article(request))

    assert len(brief_calls) == 1, (
        f"aligned brief must not trigger regeneration; got {len(brief_calls)} calls"
    )


async def _wrap(value):
    """Helper: lift a value into an awaitable so it can be returned from
    a lambda that pretends to be an async function."""
    return value
