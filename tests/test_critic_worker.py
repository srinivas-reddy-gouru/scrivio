import asyncio
from types import SimpleNamespace

from pipeline.schemas.models import (
    ArticlePlan,
    ArticleRequest,
    CriticIssue,
    CriticVerdict,
    StoryBrief,
)
from pipeline.workers.critic_worker import critique_article


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
                    name="submit_critic_verdict",
                    input=self.tool_input,
                )
            ]
        )


class _MockClient:
    def __init__(self, tool_input: dict) -> None:
        self.messages = _MockMessages(tool_input)


def _sample_plan() -> ArticlePlan:
    request = ArticleRequest(topic="Spring Boot", explanation_level="intermediate")
    brief = StoryBrief(
        thesis="x", angle="explainer", reader_pain_point="p", key_insight="k",
        hook_seed="h", suggested_title="t",
    )
    return ArticlePlan(
        request=request, brief=brief,
        sections=[], claims=[], visual_intents=[], evidence_span_ids=[],
    )


# ── Parsing ─────────────────────────────────────────────────────────

def test_critique_article_parses_approved_verdict() -> None:
    client = _MockClient({
        "approved": True, "issues": [],
        "overall_assessment": "Clean article, ships as-is.",
    })
    verdict = asyncio.run(critique_article("# Article\n\nBody.", _sample_plan(), client))
    assert isinstance(verdict, CriticVerdict)
    assert verdict.approved is True
    assert verdict.issues == []
    assert verdict.has_blocking_issues() is False


def test_critique_article_parses_blocking_issue() -> None:
    client = _MockClient({
        "approved": False,
        "issues": [{
            "category": "title",
            "severity": "blocking",
            "location": "(title)",
            "issue": "Title uses the (And It's Not X) parenthetical pattern.",
            "fix": "Rewrite as 'How X works' without parenthetical.",
        }],
        "overall_assessment": "Clean except for the title cliche.",
    })
    verdict = asyncio.run(critique_article("# Why Your X Stalls (And It's Not Y)\n\nBody.", _sample_plan(), client))

    assert verdict.approved is False
    assert verdict.has_blocking_issues() is True
    assert len(verdict.issues) == 1
    issue = verdict.issues[0]
    assert issue.category == "title"
    assert issue.severity == "blocking"
    assert issue.location == "(title)"
    assert "parenthetical" in issue.fix


def test_critique_article_classifies_severity_correctly() -> None:
    """has_blocking_issues returns true ONLY when severity is 'blocking'.
    Articles with only moderate or minor issues are publishable."""
    client = _MockClient({
        "approved": True,
        "issues": [
            {"category": "citations", "severity": "moderate", "location": "Section 2",
             "issue": "x", "fix": "y"},
            {"category": "voice", "severity": "minor", "location": "Section 3",
             "issue": "x", "fix": "y"},
        ],
        "overall_assessment": "Solid with two non-blocking notes.",
    })
    verdict = asyncio.run(critique_article("body", _sample_plan(), client))
    assert verdict.approved is True
    assert verdict.has_blocking_issues() is False


# ── User message structure ──────────────────────────────────────────

def test_critique_article_passes_full_article_and_request_context() -> None:
    """The critic needs to see user_topic + extra_context + level + angle
    + full article markdown to do its job. This test pins the user-message
    structure so accidental field-drops in refactors are caught."""
    client = _MockClient({
        "approved": True, "issues": [], "overall_assessment": "ok",
    })
    request = ArticleRequest(
        topic="Kafka",
        explanation_level="advanced",
        extra_context="cover producer reliability",
    )
    brief = StoryBrief(
        thesis="t", angle="explainer", reader_pain_point="p", key_insight="k",
        hook_seed="h", suggested_title="ti",
    )
    plan = ArticlePlan(
        request=request, brief=brief,
        sections=[], claims=[], visual_intents=[], evidence_span_ids=[],
    )
    article_md = "# Kafka\n\nKafka is a distributed log.\n\n## Producers\n\nProducers send records."

    asyncio.run(critique_article(article_md, plan, client))

    user_content = client.messages.calls[0]["messages"][0]["content"]
    assert "user_topic: Kafka" in user_content
    assert "cover producer reliability" in user_content
    assert "explanation_level: advanced" in user_content
    assert "article_angle: explainer" in user_content
    # Full article markdown including the h1 is passed through.
    assert "# Kafka" in user_content
    assert "Kafka is a distributed log." in user_content
    assert "## Producers" in user_content


