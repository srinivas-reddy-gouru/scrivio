import asyncio
import logging
from pathlib import Path

from pipeline.schemas.models import (
    ArticlePlan,
    Claim,
    EvidenceSpan,
    VerificationReport,
)
from pipeline.workers.extraction_worker import process_search_result
from pipeline.workers.search_worker import multi_search


_SYSTEM_PROMPT = (
    Path(__file__).resolve().parents[1] / "prompts" / "verifier_v1.txt"
).read_text(encoding="utf-8")


# gpt-4o-mini gives us 200K TPM (vs gpt-4o's 30K) — plenty of headroom for
# parallel claim verification. The task ("is this claim supported by this
# evidence?") is well within mini-tier capability.
_VERIFIER_MODEL = "gpt-4o-mini"

# Cap parallel verifications so a burst can't blow the per-minute token
# limit. With ~6-12 claims this is rarely the bottleneck; the cap matters
# most when an article has 20+ claims after gap-fill.
_VERIFY_CONCURRENCY = 6
_verify_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _verify_semaphore
    if _verify_semaphore is None:
        _verify_semaphore = asyncio.Semaphore(_VERIFY_CONCURRENCY)
    return _verify_semaphore


async def verify_claim(
    claim: Claim,
    spans: list[EvidenceSpan],
    client,
    *,
    user_topic: str = "",
    user_extra_context: str = "",
    article_thesis: str = "",
    article_angle: str = "",
) -> VerificationReport:
    matching_spans = _matching_spans(claim, spans)
    if not matching_spans:
        # No evidence available — can't speak to relevance either way, so
        # leave it as the default "relevant" so the claim isn't double-
        # penalized. The drop_unsupported_claims() pass drops it on
        # support_status alone.
        return VerificationReport(
            claim_id=str(claim.claim_id),
            support_status="unsupported",
            verifier_note="No matching evidence spans were found.",
        )

    evidence_context = _build_evidence_context(matching_spans)

    async with _get_semaphore():
        completion = await _call_with_rate_limit_retry(
            client, claim, evidence_context,
            user_topic=user_topic, user_extra_context=user_extra_context,
            article_thesis=article_thesis, article_angle=article_angle,
        )
    report = completion.choices[0].message.parsed

    if not isinstance(report, VerificationReport):
        report = VerificationReport.model_validate(report)

    # Pin the claim_id to the canonical input value. GPT-4o-mini can
    # hallucinate a different UUID in structured output, which causes a
    # KeyError in the report_by_claim_id lookup inside run_verification_loop.
    report.claim_id = str(claim.claim_id)
    return report


async def _call_with_rate_limit_retry(
    client,
    claim: Claim,
    evidence_context: str,
    *,
    user_topic: str = "",
    user_extra_context: str = "",
    article_thesis: str = "",
    article_angle: str = "",
    max_retries: int = 3,
):
    """Call the verifier with exponential backoff on 429 rate-limit errors.

    OpenAI's 429 response includes a 'Please try again in Xms' hint, but we
    don't parse it — exponential backoff (1s, 2s, 4s) is simpler and almost
    always sufficient given the token-bucket refill rate.

    user_topic, user_extra_context, article_thesis, and article_angle are all
    passed so the verifier has the context to judge relevance from the actual
    article intent — not from hardcoded technology-name rules. The thesis is
    the most important signal: a claim is relevant when it directly supports
    or illustrates the thesis.
    """
    last_exc: Exception = RuntimeError("no attempts made")
    user_content = (
        f"user_topic: {user_topic or '(none)'}\n"
        f"user_extra_context: {user_extra_context or '(none)'}\n"
        f"article_thesis: {article_thesis or '(none)'}\n"
        f"article_angle: {article_angle or '(none)'}\n\n"
        f"Claim:\n{claim.model_dump_json()}\n\n"
        f"Evidence:\n{evidence_context}"
    )
    for attempt in range(max_retries):
        try:
            return await client.beta.chat.completions.parse(
                model=_VERIFIER_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                response_format=VerificationReport,
            )
        except Exception as exc:  # noqa: BLE001 — RateLimitError is upstream class
            if "rate_limit" not in str(exc).lower() and "429" not in str(exc):
                raise
            last_exc = exc
            if attempt < max_retries - 1:
                backoff = 2 ** attempt
                logging.warning(
                    "Verifier rate-limited (attempt %d/%d), retrying in %ds",
                    attempt + 1, max_retries, backoff,
                )
                await asyncio.sleep(backoff)
    raise last_exc


