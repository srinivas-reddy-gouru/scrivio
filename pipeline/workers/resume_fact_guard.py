"""Deterministic factual validation for model-written resume content.

One validator, called after EVERY model mutation: initial tailoring, the
summary condenser, and instructed edits. Before this module each path
carried its own partial checks, so a fact one path protected could be
rewritten by another.

What is checked, and how
------------------------
Structure (employers, titles, dates, schools, degrees, skills, projects,
contact details) is compared field by field against the original record
it was matched to. Records are matched on their whole identity, not on a
name alone, so two roles at one employer or two degrees from one school
stay two distinct records.

Numbers are bound to CLAIMS, not to the resume. A figure in a rewritten
line is supported only when the same figure appears in the source record
next to the same thing being counted or measured. "Maintained 12
services" does not support "Mentored 12 engineers": the number matches
and the claim does not.

What is NOT checked
-------------------
Whether a sentence is true. This module verifies that protected facts and
figures trace back to the candidate's own record; it cannot judge that
"led the migration" is a fair description of what happened. New named
terms in prose are therefore surfaced for the candidate to confirm rather
than silently removed, and nothing here should be described as detecting
every unsupported claim.

The original structure is itself model-extracted. `audit_extraction`
compares it with the uploaded text so an extractor's invention is flagged
before it is treated as ground truth.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date

from pipeline.schemas.models import (
    ResumeChange,
    ResumeEducationItem,
    ResumeWorkItem,
    StructuredResume,
    TailoredResume,
)

METRIC_TOKEN = "[METRIC]"

# ── Text normalisation ──────────────────────────────────────────────────────

_STOPWORDS = frozenset("""
a an the of to by for in on with and or from at as per over under up down
across into than that this these those it its is was were be been being are
about around well approximately approx nearly roughly more less some each
every all any both while when where which who whom whose via within without
through during after before between among out off so such only also just
""".split())

# Words that scale or label a number rather than describe what is counted.
_MULTIPLIERS = {
    "thousand": 1e3, "thousands": 1e3, "k": 1e3,
    "million": 1e6, "millions": 1e6, "m": 1e6, "mm": 1e6,
    "billion": 1e9, "billions": 1e9, "b": 1e9, "bn": 1e9,
}
_PERCENT_WORDS = {"percent", "pct", "percentage"}

# A count of time is a measurement with a unit, not a count of things:
# "4 minutes" is 4min, and what matters is WHAT took four minutes.
_TIME_UNITS = {
    "year": "year", "years": "year", "yr": "year", "yrs": "year",
    "month": "month", "months": "month", "week": "week", "weeks": "week",
    "day": "day", "days": "day", "hour": "hour", "hours": "hour",
    "hr": "hour", "hrs": "hour", "minute": "min", "minutes": "min",
    "min": "min", "mins": "min", "second": "sec", "seconds": "sec",
    "sec": "sec", "secs": "sec", "ms": "ms",
}

# Words that sit next to almost any figure and so identify none of them.
# "Cut costs 30%" and "Cut latency 30%" share a verb, not a claim; "a year"
# follows revenue as readily as spend. Stems, as _stem() produces them.
_GENERIC_CONTEXT = frozenset("""
cut reduc increas improv grew grow rais lower boost brought bring br driv drov sav
achiev deliver decreas accelerat shrank shrink expand doubl tripl halv lift
drop trim slash optimiz sped speed scal generat won win ship built build led
lead manag maintain mad mak took tak got gain lost los help work us run ran
year quarter month week day hour minut second annual annually daily weekly
monthly yearly tim total overall averag median mean rough number count amount
rat level valu result
""".split())

_NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "dozen": 12, "dozens": 12,
    "hundred": 100, "hundreds": 100, "thousand": 1000, "thousands": 1000,
    "million": 1e6, "millions": 1e6, "billion": 1e9, "billions": 1e9,
}
# "a minute" supports "1 minute". Source side only: it can widen what the
# digit 1 is allowed to mean, never introduce a figure into the output.
_SOURCE_ONLY_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1}

_TOKEN_RE = re.compile(
    r"(?P<metric>\[METRIC\])"
    r"|(?P<num>(?<![A-Za-z0-9_])[$€£₹]?\d+(?:,\d{3})*(?:\.\d+)?(?:%|[A-Za-z]{1,3}\b)?)"
    r"|(?P<word>[A-Za-z][A-Za-z0-9+#'’]*)"
    r"|(?P<sep>[,;:.!?()])"
)
_NUM_PARTS_RE = re.compile(
    r"^(?P<prefix>[$€£₹]?)(?P<value>\d+(?:,\d{3})*(?:\.\d+)?)(?P<suffix>%|[A-Za-z]{1,3})?$"
)


def _stem(word: str) -> str:
    """Just enough to let 'mentored' meet 'mentoring' and 'services' meet
    'service'. Deliberately crude: a miss here makes the guard stricter,
    never looser."""
    w = word.lower().replace("’", "'")
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]
    return w


def _squash(text: str) -> str:
    """Lowercase with every non-alphanumeric removed. PDF extraction breaks
    words across spaces ('T echnical Lead'); comparing squashed strings is
    what makes 'is this in the uploaded text' answerable at all."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _norm_name(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


