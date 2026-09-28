"""The sample resume and the demo examples are about the same invented
person (R23).

In demo mode the model output is fixed. When the sample was a different
person, loading it and pressing the button answered with someone else's
resume. The label said the output was an example, which was true, and the
page still read as broken. These keep the two from drifting apart.
"""
import re
from pathlib import Path

import main

SAMPLE = (Path(__file__).resolve().parents[1] / "web" / "src" / "sample.ts").read_text(
    encoding="utf-8")


def _constant(name: str) -> str:
    return re.search(rf"export const {name} = `(.*?)`;", SAMPLE, re.S).group(1)


def _canned() -> dict:
    return main.MockAnthropicMessages._mock_resume_extraction(None)


def test_every_fact_in_the_demo_resume_is_in_the_sample():
    resume, canned = _constant("SAMPLE_RESUME"), _canned()

    facts = [v for k, v in canned["basics"].items() if v and k != "url"]
    for job in canned["work"]:
        facts += [job["name"], job["position"], job["startDate"], job["endDate"]]
        facts += job["highlights"]
    for school in canned["education"]:
        facts += [school["institution"], school["area"], school["studyType"],
                  school["startDate"], school["endDate"]]
    for group in canned["skills"]:
        facts += group["keywords"]
    facts += canned["certificates"]

    assert [f for f in facts if f not in resume] == []


def test_the_sample_has_no_job_or_school_the_demo_resume_lacks():
    resume, canned = _constant("SAMPLE_RESUME"), _canned()

    assert resume.count(" - Present") == len(
        [j for j in canned["work"] if j["endDate"] == "Present"])
    assert len(re.findall(r"^- ", resume, re.M)) == sum(
        len(j["highlights"]) for j in canned["work"])


def test_the_sample_posting_asks_for_what_the_demo_says_it_asks_for():
    posting = _constant("SAMPLE_JD")

    for named in ("Kafka", "Kubernetes", "across teams"):
        assert named in posting


def test_the_sample_is_plainly_not_a_real_person():
    resume = _constant("SAMPLE_RESUME")

    assert "@example.com" in resume
    assert "555 010" in resume, "555-01xx is the range set aside for fiction"
    assert "—" not in SAMPLE and "–" not in SAMPLE
