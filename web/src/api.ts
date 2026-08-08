import { useEffect, useRef, useState } from "react";
import type {
  ArticleSummary, InterviewDetail, InterviewSessionItem, InterviewStats,
  JobProfileDetail, JobProfileSummary, ResumeDoc, ResumeSummaryItem, SettingsInfo,
} from "./types";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  listResumes: () => fetch("/resumes").then((r) => json<ResumeSummaryItem[]>(r)),
  getResume: (id: string) => fetch(`/resumes/${id}`).then((r) => json<ResumeDoc>(r)),
  createResume: (body: {
    resume_text?: string; resume_file_b64?: string; resume_filename?: string;
    jd_text?: string; jd_url?: string; job_profile_id?: string | null;
  }) =>
    fetch("/resumes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => json<ResumeDoc>(r)),
  tailor: (id: string) =>
    fetch(`/resumes/${id}/tailor`, { method: "POST" }).then((r) => json<ResumeDoc>(r)),
  fillMetrics: (id: string, values: string[]) =>
    fetch(`/resumes/${id}/fill-metrics`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    }).then((r) => json<ResumeDoc>(r)),
  deleteResume: (id: string) => fetch(`/resumes/${id}`, { method: "DELETE" }),
  listJobProfiles: () =>
    fetch("/job-profiles").then((r) => json<JobProfileSummary[]>(r)),
  downloadUrl: (id: string, fmt: string, version: "original" | "tailored") =>
    `/resumes/${id}/download?fmt=${fmt}&version=${version}`,
  listArticles: () => fetch("/articles").then((r) => json<ArticleSummary[]>(r)),
  listInterviews: () =>
    fetch("/interviews").then((r) => json<InterviewSessionItem[]>(r)),
  getInterview: (id: string) =>
    fetch(`/interviews/${id}`).then((r) => json<InterviewDetail>(r)),
  interviewStats: () =>
    fetch("/interviews/stats").then((r) => json<InterviewStats>(r)),
  settings: () => fetch("/settings").then((r) => json<SettingsInfo>(r)),
  getJobProfile: (id: string) =>
    fetch(`/job-profiles/${id}`).then((r) => json<JobProfileDetail>(r)),
  createJobProfile: (body: {
    role_title: string; company?: string; location?: string; seniority?: string;
    extra_notes?: string; job_description?: string; jd_url?: string;
    resume_text?: string; resume_file_b64?: string; resume_filename?: string;
  }) =>
    fetch("/job-profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => json<JobProfileDetail>(r)),
  deleteJobProfile: (id: string) =>
    fetch(`/job-profiles/${id}`, { method: "DELETE" }),
};

/** Poll the doc every 2.5s while `active`; hand every fresh doc to the
 * caller. The elapsed clock ticks every second for the progress UI. */
export function useDocWatch(
  resumeId: string | null,
  active: boolean,
  onDoc: (doc: ResumeDoc) => void,
) {
  const [elapsed, setElapsed] = useState(0);
  const onDocRef = useRef(onDoc);
  onDocRef.current = onDoc;

  useEffect(() => {
    if (!resumeId || !active) return;
    setElapsed(0);
    const startedAt = Date.now();
    const clock = setInterval(
      () => setElapsed(Math.floor((Date.now() - startedAt) / 1000)),
      1000,
    );
    const poll = setInterval(async () => {
      try {
        const doc = await api.getResume(resumeId);
        onDocRef.current(doc);
      } catch {
        /* transient network blip: keep polling; a deleted doc surfaces as
           an error on the next user action instead of a silent stop */
      }
    }, 2500);
    return () => { clearInterval(clock); clearInterval(poll); };
  }, [resumeId, active]);

  return elapsed;
}

export const fmtElapsed = (s: number) =>
  `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

/* ── Interview Room ── */
import type {
  ArticleDetail, GenerateResponse, InterviewAnswerResponse,
  InterviewSessionPublic, SettingsFull,
} from "./types";

export const interviewApi = {
  create: (body: {
    topic?: string; article_id?: string; level?: string; num_questions?: number;
    mode?: string; job_profile_id?: string; duration_minutes?: number;
  }) =>
    fetch("/interviews", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.ok ? r.json() as Promise<InterviewSessionPublic>
      : r.json().then((b) => Promise.reject(new Error(b.detail || `HTTP ${r.status}`)))),
  get: (id: string) =>
    fetch(`/interviews/${id}`).then((r) => r.json() as Promise<InterviewSessionPublic>),
  answer: (id: string, body: {
    question_id: string; answer?: string; skip?: boolean; predicted_score?: number | null;
  }) =>
    fetch(`/interviews/${id}/answers`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.ok ? r.json() as Promise<InterviewAnswerResponse>
      : r.json().then((b) => Promise.reject(new Error(b.detail || `HTTP ${r.status}`)))),
};

export const articleApi = {
  generate: (body: Record<string, unknown>) =>
    fetch("/generate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.ok ? r.json() as Promise<GenerateResponse>
      : r.json().then((b) => Promise.reject(new Error(b.detail || `HTTP ${r.status}`)))),
  detail: (id: string, level?: string) =>
    fetch(`/articles/${id}${level ? `?level=${level}` : ""}`)
      .then((r) => r.json() as Promise<ArticleDetail>),
  streamUrl: (jobId: string) => `/jobs/${jobId}/stream`,
};

export const settingsApi = {
  full: () => fetch("/settings").then((r) => r.json() as Promise<SettingsFull>),
  patch: (updates: Record<string, string>) =>
    fetch("/settings", {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ updates }),
    }).then((r) => r.ok ? r.json()
      : r.json().then((b) => Promise.reject(new Error(b.detail || `HTTP ${r.status}`)))),
};