# ── Quantities and the claims they belong to ────────────────────────────────

@dataclass(frozen=True)
class Quantity:
    key: tuple[str, float, str]      # (currency, magnitude, unit)
    text: str                        # as written, for warnings
    span: tuple[int, int]            # character span to replace
    before: tuple[str, ...]          # nearest content stems before it
    after: tuple[str, ...]           # nearest content stems after it

    @property
    def is_bare_count(self) -> bool:
        return not self.key[0] and not self.key[2]


def _parse_number(raw: str) -> tuple[str, float, str] | None:
    m = _NUM_PARTS_RE.match(raw)
    if not m:
        return None
    try:
        value = float(m.group("value").replace(",", ""))
    except ValueError:
        return None
    suffix = (m.group("suffix") or "")
    unit = suffix.lower() if suffix != "%" else "%"
    if unit in _MULTIPLIERS:
        value *= _MULTIPLIERS[unit]
        unit = ""
    return m.group("prefix"), value, unit


def quantities_in(text: str, *, source: bool = False) -> list[Quantity]:
    """Every figure in `text`, each with the words that say what it measures.

    Digits glued to a preceding letter (p99, S3, EC2, k8s, OAuth2) are
    names, not measurements, and are left to the named-term check."""
    tokens = [(m.lastgroup, m.group(), m.start(), m.end())
              for m in _TOKEN_RE.finditer(text or "")]
    found: list[Quantity] = []
    for i, (kind, raw, start, end) in enumerate(tokens):
        key: tuple[str, float, str] | None = None
        if kind == "num":
            key = _parse_number(raw)
        elif kind == "word":
            low = raw.lower()
            prev_is_number = i > 0 and tokens[i - 1][0] == "num"
            if low in _NUMBER_WORDS and not prev_is_number:
                key = ("", float(_NUMBER_WORDS[low]), "")
            elif source and low in _SOURCE_ONLY_NUMBER_WORDS:
                key = ("", 1.0, "")
        if key is None:
            continue
        # A following scale or percent word belongs to the number itself.
        j = i + 1
        currency, magnitude, unit = key
        while j < len(tokens) and tokens[j][0] == "word":
            low = tokens[j][1].lower()
            if low in _MULTIPLIERS and kind == "num":
                magnitude *= _MULTIPLIERS[low]
            elif low in _PERCENT_WORDS:
                unit = "%"
            elif low in _TIME_UNITS and not unit:
                unit = _TIME_UNITS[low]
                j += 1
                break                    # the unit is context, not the figure
            else:
                break
            end = tokens[j][3]
            j += 1
        if unit in _TIME_UNITS:
            unit = _TIME_UNITS[unit]
        after = _content(tokens[j:], limit=2)
        before = _content(reversed(tokens[:i]), limit=2)
        found.append(Quantity(
            key=(currency, magnitude, unit), text=text[start:end],
            span=(start, end), before=before, after=after,
        ))
    return found


