from pathlib import Path

import openai

from pipeline.schemas.models import ArticleRequest, ClarificationState


_SYSTEM_PROMPT = (
    Path(__file__).resolve().parents[1] / "prompts" / "clarification_v1.txt"
).read_text(encoding="utf-8")


async def run_clarification(
    prompt: str, client: openai.AsyncOpenAI
) -> ClarificationState:
    completion = await client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format=ClarificationState,
    )
    state = completion.choices[0].message.parsed

    if not isinstance(state, ClarificationState):
        state = ClarificationState.model_validate(state)

    if state.is_complete and state.filled_request is not None:
        state.filled_request = ArticleRequest.model_validate(state.filled_request)

    return state


async def interactive_clarify(prompt: str, client) -> ArticleRequest:
    original_prompt = prompt
    current_prompt = prompt
    latest_state: ClarificationState | None = None

    for _ in range(3):
        latest_state = await run_clarification(current_prompt, client)

        if latest_state.is_complete and latest_state.filled_request is not None:
            return latest_state.filled_request

        if latest_state.questions_asked:
            question = latest_state.questions_asked[0]
            print(question)
            answer = input()
            current_prompt = f"{current_prompt}\n\nClarification question: {question}\nAnswer: {answer}"

    if latest_state is not None and latest_state.filled_request is not None:
        return ArticleRequest.model_validate(latest_state.filled_request)

    return ArticleRequest(topic=original_prompt.strip() or "Untitled article")
