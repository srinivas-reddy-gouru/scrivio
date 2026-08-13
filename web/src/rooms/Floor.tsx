/** Home: what to do next, then the four studios with their real state.
 *
 * The rule this page follows: propose the next action rather than
 * displaying data and leaving the reader to work it out. One computed
 * "do this next" band, four studio cards that double as status, and a
 * recent list that says what each item still needs. No second column of
 * shortcuts duplicating the cards.
 */
import { useEffect, useState } from "react";
import { api } from "../api";
import { scoreTone } from "../components/stations";
import type { RoomId } from "../components/Shell";
import type {
  ArticleSummary, InterviewSessionItem, InterviewStats, ResumeSummaryItem,
} from "../types";

/** Job targets arrive as a pasted URL as often as a role title; a raw
 * link is noise on a dashboard, so show where it points instead. */
function prettyTarget(label: string | null | undefined): string {
  if (!label) return "";
  if (!/^https?:\/\//i.test(label)) return label;
  try {
    return `${new URL(label).hostname.replace(/^www\./, "")} posting`;
  } catch { return "linked posting"; }
}

interface NextAction {
  eyebrow: string; headline: string; detail: string;
  cta: string; room: RoomId; onGo?: () => void;
}

export function Floor({ go }: { go: (r: RoomId) => void }) {
  const [resumes, setResumes] = useState<ResumeSummaryItem[]>([]);
  const [articles, setArticles] = useState<ArticleSummary[]>([]);
  const [sessions, setSessions] = useState<InterviewSessionItem[]>([]);
  const [stats, setStats] = useState<InterviewStats | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    Promise.allSettled([
      api.listResumes().then(setResumes),
      api.listArticles().then(setArticles),
      api.listInterviews().then(setSessions),
      api.interviewStats().then(setStats),
    ]).then(() => setLoaded(true));
  }, []);

  const lastResume = resumes[0];
  const lastArticle = articles[0];
  const openScreen = sessions.find((s) => s.mode === "job" && !s.complete);
  const openSessionItem = sessions.find((s) => s.mode !== "job" && !s.complete);
  const weakest = stats?.per_topic.length
    ? [...stats.per_topic].sort((a, b) => a.mastery - b.mastery)[0]
    : null;
  const nothingYet = loaded && !lastResume && !lastArticle && !sessions.length;

  /* One next action, chosen the way a colleague would: finish what is
   * open before starting anything new. */
  const nextAction = (): NextAction => {
    if (openScreen) return {
      eyebrow: "Unfinished", headline: `Finish your ${openScreen.topic} screen`,
      detail: `${openScreen.answered} of ${openScreen.total} questions answered. The rubric is already sealed and waiting.`,
      cta: "Resume the screen", room: "job",
    };
    if (openSessionItem) return {
      eyebrow: "Unfinished", headline: `Finish your ${openSessionItem.topic} session`,
      detail: `${openSessionItem.answered} of ${openSessionItem.total} answered. Pick up at the next question.`,
      cta: "Resume practice", room: "interview",
    };
    if (lastResume && lastResume.tailored_score == null && lastResume.jd_label) return {
      eyebrow: "One step left", headline: "Tailor your resume to that job description",
      detail: `${lastResume.name || "Your resume"} scored ${lastResume.score ?? "-"} against the ${prettyTarget(lastResume.jd_label)}. Tailoring rewrites it honestly for that posting.`,
      cta: "Open the resume", room: "desk",
    };
    if (lastResume?.tailored_score != null) return {
      eyebrow: "In progress", headline: "Answer the notes on your tailored resume",
      detail: `Score is ${lastResume.score ?? "-"} to ${lastResume.tailored_score}. The amber lines are claims that still need your real numbers.`,
      cta: "Open the resume", room: "desk",
    };
    if (weakest && weakest.mastery < 80) return {
      eyebrow: "Weakest topic", headline: `Drill ${weakest.topic}`,
      detail: `Mastery is ${Math.round(weakest.mastery)} out of 100 across ${weakest.sessions} session${weakest.sessions === 1 ? "" : "s"}. Sixty seconds a question, no notes.`,
      cta: "Start the drill", room: "interview",
    };
    return {
      eyebrow: "Start here", headline: "Check your resume against a real posting",
      detail: "Paste your resume and a job description. You get an explainable ATS score first, then an honest rewrite you can defend line by line.",
      cta: "Check my resume", room: "desk",
    };
  };
  const next = nextAction();

  const STUDIOS: Array<{
    room: RoomId; name: string; what: string; state: string; cta: string; icon: React.ReactNode;
  }> = [
    {
      room: "desk", name: "Resume", cta: lastResume ? "Open the desk" : "Check my resume",
      what: "An explainable ATS score, then a rewrite that refuses to invent facts.",
      state: lastResume
        ? `Last: ${lastResume.score ?? "-"}${lastResume.tailored_score != null ? ` to ${lastResume.tailored_score}` : ""}${lastResume.jd_label ? ` vs ${prettyTarget(lastResume.jd_label)}` : ""}`
        : "Nothing checked yet",
      icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="9" y1="13" x2="15" y2="13" /><line x1="9" y1="17" x2="13" y2="17" /></svg>,
    },
    {
      room: "job", name: "Job prep", cta: "Open job prep",
      what: "A role-specific mock screen and a recruiter-grade scorecard.",
      state: sessions.some((s) => s.mode === "job")
        ? `${sessions.filter((s) => s.mode === "job").length} screen${sessions.filter((s) => s.mode === "job").length === 1 ? "" : "s"} taken`
        : "No targets yet",
      icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><rect x="2" y="7" width="20" height="14" rx="2" /><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" /></svg>,
    },
    {
      room: "interview", name: "Interviews", cta: "Start practicing",
      what: "Spoken practice graded against a rubric written before you answer.",
      state: stats?.total_sessions
        ? `${stats.total_sessions} session${stats.total_sessions === 1 ? "" : "s"}${stats.average_score != null ? ` · avg ${stats.average_score}/10` : ""}`
        : "No sessions yet",
      icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2" /><line x1="12" y1="19" x2="12" y2="23" /><line x1="8" y1="23" x2="16" y2="23" /></svg>,
    },
    {
      room: "newsroom", name: "Articles", cta: "Write an article",
      what: "Sourced technical writing, every claim verified before it ships.",
      state: articles.length ? `${articles.length} in the library` : "Nothing written yet",
      icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" /></svg>,
    },
  ];

  const recent: Array<{ tag: string; title: string; status: string; room: RoomId; when: string }> = [];
  if (lastResume) recent.push({
    tag: "Resume", title: lastResume.name || "Resume report",
    status: lastResume.tailored_score != null
      ? `Tailored, ${lastResume.score ?? "-"} to ${lastResume.tailored_score}`
      : `Scored ${lastResume.score ?? "-"}, not tailored yet`,
    room: "desk", when: prettyTarget(lastResume.jd_label) || "no target posting",
  });
  if (sessions[0]) recent.push({
    tag: sessions[0].mode === "job" ? "Job screen" : "Practice",
    title: sessions[0].topic,
    status: sessions[0].complete
      ? `Complete, avg ${sessions[0].average_score ?? "-"}/10`
      : `In progress, ${sessions[0].answered} of ${sessions[0].total}`,
    room: sessions[0].mode === "job" ? "job" : "interview",
    when: sessions[0].mode,
  });
  if (lastArticle) recent.push({
    tag: "Article", title: lastArticle.title || lastArticle.topic,
    status: `${lastArticle.level}${lastArticle.version > 1 ? `, v${lastArticle.version}` : ""}`,
    room: "newsroom", when: `${lastArticle.available_levels.length} level${lastArticle.available_levels.length === 1 ? "" : "s"}`,
  });

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

      {/* One computed next step: the page proposes, it does not just report. */}
      <button className="next-action" onClick={() => go(next.room)}>
        <span className="na-body">
          <span className="eyebrow eyebrow-accent">{next.eyebrow}</span>
          <b>{next.headline}</b>
          <span className="na-detail">{next.detail}</span>
        </span>
        <span className="na-cta">{next.cta} →</span>
      </button>

      <p className="eyebrow" style={{ marginTop: "2rem" }}>The four studios</p>
      <div className="studio-grid">
        {STUDIOS.map((s, i) => (
          <button key={s.room} className="studio-tile" style={{ "--i": i } as React.CSSProperties}
            onClick={() => go(s.room)}>
            <span className="st-icon">{s.icon}</span>
            <b>{s.name}</b>
            <span className="st-what">{s.what}</span>
            <span className="st-state">{s.state}</span>
            <span className="st-cta">{s.cta} →</span>
          </button>
        ))}
      </div>

      {recent.length > 0 && (
        <>
          <p className="eyebrow" style={{ marginTop: "2rem" }}>Recent work</p>
          <div className="recent-grid">
            {recent.map((r, i) => (
              <button key={r.tag + i} className="recent-row" onClick={() => go(r.room)}>
                <span className="rr-tag">{r.tag}</span>
                <span className="rr-main">
                  <b>{r.title}</b>
                  <span>{r.status}</span>
                </span>
                <span className="rr-when">{r.when}</span>
                <span className="rr-go" aria-hidden="true">→</span>
              </button>
            ))}
          </div>
        </>
      )}

      {stats && stats.total_sessions > 0 && (
        <div className="stat-strip" style={{ marginTop: "1.4rem" }}>
          <div>
            <span className="v" style={{ color: "var(--amber)" }}>
              {stats.streak_days > 0 ? `${stats.streak_days}` : "0"}
            </span>
            <span className="l">day streak</span>
          </div>
          <div>
            <span className="v" style={{ color: "var(--teal)" }}>
              {stats.per_topic.filter((t) => t.mastery >= 80).length}
            </span>
            <span className="l">topics mastered</span>
          </div>
          {stats.recent_scores.at(-1) != null && (
            <div>
              <span className="v" style={{ color: scoreTone((stats.recent_scores.at(-1) as number) * 10) }}>
                {stats.recent_scores.at(-1)}
              </span>
              <span className="l">last session avg</span>
            </div>
          )}
          <div>
            <span className="v">{stats.total_answered}</span>
            <span className="l">questions answered</span>
          </div>
        </div>
      )}

      {nothingYet && (
        <p className="classic-note" style={{ marginTop: "1.2rem" }}>
          Nothing on file yet. Whatever you start first shows up here with
          what it still needs.
        </p>
      )}
    </div>
  );
}