def _content(tokens, *, limit: int) -> tuple[str, ...]:
    """The nearest content words, stopping at punctuation: in 'Java 17,
    Spring Boot 3' the 17 belongs to Java and to nothing after the comma."""
    out: list[str] = []
    for kind, raw, _, _ in tokens:
        if kind == "sep":
            break
        if kind != "word":
            continue
        low = raw.lower()
        if low in _STOPWORDS or low in _MULTIPLIERS or low in _PERCENT_WORDS:
            continue
        stem = _stem(raw)
        if stem in _GENERIC_CONTEXT or low in _GENERIC_CONTEXT:
            continue
        out.append(stem)
        if len(out) == limit:
            break
    return tuple(out)


def _supports(candidate: Quantity, source: Quantity) -> bool:
    """Same figure AND the same thing being counted or measured."""
    if candidate.key != source.key:
        return False
    if candidate.is_bare_count and candidate.after and source.after:
        # A count is a count OF something: the counted thing must agree.
        return bool(set(candidate.after) & set(source.after))
    return bool(
        (set(candidate.before) | set(candidate.after))
        & (set(source.before) | set(source.after))
    )


# ── Record matching ─────────────────────────────────────────────────────────

def _work_identity(w: ResumeWorkItem) -> str:
    return _norm_name(w.name)


def _edu_identity(e: ResumeEducationItem) -> str:
    return _norm_name(e.institution)


def _similarity(a: str, b: str) -> float:
    sa = {_stem(w) for w in re.findall(r"[A-Za-z]+", a) if w.lower() not in _STOPWORDS}
    sb = {_stem(w) for w in re.findall(r"[A-Za-z]+", b) if w.lower() not in _STOPWORDS}
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def _work_text(w: ResumeWorkItem) -> str:
    return " ".join([w.summary, *w.highlights])


def match_work(tailored: list[ResumeWorkItem],
               original: list[ResumeWorkItem]) -> list[int | None]:
    """For each tailored entry, the index of the original it represents.

    Exact identity first (employer, title, dates), so a promotion at one
    employer is two records that each find their own source. What is left
    is matched within the same employer by title, then dates, then by how
    much the bullets have in common. An original is claimed at most once."""
    return _match(
        tailored, original, _work_identity,
        exact=lambda w: (_norm_name(w.position), w.startDate.strip(), w.endDate.strip()),
        score=lambda t, o: (
            3.0 * (_norm_name(t.position) == _norm_name(o.position))
            + 1.5 * (t.startDate.strip() == o.startDate.strip())
            + 1.5 * (t.endDate.strip() == o.endDate.strip())
            + _similarity(_work_text(t), _work_text(o))
        ),
    )


def match_education(tailored: list[ResumeEducationItem],
                    original: list[ResumeEducationItem]) -> list[int | None]:
    return _match(
        tailored, original, _edu_identity,
        exact=lambda e: (_norm_name(e.studyType), _norm_name(e.area),
                         e.startDate.strip(), e.endDate.strip()),
        score=lambda t, o: (
            2.0 * (_norm_name(t.studyType) == _norm_name(o.studyType))
            + 2.0 * (_norm_name(t.area) == _norm_name(o.area))
            + 1.0 * (t.startDate.strip() == o.startDate.strip())
            + 1.0 * (t.endDate.strip() == o.endDate.strip())
        ),
    )


def _match(tailored, original, identity, *, exact, score) -> list[int | None]:
    result: list[int | None] = [None] * len(tailored)
    free = set(range(len(original)))
    for ti, t in enumerate(tailored):           # pass 1: whole identity
        for oi in sorted(free):
            o = original[oi]
            if identity(t) and identity(t) == identity(o) and exact(t) == exact(o):
                result[ti] = oi
                free.discard(oi)
                break
    for ti, t in enumerate(tailored):           # pass 2: best of what is left
        if result[ti] is not None or not identity(t):
            continue
        candidates = [oi for oi in sorted(free) if identity(original[oi]) == identity(t)]
        if not candidates:
            continue
        best = max(candidates, key=lambda oi: (score(t, original[oi]), -oi))
        result[ti] = best
        free.discard(best)
    return result


def find_entry(resume: StructuredResume, kind: str, like) -> int | None:
    """Locate the counterpart of `like` in another copy of the resume, by
    identity rather than index. Used when an addition or removal made on
    the tailored copy has to land on the same record in the original."""
    items = getattr(resume, kind)
    if kind == "work":
        found = match_work([like], items)[0]
    elif kind == "education":
        found = match_education([like], items)[0]
    else:
        name = _norm_name(like.name)
        found = next((i for i, it in enumerate(items) if _norm_name(it.name) == name), None)
    return found


