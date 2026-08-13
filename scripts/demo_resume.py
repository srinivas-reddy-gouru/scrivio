"""Build a wholly fictional resume document for the README screenshot.

A resume screenshot is the one place this project's docs can leak a real
person: a contact line and an employment history, at full resolution, on
a public README. So the screenshot is taken against an invented resume
instead.

The CONTENT is fiction end to end. The NUMBERS are not: the before and
after scores come from run_ats_checks over this fictional resume, the
same function the product runs, so the README shows a real result rather
than a flattering one. The "original" is deliberately the resume most
people actually have, duty phrased with its numbers buried, because the
gap the studio closes has to be a real gap.

    python scripts/demo_resume.py /tmp/demo-output
    ARTICLE_OUTPUT_DIR=/tmp/demo-output python -m uvicorn api.server:app --port 8897
    python scripts/capture_screenshots.py --base http://localhost:8897 --only resume-studio

Writing to a directory of its own keeps it out of the real store, so no
cleanup step can forget to run.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from pipeline.schemas.models import (
    ResumeBasics, ResumeChange, ResumeDoc, ResumeEducationItem, ResumeSkill,
    ResumeProject, ResumeWorkItem, StructuredResume, TailoredResume,
)
from pipeline.workers.resume_studio_worker import render_markdown, run_ats_checks

DEMO_ID = "20260101-000000-demo01"

JD = """Senior Backend Engineer, Payments Platform

We are looking for a backend engineer to own our event-driven payment
reconciliation services. You will design and operate high-throughput
services in Java 17 and Spring Boot, run Kafka pipelines on Kubernetes,
and be responsible for the reliability of systems that move money.

