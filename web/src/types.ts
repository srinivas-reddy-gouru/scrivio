/** Mirrors pipeline/schemas/models.py — the resume-studio slice only. */

export interface ResumeBasics {
  name: string; label: string; email: string; phone: string;
  url: string; location: string; summary: string;
}
export interface ResumeWorkItem {
  name: string; position: string; startDate: string; endDate: string;
  summary: string; highlights: string[];
}
export interface ResumeEducationItem {
  institution: string; area: string; studyType: string;
  startDate: string; endDate: string; score: string;
}
export interface ResumeSkill { name: string; keywords: string[]; }
export interface ResumeProject {
  name: string; description: string; url: string; highlights: string[];
}
export interface StructuredResume {
  basics: ResumeBasics;
  work: ResumeWorkItem[];
  education: ResumeEducationItem[];
  skills: ResumeSkill[];
  projects: ResumeProject[];
  certificates: string[];
}

export interface AtsCheck {
  id: string; label: string; passed: boolean; weight: number; detail: string;
}
export interface KeywordCoverage {
  found: string[]; missing: string[]; percent: number;
}
export interface AtsReport {
  score: number; checks: AtsCheck[]; keyword_coverage: KeywordCoverage | null;
}
export interface ResumeIssue { category: string; detail: string; fix: string; }
export interface ResumeReview {
  strengths: string[]; issues: ResumeIssue[];
  missing_keywords: string[]; summary: string;
}
export interface ResumeChange { kind: string; where: string; what: string; }
export interface TailoredResume {
  resume: StructuredResume; changes: ResumeChange[]; warnings: string[];
}

export interface ResumeDoc {
  resume_id: string;
  original_text: string;
  status: "analyzing" | "ready" | "error";
  error: string;
  tailor_status: "idle" | "tailoring" | "error";
  tailor_error: string;
  structured: StructuredResume | null;
  jd_text: string;
  jd_label: string;
  report: AtsReport | null;
  review: ResumeReview | null;
  tailored: TailoredResume | null;
  tailored_report: AtsReport | null;
  created_at: string;
}

export interface ResumeSummaryItem {
  resume_id: string; name: string; jd_label: string;
  score: number | null; tailored_score: number | null; created_at: string;
}

export interface JobProfileSummary {
  profile_id: string; role_title: string; company: string;
  location: string; seniority: string; created_at: string;
}