# ── Structural guard ────────────────────────────────────────────────────────

def _restore(kept: list, kept_sources: list[int | None], original: list) -> list:
    """Put back original entries the model dropped, near where they were."""
    present = {s for s in kept_sources if s is not None}
    out = list(kept)
    for oi, item in enumerate(original):
        if oi not in present:
            out.insert(min(oi, len(out)), item.model_copy(deep=True))
    return out


def _term_present(term: str, haystack_squashed: str) -> bool:
    needle = _squash(term)
    return bool(needle) and needle in haystack_squashed


def _all_strings(resume: StructuredResume) -> list[str]:
    out: list[str] = []

    def walk(value) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(resume.model_dump())
    return out


def enforce_honesty(
    original: StructuredResume, tailored: TailoredResume
) -> TailoredResume:
    """Deterministic backstop for the facts a model may not touch.

    Contact details, employers, titles, employment dates, schools, degree
    type, field of study, education dates, skills, project identity,
    certificates, and user-made sections must trace to the original.
    Inventions are removed, alterations are reverted, omissions of a job
    or a degree are restored, and every one of those is reported with the
    field it happened in. Nothing is changed silently."""
    warnings = list(tailored.warnings)
    resume = tailored.resume

    # Contact details identify the person; a tailor has no reason to touch them.
    for field in ("name", "email", "phone", "url", "location"):
        was, now = getattr(original.basics, field), getattr(resume.basics, field)
        if now == was:
            continue
        if now.strip():
            warnings.append(
                f"[basics.{field}] Reverted: your {field} was changed from "
                f"'{was}' to '{now}'. Contact details are never rewritten."
            )
        setattr(resume.basics, field, was)

    # Work history.
    sources = match_work(resume.work, original.work)
    kept_work: list[ResumeWorkItem] = []
    kept_sources: list[int | None] = []
    for item, oi in zip(resume.work, sources):
        if oi is None:
            warnings.append(
                f"Removed work entry '{item.name or item.position}' — that employer "
                "is not on the original resume."
            )
            continue
        source = original.work[oi]
        if item.position != source.position:
            warnings.append(
                f"Reverted job title at {source.name} from '{item.position}' to "
                f"'{source.position}' — titles cannot be changed."
            )
            item.position = source.position
        if (item.startDate, item.endDate) != (source.startDate, source.endDate):
            warnings.append(
                f"Reverted employment dates at {source.name} — dates cannot be changed."
            )
            item.startDate, item.endDate = source.startDate, source.endDate
        item.name = source.name
        kept_work.append(item)
        kept_sources.append(oi)
    dropped = [o for i, o in enumerate(original.work) if i not in set(kept_sources)]
    for o in dropped:
        warnings.append(
            f"Restored the {o.position or 'role'} entry at {o.name}: a job cannot "
            "be dropped from your history by a rewrite. Remove it yourself in "
            "edit mode if you want it gone."
        )
    resume.work = _restore(kept_work, kept_sources, original.work)

    # Education: the degree, the field, and the dates are facts, not phrasing.
    edu_sources = match_education(resume.education, original.education)
    kept_edu: list[ResumeEducationItem] = []
    kept_edu_sources: list[int | None] = []
    for edu, oi in zip(resume.education, edu_sources):
        if oi is None:
            warnings.append(
                f"Removed education entry '{edu.institution}' — that institution "
                "is not on the original resume."
            )
            continue
        source = original.education[oi]
        for field, label in (("studyType", "degree"), ("area", "field of study"),
                             ("startDate", "start date"), ("endDate", "end date"),
                             ("score", "grade")):
            was, now = getattr(source, field), getattr(edu, field)
            if now != was:
                warnings.append(
                    f"[education[{len(kept_edu)}]] Reverted the {label} at "
                    f"{source.institution} from '{now}' to '{was}'. Degrees, "
                    "fields, and dates are never rewritten."
                )
                setattr(edu, field, was)
        edu.institution = source.institution
        kept_edu.append(edu)
        kept_edu_sources.append(oi)
    for i, o in enumerate(original.education):
        if i not in set(kept_edu_sources):
            warnings.append(
                f"Restored the education entry at {o.institution}: a rewrite "
                "cannot drop a degree."
            )
    resume.education = _restore(kept_edu, kept_edu_sources, original.education)

    # Skills: a keyword needs a basis somewhere in the original record.
    basis = _squash(" ".join(_all_strings(original)))
    kept_skills = []
    removed_skills: list[str] = []
    for group in resume.skills:
        keep = []
        for keyword in group.keywords:
            if METRIC_TOKEN in keyword or _term_present(keyword, basis):
                keep.append(keyword)
            else:
                removed_skills.append(keyword)
        group.keywords = keep
        if keep:
            kept_skills.append(group)
    if removed_skills:
        warnings.append(
            "Removed skills with no basis in your original resume: "
            + ", ".join(removed_skills)
            + ". If you have one of them, add it yourself in edit mode."
        )
    resume.skills = kept_skills

    # Projects: identity and link are facts; which projects to show is not.
    orig_projects = {_norm_name(p.name): p for p in original.projects if p.name}
    kept_projects = []
    for project in resume.projects:
        source = orig_projects.get(_norm_name(project.name))
        if source is None:
            warnings.append(
                f"Removed project '{project.name}' — it is not on the original resume."
            )
            continue
        if project.url != source.url:
            if project.url.strip():
                warnings.append(
                    f"Reverted the link on project '{source.name}' to the original."
                )
            project.url = source.url
        project.name = source.name
        kept_projects.append(project)
    resume.projects = kept_projects

    orig_certs = set(original.certificates)
    invented_certs = [c for c in resume.certificates if c not in orig_certs]
    if invented_certs:
        resume.certificates = [c for c in resume.certificates if c in orig_certs]
        warnings.append(
            "Removed certificates not on the original resume: "
            + ", ".join(invented_certs)
        )

    # Custom sections exist so the USER can add what the standard has no
    # field for. A section the original does not have is the model
    # inventing a whole category of experience.
    orig_sections = {c.name for c in original.custom}
    kept_custom = []
    for section in resume.custom:
        if section.name not in orig_sections:
            warnings.append(
                f"Removed the '{section.name or 'untitled'}' section — it is not "
                "on the original resume."
            )
            continue
        kept_custom.append(section)
    # And the reverse: a section the user made must survive a tailor run
    # that simply forgot to emit it.
    kept_names = {c.name for c in kept_custom}
    for section in original.custom:
        if section.name not in kept_names:
            kept_custom.append(section.model_copy(deep=True))
    resume.custom = kept_custom

    tailored.warnings = warnings
    return tailored


