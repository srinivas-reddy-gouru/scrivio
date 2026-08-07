"""Job-targeted interview: resume + JD analysis, question generation, and
the post-interview scorecard.

Flow: analyze_job_fit derives the competency rubric FROM the JD (the
scorecard's backbone); research_job_questions gathers what interviewers
actually ask for this role/company; generate_job_interview writes the
structured 30/45-minute screen with every question tagged to a competency;
generate_job_scorecard rolls scores up per competency (deterministic),
adds the panel debrief (one LLM call), and builds a trust-ranked, cited
study plan for every weak competency.
"""
from __future__ import annotations

import logging
import re

from pipeline.model_config import get_model
from pipeline.prompt_loader import load_prompt
from pipeline.schemas.models import (
    CompetencyScore,
    InterviewQuestionSet,
    JobAnalysis,
    JobDebrief,
    JobProfile,
    JobScorecard,
    StudyResource,
)
from pipeline.workers.extraction_worker import score_url
from pipeline.workers.search_worker import multi_search


_ANALYST_PROMPT = load_prompt("job_analyst_v1.txt")
_INTERVIEWER_PROMPT = load_prompt("job_interviewer_v1.txt")
_DEBRIEF_PROMPT = load_prompt("job_debrief_v1.txt")

_ANALYSIS_TOOL: dict = {
    "name": "submit_job_analysis",
    "description": "Submit the interviewer's briefing for this candidate and role.",
    "input_schema": JobAnalysis.model_json_schema(),
}
_QUESTIONS_TOOL: dict = {
    "name": "submit_interview_questions",
    "description": "Submit the structured interview screen.",
    "input_schema": InterviewQuestionSet.model_json_schema(),
}
_DEBRIEF_TOOL: dict = {
    "name": "submit_job_debrief",
    "description": "Submit the hiring panel's debrief and requirement coverage.",
    "input_schema": JobDebrief.model_json_schema(),
}

_COMPETENCY_TAG_RE = re.compile(r"\[competency:\s*(.+?)\]", re.IGNORECASE)

# Question counts per duration (structured-interview practice: expect
# lengthy answers, so 45 min ≈ 13 questions incl. warm-up and closing).
QUESTIONS_FOR_DURATION = {30: 9, 45: 13}

# Study-plan sourcing thresholds.
_STUDY_TRUST_FLOOR = 0.6
_STUDY_LINKS_PER_COMPETENCY = 3
_WEAK_COMPETENCY_BAR = 7.0


def _profile_block(profile: JobProfile) -> str:
    return (
        f"role_title: {profile.role_title}\n"
        f"company: {profile.company or '(not specified)'}\n"
        f"location: {profile.location or '(not specified)'}\n"
        f"seniority: {profile.seniority or '(not specified)'}\n"
        f"extra_notes: {profile.extra_notes or '(none)'}\n\n"
        f"job_description:\n{profile.job_description}\n\n"
        f"resume:\n{profile.resume_text}"
    )


