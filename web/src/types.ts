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
export interface CustomSection { name: string; items: string[] }

export interface StructuredResume {
  basics: ResumeBasics;
  work: ResumeWorkItem[];
  education: ResumeEducationItem[];
  skills: ResumeSkill[];
  projects: ResumeProject[];
  certificates: string[];
  /** Sections the user made, which the JSON Resume standard has no field
   * for. Optional here because documents saved before this existed. */
  custom?: CustomSection[];
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
  note?: string;
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
  tailored_history: TailoredResume[];
  created_at: string;
}

export interface ChatTurn { role: "user" | "assistant"; content: string; }

export interface ResumeSummaryItem {
  resume_id: string; name: string; jd_label: string;
  score: number | null; tailored_score: number | null; created_at: string;
}

export interface JobProfileSummary {
  profile_id: string; role_title: string; company: string;
  location: string; seniority: string; created_at: string;
}

/* ── Studio shell + Floor ── */

export interface ArticleSummary {
  id: string; title: string; topic: string; level: string;
  generated_at: string; available_levels: string[]; version: number;
}

export interface InterviewSessionItem {
  session_id: string; article_id: string | null; topic: string;
  level: string; mode: string; job_profile_id: string | null;
  created_at: string;
  complete: boolean; answered: number; total: number;
  average_score: number | null;
}

export interface TopicStats {
  topic: string; sessions: number; average_score: number | null; mastery: number;
}

export interface InterviewStats {
  total_sessions: number; completed_sessions: number; total_answered: number;
  average_score: number | null; per_topic: TopicStats[];
  recent_scores: number[]; streak_days: number;
}

export interface SettingsInfo {
  resolved_provider: string; provider_auto: boolean; active_cli: string;
  has_search: boolean;
}

/* ── Job Room ── */

export interface Competency {
  name: string; why_it_matters: string;
  evidence_in_resume: "strong" | "partial" | "missing";
  probe_note: string;
}
export interface JobAnalysis {
  competencies: Competency[]; resume_highlights: string[];
  gaps: string[]; company_context: string;
}
export interface JobProfile {
  profile_id: string; role_title: string; company: string; location: string;
  seniority: string; job_description: string; resume_text: string;
  extra_notes: string; created_at: string;
}
export interface JobProfileDetail { profile: JobProfile; analysis: JobAnalysis; }

export interface CompetencyScore {
  name: string; score: number | null; band: string;
  evidence: string[]; gaps: string[];
}
export interface StudyResource {
  competency: string; title: string; url: string; trust_score: number;
}
export interface JobScorecard {
  competency_scores: CompetencyScore[];
  requirement_coverage: Array<{ requirement: string; status: string }>;
  hire_signal: string; debrief: string; study_plan: StudyResource[];
}
export interface InterviewDetail {
  session_id: string; topic: string; level: string; mode: string;
  summary: {
    answered: number; average_score: number | null; debrief: string;
    scorecard: JobScorecard | null;
  } | null;
}

/* ── Interview Room ── */

export interface AnswerEvaluation {
  score: number; verdict: string; strengths: string[]; gaps: string[];
  misconceptions: string[]; suggestions: string[]; section_pointers: string[];
  needs_followup: boolean; followup_question: string;
}
export interface InterviewQuestionPublic {
  id: string; question: string; difficulty: string; section_anchor: string;
  status: "pending" | "awaiting_followup" | "completed" | "skipped";
  first_answer: string | null; first_evaluation: AnswerEvaluation | null;
  followup_question: string | null; followup_answer: string | null;
  final_evaluation: AnswerEvaluation | null;
  model_answer: string | null; rubric_key_points: string[] | null;
  final_score: number | null; predicted_score: number | null;
}
export interface InterviewSummaryFull {
  total_questions: number; answered: number; skipped: number;
  average_score: number | null;
  per_question: Array<{ id: string; question: string; final_score: number | null; status: string }>;
  weak_sections: string[]; top_gaps: string[];
  calibration_gap: number | null; debrief: string;
  scorecard: JobScorecard | null;
}
export interface CodingProblemPublic {
  title: string; statement: string; language: string; signature: string;
  stated_constraints: string[];
  unstated_constraints: string[] | null;
  optimal_complexity: string | null;
  model_solution: string | null;
}

export interface InterviewSessionPublic {
  session_id: string; article_id: string | null; topic: string; level: string;
  mode: string; job_profile_id: string | null; duration_minutes: number;
  created_at: string; updated_at: string; complete: boolean;
  questions: InterviewQuestionPublic[];
  coding_problem?: CodingProblemPublic | null;
  summary: InterviewSummaryFull | null;
}
export interface InterviewAnswerResponse {
  question: InterviewQuestionPublic;
  evaluation: AnswerEvaluation | null;
  followup_question: string | null;
  session_complete: boolean;
  summary: InterviewSummaryFull | null;
}

/* ── Article Studio ── */

export interface ClarificationQuestion { id: string; question: string; options: string[]; }
export interface GenerateResponse {
  job_id: string | null; clarification_required: boolean;
  questions: ClarificationQuestion[]; default_if_skipped: string;
}
export interface ProgressEvent {
  type: string; stage: string; message: string; timestamp: string;
  data: Record<string, unknown>;
}
export interface ArticleDetail {
  id: string; title: string; topic: string; level: string;
  generated_at: string; available_levels: string[]; markdown: string;
  request: Record<string, unknown>; version: number;
}

/* ── Back Office ── */

export interface KeyStatus {
  key: string; description: string; present: boolean;
  masked_value: string; plain: boolean;
}
export interface SettingsFull {
  keys: KeyStatus[]; resolved_provider: string; provider_auto: boolean;
  provider_preference: string; has_search: boolean;
  has_anthropic: boolean; has_openai: boolean; has_claude_cli: boolean;
  active_cli: string; detected_clis: string[];
}