# ── Number guard ────────────────────────────────────────────────────────────

_YEAR_RE = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")


def career_years(resume: StructuredResume, today: date | None = None) -> int:
    """Upper bound on 'N years of experience', from the work dates alone."""
    today = today or date.today()
    starts, ends = [], []
    for w in resume.work:
        s = _YEAR_RE.search(w.startDate or "")
        if not s:
            continue
        starts.append(int(s.group()))
        e = _YEAR_RE.search(w.endDate or "")
        ends.append(int(e.group()) if e else today.year)
    if not starts:
        return 0
    return max(ends) - min(starts) + 1


def _record_claims(item) -> list[str]:
    parts: list[str] = []
    for field in ("summary", "description"):
        value = getattr(item, field, "")
        if value:
            parts.append(value)
    parts.extend(getattr(item, "highlights", []) or [])
    parts.extend(getattr(item, "items", []) or [])
    return parts


def _identity_claims(resume: StructuredResume) -> list[str]:
    """Facts that describe the person rather than one job: a version in the
    skills list ('Java 17') may be named in any line."""
    out = [resume.basics.label, resume.basics.summary, *resume.certificates]
    for group in resume.skills:
        out.append(", ".join(group.keywords))
    return [s for s in out if s]


def _fields_with_sources(candidate: StructuredResume, original: StructuredResume):
    """(path, get, set, source claims) for every prose field in `candidate`.

    A line under a job may draw on THAT job's original claims and on the
    person-level facts, and on nothing else: a figure does not get to
    move from one employer to another. The summary speaks for the whole
    career, so it may draw on all of it."""
    identity = _identity_claims(original)
    everything = list(identity)
    for group in (original.work, original.projects, original.custom):
        for item in group:
            everything.extend(_record_claims(item))

    yield ("basics.summary", lambda: candidate.basics.summary,
           lambda v: setattr(candidate.basics, "summary", v), everything)

    work_sources = match_work(candidate.work, original.work)
    for i, (w, oi) in enumerate(zip(candidate.work, work_sources)):
        claims = identity + (_record_claims(original.work[oi]) if oi is not None else [])
        yield (f"work[{i}].summary", lambda w=w: w.summary,
               lambda v, w=w: setattr(w, "summary", v), claims)
        for j in range(len(w.highlights)):
            yield (f"work[{i}].highlights[{j}]",
                   lambda w=w, j=j: w.highlights[j],
                   lambda v, w=w, j=j: w.highlights.__setitem__(j, v), claims)

    by_name = {_norm_name(p.name): p for p in original.projects}
    for i, p in enumerate(candidate.projects):
        source = by_name.get(_norm_name(p.name))
        claims = identity + (_record_claims(source) if source else [])
        yield (f"projects[{i}].description", lambda p=p: p.description,
               lambda v, p=p: setattr(p, "description", v), claims)
        for j in range(len(p.highlights)):
            yield (f"projects[{i}].highlights[{j}]",
                   lambda p=p, j=j: p.highlights[j],
                   lambda v, p=p, j=j: p.highlights.__setitem__(j, v), claims)

    sections = {c.name: c for c in original.custom}
    for i, c in enumerate(candidate.custom):
        source = sections.get(c.name)
        claims = identity + (_record_claims(source) if source else [])
        for j in range(len(c.items)):
            yield (f"custom[{i}].items[{j}]",
                   lambda c=c, j=j: c.items[j],
                   lambda v, c=c, j=j: c.items.__setitem__(j, v), claims)


