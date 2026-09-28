import { useEffect, useRef, useState } from "react";
import type {
  ArticleSummary, InterviewDetail, InterviewSessionItem, InterviewStats,
  JobProfileDetail, JobProfileSummary, ModeStatus, ResumeDoc, ResumeSummaryItem, SettingsInfo,
} from "./types";

/** An error from the API, with the status kept so a caller can tell a
 * refusal (409, 422) from an outage (502, 503) from "sign in" (401). */
export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

const FALLBACK: Record<number, string> = {
  401: "This browser is no longer paired with Scrivio. Reload the page to pair it again.",
  413: "That is too large to send.",
  429: "Too much is running at once. Wait for something to finish, then try again.",
  502: "The provider returned an error. Try again in a moment.",
  503: "No model provider is available. Check Settings.",
};

/** Every request goes through here, so a failed one is an error and
 * never data. Several helpers used to call r.json() without looking at
 * the status: a 404 body was handed to the caller as if it were the
 * article, and rendered as a blank page.
 *
 * It also catches the quiet failure in development. When the dev server
 * has no route for a path it answers with the app's own index.html and
 * status 200. That is not JSON, and it is reported as what it is. */
async function call<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const { json: body, ...rest } = init ?? {};
  let res: Response;
  try {
    res = await fetch(path, body === undefined ? rest : {
      ...rest,
      headers: { "Content-Type": "application/json", ...(rest.headers ?? {}) },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError("Could not reach the server. Is it still running?", 0);
  }
  const isJson = (res.headers.get("content-type") ?? "").includes("json");
  if (!res.ok) {
    const detail = isJson
      ? await res.json().then((b) => b?.detail).catch(() => undefined)
      : undefined;
    throw new ApiError(
      typeof detail === "string" && detail ? detail
        : FALLBACK[res.status] ?? `The request failed (HTTP ${res.status}).`,
      res.status);
  }
  if (res.status === 204) return undefined as T;
  if (!isJson) {
    throw new ApiError(
      "The server answered with a web page instead of data. The API is "
      + "probably not running, or this address is not routed to it.", res.status);
  }
  return res.json() as Promise<T>;
}

const post = <T>(path: string, json?: unknown) => call<T>(path, { method: "POST", json });

type Turn = { role: string; content: string };

export const api = {
  listResumes: () => call<ResumeSummaryItem[]>("/resumes"),
  getResume: (id: string) => call<ResumeDoc>(`/resumes/${id}`),
  createResume: (body: {
    resume_text?: string; resume_file_b64?: string; resume_filename?: string;
    jd_text?: string; jd_url?: string; job_profile_id?: string | null;
  }) => post<ResumeDoc>("/resumes", body),
  /** For a resume whose analysis failed or was cut short by a restart. */
  analyzeAgain: (id: string) => post<ResumeDoc>(`/resumes/${id}/analyze`),
  tailor: (id: string) => post<ResumeDoc>(`/resumes/${id}/tailor`),
  fillMetrics: (id: string, values: string[]) =>
    post<ResumeDoc>(`/resumes/${id}/fill-metrics`, { values }),
  editTailored: (id: string, edits: Array<{ path: string; value: string }>) =>
    post<ResumeDoc>(`/resumes/${id}/edit-tailored`, { edits }),
  /** Additions go through their own road, not editTailored: the server
   * writes them into the original structure as well, so the next tailor
   * run does not treat the user's own job as an invention. */
  addToResume: (id: string, payload: Record<string, unknown>) =>
    post<ResumeDoc>(`/resumes/${id}/add`, payload),
  removeEntry: (id: string, path: string) =>
    post<ResumeDoc>(`/resumes/${id}/remove-entry`, { path }),
  adviseResume: (id: string, question: string, history: Turn[]) =>
    post<{ answer: string }>(`/resumes/${id}/advise`, { question, history }),
  requestEdit: (id: string, instruction: string, history: Turn[] = []) =>
    post<ResumeDoc>(`/resumes/${id}/request-edit`, { instruction, history }),
  undoTailored: (id: string) => post<ResumeDoc>(`/resumes/${id}/undo-tailored`),
  deleteResume: (id: string) => call<unknown>(`/resumes/${id}`, { method: "DELETE" }),
  listJobProfiles: () => call<JobProfileSummary[]>("/job-profiles"),
  /** Exports go through fetch, not a bare link. A link cannot show why
   * the server said no: the browser would navigate to a page of JSON. */
  downloadResume: async (
    id: string, fmt: string,
    opts: { version: "original" | "tailored"; draft?: boolean; expect?: string },
  ): Promise<void> => {
    const query = new URLSearchParams({ fmt, version: opts.version });
    if (opts.draft) query.set("draft", "true");
    if (opts.expect) query.set("expect", opts.expect);
    let res: Response;
    try {
      res = await fetch(`/resumes/${id}/download?${query}`);
    } catch {
      throw new ApiError("Could not reach the server. Is it still running?", 0);
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new ApiError(
        body.detail || FALLBACK[res.status] || `The download failed (HTTP ${res.status}).`,
        res.status);
    }
    if (!res.headers.get("x-scrivio-export")) {
      throw new ApiError(
        "The server answered with something that is not a resume export.", res.status);
    }
    const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") ?? "")?.[1]
      ?? `resume.${fmt}`;
    const url = URL.createObjectURL(await res.blob());
    const link = document.createElement("a");
    link.href = url; link.download = name;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  listArticles: () => call<ArticleSummary[]>("/articles"),
  listInterviews: () => call<InterviewSessionItem[]>("/interviews"),
  getInterview: (id: string) => call<InterviewDetail>(`/interviews/${id}`),
  interviewStats: () => call<InterviewStats>("/interviews/stats"),
  settings: () => call<SettingsInfo>("/settings"),
  mode: () => call<ModeStatus>("/mode"),
  getJobProfile: (id: string) => call<JobProfileDetail>(`/job-profiles/${id}`),
  createJobProfile: (body: {
    role_title: string; company?: string; location?: string; seniority?: string;
    extra_notes?: string; job_description?: string; jd_url?: string;
    resume_text?: string; resume_file_b64?: string; resume_filename?: string;
  }) => post<JobProfileDetail>("/job-profiles", body),
  deleteJobProfile: (id: string) =>
    call<unknown>(`/job-profiles/${id}`, { method: "DELETE" }),
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

/** Hand a session to the Interview Room: the room reads the id from
 * sessionStorage on mount, so it opens seated at the first open
 * question (or the summary when the session is complete). */
export function openSession(sessionId: string) {
  sessionStorage.setItem("studio-open-session", sessionId);
  const already = window.location.hash.replace(/^#\/?/, "") === "interview";
  window.location.hash = "/interview";
  // Already in the room: no remount happens, so nudge it directly.
  if (already) window.dispatchEvent(new Event("studio-open-session"));
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
    language?: string;
  }) => post<InterviewSessionPublic>("/interviews", body),
  get: (id: string) => call<InterviewSessionPublic>(`/interviews/${id}`),
  answer: (id: string, body: {
    question_id: string; answer?: string; skip?: boolean; predicted_score?: number | null;
  }) => post<InterviewAnswerResponse>(`/interviews/${id}/answers`, body),
};

export interface JobStatus {
  status: "pending" | "complete" | "error" | "cancelled" | "interrupted";
  error?: string | null;
}

export const articleApi = {
  generate: (body: Record<string, unknown>) => post<GenerateResponse>("/generate", body),
  detail: (id: string, level?: string) =>
    call<ArticleDetail>(`/articles/${id}${level ? `?level=${level}` : ""}`),
  streamUrl: (jobId: string) => `/jobs/${jobId}/stream`,
  /** Where a job stands, asked directly. The stream is the normal way to
   * follow one; this is how to find out what happened when it goes quiet. */
  status: (jobId: string) => call<JobStatus>(`/jobs/${jobId}`),
  cancel: (jobId: string) => call<unknown>(`/jobs/${jobId}`, { method: "DELETE" }),
};

export const settingsApi = {
  full: () => call<SettingsFull>("/settings"),
  patch: (updates: Record<string, string>) =>
    call<unknown>("/settings", { method: "PATCH", json: { updates } }),
};
