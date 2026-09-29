"""Collect public postings for the evaluation corpus. Run by hand.

    python -m evals.collect_postings --fetch usajobs 869294500 --into evals/corpus/postings-v2
    python -m evals.collect_postings --fetch tts login-engineer-2023.md --into ...

This is the only file under evals/ that uses the network, and it does so
only when --fetch is given. Nothing imports it. The evaluation reads what
this wrote and never fetches anything.

WHAT MAY BE COLLECTED
---------------------
Only text that may be copied. A job posting is somebody's writing, and
this repository is public. The two sources used so far:

  USAJOBS          announcements written by federal agencies. Works of
                   the United States Government, which are not subject
                   to copyright in the United States (17 U.S.C. 105).
  18F/join.tts.gsa.gov
                   position descriptions in a repository dedicated to
                   the public domain under CC0 1.0.

A posting from a company's careers page is not to be added. It can be
linked to. Its text cannot be kept here.

WHAT IS KEPT
------------
The parts that say what the job is: title, summary, duties,
qualifications. Left out: how to apply, conditions of employment,
benefits, and the agency's contact details, which name a person.
Anything that looks like an email address or a phone number in what is
kept is counted, and the corpus tests fail if the count is not zero.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

BLOCK = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "br", "tr", "section"}
UNSEEN = {"script", "style", "button", "svg"}
PERSONAL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|\(?\b\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")
TTS_COMMIT = "5dfa66db0f4b"


class _Under(HTMLParser):
    """The text under the element with a given id, one block to a line."""

    def __init__(self, wanted: str) -> None:
        super().__init__(convert_charrefs=True)
        self.wanted, self.depth, self.inside, self.unseen = wanted, 0, False, 0
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if not self.inside and dict(attrs).get("id") == self.wanted:
            self.inside, self.depth = True, 1
            return
        if not self.inside:
            return
        if tag not in ("br", "img", "input", "hr", "meta", "link"):
            self.depth += 1
        if tag in UNSEEN:
            self.unseen += 1
        self.out.append("\n- " if tag == "li" else
                        "\n\n## " if tag in ("h1", "h2", "h3", "h4", "h5") else
                        "\n" if tag in BLOCK else "")

    def handle_endtag(self, tag):
        if not self.inside:
            return
        if tag in UNSEEN:
            self.unseen = max(0, self.unseen - 1)
        if tag in BLOCK:
            self.out.append("\n")
        self.depth -= 1
        self.inside = self.depth > 0

    def handle_data(self, data):
        if self.inside and not self.unseen:
            self.out.append(data)


def under(page: str, element_id: str) -> str:
    parser = _Under(element_id)
    parser.feed(page)
    kept: list[str] = []
    gap = False
    for line in "".join(parser.out).replace("\xa0", " ").splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line or line in ("-", "##", "Help", "## Help"):   # "Help" is a button
            gap = True
            continue
        if gap and kept:
            kept.append("")
        kept.append(line)
        gap = False
    return "\n".join(kept).strip()


def from_usajobs(page: str) -> dict:
    title = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    title = html.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip() if title else ""
    agency = re.search(r"usajobs-joa-banner__dept[^>]*>(.*?)<", page, re.S)
    requirements = under(page, "joa-requirements")
    qualifications = re.search(
        r"## Qualifications\n(.*?)(?=\n## Additional information|\Z)", requirements, re.S)
    parts = [
        title,
        re.sub(r"^## Summary\n+", "", under(page, "joa-summary")),
        "Duties\n" + re.sub(r"^## Duties\n+", "", under(page, "joa-duties")),
        "Qualifications\n" + (qualifications.group(1).strip() if qualifications else ""),
    ]
    return {"title": title,
            "publisher": html.unescape(agency.group(1)).strip() if agency else "",
            "text": re.sub(r"\n## ", "\n", "\n\n".join(p for p in parts if p.strip())),
            "sections_kept": ["title", "summary", "duties", "qualifications"],
            "sections_left_out": ["conditions of employment", "additional information",
                                  "how to apply", "required documents",
                                  "agency contact information", "benefits"]}


def from_tts(source: str) -> dict:
    front, body = source.split("\n---\n", 1)
    front = "\n".join(l for l in front.splitlines() if not l.lstrip().startswith("#"))
    title = re.search(r'^title:\s*"?(.*?)"?\s*$', front, re.M).group(1)
    objectives = re.search(r"^key objectives:\n(.*?)(?=^\S)", front + "\nend:", re.S | re.M)
    objectives = "\n".join(l.rstrip() for l in (objectives.group(1) if objectives else "").splitlines())
    body = re.sub(r"\{%\s*comment.*?endcomment[^%]*%\}", "", body, flags=re.S)
    body = re.sub(r"\{%.*?%\}", "", body, flags=re.S).replace("{{ page.title }}", title)

    def section(name: str) -> str:
        m = re.search(rf"^## {name}\s*\n(.*?)(?=^## |\Z)", body, re.S | re.M)
        return re.sub(r"\n{3,}", "\n\n", m.group(1)).strip() if m else ""

    parts = [title,
             "Role summary\n" + section("Role summary") if section("Role summary") else "",
             "Key objectives\n" + objectives.strip("\n") if objectives.strip() else "",
             "Qualifications\n" + section("Qualifications") if section("Qualifications") else ""]
    return {"title": title, "publisher": "General Services Administration",
            "text": "\n\n".join(p for p in parts if p),
            "sections_kept": ["title", "role summary", "key objectives", "qualifications"],
            "sections_left_out": ["basic information", "how to apply", "information sessions",
                                  "the authoring template's instructions"]}


def split_of(posting_id: str) -> str:
    """Tuning or held out, by the identifier alone. A third are held out.
    Decided before any result is seen, and not open to choice."""
    return "held_out" if int(hashlib.sha256(posting_id.encode()).hexdigest(), 16) % 3 == 0 \
        else "tuning"


SOURCES = {
    "usajobs": {
        "source": "USAJOBS", "address": "https://www.usajobs.gov/job/{name}",
        "fetch": "https://www.usajobs.gov/job/{name}", "read": from_usajobs,
        "licence": "Public domain in the United States",
        "licence_basis": "A work of the United States Government (17 U.S.C. 105)."},
    "tts": {
        "source": "TTS, U.S. General Services Administration",
        "address": "https://github.com/18F/join.tts.gsa.gov/blob/" + TTS_COMMIT + "/positions/{name}",
        "fetch": "https://raw.githubusercontent.com/18F/join.tts.gsa.gov/" + TTS_COMMIT
                 + "/positions/{name}", "read": from_tts,
        "licence": "CC0 1.0 Universal",
        "licence_basis": "The repository's LICENSE.md dedicates it to the public domain "
                         "under CC0 1.0."},
}


def record(kind: str, name: str, raw: bytes, collected_on: str) -> dict:
    source = SOURCES[kind]
    read = source["read"](raw.decode("utf-8", errors="replace"))
    stem = re.sub(r"\.md$", "", name)
    posting_id = f"{kind}-" + re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return {
        "id": posting_id, "source": source["source"],
        "source_url": source["address"].format(name=name), "collected_on": collected_on,
        "raw_sha256": hashlib.sha256(raw).hexdigest(), "licence": source["licence"],
        "licence_basis": source["licence_basis"], "publisher": read["publisher"],
        "title": read["title"], "sections_kept": read["sections_kept"],
        "sections_left_out": read["sections_left_out"], "text": read["text"],
        "split": split_of(posting_id),
        "personal_details_found": len(PERSONAL.findall(read["text"]))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fetch", nargs=2, metavar=("SOURCE", "NAME"),
                        help="usajobs <announcement number>, or tts <file name>")
    parser.add_argument("--into", type=Path, help="the corpus folder to write into")
    args = parser.parse_args(argv)
    if not args.fetch or not args.into:
        parser.print_help()
        print("\nNothing was fetched.")
        return 0
    kind, name = args.fetch
    if kind not in SOURCES:
        print(f"{kind} is not a source whose text may be copied. See the top of this file.",
              file=sys.stderr)
        return 2
    request = urllib.request.Request(
        SOURCES[kind]["fetch"].format(name=name),
        headers={"User-Agent": "Mozilla/5.0 (evaluation corpus; public domain text)"})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
    made = record(kind, name, raw, date.today().isoformat())
    if made["personal_details_found"]:
        print(f"Not written: {made['personal_details_found']} email addresses or phone "
              "numbers in the text that would be kept.", file=sys.stderr)
        return 1
    target = args.into / "postings" / f"{made['id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(made, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{made['id']}: {made['split']}, {len(made['text'])} characters, {made['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