def _user_numbers(user_text: str) -> set[tuple[str, float, str]]:
    return {q.key for q in quantities_in(user_text)}


def unsupported_quantities(
    text: str, claims: list[str], *, original: StructuredResume,
    prior_text: str = "", user_keys: frozenset = frozenset(),
) -> list[Quantity]:
    """Figures in `text` that nothing the candidate provided accounts for."""
    sources = [q for claim in claims for q in quantities_in(claim, source=True)]
    prior = quantities_in(prior_text, source=True)
    span_years = career_years(original)
    missing: list[Quantity] = []
    for q in quantities_in(text):
        if q.key in user_keys:
            continue                     # the user typed it in this request
        if any(q.key == p.key for p in prior):
            continue                     # already in this very line
        if any(_supports(q, s) for s in sources):
            continue
        if q.key[2] == "year" and not q.key[0] and 0 < q.key[1] <= span_years:
            continue                     # 'N years', within the work dates
        missing.append(q)
    return missing


def _replace_spans(text: str, quantities: list[Quantity]) -> str:
    for q in sorted(quantities, key=lambda q: q.span[0], reverse=True):
        text = text[:q.span[0]] + METRIC_TOKEN + text[q.span[1]:]
    return text


def guard_numbers(
    original: StructuredResume, candidate: TailoredResume, *,
    baseline: TailoredResume | None = None, user_text: str = "",
) -> TailoredResume:
    """Every figure in model-written prose must trace to a source claim.

    On first tailoring there is no earlier version of a line to fall back
    to, so an unsupported figure becomes a [METRIC] placeholder: the
    sentence survives and the number becomes the candidate's to supply,
    which is the explicit confirmation an invented metric needs. On an
    edit there IS an earlier version, so the line reverts to it."""
    user_keys = frozenset(_user_numbers(user_text))
    before: dict[str, str] = {}
    if baseline is not None:
        before = {path: get() for path, get, _, _
                  in _fields_with_sources(baseline.resume, original)}
    for path, get, set_, claims in _fields_with_sources(candidate.resume, original):
        text = get()
        if not text:
            continue
        prior = before.get(path, "")
        if baseline is not None and text == prior:
            continue
        missing = unsupported_quantities(
            text, claims, original=original, prior_text=prior, user_keys=user_keys)
        if not missing:
            continue
        figures = ", ".join(sorted({q.text for q in missing}))
        if baseline is not None and path in before:
            set_(prior)
            candidate.warnings.append(
                f"Reverted {path}: the edit introduced the number(s) {figures} "
                "which came neither from that part of your resume nor from "
                "you. Scrivio never invents metrics; supply the real figure "
                "and it will be applied."
            )
        else:
            set_(_replace_spans(text, missing))
            candidate.warnings.append(
                f"[{path}] Unsupported number: the figure {figures} is not "
                "backed by this entry in your original resume, so it was "
                "replaced with a placeholder. Type the real number if you "
                "have one, or reword the line so it does not need one."
            )
            candidate.changes.append(ResumeChange(
                kind="placeholder", where=path,
                what=f"Replaced the unsupported figure {figures} with a placeholder.",
            ))
    # The headline has no placeholder convention: an unsupported figure
    # there puts the original headline back.
    label = candidate.resume.basics.label
    if label and label != original.basics.label:
        bad = unsupported_quantities(
            label, _identity_claims(original), original=original,
            prior_text=baseline.resume.basics.label if baseline else "",
            user_keys=user_keys)
        if bad:
            candidate.resume.basics.label = (
                baseline.resume.basics.label if baseline else original.basics.label)
            candidate.warnings.append(
                "[basics.label] Reverted the headline: it introduced "
                f"{', '.join(q.text for q in bad)}, which your resume does not support."
            )
    return candidate


