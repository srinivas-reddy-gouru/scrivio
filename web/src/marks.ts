/** Deriving paper marks from real report data.
 *
 * The desk's rule: findings live ON the paper. This module maps the
 * backend's check results and change log onto specific rendered nodes.
 */
import type { AtsReport, ResumeChange, StructuredResume } from "./types";

/** Client mirror of the backend's weak-phrase list (resume_studio_worker
 * _WEAK_PHRASES) — used only for underlining; the verdict comes from the
 * server's check. */
const WEAK_PHRASES = [
  "responsible for", "duties included", "tasked with", "in charge of",
  "worked on", "helped with", "assisted with", "participated in",
  "was involved in", "leveraged", "utilized", "utilizing",
  "proven track record", "passionate about", "seasoned professional",
  "highly motivated", "cutting-edge", "state-of-the-art",
  "best-in-class", "world-class",
];

export type MarkTone = "red" | "amber" | "teal";

export interface HighlightMark {
  tone: MarkTone;
  /** Which finding ids this mark answers to (for rail linking). */
  findings: string[];
  /** Phrase to underline within the text, if any. */
  underline?: string;
}

const hasDigit = (s: string) => /[0-9%$]/.test(s);

export function weakPhraseIn(text: string): string | undefined {
  const lower = text.toLowerCase();
  return WEAK_PHRASES.find((p) => lower.includes(p));
}

/** Mark for one work/project highlight line on the ORIGINAL paper. */
export function markForHighlight(text: string, report: AtsReport | null): HighlightMark | null {
  const weak = weakPhraseIn(text);
  if (weak && checkFailed(report, "weak-language"))
    return { tone: "red", findings: ["weak-language"], underline: weak };
  if (!hasDigit(text) && checkFailed(report, "quantification"))
    return { tone: "amber", findings: ["quantification"] };
  return null;
}

export function checkFailed(report: AtsReport | null, id: string): boolean {
  return !!report?.checks.find((c) => c.id === id && !c.passed);
}

/** Parse a change-log path like "work[0].highlights[2]" into an
 * addressable location. Unparseable paths stay rail-only. */
export interface ChangeLoc {
  section: "basics" | "work" | "skills" | "projects";
  field?: string;
  workIndex?: number;
  highlightIndex?: number;
}

export function parseWhere(where: string): ChangeLoc | null {
  let m = where.match(/^work\[(\d+)\]\.highlights\[(\d+)\]$/);
  if (m) return { section: "work", workIndex: +m[1], highlightIndex: +m[2] };
  m = where.match(/^work\[(\d+)\]\.(\w+)$/);
  if (m) return { section: "work", workIndex: +m[1], field: m[2] };
  m = where.match(/^basics\.(\w+)$/);
  if (m) return { section: "basics", field: m[1] };
  if (where === "skills" || where.startsWith("skills[")) return { section: "skills" };
  if (where.startsWith("projects")) return { section: "projects" };
  return null;
}

/** Index tailored changes by location for O(1) lookup while rendering. */
export function changeIndex(changes: ResumeChange[]) {
  const byHighlight = new Map<string, ResumeChange>();
  const byField = new Map<string, ResumeChange>();
  let skills: ResumeChange | undefined;
  for (const c of changes) {
    const loc = parseWhere(c.where);
    if (!loc) continue;
    if (loc.section === "work" && loc.highlightIndex !== undefined)
      byHighlight.set(`${loc.workIndex}:${loc.highlightIndex}`, c);
    else if (loc.section === "work" && loc.field)
      byField.set(`work${loc.workIndex}.${loc.field}`, c);
    else if (loc.section === "basics" && loc.field)
      byField.set(`basics.${loc.field}`, c);
    else if (loc.section === "skills" && !skills) skills = c;
  }
  return { byHighlight, byField, skills };
}

/** An honesty note that names a field belongs ON that line of the paper.
 * Notes without a path (standing guards, document-wide observations) have
 * no home there and stay in the list. */
export interface PaperNote { index: number; text: string; path: string; }

const NOTE_PATH_RE = /(basics\.(?:summary|label)|work\[\d+\]\.summary|work\[\d+\]\.highlights\[\d+\]|projects\[\d+\]\.description|projects\[\d+\]\.highlights\[\d+\])/;

/** Field paths are addresses for the code, noise for the reader: the note
 * is already pinned to the line it names. Strip the path, the machine
 * prefix, and the echo of the sentence the reader can see anyway. */
export function displayNote(text: string): string {
  let out = text
    // machine prefix the tailor now emits: [work[0].highlights[1]]
    .replace(/^\s*\[(?:basics|work|projects)[^\]]*\]\s*/, "")
    // "[METRIC] in work[1].highlights[0]:" reads as jargon; name the problem
    .replace(/^\[METRIC\][^:]*:\s*/i, "Missing number: ")
    // any remaining address, with the connector word that introduced it
    .replace(/(?:,|\s)*\b(?:at|in|on|for|vs\.?)?\s*(?:basics\.\w+|work\[\d+\](?:\.(?:summary|highlights\[\d+\]))?|projects\[\d+\](?:\.(?:description|highlights\[\d+\]))?|highlights\[\d+\])/g, " ")
    // parenthesised addresses and the echo of the sentence being read
    .replace(/\(\s*(?:highlights\[\d+\]|work\[\d+\][^)]*)\s*\)/g, "")
    .replace(/\(\s*\)/g, "")
    .replace(/\s{2,}/g, " ")
    .replace(/\s+([:,.])/g, "$1")
    .trim();
  // "Ambiguous metric ("the whole bullet"): why" — the reader sees the bullet.
  out = out.replace(/^([A-Z][A-Za-z ]{2,30}?)\s*\(\s*["'“][^"'”]{12,}["'”]\s*\)\s*:\s*/, "$1: ");
  out = out.replace(/^[\s,:;-]+/, "");
  return out.charAt(0).toUpperCase() + out.slice(1);
}

/** The leading clause of a note is its kind ("Ambiguous metric",
 * "Weak metric"): worth showing as a chip, not repeated in the body. */
export function noteHeadline(text: string): { title: string; body: string } {
  const clean = displayNote(text);
  const m = /^([A-Z][A-Za-z ]{2,30}?):\s/.exec(clean);
  if (!m) return { title: "Needs your input", body: clean };
  return { title: m[1].trim(), body: clean.slice(m[0].length).trim() };
}

export function noteIndex(warnings: string[]) {
  const byPath = new Map<string, PaperNote[]>();
  const unplaced: PaperNote[] = [];
  warnings.forEach((text, index) => {
    const prefix = /^\s*\[((?:basics|work|projects)[^\]]*)\]/.exec(text);
    const m = prefix ?? NOTE_PATH_RE.exec(text);
    if (!m) { unplaced.push({ index, text, path: "" }); return; }
    const path = m[1];
    byPath.set(path, [...(byPath.get(path) ?? []), { index, text, path }]);
  });
  return { byPath, unplaced };
}

export const METRIC_TOKEN = "[METRIC]";

/** Occurrence order MUST mirror the server traversal
 * (resume_studio_worker._metric_fields): basics.summary, work summaries +
 * highlights, projects, skills keywords, certificates. */
export function countMetrics(s: StructuredResume): number {
  const all: string[] = [
    s.basics.summary,
    ...s.work.flatMap((w) => [w.summary, ...w.highlights]),
    ...s.projects.flatMap((p) => [p.description, ...p.highlights]),
    ...s.skills.flatMap((k) => k.keywords),
    ...s.certificates,
  ];
  return all.reduce(
    (n, t) => n + (t.split(METRIC_TOKEN).length - 1), 0);
}