def test_critique_article_uses_tool_use_with_correct_name() -> None:
    client = _MockClient({
        "approved": True, "issues": [], "overall_assessment": "ok",
    })
    asyncio.run(critique_article("body", _sample_plan(), client))
    call = client.messages.calls[0]
    assert call["tool_choice"]["name"] == "submit_critic_verdict"


def test_critique_article_handles_missing_brief() -> None:
    """Articles without a brief (rare — only when the brief stage failed)
    still need a critic verdict. The angle field becomes '(unknown)' rather
    than crashing."""
    request = ArticleRequest(topic="x", explanation_level="intermediate")
    plan = ArticlePlan(
        request=request, brief=None,
        sections=[], claims=[], visual_intents=[], evidence_span_ids=[],
    )
    client = _MockClient({
        "approved": True, "issues": [], "overall_assessment": "ok",
    })
    asyncio.run(critique_article("body", plan, client))
    user_content = client.messages.calls[0]["messages"][0]["content"]
    assert "article_angle: (unknown)" in user_content


# ── Integration: critic kicks back to polish for blocking issues ────

def test_generate_article_re_polishes_when_critic_blocks(
    monkeypatch, tmp_path,
) -> None:
    """When the critic flags blocking issues, generate_article must run a
    SECOND polish pass with the critic's feedback. No third pass — cap is 1."""
    import main
    from pipeline.schemas.models import (
        ArticlePlan, DraftPackage, DraftSection, EditorReport,
        EvidenceSpan, PublishedArticle, RelevanceCheck,
    )

    request = ArticleRequest(topic="Spring Boot", explanation_level="intermediate")
    polish_calls: list[dict] = []

    async def fake_polish(draft, plan, client, assets=None, *,
                          critic_feedback=None, prior_markdown=None):
        polish_calls.append({
            "critic_feedback": critic_feedback,
            "prior_markdown": prior_markdown,
        })
        return PublishedArticle(
            request=request,
            title="Polished",
            markdown="# Polished\n\nBody.",
        )

    monkeypatch.setattr(main, "polish_draft_to_article", fake_polish)

    # Critic returns one blocking issue on the first call.
    critic_calls: list[str] = []

    async def fake_critic(article_md, plan, client):
        critic_calls.append(article_md)
        return CriticVerdict(
            approved=False,
            issues=[CriticIssue(
                category="title", severity="blocking",
                location="(title)", issue="bad title", fix="rewrite it",
            )],
            overall_assessment="Title needs fixing.",
        )

    monkeypatch.setattr(main, "critique_article", fake_critic)

    # Stub everything upstream (brief through editor).
    async def fake_brief(req, client):
        return StoryBrief(
            thesis="t", angle="explainer", reader_pain_point="p",
            key_insight="k", hook_seed="h", suggested_title="ti",
        )
    monkeypatch.setattr(main, "_generate_brief", fake_brief)

    async def fake_rel(req, brief, client):
        return RelevanceCheck(aligned=True, missing_aspects=[])
    monkeypatch.setattr(main, "check_relevance", fake_rel)

    span = EvidenceSpan(source_url="https://x.example", content="x")

    async def stub_collect(*a, **kw): return [span]
    monkeypatch.setattr(main, "_collect_evidence_spans", stub_collect)

    empty_plan = ArticlePlan(
        request=request, sections=[], claims=[], visual_intents=[],
        evidence_span_ids=[],
    )
    async def stub_plan(*a, **kw): return empty_plan
    monkeypatch.setattr(main, "run_planner", stub_plan)

    async def stub_gap(*a, **kw): return [span]
    monkeypatch.setattr(main, "_fill_evidence_gaps", stub_gap)

    async def stub_verify(*a, **kw): return empty_plan, [span], []
    monkeypatch.setattr(main, "run_verification_loop", stub_verify)

    empty_draft = DraftPackage(plan=empty_plan, sections=[], raw_markdown="")
    async def stub_draft(*a, **kw): return empty_draft
    monkeypatch.setattr(main, "draft_all_sections", stub_draft)

    async def stub_assets(*a, **kw): return []
    monkeypatch.setattr(main, "_generate_assets", stub_assets)

    async def stub_editor(*a, **kw):
        return EditorReport(approved=True, overall_assessment="ok")
    monkeypatch.setattr(main, "run_editor_review", stub_editor)

    monkeypatch.setenv("ARTICLE_CACHE", "0")

    asyncio.run(main.generate_article(request))

    # Polish ran TWICE: once with no feedback (first pass), once with the
    # critic's issues (refinement). Not three times — retry is capped at 1.
    assert len(polish_calls) == 2, f"expected 2 polish calls, got {len(polish_calls)}"
    assert polish_calls[0]["critic_feedback"] is None
    assert polish_calls[1]["critic_feedback"] is not None
    assert polish_calls[1]["critic_feedback"][0].severity == "blocking"
    assert polish_calls[1]["prior_markdown"] is not None

    # Critic ran exactly ONCE — no recursive re-critique.
    assert len(critic_calls) == 1