# ── Named terms in prose ────────────────────────────────────────────────────

_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#]*(?:[./-][A-Za-z0-9+#]+)*")


def _looks_named(token: str, sentence_initial: bool) -> bool:
    """A technology, credential, or proper noun, as opposed to a word."""
    if len(token) < 2:
        return False
    if any(ch.isdigit() for ch in token):
        return True
    if any(ch.isupper() for ch in token[1:]):
        return True                      # PhD, AWS, gRPC, PostgreSQL
    return token[0].isupper() and not sentence_initial


def new_named_terms(text: str, known_squashed: str, jd_text: str = "") -> list[str]:
    """Named terms in `text` that appear nowhere in what the candidate gave.

    A capitalised first word is usually a verb ('Built'), so it counts only
    when the job description uses that same word as a name: that is the
    shape keyword stuffing takes."""
    jd_named = {
        m.group().casefold() for m in _TERM_RE.finditer(jd_text or "")
        if _looks_named(m.group(), sentence_initial=False)
    }
    found: list[str] = []
    for sentence in re.split(r"(?<=[.!?;:])\s+|\n+", text or ""):
        for k, m in enumerate(_TERM_RE.finditer(sentence.replace(METRIC_TOKEN, " "))):
            token = m.group().strip(".-/")
            initial = k == 0
            named = _looks_named(token, initial) or (
                initial and token.casefold() in jd_named)
            if not named or _term_present(token, known_squashed):
                continue
            if token not in found:
                found.append(token)
    return found


def note_new_terms(
    original: StructuredResume, candidate: TailoredResume, *,
    baseline: TailoredResume | None = None, user_text: str = "",
    jd_text: str = "", limit: int = 6,
) -> TailoredResume:
    """Surface, never remove. Naming a technology the resume implies is
    legitimate tailoring and naming one it does not is fabrication, and
    telling those apart is a judgment about the candidate's life. So the
    line stays and the candidate is asked."""
    known = _squash(" ".join(_all_strings(original)) + " " + user_text)
    if baseline is not None:
        known += _squash(" ".join(_all_strings(baseline.resume)))
    raised = 0
    seen: set[str] = set()
    for path, get, _, _ in _fields_with_sources(candidate.resume, original):
        for term in new_named_terms(get(), known, jd_text):
            if term.casefold() in seen or raised >= limit:
                continue
            seen.add(term.casefold())
            raised += 1
            candidate.warnings.append(
                f"[{path}] New term: '{term}' appears here but nowhere in your "
                "original resume. Keep it only if you have really worked with "
                "it and could answer an interviewer's follow-up. Otherwise "
                "edit the line to remove it."
            )
    return candidate


# ── The one validator ───────────────────────────────────────────────────────

