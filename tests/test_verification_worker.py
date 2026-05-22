import asyncio
from types import SimpleNamespace

from pipeline.schemas.models import (
    ArticlePlan,
    ArticleRequest,
    ArticleSection,
    Claim,
    EvidenceSpan,
    VerificationReport,
)
from pipeline.workers import verification_worker
from pipeline.workers.verification_worker import (
    drop_unsupported_claims,
    run_verification_loop,
    verify_claim,
)


class MockParseCompletions:
    def __init__(self, reports: list[VerificationReport]) -> None:
        self.reports = reports
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        report = self.reports.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=report))]
        )


class MockCreateCompletions:
    def __init__(self, queries: list[str] | None = None) -> None:
        self.queries = queries or ["corrective query"]
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        query = self.queries.pop(0) if self.queries else "corrective query"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=query))]
        )


class MockOpenAIClient:
    def __init__(
        self,
        reports: list[VerificationReport],
        queries: list[str] | None = None,
    ) -> None:
        self.parse_completions = MockParseCompletions(reports)
        self.create_completions = MockCreateCompletions(queries)
        self.beta = SimpleNamespace(
            chat=SimpleNamespace(completions=self.parse_completions)
        )
        self.chat = SimpleNamespace(completions=self.create_completions)


def make_plan(
    claim: Claim, span: EvidenceSpan | None = None
) -> tuple[ArticlePlan, list[EvidenceSpan]]:
    spans = [span] if span is not None else []
    plan = ArticlePlan(
        request=ArticleRequest(topic="verification"),
        sections=[
            ArticleSection(title="Checks", claim_ids=[str(claim.claim_id)])
        ],
        claims=[claim],
        visual_intents=[],
        evidence_span_ids=[str(item.span_id) for item in spans],
    )
    return plan, spans


def test_verify_claim_parses_supported_report() -> None:
    span = EvidenceSpan(
        source_url="https://example.com/source",
        content="Postgres uses MVCC for transaction isolation.",
    )
    claim = Claim(
        text="Postgres uses MVCC.",
        source_ids=[str(span.span_id)],
    )
    expected = VerificationReport(
        claim_id=str(claim.claim_id),
        support_status="supported",
        verifier_note="The evidence states this directly.",
    )
    client = MockOpenAIClient([expected])

    report = asyncio.run(verify_claim(claim, [span], client))

    assert report == expected
    assert len(client.parse_completions.calls) == 1
    assert client.parse_completions.calls[0]["model"] == "gpt-4o-mini"
    assert client.parse_completions.calls[0]["response_format"] is VerificationReport


def test_verify_claim_empty_source_ids_returns_unsupported_without_llm() -> None:
    claim = Claim(text="This claim has no evidence.", source_ids=[])
    client = MockOpenAIClient([])

    report = asyncio.run(verify_claim(claim, [], client))

    assert report.claim_id == str(claim.claim_id)
    assert report.support_status == "unsupported"
    assert client.parse_completions.calls == []


def test_run_verification_loop_retries_weak_claim_and_reverifies(
    monkeypatch,
) -> None:
    old_span = EvidenceSpan(
        source_url="https://example.com/old",
        content="Old evidence is tangential.",
    )
    new_span = EvidenceSpan(
        source_url="https://example.com/new",
        content="New evidence directly supports the claim.",
    )
    claim = Claim(
        text="The new evidence supports the claim.",
        source_ids=[str(old_span.span_id)],
    )
    plan, spans = make_plan(claim, old_span)
    reports = [
        VerificationReport(claim_id=str(claim.claim_id), support_status="weak"),
        VerificationReport(claim_id=str(claim.claim_id), support_status="supported"),
    ]
    client = MockOpenAIClient(reports)

    async def fake_corrective_search(claim, client_search, client_llm):
        return [new_span]

    monkeypatch.setattr(
        verification_worker, "corrective_search", fake_corrective_search
    )

    updated_plan, updated_spans, final_reports = asyncio.run(
        run_verification_loop(plan, spans, client)
    )

    assert updated_plan.claims[0].support_status == "supported"
    assert updated_plan.claims[0].corrective_attempts == 1
    assert str(new_span.span_id) in updated_plan.claims[0].source_ids
    assert updated_spans == [old_span, new_span]
    assert final_reports[0].support_status == "supported"


def test_drop_unsupported_claims_removes_unsupported_and_empty_sections() -> None:
    supported_claim = Claim(text="Kept claim.", source_ids=["s1"], support_status="supported")
    weak_claim = Claim(text="Weak claim.", source_ids=["s2"], support_status="weak")
    unsupported_claim = Claim(
        text="Dropped claim.", source_ids=["s3"], support_status="unsupported"
    )
    plan = ArticlePlan(
        request=ArticleRequest(topic="filter"),
        sections=[
            ArticleSection(
                title="Mixed section",
                claim_ids=[str(supported_claim.claim_id), str(unsupported_claim.claim_id)],
            ),
            ArticleSection(
                title="All unsupported section",
                claim_ids=[str(unsupported_claim.claim_id)],
            ),
            ArticleSection(
                title="Weak section",
                claim_ids=[str(weak_claim.claim_id)],
            ),
        ],
        claims=[supported_claim, weak_claim, unsupported_claim],
        visual_intents=[],
        evidence_span_ids=[],
    )

    pruned = drop_unsupported_claims(plan)

    assert {c.text for c in pruned.claims} == {"Kept claim.", "Weak claim."}
    assert [s.title for s in pruned.sections] == ["Mixed section", "Weak section"]
    assert pruned.sections[0].claim_ids == [str(supported_claim.claim_id)]


