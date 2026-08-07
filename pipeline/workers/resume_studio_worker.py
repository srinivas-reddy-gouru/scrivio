"""Resume studio: structured extraction, deterministic ATS checks,
recruiter-lens review, and honest JD tailoring.

Design (from the Reactive Resume / JSON Resume research): the resume is
extracted into a JSON Resume-shaped structure ONCE, and everything else —
checks, tailoring, the change log, Markdown/DOCX/JSON exports — operates
on that structure, never on a text blob. The ATS score is a transparent
weighted checklist computed in Python (every number explainable in the
UI); the only LLM judgments are the recruiter review and the tailored
rewrite, and the rewrite is followed by a deterministic honesty guard
(employers, titles, and dates in the output must be a subset of the
original's — a lying model gets its inventions stripped, not shipped).
"""
from __future__ import annotations

import io
import re
from collections import Counter

from pipeline.model_config import get_model
from pipeline.prompt_loader import load_prompt
from pipeline.schemas.models import (
    AtsCheck,
    AtsReport,
    KeywordCoverage,
    ResumeBasics,
    ResumeChange,
    ResumeEducationItem,
    ResumeProject,
    ResumeReview,
    ResumeSkill,
    ResumeWorkItem,
    StructuredResume,
    TailoredResume,
)

_EXTRACTOR_PROMPT = load_prompt("resume_extractor_v1.txt")
_REVIEWER_PROMPT = load_prompt("resume_reviewer_v1.txt")
_TAILOR_PROMPT = load_prompt("resume_tailor_v1.txt")

_EXTRACTION_TOOL: dict = {
    "name": "submit_resume_extraction",
    "description": "Submit the resume mapped faithfully into the JSON Resume structure.",
    "input_schema": StructuredResume.model_json_schema(),
}
_REVIEW_TOOL: dict = {
    "name": "submit_resume_review",
    "description": "Submit the recruiter's review of this resume.",
    "input_schema": ResumeReview.model_json_schema(),
}
_TAILOR_TOOL: dict = {
    "name": "submit_tailored_resume",
    "description": "Submit the tailored resume structure with its change log and honesty warnings.",
    "input_schema": TailoredResume.model_json_schema(),
}


# ── LLM calls ────────────────────────────────────────────────────────────────