async def analyze_job_fit(
    profile: JobProfile, client, preset: str = "balanced"
) -> JobAnalysis:
    response = await client.messages.create(
        model=get_model("interviewer", preset),
        max_tokens=2048,
        system=_ANALYST_PROMPT,
        tools=[_ANALYSIS_TOOL],
        tool_choice={"type": "tool", "name": "submit_job_analysis"},
        messages=[{"role": "user", "content": _profile_block(profile)}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return JobAnalysis.model_validate(tool_use.input)


async def research_job_questions(profile: JobProfile) -> list[str]:
    """What interviewers actually ask for this role (and company). Same
    degradation contract as topic patterns: no search → empty list."""
    role = profile.role_title
    queries = [f"{role} interview questions"]
    if profile.company:
        queries.append(f"{profile.company} {role} interview questions")
        queries.append(f"{profile.company} {role} interview process")
    if profile.seniority:
        queries.append(f"{profile.seniority} {role} interview questions")
    try:
        results = await multi_search(queries[:4])
    except Exception:
        return []
    patterns: list[str] = []
    for r in results:
        text = " — ".join(p for p in (r.title.strip(), r.snippet.strip()) if p)
        if text:
            patterns.append(text)
        if len(patterns) >= 14:
            break
    return patterns


async def generate_job_interview(
    *,
    profile: JobProfile,
    analysis: JobAnalysis,
    patterns: list[str],
    duration_minutes: int,
    client,
    preset: str = "balanced",
) -> InterviewQuestionSet:
    num_questions = QUESTIONS_FOR_DURATION.get(duration_minutes, 13)
    patterns_block = "\n".join(f"- {p}" for p in patterns) if patterns else "none"
    user_content = (
        f"{_profile_block(profile)}\n\n"
        f"duration_minutes: {duration_minutes}\n"
        f"num_questions: {num_questions}\n\n"
        f"job_analysis:\n{analysis.model_dump_json(indent=1)}\n\n"
        f"real_question_patterns:\n{patterns_block}"
    )
    response = await client.messages.create(
        model=get_model("interviewer", preset),
        max_tokens=8192,
        system=_INTERVIEWER_PROMPT,
        tools=[_QUESTIONS_TOOL],
        tool_choice={"type": "tool", "name": "submit_interview_questions"},
        messages=[{"role": "user", "content": user_content}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    question_set = InterviewQuestionSet.model_validate(tool_use.input)
    questions = question_set.questions[:num_questions]
    for i, q in enumerate(questions):
        q.id = f"q{i + 1}"
    return InterviewQuestionSet(questions=questions)


def competency_for_question(question) -> str | None:
    """The '[competency: X]' tag lives in the rubric so the client never
    sees it mid-session (rubrics are redacted until the question closes)."""
    for point in question.rubric_key_points:
        match = _COMPETENCY_TAG_RE.search(point)
        if match:
            return match.group(1).strip()
    return None


def rollup_competencies(session, analysis: JobAnalysis) -> list[CompetencyScore]:
    """Deterministic scorecard backbone: average each competency's tagged
    question scores; evidence = the evaluator's quoted strengths."""
    buckets: dict[str, dict] = {
        c.name: {"scores": [], "evidence": [], "gaps": []}
        for c in analysis.competencies
    }
    for state in session.questions:
        name = competency_for_question(state.question)
        if name is None:
            continue
        bucket = buckets.setdefault(name, {"scores": [], "evidence": [], "gaps": []})
        if state.status != "completed" or state.final_score is None:
            continue
        evaluation = state.followup.evaluation if state.followup else (
            state.first.evaluation if state.first else None
        )
        bucket["scores"].append(state.final_score)
        if evaluation:
            bucket["evidence"].extend(evaluation.strengths[:2])
            bucket["gaps"].extend(evaluation.gaps[:2])

    scores: list[CompetencyScore] = []
    for name, bucket in buckets.items():
        if bucket["scores"]:
            avg = round(sum(bucket["scores"]) / len(bucket["scores"]), 1)
            band = (
                "strong" if avg >= 8
                else "adequate" if avg >= 6
                else "needs work"
            )
        else:
            avg, band = None, "not assessed"
        scores.append(CompetencyScore(
            name=name, score=avg, band=band,
            evidence=bucket["evidence"][:4], gaps=bucket["gaps"][:4],
        ))
    # Weakest first — that is what the candidate needs to see.
    scores.sort(key=lambda s: (s.score is None, s.score if s.score is not None else 0))
    return scores


async def build_study_plan(
    competency_scores: list[CompetencyScore], role_title: str
) -> list[StudyResource]:
    """Cited prep resources for every weak competency, trust-ranked with
    the same tiers as article sourcing (official docs beat SEO farms).
    Failure-tolerant per competency: a search hiccup drops that entry,
    never the scorecard."""
    plan: list[StudyResource] = []
    for cs in competency_scores:
        if cs.score is not None and cs.score >= _WEAK_COMPETENCY_BAR:
            continue
        try:
            results = await multi_search(
                [f"{cs.name} {role_title} interview preparation guide"]
            )
        except Exception:
            continue
        ranked = sorted(
            (r for r in results if score_url(r.url) >= _STUDY_TRUST_FLOOR),
            key=lambda r: score_url(r.url),
            reverse=True,
        )
        for r in ranked[:_STUDY_LINKS_PER_COMPETENCY]:
            plan.append(StudyResource(
                competency=cs.name,
                title=r.title or r.url,
                url=r.url,
                trust_score=round(score_url(r.url), 2),
            ))
    return plan


async def generate_job_scorecard(
    *,
    session,
    profile: JobProfile,
    analysis: JobAnalysis,
    client,
    preset: str = "balanced",
) -> JobScorecard:
    competency_scores = rollup_competencies(session, analysis)

    # Panel debrief + requirement coverage: the one judgment call.
    results_lines = []
    for i, state in enumerate(session.questions, start=1):
        evaluation = state.followup.evaluation if state.followup else (
            state.first.evaluation if state.first else None
        )
        gaps = "; ".join(evaluation.gaps[:2]) if evaluation else "skipped"
        strength = evaluation.strengths[0] if evaluation and evaluation.strengths else ""
        results_lines.append(
            f"{i}. [{state.question.section_anchor}] "
            f"[{competency_for_question(state.question) or 'untagged'}] "
            f"{state.question.question}\n"
            f"   score: {state.final_score}/10 · gaps: {gaps}\n"
            f"   strongest moment: {strength or 'none'}"
        )
    user_content = (
        f"role_title: {profile.role_title}\n"
        f"seniority: {profile.seniority or '(not specified)'}\n\n"
        f"job_description:\n{profile.job_description[:6000]}\n\n"
        f"results:\n" + "\n".join(results_lines)
    )
    hire_signal, debrief_text, coverage = "", "", []
    try:
        response = await client.messages.create(
            model=get_model("evaluator", preset),
            max_tokens=2048,
            system=_DEBRIEF_PROMPT,
            tools=[_DEBRIEF_TOOL],
            tool_choice={"type": "tool", "name": "submit_job_debrief"},
            messages=[{"role": "user", "content": user_content}],
        )
        tool_use = next(b for b in response.content if b.type == "tool_use")
        debrief = JobDebrief.model_validate(tool_use.input)
        hire_signal, debrief_text = debrief.hire_signal, debrief.debrief
        coverage = debrief.requirement_coverage
    except Exception:
        logging.exception("Job debrief failed; scorecard continues without it")

    study_plan = await build_study_plan(competency_scores, profile.role_title)

    return JobScorecard(
        competency_scores=competency_scores,
        requirement_coverage=coverage,
        hire_signal=hire_signal,
        debrief=debrief_text,
        study_plan=study_plan,
    )