def test_generate_article_does_not_re_polish_when_critic_approves(
    monkeypatch,
) -> None:
    """When the critic returns no blocking issues, polish runs ONCE.
    Moderate and minor issues are logged but don't trigger a re-polish."""
    import main
    from pipeline.schemas.models import (
        ArticlePlan, DraftPackage, EditorReport, EvidenceSpan,
        PublishedArticle, RelevanceCheck,
    )

    request = ArticleRequest(topic="Spring Boot", explanation_level="intermediate")
    polish_calls: list[dict] = []

    async def fake_polish(draft, plan, client, assets=None, *,
                          critic_feedback=None, prior_markdown=None):
        polish_calls.append({"critic_feedback": critic_feedback})
        return PublishedArticle(
            request=request, title="Polished", markdown="# Polished\n\nBody.",
        )

    async def fake_critic(article_md, plan, client):
        return CriticVerdict(
            approved=True,
            issues=[CriticIssue(
                category="citations", severity="moderate",
                location="Section 2", issue="thin", fix="add cite",
            )],
            overall_assessment="Approved with a moderate note.",
        )

    monkeypatch.setattr(main, "polish_draft_to_article", fake_polish)
    monkeypatch.setattr(main, "critique_article", fake_critic)

    async def fake_brief(req, client):
        return StoryBrief(
            thesis="t", angle="explainer", reader_pain_point="p",
            key_insight="k", hook_seed="h", suggested_title="ti",
        )
    monkeypatch.setattr(main, "_generate_brief", fake_brief)
    monkeypatch.setattr(main, "check_relevance",
                        lambda *a, **kw: _wrap(RelevanceCheck(aligned=True, missing_aspects=[])))

    span = EvidenceSpan(source_url="https://x.example", content="x")
    monkeypatch.setattr(main, "_collect_evidence_spans",
                        lambda *a, **kw: _wrap([span]))
    empty_plan = ArticlePlan(
        request=request, sections=[], claims=[], visual_intents=[],
        evidence_span_ids=[],
    )
    monkeypatch.setattr(main, "run_planner", lambda *a, **kw: _wrap(empty_plan))
    monkeypatch.setattr(main, "_fill_evidence_gaps",
                        lambda *a, **kw: _wrap([span]))
    monkeypatch.setattr(main, "run_verification_loop",
                        lambda *a, **kw: _wrap((empty_plan, [span], [])))
    empty_draft = DraftPackage(plan=empty_plan, sections=[], raw_markdown="")
    monkeypatch.setattr(main, "draft_all_sections",
                        lambda *a, **kw: _wrap(empty_draft))
    monkeypatch.setattr(main, "_generate_assets", lambda *a, **kw: _wrap([]))
    monkeypatch.setattr(main, "run_editor_review", lambda *a, **kw:
                        _wrap(EditorReport(approved=True, overall_assessment="ok")))
    monkeypatch.setenv("ARTICLE_CACHE", "0")

    asyncio.run(main.generate_article(request))

    # Polish ran ONCE — no retry because no blocking issue.
    assert len(polish_calls) == 1


async def _wrap(value):
    """Lift a value into an awaitable so it can be returned from a lambda
    pretending to be an async function."""
    return value
