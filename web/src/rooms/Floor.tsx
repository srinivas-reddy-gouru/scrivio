/** The Floor: a workspace dashboard. The mini-papers on the desk are the
 * user's real artifacts, and they ARE the navigation. */
import { useEffect, useState } from "react";
import { api } from "../api";
import { scoreTone } from "../components/stations";
import type { RoomId } from "../components/Shell";
import type {
  ArticleSummary, InterviewSessionItem, InterviewStats, ResumeSummaryItem,
} from "../types";

export function Floor({ go }: { go: (r: RoomId) => void }) {
  const [resumes, setResumes] = useState<ResumeSummaryItem[]>([]);
  const [articles, setArticles] = useState<ArticleSummary[]>([]);
  const [sessions, setSessions] = useState<InterviewSessionItem[]>([]);
  const [stats, setStats] = useState<InterviewStats | null>(null);

  useEffect(() => {
    api.listResumes().then(setResumes).catch(() => {});
    api.listArticles().then(setArticles).catch(() => {});
    api.listInterviews().then(setSessions).catch(() => {});
    api.interviewStats().then(setStats).catch(() => {});
  }, []);

  const lastResume = resumes[0];
  const lastArticle = articles[0];
  const lastSession = sessions[0];
  const mastered = stats?.per_topic.filter((t) => t.mastery >= 80).length ?? 0;
  const lastScore = stats?.recent_scores.at(-1);
  const weakest = stats?.per_topic.length
    ? [...stats.per_topic].sort((a, b) => a.mastery - b.mastery)[0]
    : null;
  let paperIndex = 0;

  const STUDIOS: Array<{
    room: RoomId; name: string; what: string; points: string[]; cta: string;
    icon: React.ReactNode; badge?: string;
  }> = [
    { room: "newsroom", name: "Articles", cta: "Write an article",
      what: "Deep technical articles researched from trusted sources, fact-checked claim by claim, written at your reading level.",
      points: ["Live web research with cited sources", "Every claim verified before it ships", "Basic, intermediate, or advanced depth"],
      icon: <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" /></svg> },
    { room: "interview", name: "Interviews", cta: "Start practicing",
      what: "Voice interviews on any topic. The interviewer speaks, you answer out loud, and the grading never flatters.",
      points: ["Practice, simulation, and 60s drills", "Hidden rubric: feedback you can trust", "Mastery tracking, streaks, weak-spot drills"],
      icon: <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2" /><line x1="12" y1="19" x2="12" y2="23" /><line x1="8" y1="23" x2="16" y2="23" /></svg> },
    { room: "job", name: "Job prep", cta: "Prep for a job",
      what: "Upload your resume and the job description to get a realistic 30-45 minute screen and a recruiter-grade scorecard.",
      points: ["Questions real interviewers ask for the role", "Grills your resume's own claims", "Hire signal plus a cited study plan for gaps"],
      icon: <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><rect x="2" y="7" width="20" height="14" rx="2" /><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" /></svg> },
    { room: "desk", name: "Resume", cta: "Check my resume",
      what: "A transparent ATS readiness report, every point explained, then an honest rewrite tailored to the job description.",
      points: ["Explainable score, not a magic number", "Tailoring that refuses to invent facts", "PDF, Word, Markdown, JSON Resume exports"],
      icon: <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="9" y1="13" x2="15" y2="13" /><line x1="9" y1="17" x2="13" y2="17" /></svg> },
  ];

  return (
    <div className="room-wrap">
      <div className="home-hero">
        <h1>Learn it. <span className="grad-text">Prove it.</span> Get the job.</h1>
        <div className="bar" />
        <p>
          Scrivio researches like a journalist, interviews like a senior engineer,
          and gives feedback like a hiring panel, all on your own AI subscription.
        </p>
      </div>
      <div className="feature-grid">
        {STUDIOS.map((s, i) => (
          <button key={s.room} className="feature-card" style={{ "--i": i } as React.CSSProperties}
            onClick={() => go(s.room)}>
            {s.badge && <span className="feature-badge">{s.badge}</span>}
            <span className="feature-icon">{s.icon}</span>
            <h3>{s.name}</h3>
            <span className="desc">{s.what}</span>
            <ul>{s.points.map((p) => <li key={p}>✓ {p}</li>)}</ul>
            <span className="cta">{s.cta}</span>
          </button>
        ))}
      </div>
      <div className="floor-grid">
        <div>
          <p className="eyebrow">Pick up where you left off</p>
          <div className="desk-row">
            {lastResume && (
              <button className="floor-paper" style={{ "--i": paperIndex++ } as React.CSSProperties}
                onClick={() => go("desk")}>
                <span className="tag">Resume{lastResume.tailored_score != null ? " · tailored" : ""}</span>
                {lastResume.score != null && (
                  <span className="pill" style={{ color: "#1A7A4E" }}>
                    {lastResume.score}{lastResume.tailored_score != null ? ` → ${lastResume.tailored_score}` : ""}
                  </span>
                )}
                <b>{lastResume.name || "Resume report"}</b>
                {lastResume.jd_label || "no target JD"}
              </button>
            )}
            {lastArticle && (
              <button className="floor-paper" style={{ "--i": paperIndex++ } as React.CSSProperties}
                onClick={() => go("newsroom")}>
                <span className="tag">Article · {lastArticle.level}</span>
                {lastArticle.version > 1 && <span className="pill" style={{ color: "#8A6420" }}>v{lastArticle.version}</span>}
                <b>{lastArticle.title || lastArticle.topic}</b>
                Read it, or spin a practice interview from it
              </button>
            )}
            {lastSession && (
              <button className="floor-paper" style={{ "--i": paperIndex++ } as React.CSSProperties}
                onClick={() => go(lastSession.mode === "job" ? "job" : "interview")}>
                <span className="tag">
                  {lastSession.mode === "job" ? "Job screen" : `Session · ${lastSession.mode}`}
                </span>
                {lastSession.average_score != null && (
                  <span className="pill" style={{ color: "#B0552F" }}>{lastSession.average_score}/10</span>
                )}
                <b>{lastSession.topic}</b>
                {lastSession.complete
                  ? `${lastSession.answered}/${lastSession.total} answered`
                  : `in progress: ${lastSession.answered}/${lastSession.total}`}
              </button>
            )}
            {paperIndex === 0 && (
              <p className="classic-note">
                Nothing yet. Open a studio above; your work lands here as it happens.
              </p>
            )}
          </div>
          {stats && stats.total_sessions > 0 && (
            <div className="stat-strip">
              <div>
                <span className="v" style={{ color: "var(--amber)" }}>
                  {stats.streak_days > 0 ? `🔥 ${stats.streak_days}` : "—".replace("—", "0")}
                </span>
                <span className="l">day streak</span>
              </div>
              <div>
                <span className="v" style={{ color: "var(--teal)" }}>{mastered}</span>
                <span className="l">topics ≥80 mastery</span>
              </div>
              {lastScore != null && (
                <div>
                  <span className="v" style={{ color: scoreTone(lastScore * 10) }}>{lastScore}</span>
                  <span className="l">last session avg</span>
                </div>
              )}
              <div>
                <span className="v">{stats.total_answered}</span>
                <span className="l">questions answered</span>
              </div>
            </div>
          )}
        </div>
        <div className="quick">
          <p className="eyebrow">Start something</p>
          {weakest && weakest.mastery < 80 ? (
            <button className="btn" onClick={() => go("interview")}>▶ Drill your weak spot: {weakest.topic}</button>
          ) : (
            <button className="btn" onClick={() => go("interview")}>▶ Practice an interview</button>
          )}
          <button className="btn btn-quiet" onClick={() => go("desk")}>Check a resume</button>
          <button className="btn btn-quiet" onClick={() => go("newsroom")}>Write an article</button>
          <button className="btn btn-quiet" onClick={() => go("job")}>Prep for a job</button>
        </div>
      </div>
    </div>
  );
}