async def extract_resume(text: str, client, preset: str = "balanced") -> StructuredResume:
    """Faithful text → structure mapping. No improvement, no invention."""
    response = await client.messages.create(
        model=get_model("resume_extract", preset),
        max_tokens=8192,
        system=_EXTRACTOR_PROMPT,
        tools=[_EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "submit_resume_extraction"},
        messages=[{"role": "user", "content": f"resume_text:\n{text}"}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return StructuredResume.model_validate(tool_use.input)


async def review_resume(
    text: str, jd_text: str, client, preset: str = "balanced"
) -> ResumeReview:
    user_content = (
        f"resume:\n{text}\n\n"
        f"job_description:\n{jd_text or '(none provided — review the resume on its own merits; leave missing_keywords empty)'}"
    )
    response = await client.messages.create(
        model=get_model("resume_review", preset),
        max_tokens=3072,
        system=_REVIEWER_PROMPT,
        tools=[_REVIEW_TOOL],
        tool_choice={"type": "tool", "name": "submit_resume_review"},
        messages=[{"role": "user", "content": user_content}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return ResumeReview.model_validate(tool_use.input)


async def tailor_resume(
    *,
    structured: StructuredResume,
    jd_text: str,
    review: ResumeReview | None,
    report: AtsReport | None,
    client,
    preset: str = "balanced",
) -> TailoredResume:
    """JD-targeted rewrite of the STRUCTURE. The prompt carries the honesty
    guardrails; enforce_honesty() is the deterministic belt behind them."""
    review_block = review.model_dump_json(indent=1) if review else "(none)"
    failed = [c for c in (report.checks if report else []) if not c.passed]
    checks_block = (
        "\n".join(f"- {c.label}: {c.detail}" for c in failed) if failed else "(all passed)"
    )
    coverage_block = "(no keyword data)"
    if report and report.keyword_coverage:
        coverage_block = "missing keywords: " + (
            ", ".join(report.keyword_coverage.missing) or "(none)"
        )
    user_content = (
        f"current_resume_structure:\n{structured.model_dump_json(indent=1)}\n\n"
        f"job_description:\n{jd_text}\n\n"
        f"reviewer_findings:\n{review_block}\n\n"
        f"failed_ats_checks:\n{checks_block}\n\n"
        f"keyword_coverage:\n{coverage_block}"
    )
    response = await client.messages.create(
        model=get_model("resume_tailor", preset),
        max_tokens=8192,
        system=_TAILOR_PROMPT,
        tools=[_TAILOR_TOOL],
        tool_choice={"type": "tool", "name": "submit_tailored_resume"},
        messages=[{"role": "user", "content": user_content}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    tailored = TailoredResume.model_validate(tool_use.input)
    return enforce_honesty(structured, tailored)


# ── Honesty post-guard ──────────────────────────────────────────────────────

def enforce_honesty(
    original: StructuredResume, tailored: TailoredResume
) -> TailoredResume:
    """Deterministic backstop: facts the model may not touch — employers,
    titles, employment dates, schools, degrees, certificates — must be a
    subset of the original's. Inventions are stripped (or reverted) and
    surfaced as warnings, never silently shipped."""
    warnings = list(tailored.warnings)

    orig_by_employer = {w.name: w for w in original.work if w.name}
    kept_work: list[ResumeWorkItem] = []
    for item in tailored.resume.work:
        source = orig_by_employer.get(item.name)
        if source is None:
            warnings.append(
                f"Removed work entry '{item.name or item.position}' — that employer "
                "is not on the original resume."
            )
            continue
        if item.position != source.position:
            warnings.append(
                f"Reverted job title at {item.name} to '{source.position}' — titles "
                "cannot be changed."
            )
            item.position = source.position
        if (item.startDate, item.endDate) != (source.startDate, source.endDate):
            warnings.append(
                f"Reverted employment dates at {item.name} — dates cannot be changed."
            )
            item.startDate, item.endDate = source.startDate, source.endDate
        kept_work.append(item)
    tailored.resume.work = kept_work

    orig_schools = {e.institution for e in original.education if e.institution}
    kept_edu = []
    for edu in tailored.resume.education:
        if edu.institution and edu.institution not in orig_schools:
            warnings.append(
                f"Removed education entry '{edu.institution}' — that institution "
                "is not on the original resume."
            )
            continue
        kept_edu.append(edu)
    tailored.resume.education = kept_edu

    orig_certs = set(original.certificates)
    invented_certs = [c for c in tailored.resume.certificates if c not in orig_certs]
    if invented_certs:
        tailored.resume.certificates = [
            c for c in tailored.resume.certificates if c in orig_certs
        ]
        warnings.append(
            "Removed certificates not on the original resume: "
            + ", ".join(invented_certs)
        )

    tailored.warnings = warnings
    return tailored


# ── JSON Resume interop ─────────────────────────────────────────────────────

def to_jsonresume(s: StructuredResume) -> dict:
    """Export in the JSON Resume open-standard shape (jsonresume.org) —
    importable by Reactive Resume and the wider ecosystem."""
    data = s.model_dump()
    loc = data["basics"].pop("location", "")
    data["basics"]["location"] = {"address": loc} if loc else {}
    data["certificates"] = [{"name": c} for c in data["certificates"]]
    data["$schema"] = (
        "https://raw.githubusercontent.com/jsonresume/resume-schema/master/schema.json"
    )
    return data


def from_jsonresume(data: dict) -> StructuredResume:
    """Import a JSON Resume document, tolerating the standard's optional
    fields and object-shaped location. Unknown sections are ignored."""
    if not isinstance(data, dict):
        raise ValueError("JSON Resume import expects a top-level object.")
    basics_in = data.get("basics") or {}
    loc = basics_in.get("location") or {}
    if isinstance(loc, dict):
        loc_str = ", ".join(
            str(v) for v in (loc.get("city"), loc.get("region"), loc.get("countryCode"))
            if v
        ) or str(loc.get("address") or "")
    else:
        loc_str = str(loc)
    basics = ResumeBasics.model_validate(
        {**{k: v for k, v in basics_in.items() if isinstance(v, str)}, "location": loc_str}
    )
    certs = []
    for c in data.get("certificates") or []:
        if isinstance(c, dict) and c.get("name"):
            certs.append(str(c["name"]))
        elif isinstance(c, str) and c:
            certs.append(c)
    return StructuredResume(
        basics=basics,
        work=[ResumeWorkItem.model_validate(w) for w in data.get("work") or []],
        education=[
            ResumeEducationItem.model_validate(e) for e in data.get("education") or []
        ],
        skills=[ResumeSkill.model_validate(k) for k in data.get("skills") or []],
        projects=[ResumeProject.model_validate(p) for p in data.get("projects") or []],
        certificates=certs,
    )


# ── Rendering (pure functions, no LLM) ──────────────────────────────────────

def _date_range(start: str, end: str) -> str:
    if start and end:
        return f"{start} – {end}"
    return start or end or ""


def render_markdown(s: StructuredResume) -> str:
    """Single-column, standard-headers markdown — the ATS-safe layout."""
    b = s.basics
    lines: list[str] = [f"# {b.name}".rstrip()]
    if b.label:
        lines.append(f"**{b.label}**")
    contact = " · ".join(p for p in (b.email, b.phone, b.location, b.url) if p)
    if contact:
        lines.append(contact)
    if b.summary:
        lines += ["", "## Summary", "", b.summary]
    if s.work:
        lines += ["", "## Experience"]
        for w in s.work:
            heading = " — ".join(p for p in (w.position, w.name) if p)
            lines += ["", f"### {heading}" if heading else "### Role"]
            dates = _date_range(w.startDate, w.endDate)
            if dates:
                lines.append(dates)
            if w.summary:
                lines.append(w.summary)
            lines += [f"- {h}" for h in w.highlights]
    if s.projects:
        lines += ["", "## Projects"]
        for p in s.projects:
            lines += ["", f"### {p.name}" if p.name else "### Project"]
            if p.description:
                lines.append(p.description)
            if p.url:
                lines.append(p.url)
            lines += [f"- {h}" for h in p.highlights]
    if s.education:
        lines += ["", "## Education"]
        for e in s.education:
            degree = " in ".join(p for p in (e.studyType, e.area) if p)
            heading = " — ".join(p for p in (degree, e.institution) if p)
            lines += ["", f"### {heading}" if heading else "### Education"]
            tail = " · ".join(
                p for p in (_date_range(e.startDate, e.endDate), e.score) if p
            )
            if tail:
                lines.append(tail)
    if s.skills:
        lines += ["", "## Skills", ""]
        for sk in s.skills:
            kw = ", ".join(sk.keywords)
            lines.append(f"- **{sk.name}:** {kw}" if sk.name else f"- {kw}")
    if s.certificates:
        lines += ["", "## Certifications", ""]
        lines += [f"- {c}" for c in s.certificates]
    return "\n".join(lines).strip() + "\n"


def render_docx(s: StructuredResume) -> bytes:
    """Clean DOCX from structure: real Heading styles and bullet lists —
    field mapping beats markdown-to-docx heuristics."""
    from docx import Document

    doc = Document()
    b = s.basics
    doc.add_heading(b.name or "Resume", level=0)
    if b.label:
        doc.add_paragraph(b.label)
    contact = " · ".join(p for p in (b.email, b.phone, b.location, b.url) if p)
    if contact:
        doc.add_paragraph(contact)
    if b.summary:
        doc.add_heading("Summary", level=1)
        doc.add_paragraph(b.summary)
    if s.work:
        doc.add_heading("Experience", level=1)
        for w in s.work:
            heading = " — ".join(p for p in (w.position, w.name) if p)
            doc.add_heading(heading or "Role", level=2)
            dates = _date_range(w.startDate, w.endDate)
            if dates:
                doc.add_paragraph(dates)
            if w.summary:
                doc.add_paragraph(w.summary)
            for h in w.highlights:
                doc.add_paragraph(h, style="List Bullet")
    if s.projects:
        doc.add_heading("Projects", level=1)
        for p in s.projects:
            doc.add_heading(p.name or "Project", level=2)
            if p.description:
                doc.add_paragraph(p.description)
            for h in p.highlights:
                doc.add_paragraph(h, style="List Bullet")
    if s.education:
        doc.add_heading("Education", level=1)
        for e in s.education:
            degree = " in ".join(p for p in (e.studyType, e.area) if p)
            heading = " — ".join(p for p in (degree, e.institution) if p)
            doc.add_heading(heading or "Education", level=2)
            tail = " · ".join(
                p for p in (_date_range(e.startDate, e.endDate), e.score) if p
            )
            if tail:
                doc.add_paragraph(tail)
    if s.skills:
        doc.add_heading("Skills", level=1)
        for sk in s.skills:
            kw = ", ".join(sk.keywords)
            doc.add_paragraph(f"{sk.name}: {kw}" if sk.name else kw, style="List Bullet")
    if s.certificates:
        doc.add_heading("Certifications", level=1)
        for c in s.certificates:
            doc.add_paragraph(c, style="List Bullet")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _pdf_safe(text: str) -> str:
    """fpdf2's built-in core fonts are latin-1; swap the typographic
    characters resumes actually contain for ASCII equivalents rather than
    embedding a font (keeps the dependency footprint tiny)."""
    replacements = {
        "–": "-", "—": "-", "•": "-", "·": "|",
        "‘": "'", "’": "'", "“": '"', "”": '"',
        "…": "...", " ": " ", "→": "->",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def render_pdf(s: StructuredResume) -> bytes:
    """Single-column, ATS-safe PDF — the format most application portals
    demand. Same field mapping as the DOCX render, core fonts only."""
    from fpdf import FPDF

    pdf = FPDF(format="letter")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 16, 18)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def line(text, size=10, style="", color=(20, 20, 20), spacing=1.45, before=0):
        if before:
            pdf.ln(before)
        pdf.set_font("Helvetica", style, size)
        pdf.set_text_color(*color)
        # new_x=LMARGIN: fpdf2's default parks the cursor at the cell's
        # right edge, which truncates every following line to zero width.
        pdf.multi_cell(
            width, size * 0.353 * spacing, _pdf_safe(text),
            new_x="LMARGIN", new_y="NEXT",
        )

    def section(title):
        pdf.ln(3)
        line(title.upper(), size=11, style="B", color=(30, 64, 60))
        y = pdf.get_y() + 0.5
        pdf.set_draw_color(180, 190, 188)
        pdf.line(pdf.l_margin, y, pdf.l_margin + width, y)
        pdf.ln(2)

    b = s.basics
    line(b.name or "Resume", size=19, style="B")
    if b.label:
        line(b.label, size=11, color=(70, 80, 78))
    contact = "  |  ".join(p for p in (b.email, b.phone, b.location, b.url) if p)
    if contact:
        line(contact, size=9, color=(90, 100, 98))
    if b.summary:
        section("Summary")
        line(b.summary)
    if s.work:
        section("Experience")
        for w in s.work:
            heading = " - ".join(p for p in (w.position, w.name) if p)
            line(heading or "Role", style="B", size=10.5, before=1.5)
            dates = _date_range(w.startDate, w.endDate)
            if dates:
                line(dates, size=9, color=(90, 100, 98))
            if w.summary:
                line(w.summary, size=9.5)
            for h in w.highlights:
                line(f"-  {h}", size=9.5)
    if s.projects:
        section("Projects")
        for p in s.projects:
            line(p.name or "Project", style="B", size=10.5, before=1.5)
            if p.description:
                line(p.description, size=9.5)
            if p.url:
                line(p.url, size=9, color=(90, 100, 98))
            for h in p.highlights:
                line(f"-  {h}", size=9.5)
    if s.education:
        section("Education")
        for e in s.education:
            degree = " in ".join(p for p in (e.studyType, e.area) if p)
            heading = " - ".join(p for p in (degree, e.institution) if p)
            line(heading or "Education", style="B", size=10.5, before=1.5)
            tail = "  |  ".join(
                p for p in (_date_range(e.startDate, e.endDate), e.score) if p
            )
            if tail:
                line(tail, size=9, color=(90, 100, 98))
    if s.skills:
        section("Skills")
        for sk in s.skills:
            kw = ", ".join(sk.keywords)
            line(f"{sk.name}: {kw}" if sk.name else kw, size=9.5)
    if s.certificates:
        section("Certifications")
        for c in s.certificates:
            line(f"-  {c}", size=9.5)
    return bytes(pdf.output())


# ── Deterministic ATS checks ────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?:\+?\d[\d ()\-.]{7,}\d)")
_BULLET_RE = re.compile(r"^\s*[-•*–▪●◦]\s+")
_HEADER_WORDS = (
    "experience", "employment", "work history",
    "education",
    "skills",
    "summary", "profile", "objective",
    "projects",
    "certifications", "certificates", "licenses",
)
_MONTH_DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}",
    re.IGNORECASE,
)
_NUMERIC_DATE_RE = re.compile(r"\b\d{1,2}/\d{4}\b")
_YEAR_RANGE_RE = re.compile(r"\b(?:19|20)\d{2}\s*[-–—]", re.IGNORECASE)
# "I" must stay case-sensitive (lowercase "i" would catch list markers),
# but sentence-start "My"/"Me" count.
_FIRST_PERSON_RE = re.compile(r"\b(?:I|[Mm]e|[Mm]y)\b")
_QUANT_RE = re.compile(r"\d|%|\$|€|£")
_MULTISPACE_RE = re.compile(r"\S\s{3,}\S")

_FILLER_PHRASES = (
    "synergy", "synergies", "go-getter", "go getter", "results-driven",
    "results driven", "think outside the box", "hard worker", "team player",
    "self-starter", "detail-oriented", "guru", "ninja", "rockstar",
)

_STOPWORDS = frozenset("""
a an and are as at be been but by for from has have he her his i if in into is
it its me my nor not of on or our out she so that the their them then there
these they this to was we were what when where which while who will with you
your years experience work working strong ability able etc using use team
role responsibilities requirements qualifications preferred must plus new
including knowledge skills skill similar related relevant s t re ll d
""".split())

# Tokens too generic to flag as "stuffed" even at high counts.
_STUFFING_EXEMPT = frozenset((
    "experience", "engineering", "software", "development", "team", "data",
    "product", "design", "management", "project", "systems", "services",
))


def _score(checks: list[AtsCheck], coverage: KeywordCoverage | None) -> int:
    total = sum(c.weight for c in checks) or 1
    passed = sum(c.weight for c in checks if c.passed)
    base = 100 * passed / total
    if coverage is not None:
        return round(0.7 * base + 0.3 * coverage.percent)
    return round(base)


def run_ats_checks(
    text: str,
    jd_text: str | None = None,
    structured: StructuredResume | None = None,
) -> AtsReport:
    """Weighted, explainable checklist — pure Python, unit-testable.
    Score = weighted passed checks normalized to 100; with a JD it becomes
    70% checks + 30% keyword coverage."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    nonempty = [ln for ln in lines if ln.strip()]
    words = text.split()
    checks: list[AtsCheck] = []

    def add(id_: str, label: str, passed: bool, weight: int, detail: str) -> None:
        checks.append(
            AtsCheck(id=id_, label=label, passed=passed, weight=weight, detail=detail)
        )

    # contact-info — email + phone near the top (ATS field parsing reads there).
    head = "\n".join(nonempty[:15])
    has_email = bool(_EMAIL_RE.search(head)) or (
        structured is not None and bool(structured.basics.email)
    )
    has_phone = bool(_PHONE_RE.search(head)) or (
        structured is not None and bool(structured.basics.phone)
    )
    add(
        "contact-info", "Contact info up top", has_email and has_phone, 15,
        "Email and phone found near the top."
        if has_email and has_phone
        else "Missing "
        + " and ".join(p for p, ok in (("an email", has_email), ("a phone number", has_phone)) if not ok)
        + " in the top lines — ATS field parsers read contact details there.",
    )

    # section-headers — ≥3 standard headers at line starts.
    found_headers = set()
    for ln in nonempty:
        stripped = ln.strip().strip("#*").strip().lower().rstrip(":")
        for h in _HEADER_WORDS:
            if stripped == h or stripped.startswith(h + " "):
                found_headers.add(h.split()[0])
    add(
        "section-headers", "Standard section headers", len(found_headers) >= 3, 15,
        f"Recognized sections: {', '.join(sorted(found_headers)) or 'none'}. "
        + ("Good — ATS segmentation keys off these."
           if len(found_headers) >= 3
           else "Fewer than 3 standard headers (Experience, Education, Skills, Summary, Projects, Certifications) — ATS segmentation may misfile content."),
    )

    # length — 350-1100 words.
    wc = len(words)
    add(
        "length", "Length in range", 350 <= wc <= 1100, 10,
        f"{wc} words. "
        + ("Within the 350–1100 sweet spot."
           if 350 <= wc <= 1100
           else "Under 350 reads thin — add substance." if wc < 350
           else "Over 1100 words (2+ pages) — condense to the most relevant material."),
    )

    # bullets — enough bullet lines, no wall-of-text paragraphs.
    bullet_lines = [ln for ln in nonempty if _BULLET_RE.match(ln)]
    long_paras = [ln for ln in nonempty if not _BULLET_RE.match(ln) and len(ln.split()) > 60]
    bullets_ok = len(bullet_lines) >= 5 and not long_paras
    add(
        "bullets", "Bullet-driven experience", bullets_ok, 10,
        f"{len(bullet_lines)} bullet lines, {len(long_paras)} paragraph(s) over 60 words. "
        + ("Scannable." if bullets_ok
           else "Use bullets for achievements and keep paragraphs short — recruiters scan, they don't read."),
    )

    # quantification — ≥30% of bullets carry a number/%/currency.
    if bullet_lines:
        quantified = [ln for ln in bullet_lines if _QUANT_RE.search(ln)]
        ratio = len(quantified) / len(bullet_lines)
        quant_ok = ratio >= 0.3
        quant_detail = (
            f"{len(quantified)}/{len(bullet_lines)} bullets are quantified "
            f"({round(ratio * 100)}%). "
            + ("Numbers are the strongest impact signal — good."
               if quant_ok
               else "Add numbers (%, $, counts, latency, users) to at least a third of your bullets.")
        )
    else:
        quant_ok, quant_detail = False, "No bullet points found to quantify."
    add("quantification", "Quantified achievements", quant_ok, 15, quant_detail)

    # dates — one consistent format family.
    month_hits = _MONTH_DATE_RE.findall(text)
    stripped_text = _MONTH_DATE_RE.sub("", text)
    numeric_hits = _NUMERIC_DATE_RE.findall(stripped_text)
    year_hits = _YEAR_RANGE_RE.findall(_NUMERIC_DATE_RE.sub("", stripped_text))
    families = [n for n in (len(month_hits), len(numeric_hits), len(year_hits)) if n >= 2]
    any_dates = bool(month_hits or numeric_hits or year_hits)
    dates_ok = any_dates and len(families) <= 1
    add(
        "dates", "Consistent date format", dates_ok, 10,
        "One consistent date style." if dates_ok
        else ("No dates found — ATS ranking uses employment dates." if not any_dates
              else "Mixed date styles (e.g. 'Jan 2024' and '01/2024') confuse ATS date parsing — pick one."),
    )

    # first-person — resumes are written in implied first person, no I/me/my.
    fp = len(_FIRST_PERSON_RE.findall(text))
    add(
        "first-person", "No first-person pronouns", fp < 5, 5,
        "Implied first person throughout." if fp < 5
        else f"{fp} uses of I/me/my — drop the pronouns ('Led migration…', not 'I led…').",
    )

    # parse-risk — column/table artifacts from extraction.
    pipe_lines = sum(1 for ln in nonempty if ln.count("|") >= 2)
    gap_lines = sum(1 for ln in nonempty if _MULTISPACE_RE.search(ln))
    parse_ok = pipe_lines < 3 and gap_lines < 6
    add(
        "parse-risk", "Single-column, parse-safe layout", parse_ok, 10,
        "No column or table artifacts detected." if parse_ok
        else "Extraction shows column/table artifacts (pipes or wide gaps mid-line) — multi-column layouts scramble ATS reading order; use a single column.",
    )

    # buzzword-stuffing — filler phrases or one keyword repeated >6×.
    lower = text.lower()
    fillers = [p for p in _FILLER_PHRASES if p in lower]
    tokens = [
        t for t in re.findall(r"[a-z][a-z+#.-]{3,}", lower)
        if t not in _STOPWORDS and t not in _STUFFING_EXEMPT
    ]
    common = Counter(tokens).most_common(1)
    stuffed = common[0] if common and common[0][1] > 6 else None
    stuffing_ok = not fillers and stuffed is None
    add(
        "buzzword-stuffing", "No stuffing or filler", stuffing_ok, 10,
        "No filler buzzwords or keyword stuffing." if stuffing_ok
        else "Found: "
        + "; ".join(
            filter(None, [
                ("filler phrases (" + ", ".join(fillers) + ")") if fillers else "",
                (f"'{stuffed[0]}' repeated {stuffed[1]}×") if stuffed else "",
            ])
        )
        + ". Modern ATS treats stuffing as a fraud signal.",
    )

    # Structure-aware checks — only when we have the structured resume.
    if structured is not None:
        undated = [w.name or w.position for w in structured.work if not w.startDate]
        add(
            "work-dates", "Every job has dates", not structured.work or not undated, 8,
            "All work entries carry start dates." if not undated
            else f"Missing start dates on: {', '.join(filter(None, undated)) or 'some entries'} — undated experience gets discounted by ATS ranking.",
        )
        has_skills = any(sk.keywords or sk.name for sk in structured.skills)
        add(
            "skills-section", "Dedicated skills section", has_skills, 8,
            "Skills section present — the easiest keyword-match surface."
            if has_skills
            else "No skills section found — ATS keyword matching leans on it heavily.",
        )

    coverage = compute_keyword_coverage(text, jd_text) if jd_text else None
    return AtsReport(score=_score(checks, coverage), checks=checks, keyword_coverage=coverage)


# ── JD keyword extraction (deterministic) ───────────────────────────────────

_REQ_HEADER_RE = re.compile(
    r"^\s*(?:requirements?|qualifications?|must[- ]haves?|what you.ll need|"
    r"what we.re looking for|about you|skills?)\b",
    re.IGNORECASE,
)


def extract_jd_keywords(jd_text: str, limit: int = 18) -> list[str]:
    """Frequency-weighted unigrams + bigrams from the JD, minus stopwords,
    with lines under a Requirements-style header counted double."""
    weights: Counter[str] = Counter()
    in_requirements = False
    for line in jd_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _REQ_HEADER_RE.match(stripped):
            in_requirements = True
        elif stripped.endswith(":") or (len(stripped) < 60 and stripped.isupper()):
            in_requirements = _REQ_HEADER_RE.match(stripped) is not None
        boost = 2 if in_requirements else 1
        tokens = [
            t for t in (
                # Keep interior punctuation (node.js, ci/cd) and trailing +/#
                # (c++, c#), but strip sentence punctuation off the end.
                raw.rstrip("./-")
                for raw in re.findall(r"[a-z0-9][a-z0-9+#./-]{1,}", stripped.lower())
            )
            if t and t not in _STOPWORDS and not t.isdigit() and len(t) >= 2
        ]
        for t in tokens:
            if len(t) >= 3 or t in ("go", "c#", "r"):
                weights[t] += boost
        for a, b in zip(tokens, tokens[1:]):
            if len(a) >= 3 and len(b) >= 3:
                weights[f"{a} {b}"] += boost
    # Prefer bigrams over their constituent unigrams when both rank.
    ranked = [t for t, _ in weights.most_common(limit * 3)]
    picked: list[str] = []
    for term in ranked:
        if len(picked) >= limit:
            break
        if " " not in term and any(term in bg.split() for bg in picked):
            continue
        picked.append(term)
    return picked


def compute_keyword_coverage(resume_text: str, jd_text: str) -> KeywordCoverage:
    keywords = extract_jd_keywords(jd_text)
    if not keywords:
        return KeywordCoverage(found=[], missing=[], percent=100)
    haystack = re.sub(r"\s+", " ", resume_text.lower())
    found = [k for k in keywords if k in haystack]
    missing = [k for k in keywords if k not in haystack]
    return KeywordCoverage(
        found=found,
        missing=missing,
        percent=round(100 * len(found) / len(keywords)),
    )