def test_drop_unsupported_claims_keeps_original_when_everything_is_unsupported() -> None:
    bad = Claim(text="Bad.", source_ids=[], support_status="unsupported")
    plan = ArticlePlan(
        request=ArticleRequest(topic="filter"),
        sections=[ArticleSection(title="Only", claim_ids=[str(bad.claim_id)])],
        claims=[bad],
        visual_intents=[],
        evidence_span_ids=[],
    )

    pruned = drop_unsupported_claims(plan)

    assert pruned is plan


# ── Sprint 5: relevance_status verdict ──────────────────────────────────

def test_verify_claim_passes_user_topic_to_llm() -> None:
    """The verifier must see user_topic + user_extra_context so it can
    produce the relevance_status verdict. Asserts the user message contains
    those fields — protects against accidental drop in future refactors."""
    span = EvidenceSpan(
        source_url="https://example.com/source",
        content="Spring Boot uses Hikari as the default connection pool.",
    )
    claim = Claim(
        text="Spring Boot uses Hikari as the default connection pool.",
        source_ids=[str(span.span_id)],
    )
    expected = VerificationReport(
        claim_id=str(claim.claim_id),
        support_status="supported",
        relevance_status="relevant",
    )
    client = MockOpenAIClient([expected])

    asyncio.run(
        verify_claim(
            claim, [span], client,
            user_topic="Spring Boot",
            user_extra_context="cover the bean lifecycle",
            article_thesis="Spring Boot's auto-config causes subtle production failures.",
            article_angle="deep-dive",
        )
    )

    user_content = client.parse_completions.calls[0]["messages"][1]["content"]
    assert "user_topic: Spring Boot" in user_content
    assert "user_extra_context: cover the bean lifecycle" in user_content
    assert "article_thesis: Spring Boot's auto-config" in user_content
    assert "article_angle: deep-dive" in user_content


def test_verify_claim_emits_none_marker_when_topic_blank() -> None:
    """A blank user_topic must still appear in the user message as '(none)'
    so the verifier never has to guess whether it was omitted vs. forgotten.
    Same applies to article_thesis and article_angle when not provided."""
    span = EvidenceSpan(source_url="https://x.example", content="content.")
    claim = Claim(text="a claim", source_ids=[str(span.span_id)])
    client = MockOpenAIClient([
        VerificationReport(claim_id=str(claim.claim_id), support_status="supported")
    ])

    asyncio.run(verify_claim(claim, [span], client))

    user_content = client.parse_completions.calls[0]["messages"][1]["content"]
    assert "user_topic: (none)" in user_content
    assert "user_extra_context: (none)" in user_content
    assert "article_thesis: (none)" in user_content
    assert "article_angle: (none)" in user_content


def test_verify_all_claims_threads_thesis_and_angle_through() -> None:
    """verify_all_claims must pull article_thesis + article_angle from
    plan.brief and pass them to each verify_claim call. Without them the
    verifier can't make context-aware relevance decisions — it would
    fall back to guessing from user_topic alone."""
    from pipeline.schemas.models import StoryBrief
    from pipeline.workers.verification_worker import verify_all_claims

    span = EvidenceSpan(
        source_url="https://example.com",
        content="Spring Boot beans are managed by the IoC container.",
    )
    claim = Claim(
        text="Spring Boot beans are managed by the IoC container.",
        source_ids=[str(span.span_id)],
    )
    brief = StoryBrief(
        thesis="Most Spring Boot apps fail in production from misconfigured resilience patterns.",
        angle="deep-dive",
        reader_pain_point="Silent failures in prod.",
        key_insight="Auto-config has well-defined override hooks.",
        hook_seed="The app starts cleanly but writes to the wrong database.",
        suggested_title="Spring Boot in Production",
    )
    plan = ArticlePlan(
        request=ArticleRequest(topic="Spring Boot", extra_context="cover beans"),
        brief=brief,
        sections=[ArticleSection(title="x", claim_ids=[str(claim.claim_id)])],
        claims=[claim],
        visual_intents=[],
        evidence_span_ids=[str(span.span_id)],
    )
    client = MockOpenAIClient([
        VerificationReport(claim_id=str(claim.claim_id), support_status="supported")
    ])

    asyncio.run(verify_all_claims(plan, [span], client))

    user_content = client.parse_completions.calls[0]["messages"][1]["content"]
    assert "user_topic: Spring Boot" in user_content
    assert "cover beans" in user_content
    assert "Most Spring Boot apps fail in production" in user_content
    assert "article_angle: deep-dive" in user_content