def validate_model_output(
    original: StructuredResume, candidate: TailoredResume, *,
    baseline: TailoredResume | None = None, user_text: str = "",
    jd_text: str = "",
) -> TailoredResume:
    """Run after every model mutation of a resume, without exception."""
    candidate = enforce_honesty(original, candidate)
    candidate = guard_numbers(
        original, candidate, baseline=baseline, user_text=user_text)
    candidate = note_new_terms(
        original, candidate, baseline=baseline, user_text=user_text,
        jd_text=jd_text)
    return candidate


def summary_rewrite_is_safe(
    original: StructuredResume, before: str, after: str, jd_text: str = "",
) -> list[str]:
    """Reasons a condensed summary must be refused; empty when it is safe.

    A condensation can only remove. Anything it names or counts that the
    longer summary and the resume did not is, by construction, new."""
    problems: list[str] = []
    everything = _identity_claims(original)
    for group in (original.work, original.projects, original.custom):
        for item in group:
            everything.extend(_record_claims(item))
    bad = unsupported_quantities(
        after, everything, original=original, prior_text=before)
    if bad:
        problems.append("it introduced the figure(s) " + ", ".join(q.text for q in bad))
    known = _squash(" ".join(_all_strings(original)) + " " + before)
    terms = new_named_terms(after, known, jd_text)
    if terms:
        problems.append("it named " + ", ".join(terms)
                        + ", which the summary it was given did not")
    return problems


# ── Auditing the extraction ─────────────────────────────────────────────────

def audit_extraction(original_text: str, structured: StructuredResume) -> list[str]:
    """Facts in the extracted structure that the uploaded text does not
    contain. The structure becomes the record every later check trusts,
    so what the extractor added has to be caught here or never."""
    haystack = _squash(original_text)
    if not haystack:
        return []
    findings: list[str] = []

    def check(value: str, what: str) -> None:
        if value and value.strip() and not _term_present(value, haystack):
            findings.append(f"{what} '{value.strip()}'")

    for w in structured.work:
        check(w.name, "the employer")
        check(w.position, f"the job title at {w.name or 'one employer'}")
        for d in (w.startDate, w.endDate):
            for year in _YEAR_RE.finditer(d or ""):
                if year.group() not in original_text:
                    findings.append(
                        f"the year {year.group()} in the dates at {w.name or 'one employer'}")
    for e in structured.education:
        check(e.institution, "the school")
    for group in structured.skills:
        for keyword in group.keywords:
            check(keyword, "the skill")
    for c in structured.certificates:
        check(c, "the certificate")

    source_numbers = {q.key for q in quantities_in(original_text, source=True)}
    for group in (structured.work, structured.projects):
        for item in group:
            for claim in _record_claims(item):
                for q in quantities_in(claim):
                    if q.key not in source_numbers:
                        findings.append(
                            f"the figure {q.text} under {item.name or 'an entry'}")
    seen: set[str] = set()
    return [f for f in findings if not (f in seen or seen.add(f))]


# ── What still stands between a resume and being sent ───────────────────────

def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def unresolved_placeholders(resume: StructuredResume) -> list[str]:
    """Where every [METRIC] still is, in words a person can act on.

    Scans every string on the resume, not only the fields the placeholder
    editor knows how to fill: a placeholder in the headline is just as
    unsendable as one in a bullet."""
    found: list[str] = []

    def look(text: str, where: str) -> None:
        count = (text or "").count(METRIC_TOKEN)
        if count:
            times = "" if count == 1 else f" ({count} of them)"
            found.append(f"{where}{times}")

    look(resume.basics.label, "the headline")
    look(resume.basics.summary, "the summary")
    for w in resume.work:
        at = w.name or w.position or "a job"
        look(w.summary, f"the description line under {at}")
        for i, h in enumerate(w.highlights, 1):
            look(h, f"the {_ordinal(i)} bullet under {at}")
    for p in resume.projects:
        at = p.name or "a project"
        look(p.description, f"the description of {at}")
        for i, h in enumerate(p.highlights, 1):
            look(h, f"the {_ordinal(i)} bullet under {at}")
    for group in resume.skills:
        for keyword in group.keywords:
            look(keyword, "the skills list")
    for c in resume.certificates:
        look(c, "the certifications")
    for section in resume.custom:
        for i, item in enumerate(section.items, 1):
            look(item, f"the {_ordinal(i)} line of {section.name or 'a section'}")
    return found