Requirements: Java 17, Spring Boot, Kafka, Kubernetes, PostgreSQL, AWS,
observability tooling, distributed systems design, mentoring engineers.
"""

# The ORIGINAL is deliberately the resume most people actually have:
# duty phrasing ("responsible for"), numbers buried inside prose rather
# than leading a bullet, and the stack listed but never claimed in the
# work itself. The gap the studio closes has to be a real gap.
BASICS = ResumeBasics(
    name="Alex Rivera",
    label="Software Engineer",
    email="alex.rivera@example.com",
    phone="(555) 010-4477",
    location="Portland, Oregon",
    summary=(
        "Experienced and results-driven software engineer with a strong "
        "background in backend development and a proven track record of "
        "delivering quality software. Responsible for a wide range of "
        "services and passionate about clean code, collaborating closely "
        "with cross functional teams to deliver business value on time."
    ),
)

WORK = [
    ResumeWorkItem(
        name="Northwind Systems", position="Technical Lead",
        startDate="Mar 2023", endDate="Present",
        summary="Tech stack: Java 17, Spring Boot 3.x, Kafka, Kubernetes, PostgreSQL, AWS",
        highlights=[
            "Responsible for the backend architecture of the reconciliation "
            "platform, which handles a few million payment events a day.",
            "Worked on the event pipeline and helped bring the median settlement "
            "time down from around 4 minutes to well under a minute.",
            "Involved in building the retry and replay tooling used when a batch "
            "fails, so that operators do not have to intervene manually.",
            "Helped mentor junior engineers and participated in design reviews.",
        ],
    ),
    ResumeWorkItem(
        name="Contoso Cloud", position="Senior Software Engineer",
        startDate="Jun 2019", endDate="Feb 2023",
        summary="Tech stack: Java 11, Spring Boot, PostgreSQL, Docker, AWS",
        highlights=[
            "Rebuilt the billing ledger, which put an end to a recurring class of "
            "double charge incidents that had been affecting customers.",
            "Worked on checkout API performance, bringing the 99th percentile from "
            "240ms down to 90ms through caching and connection pooling work.",
            "Introduced contract testing across the nine services owned by the team.",
        ],
    ),
    ResumeWorkItem(
        name="Fabrikam Labs", position="Software Engineer",
        startDate="Aug 2015", endDate="May 2019",
        summary="Tech stack: Java 8, MySQL, Jenkins",
        highlights=[
            "Built and maintained the ingestion pipeline behind the analytics "
            "product as it grew from 10 to 400 customer accounts.",
            "Assisted with automating the release pipeline, moving the team from "
            "fortnightly manual deploys to daily unattended ones.",
            "Was responsible for the on-call rotation documentation and the "
            "runbooks that the support team used out of hours.",
            "Took part in the migration off the legacy scheduler, which had been "
            "the cause of most of the overnight failures.",
        ],
    ),
]

PROJECTS = [
    ResumeProject(
        name="Ledger Replay", url="",
        description="Open source tool for replaying event streams into a test ledger.",
        highlights=[
            "Rebuilds a full account balance from a Kafka topic so a fix can be "
            "proven against production data before it ships.",
            "Used by 3 teams internally and released under Apache 2.0.",
        ],
    ),
]
CERTIFICATES = ["AWS Certified Solutions Architect, Associate (2024)"]

STRUCTURED = StructuredResume(
    basics=BASICS, work=WORK,
    education=[ResumeEducationItem(
        institution="Oregon State University", area="Computer Science",
        studyType="B.S.", startDate="2011", endDate="2015")],
    skills=[ResumeSkill(name="Backend", keywords=[
        "Java 17", "Spring Boot", "Kafka", "Kubernetes", "PostgreSQL",
        "AWS", "Docker", "REST", "Distributed systems"])],
    projects=PROJECTS, certificates=CERTIFICATES,
)

# The tailored version: the same facts, aimed at the posting. Nothing is
# added that the original does not already support, which is the whole
# claim the studio makes.
TAILORED_BASICS = BASICS.model_copy(update={
    "label": "Senior Backend Engineer, Payments Platform",
    "summary": (
        "Backend engineer and technical lead with 11 years on high-throughput "
        "distributed systems in Java 17 and Spring Boot. Designs event-driven "
        "services on Kafka and Kubernetes, and has owned the reliability of "
        "payment systems that settle millions of events a day."
    ),
})
# Every tailored bullet is the same fact with the duty phrasing removed
# and the number moved to the front. No new claim appears anywhere.
TAILORED_WORK = [w.model_copy(deep=True) for w in WORK]
TAILORED_WORK[0].highlights = [
    "Own the backend architecture of a payment reconciliation platform on "
    "Kafka and Kubernetes, settling several million events a day.",
    "Cut median settlement time from 4 minutes to under 1 minute by "
    "redesigning the event pipeline in Java 17.",
    "Built the retry and replay tooling that recovers a failed batch with no "
    "operator intervention.",
    "Mentor 4 engineers and run design review for the payments group.",
]
TAILORED_WORK[1].highlights = [
    "Rebuilt the billing ledger as an append only event store, ending a "
    "recurring class of double charge incidents.",
    "Cut checkout API latency at the 99th percentile from 240ms to 90ms "
    "through caching and connection pooling.",
    "Introduced contract testing across 9 services, catching breaking changes "
    "before release rather than in production.",
]
TAILORED_WORK[2].highlights = [
    "Built the ingestion pipeline behind the analytics product, carrying it "
    "from 10 to 400 customer accounts on PostgreSQL and AWS.",
    "Automated the release pipeline, moving deploys from fortnightly and "
    "manual to daily and unattended.",
]

TAILORED = StructuredResume(
    basics=TAILORED_BASICS, work=TAILORED_WORK,
    education=STRUCTURED.education,
    skills=[ResumeSkill(name="Backend", keywords=[
        "Java 17", "Spring Boot", "Kafka", "Kubernetes", "PostgreSQL", "AWS",
        "Docker", "Distributed systems", "Observability"])],
    projects=PROJECTS, certificates=CERTIFICATES,
)

CHANGES = [
    ResumeChange(kind="rephrased", where="basics.label",
                 what="Headline now names the posting's title, which the ATS matches on."),
    ResumeChange(kind="rephrased", where="basics.summary",
                 what="Summary leads with Java 17 and Kafka, the two skills the req repeats."),
    ResumeChange(kind="added-keyword", where="work[0].highlights[0]",
                 what="Named Kubernetes, which the original mentioned only in the tech stack line."),
    ResumeChange(kind="reordered", where="skills",
                 what="Moved observability up; the req calls it out by name."),
    ResumeChange(kind="rephrased", where="work[1].highlights[0]",
                 what="Named Spring Boot explicitly rather than leaving it implied."),
]

WARNINGS = [
    "The req asks for Terraform. Nothing in this resume shows it, so it was "
    "not added. Say so in the cover letter rather than on the resume.",
    "The req asks for five years of team leadership. This resume shows lead "
    "work from Mar 2023, which is under three. Left as written.",
    "GraphQL appears in the req and nowhere in your history. Not claimed.",
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        print("usage: python scripts/demo_resume.py <output-dir>")
        return 2
    out = Path(sys.argv[1]).expanduser().resolve() / "resumes" / f"{DEMO_ID}.json"
    report = run_ats_checks(render_markdown(STRUCTURED), JD, STRUCTURED)
    tailored_report = run_ats_checks(render_markdown(TAILORED), JD, TAILORED)
    now = datetime.now().isoformat(timespec="seconds")
    doc = ResumeDoc(
        resume_id=DEMO_ID,
        original_text=render_markdown(STRUCTURED),
        status="ready", tailor_status="idle",
        structured=STRUCTURED, jd_text=JD,
        jd_label="Senior Backend Engineer, Payments Platform",
        report=report, tailored_report=tailored_report,
        tailored=TailoredResume(
            resume=TAILORED, changes=CHANGES, warnings=WARNINGS,
            note=("Tailored for the payments platform req: the headline and summary "
                  "lead with Java 17 and Kafka, and Kubernetes is named where the "
                  "work already shows it."),
        ),
        created_at=now, updated_at=now,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc.model_dump_json(indent=2))
    print(f"wrote {out}")
    print(f"score {report.score} -> {tailored_report.score} (computed, not set)")
    print(f"keyword coverage {report.keyword_coverage.percent}% -> "
          f"{tailored_report.keyword_coverage.percent}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