async def verify_all_claims(
    plan: ArticlePlan, spans: list[EvidenceSpan], client
) -> list[VerificationReport]:
    thesis = plan.brief.thesis if plan.brief else ""
    angle = plan.brief.angle if plan.brief else ""
    return list(
        await asyncio.gather(
            *(
                verify_claim(
                    claim, spans, client,
                    user_topic=plan.request.topic,
                    user_extra_context=plan.request.extra_context,
                    article_thesis=thesis,
                    article_angle=angle,
                )
                for claim in plan.claims
            )
        )
    )


async def corrective_search(
    claim: Claim, client_search, client_llm
) -> list[EvidenceSpan]:
    response = await client_llm.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "Rewrite the claim as a concise web search query.",
            },
            {"role": "user", "content": claim.text},
        ],
    )
    query = _extract_chat_response_text(response).strip().strip('"')
    search_results = await multi_search([query])
    span_groups = await asyncio.gather(
        *(process_search_result(result) for result in search_results[:3])
    )

    return [span for group in span_groups for span in group]


async def run_verification_loop(
    plan,
    all_spans,
    openai_client,
    max_retries=2,
) -> tuple:
    current_spans = list(all_spans)
    reports = await verify_all_claims(plan, current_spans, openai_client)

    while True:
        report_by_claim_id = {report.claim_id: report for report in reports}
        retry_claims = [
            claim
            for claim in plan.claims
            if report_by_claim_id[str(claim.claim_id)].support_status
            in {"weak", "unsupported"}
            and claim.corrective_attempts < max_retries
        ]

        if not retry_claims:
            break

        for claim in retry_claims:
            new_spans = await corrective_search(claim, None, openai_client)
            current_spans.extend(new_spans)
            claim.source_ids.extend(str(span.span_id) for span in new_spans)
            claim.corrective_attempts += 1

        reports = await verify_all_claims(plan, current_spans, openai_client)

    final_report_by_claim_id = {report.claim_id: report for report in reports}
    for claim in plan.claims:
        report = final_report_by_claim_id[str(claim.claim_id)]
        claim.support_status = report.support_status
        # Carry the verifier's relevance verdict onto the Claim so the
        # downstream drop pass can use it without needing the reports list.
        claim.relevance_status = report.relevance_status

    return plan, current_spans, reports


def drop_unsupported_claims(plan: ArticlePlan) -> ArticlePlan:
    """Return a plan with unsupported AND off_topic claims (and now-empty
    sections) removed.

    Two reasons to drop a claim:
    - support_status == "unsupported": no evidence backs the claim.
    - relevance_status == "off_topic": the claim is factually fine but
      doesn't belong in an article about the user's topic.

    A factually-supported-but-off-topic claim is still a defect — publishing
    it makes the article feel padded or unfocused. Dropping both kinds with
    the same pruning logic keeps the cleanup symmetric.

    If every claim is dropped, or removing them would leave no sections,
    the original plan is returned unchanged — better to ship a slightly
    drifty article than to ship nothing.
    """
    kept_claims = [
        c
        for c in plan.claims
        if c.support_status != "unsupported"
        and c.relevance_status != "off_topic"
    ]
    if not kept_claims:
        return plan

    kept_ids = {str(c.claim_id) for c in kept_claims}
    new_sections = []
    for section in plan.sections:
        kept = [cid for cid in section.claim_ids if cid in kept_ids]
        if kept:
            new_sections.append(section.model_copy(update={"claim_ids": kept}))

    if not new_sections:
        return plan

    return plan.model_copy(update={"claims": kept_claims, "sections": new_sections})


def _matching_spans(claim: Claim, spans: list[EvidenceSpan]) -> list[EvidenceSpan]:
    source_ids = set(claim.source_ids)
    return [span for span in spans if str(span.span_id) in source_ids]


def _build_evidence_context(spans: list[EvidenceSpan], max_chars: int = 3000) -> str:
    context = "\n\n".join(
        f"[{span.span_id}] ({span.source_url})\n{span.content}" for span in spans
    )
    return context[:max_chars]


def _extract_chat_response_text(response) -> str:
    return response.choices[0].message.content or ""
