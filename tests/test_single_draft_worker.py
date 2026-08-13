"""The one-call article path and, mostly, its refusal to publish a bad one.

The acceptance gate is the reason this mode is safe to run: a single call
can fail in ways a section relay cannot, and none of those failures are
visible to the model producing them.
"""
import asyncio
from types import SimpleNamespace

import pytest

from pipeline.schemas.models import (
    ArticlePlan, ArticleRequest, ArticleSection, EvidenceSpan,
)
from pipeline.workers.single_draft_worker import (
    SinglePassRejected, generate_whole_article, single_pass_defects,
)


def _plan(n_sections: int = 4) -> ArticlePlan:
    return ArticlePlan(
        request=ArticleRequest(topic="How does Kafka handle backpressure?"),
        sections=[
            ArticleSection(title=f"Section {i}", claim_ids=[],
                           narrative_note="explain the mechanism")
            for i in range(n_sections)
        ],
        claims=[],
        visual_intents=[],
        evidence_span_ids=[],
    )


def _spans(n: int = 2) -> list[EvidenceSpan]:
    return [
        EvidenceSpan(
            source_url=f"https://kafka.apache.org/43/design/{i}",
            source_title="Design", content=f"Evidence {i}.", trust_score=1.0,
        )
        for i in range(n)
    ]


def _good_article(plan: ArticlePlan, span_id: str) -> str:
    body = "\n\n".join(
        f"## {s.title}\n\n" + ("Real prose that runs on for a while. " * 40)
        for s in plan.sections
    )
    return f"# Title\n\n{body}\n\nA closing line with a citation [src:{span_id}]."


# ── The gate ─────────────────────────────────────────────────────────

def test_gate_accepts_a_complete_article():
    plan, spans = _plan(), _spans()
    md = _good_article(plan, str(spans[0].span_id))
    assert single_pass_defects(md, plan, spans, stop_reason="end_turn") == []


@pytest.mark.parametrize("stop_reason,expected", [
    ("max_tokens", "token ceiling"),
    ("end_turn", "finished sentence"),
])
def test_gate_catches_truncation(stop_reason, expected):
    """The failure a section relay structurally cannot have: the article
    stops mid-sentence because one call ran out of output budget."""
    plan, spans = _plan(), _spans()
    md = _good_article(plan, str(spans[0].span_id)).rsplit(".", 1)[0] + " and then"
    defects = single_pass_defects(md, plan, spans, stop_reason=stop_reason)
    assert any(expected in d for d in defects), defects


def test_gate_catches_dropped_sections():
    plan, spans = _plan(6), _spans()
    short = _good_article(_plan(2), str(spans[0].span_id))
    defects = single_pass_defects(short, plan, spans, "end_turn")
    assert any("covered 2 of 6" in d for d in defects), defects


def test_gate_catches_missing_citations_and_unclosed_fences():
    plan, spans = _plan(), _spans()
    md = _good_article(plan, "x").replace("[src:x]", "")
    assert any("no citations" in d for d in single_pass_defects(md, plan, spans, "end_turn"))

    fenced = _good_article(plan, str(spans[0].span_id)) + "\n```python\nprint(1)\n"
    assert any("unclosed code fence" in d
               for d in single_pass_defects(fenced, plan, spans, "end_turn"))


# ── The call, its retry, and its refusal ─────────────────────────────

def _client(*responses):
    calls = []

    class Messages:
        @staticmethod
        async def create(**kwargs):
            calls.append(kwargs)
            text = responses[min(len(calls) - 1, len(responses) - 1)]
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                stop_reason="end_turn",
            )

    return SimpleNamespace(messages=Messages()), calls


def test_one_good_call_needs_no_retry():
    plan, spans = _plan(), _spans()
    client, calls = _client(_good_article(plan, str(spans[0].span_id)))
    article = asyncio.run(generate_whole_article(
        plan, spans, ArticleRequest(topic="t"), client))
    assert len(calls) == 1
    assert article.title == "Title"
    assert "[src:" in article.markdown


def test_a_bad_first_call_retries_leaner_then_succeeds():
    """Truncation is usually an input-size problem, so the retry ships a
    smaller evidence block rather than giving up on the mode."""
    plan, spans = _plan(), _spans(40)
    client, calls = _client("too short", _good_article(plan, str(spans[0].span_id)))
    article = asyncio.run(generate_whole_article(
        plan, spans, ArticleRequest(topic="t"), client))
    assert len(calls) == 2
    first, second = calls[0]["messages"][0]["content"], calls[1]["messages"][0]["content"]
    assert second.count("trust 1.00") < first.count("trust 1.00")
    assert "Title" == article.title


def test_two_bad_calls_raise_so_the_caller_can_fall_back():
    """The whole point of the gate: a rejected single pass must surface,
    not publish. main.py catches this and runs the section relay."""
    plan, spans = _plan(), _spans()
    client, calls = _client("still broken")
    with pytest.raises(SinglePassRejected) as exc:
        asyncio.run(generate_whole_article(
            plan, spans, ArticleRequest(topic="t"), client))
    assert len(calls) == 2
    assert exc.value.defects, "defects must explain why it was rejected"
