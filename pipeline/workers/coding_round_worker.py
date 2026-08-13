"""The coding round: one problem, four phases, a sealed bar.

Design premise, from how these interviews actually fail. Candidates rarely
lose because they cannot solve the problem; they lose because their
thinking stops being legible under pressure, and because AI assistance has
made "produced correct code" a weak signal that companies now discount in
favour of understanding and communication.

So this round does not grade a submission. It grades an interview:

  clarify  what do you need to know before you write anything
  approach what are you going to do, and what will it cost
  code     write it
  defend   the interviewer probes the thing you were weakest on

Each phase carries a rubric written before the candidate starts, the same
contract the spoken rounds use. The code phase adds deterministic checks
computed by PARSING, never executing: an interview tool must not run a
stranger's code, and the findings that matter here (does it parse, does it
define what was asked, is it a stub) need no execution. Those findings go
to the grader as facts, so a confident explanation cannot talk the model
out of a syntax error.
"""
from __future__ import annotations

import ast
import logging
import re

from pipeline.model_config import get_model
from pipeline.workers.citation_utils import scrub_dashes_in_model
from pipeline.prompt_loader import load_prompt
from pipeline.schemas.models import CodeCheck, CodingRound

_CODING_PROMPT = load_prompt("coding_interviewer_v1.txt")

_CODING_TOOL: dict = {
    "name": "submit_coding_round",
    "description": "Submit the coding problem and the sealed rubric for each phase.",
    "input_schema": CodingRound.model_json_schema(),
}

PHASE_ORDER = ("clarify", "approach", "code", "defend")

_STUB_MARKERS = ("pass", "...", "todo", "notimplementederror", "your code here")


def _function_names(tree: ast.AST) -> set[str]:
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _required_name(signature: str) -> str:
    match = re.search(r"def\s+(\w+)", signature or "")
    return match.group(1) if match else ""


def _max_loop_depth(tree: ast.AST) -> int:
    """Deepest nesting of for/while. Reported as a fact, never as a verdict:
    a nested loop is correct for plenty of problems, and the grader is the
    one that knows whether this problem's optimal solution allows it."""
    def depth(node: ast.AST, current: int = 0) -> int:
        best = current
        for child in ast.iter_child_nodes(node):
            nxt = current + 1 if isinstance(child, (ast.For, ast.While, ast.AsyncFor)) else current
            best = max(best, depth(child, nxt))
        return best
    return depth(tree)


def static_code_checks(code: str, signature: str, language: str = "python") -> list[CodeCheck]:
    """Facts about the submission, obtained without running it.

    Only Python is parsed today; other languages get the checks that are
    honest without a parser rather than guesses dressed as findings."""
    text = (code or "").strip()
    checks: list[CodeCheck] = []

    if not text:
        return [CodeCheck(name="submitted", passed=False, detail="No code was submitted.")]

    stripped = text.lower()
    body = re.sub(r"^\s*def .*?:", "", stripped, flags=re.S)
    is_stub = all(
        marker in stripped for marker in ("def",)
    ) and any(body.strip().startswith(m) for m in _STUB_MARKERS)

    if language.lower() != "python":
        checks.append(CodeCheck(
            name="parses", passed=True,
            detail=f"{language} is not parsed here; correctness is judged by reading.",
        ))
    else:
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            return [CodeCheck(
                name="parses", passed=False,
                detail=f"Does not parse: {exc.msg} on line {exc.lineno}.",
            )]
        checks.append(CodeCheck(
            name="parses", passed=True, detail="Valid Python syntax."))

        required = _required_name(signature)
        defined = _function_names(tree)
        if required:
            checks.append(CodeCheck(
                name="signature", passed=required in defined,
                detail=(f"Defines {required}()." if required in defined
                        else f"Does not define {required}(); found {sorted(defined) or 'no functions'}."),
            ))
        has_return = any(isinstance(n, ast.Return) and n.value is not None
                         for n in ast.walk(tree))
        checks.append(CodeCheck(
            name="returns", passed=has_return,
            detail="Returns a value." if has_return else "No return statement with a value.",
        ))
        depth = _max_loop_depth(tree)
        checks.append(CodeCheck(
            name="loop_depth", passed=True,
            detail=f"Deepest loop nesting: {depth}." if depth else "No loops.",
        ))

    if is_stub:
        checks.append(CodeCheck(
            name="stub", passed=False,
            detail="The function body is a placeholder rather than an implementation.",
        ))
    return checks


def checks_block(checks: list[CodeCheck]) -> str:
    """Render checks for the grader's prompt."""
    if not checks:
        return "(no static checks ran)"
    return "\n".join(
        f"- {c.name}: {'pass' if c.passed else 'FAIL'} — {c.detail}" for c in checks
    )


async def generate_coding_round(
    *, topic: str, level: str, language: str, client, preset: str = "balanced",
    job_context: str = "",
) -> CodingRound:
    """Write the problem and the sealed per-phase rubric in one call, before
    the candidate has typed anything."""
    context = f"\n\njob_context (target the problem at this role):\n{job_context[:1500]}" if job_context else ""
    user_content = (
        f"topic: {topic}\n"
        f"level: {level}\n"
        f"language: {language}\n"
        f"phases (in this order): {', '.join(PHASE_ORDER)}{context}"
    )
    response = await client.messages.create(
        model=get_model("interviewer", preset),
        max_tokens=4096,
        system=_CODING_PROMPT,
        tools=[_CODING_TOOL],
        tool_choice={"type": "tool", "name": "submit_coding_round"},
        messages=[{"role": "user", "content": user_content}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    round_ = scrub_dashes_in_model(CodingRound.model_validate(tool_use.input))
    logging.info(
        "Coding round: %s, %d phases, %d unstated constraints sealed",
        round_.problem.title, len(round_.phases), len(round_.problem.unstated_constraints),
    )
    return round_