def test_run_verification_loop_copies_relevance_status_onto_claims(
    monkeypatch,
) -> None:
    """After the loop terminates, each claim must carry the verifier's
    relevance verdict — that's what drop_unsupported_claims later reads."""
    span = EvidenceSpan(
        source_url="https://example.com/a",
        content="...",
    )
    claim = Claim(text="Some claim.", source_ids=[str(span.span_id)])
    plan, spans = make_plan(claim, span)
    reports = [
        VerificationReport(
            claim_id=str(claim.claim_id),
            support_status="supported",
            relevance_status="off_topic",
        ),
    ]
    client = MockOpenAIClient(reports)

    updated_plan, _, _ = asyncio.run(run_verification_loop(plan, spans, client))

    assert updated_plan.claims[0].relevance_status == "off_topic"
    assert updated_plan.claims[0].support_status == "supported"


def test_drop_unsupported_claims_drops_off_topic_even_when_supported() -> None:
    """The Sprint 5 promise: a factually-supported-but-off-topic claim
    (e.g., MongoDB sharding in a Spring Boot article) gets pruned just
    like an unsupported one."""
    on_topic = Claim(
        text="Spring Boot uses Hikari.",
        source_ids=["s1"],
        support_status="supported",
        relevance_status="relevant",
    )
    off_topic = Claim(
        text="MongoDB uses sharding for horizontal scale.",
        source_ids=["s2"],
        support_status="supported",
        relevance_status="off_topic",
    )
    plan = ArticlePlan(
        request=ArticleRequest(topic="Spring Boot"),
        sections=[
            ArticleSection(
                title="Mixed",
                claim_ids=[str(on_topic.claim_id), str(off_topic.claim_id)],
            ),
        ],
        claims=[on_topic, off_topic],
        visual_intents=[],
        evidence_span_ids=[],
    )

    pruned = drop_unsupported_claims(plan)

    assert {c.text for c in pruned.claims} == {"Spring Boot uses Hikari."}
    assert pruned.sections[0].claim_ids == [str(on_topic.claim_id)]


def test_drop_unsupported_claims_keeps_tangential_claims() -> None:
    """tangential is the soft category — the verifier is uncertain about
    relevance. Keep them; the editor and planner have their own filters."""
    tangential = Claim(
        text="A loosely related claim.",
        source_ids=["s1"],
        support_status="supported",
        relevance_status="tangential",
    )
    plan = ArticlePlan(
        request=ArticleRequest(topic="x"),
        sections=[ArticleSection(title="s", claim_ids=[str(tangential.claim_id)])],
        claims=[tangential],
        visual_intents=[],
        evidence_span_ids=[],
    )

    pruned = drop_unsupported_claims(plan)

    assert len(pruned.claims) == 1
    assert pruned.claims[0].relevance_status == "tangential"


def test_verify_claim_pins_claim_id_when_llm_returns_wrong_id() -> None:
    """GPT-4o-mini can hallucinate a different claim_id in structured output.
    verify_claim must override it with the canonical input ID, otherwise
    run_verification_loop raises KeyError on the report_by_claim_id lookup."""
    span = EvidenceSpan(source_url="https://example.com", content="evidence text.")
    claim = Claim(text="a claim.", source_ids=[str(span.span_id)])
    # Simulate the LLM returning a completely different claim_id.
    wrong_report = VerificationReport(
        claim_id="00000000-0000-0000-0000-000000000000",
        support_status="supported",
    )
    client = MockOpenAIClient([wrong_report])

    report = asyncio.run(verify_claim(claim, [span], client))

    assert report.claim_id == str(claim.claim_id)
    assert report.support_status == "supported"


def test_run_verification_loop_stops_after_two_failed_attempts(
    monkeypatch,
) -> None:
    original_span = EvidenceSpan(
        source_url="https://example.com/original",
        content="Original evidence is weak.",
    )
    new_spans = [
        EvidenceSpan(source_url="https://example.com/one", content="Still weak."),
        EvidenceSpan(source_url="https://example.com/two", content="Still weak."),
    ]
    claim = Claim(
        text="The claim keeps failing verification.",
        source_ids=[str(original_span.span_id)],
    )
    plan, spans = make_plan(claim, original_span)
    reports = [
        VerificationReport(claim_id=str(claim.claim_id), support_status="weak"),
        VerificationReport(claim_id=str(claim.claim_id), support_status="weak"),
        VerificationReport(claim_id=str(claim.claim_id), support_status="weak"),
    ]
    client = MockOpenAIClient(reports)
    corrective_calls = []

    async def fake_corrective_search(claim, client_search, client_llm):
        corrective_calls.append(str(claim.claim_id))
        return [new_spans[len(corrective_calls) - 1]]

    monkeypatch.setattr(
        verification_worker, "corrective_search", fake_corrective_search
    )

    updated_plan, updated_spans, final_reports = asyncio.run(
        run_verification_loop(plan, spans, client, max_retries=2)
    )

    assert updated_plan.claims[0].support_status == "weak"
    assert updated_plan.claims[0].corrective_attempts == 2
    assert len(corrective_calls) == 2
    assert updated_spans == [original_span, *new_spans]
    assert final_reports[0].support_status == "weak"
