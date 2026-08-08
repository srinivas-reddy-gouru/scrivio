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

  return (
    <div className="room-wrap">
      <h1 className="room-title bar-tick-left">The studio floor</h1>
      <p className="room-sub">Where you left off, and what is on your desk.</p>
      <div className="floor-grid">
        <div>
          <p className="eyebrow">On your desk</p>
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
                Nothing on the desk yet. Start below; your work lands here as it happens.
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
          <button className="btn btn-quiet" onClick={() => go("job")}>Open the Job Room</button>
        </div>
      </div>
    </div>
  );
}
