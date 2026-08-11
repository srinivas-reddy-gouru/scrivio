/** The Interview Room: the question on a card, your words on the
 * notepad, and the rubric face-down on the table the whole time you
 * speak — flipping only when the answer closes. Voice = browser
 * dictation (Chrome) with typing always available; the interviewer
 * speaks via the browser's own voice, mutable. */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, interviewApi } from "../api";
import { Dial, scoreTone } from "../components/stations";
import type {
  AnswerEvaluation, InterviewAnswerResponse, InterviewSessionItem,
  InterviewSessionPublic, InterviewStats,
} from "../types";

const VERDICT_STYLE: Record<string, { color: string; bg: string }> = {
  strong: { color: "var(--green)", bg: "rgba(52,211,153,0.12)" },
  adequate: { color: "var(--amber)", bg: "rgba(229,176,76,0.14)" },
  shallow: { color: "#E09A55", bg: "rgba(224,154,85,0.14)" },
  incorrect: { color: "var(--redpen)", bg: "rgba(224,106,85,0.14)" },
};

/* ── Browser dictation (graceful: absent → typing only) ── */
function useDictation(onText: (final: string, interim: string) => void) {
  const recRef = useRef<{ stop: () => void } | null>(null);
  const [recording, setRecording] = useState(false);
  const [supported] = useState(() =>
    "webkitSpeechRecognition" in window || "SpeechRecognition" in window);

  const stop = useCallback(() => {
    recRef.current?.stop(); recRef.current = null; setRecording(false);
  }, []);

  const start = useCallback(() => {
    const Ctor = (window as unknown as Record<string, unknown>).SpeechRecognition
      || (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
    if (!Ctor) return;
    const rec = new (Ctor as new () => {
      continuous: boolean; interimResults: boolean; lang: string;
      onresult: (e: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void;
      onend: () => void; onerror: () => void;
      start: () => void; stop: () => void;
    })();
    rec.continuous = true; rec.interimResults = true; rec.lang = "en-US";
    let finals = "";
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) finals += r[0].transcript + " ";
        else interim += r[0].transcript;
      }
      onText(finals, interim);
    };
    rec.onend = () => setRecording(false);
    rec.onerror = () => setRecording(false);
    rec.start();
    recRef.current = rec; setRecording(true);
  }, [onText]);

  useEffect(() => stop, [stop]);
  return { supported, recording, start, stop };
}

const speak = (text: string) => {
  if (localStorage.getItem("studio-tts") === "off") return;
  try {
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.04;
    speechSynthesis.speak(u);
  } catch { /* voice is garnish */ }
};

/* ── Room ── */

type View =
  | { kind: "setup" }
  | { kind: "live"; session: InterviewSessionPublic; qIndex: number }
  | { kind: "summary"; session: InterviewSessionPublic };

export function InterviewRoom() {
  const [view, setView] = useState<View>({ kind: "setup" });

  // A session handed over by another room (the Job Room's "Take the
  // screen", a palette hit) arrives as an id in sessionStorage: pick it
  // up on mount, and on the nudge event when the room is already open.
  useEffect(() => {
    const pickUp = () => {
      const handed = sessionStorage.getItem("studio-open-session");
      if (!handed) return;
      sessionStorage.removeItem("studio-open-session");
      interviewApi.get(handed).then((s) => {
        setView(s.complete
          ? { kind: "summary", session: s }
          : { kind: "live", session: s, qIndex: firstOpen(s) });
      }).catch(() => {});
    };
    pickUp();
    window.addEventListener("studio-open-session", pickUp);
    return () => window.removeEventListener("studio-open-session", pickUp);
  }, []);

  return view.kind === "setup" ? (
    <Setup onStart={(s) => setView({ kind: "live", session: s, qIndex: firstOpen(s) })}
      onReview={(s) => setView(s.complete
        ? { kind: "summary", session: s }
        : { kind: "live", session: s, qIndex: firstOpen(s) })} />
  ) : view.kind === "live" ? (
    <Live session={view.session} qIndex={view.qIndex}
      onNext={(s, i) => setView({ kind: "live", session: s, qIndex: i })}
      onDone={(s) => setView({ kind: "summary", session: s })}
      onQuit={() => setView({ kind: "setup" })} />
  ) : (
    <Summary session={view.session} onBack={() => setView({ kind: "setup" })} />
  );
}

const firstOpen = (s: InterviewSessionPublic) =>
  Math.max(0, s.questions.findIndex((q) => q.status === "pending" || q.status === "awaiting_followup"));

/* ── Setup + the wall ── */

function Setup({ onStart, onReview }: {
  onStart: (s: InterviewSessionPublic) => void;
  onReview: (s: InterviewSessionPublic) => void;
}) {
  const [topic, setTopic] = useState("");
  const [level, setLevel] = useState("intermediate");
  const [mode, setMode] = useState("practice");
  const [count, setCount] = useState(5);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [stats, setStats] = useState<InterviewStats | null>(null);
  const [recent, setRecent] = useState<InterviewSessionItem[]>([]);

  useEffect(() => {
    api.interviewStats().then(setStats).catch(() => {});
    api.listInterviews().then((all) => setRecent(all.filter((s) => s.mode !== "job").slice(0, 5))).catch(() => {});
  }, []);

  const start = async () => {
    setBusy(true); setError("");
    try {
      const s = await interviewApi.create({
        topic: topic.trim() || undefined, level, mode, num_questions: count,
      });
      onStart(s);
    } catch (e) { setError((e as Error).message); setBusy(false); }
  };

  const MODES: Array<[string, string]> = [
    ["practice", "Practice · coach at the table"],
    ["simulation", "Simulation · cards stay down"],
    ["drill", "Drill · 60s a question"],
  ];

  if (busy) {
    return (
      <div className="room-wrap"><div className="working">
        <p className="line font-display" style={{ fontSize: "1.1rem" }}>
          The interviewer is writing the rubric…
        </p>
        <p className="line" style={{ fontSize: "0.76rem" }}>
          Questions and ideal answers are sealed before you speak. That is the bar.
        </p>
      </div></div>
    );
  }

  return (
    <div className="room-wrap">
      <h1 className="room-title bar-tick-left">Interviews</h1>
      <p className="room-sub">The rubric is written before you speak, and sits face-down until your answer closes.</p>
      <div className="iv-grid">
        <div className="iv-setup">
          <p className="eyebrow">Topic</p>
          <input type="text" value={topic} onChange={(e) => setTopic(e.target.value)}
            placeholder={stats?.per_topic.length
              ? `Empty = drill your weak spots (${[...stats.per_topic].sort((a, b) => a.mastery - b.mastery)[0]?.topic})`
              : "Kafka consumer groups, Python's GIL, anything"} />
          <p className="eyebrow">Table setup</p>
          <div className="seg">
            {MODES.map(([id, label]) => (
              <button key={id} className={"seg-pill" + (mode === id ? " on" : "")}
                onClick={() => setMode(id)}>{label}</button>
            ))}
          </div>
          <p className="eyebrow">Level · questions</p>
          <div className="seg">
            {["basic", "intermediate", "advanced"].map((l) => (
              <button key={l} className={"seg-pill" + (level === l ? " on" : "")}
                onClick={() => setLevel(l)}>{l}</button>
            ))}
            {[3, 5, 8].map((n) => (
              <button key={n} className={"seg-pill" + (count === n ? " on" : "")}
                onClick={() => setCount(n)}>{n} q</button>
            ))}
          </div>
          {error && <div className="errbox" style={{ marginBottom: "0.8rem" }}>{error}</div>}
          <button className="btn" onClick={start}>Take a seat</button>

          {recent.length > 0 && (
            <div style={{ marginTop: "1.6rem" }}>
              <p className="eyebrow">Recent sessions</p>
              {recent.map((s) => (
                <button key={s.session_id} className="lib-row"
                  onClick={() => interviewApi.get(s.session_id).then(onReview)}>
                  <span><b>{s.topic}</b>
                    <span className="meta" style={{ display: "block" }}>
                      {s.mode} · {s.complete ? `avg ${s.average_score ?? "-"}/10` : `${s.answered}/${s.total}, resume it`}
                    </span></span>
                  <span style={{ color: "var(--text-faint)" }}>→</span>
                </button>
              ))}
            </div>
          )}
        </div>
        <aside className="panel wall">
          <p className="eyebrow">The wall</p>
          {stats && (
            <>
              <div className="shelf"><span>🔥 Streak</span><span className="mono">{stats.streak_days} days</span></div>
              {stats.per_topic.slice(0, 5).map((t) => (
                <div className="shelf" key={t.topic}>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.topic}</span>
                  <span className="mono" style={{ color: t.mastery >= 80 ? "var(--green)" : t.mastery >= 50 ? "var(--teal)" : "var(--redpen)" }}>
                    {t.mastery}
                  </span>
                </div>
              ))}
              <div className="shelf"><span>Questions answered</span><span className="mono">{stats.total_answered}</span></div>
            </>
          )}
        </aside>
      </div>
    </div>
  );
}

/* ── Live: card, notepad, sealed rubric ── */

function Live({ session, qIndex, onNext, onDone, onQuit }: {
  session: InterviewSessionPublic; qIndex: number;
  onNext: (s: InterviewSessionPublic, i: number) => void;
  onDone: (s: InterviewSessionPublic) => void;
  onQuit: () => void;
}) {
  const q = session.questions[qIndex];
  const [answer, setAnswer] = useState("");
  const [predicted, setPredicted] = useState(7);
  const [grading, setGrading] = useState(false);
  const [reveal, setReveal] = useState<InterviewAnswerResponse | null>(null);
  const [followup, setFollowup] = useState<string | null>(
    q.status === "awaiting_followup" ? q.followup_question : null);
  const [error, setError] = useState("");
  const [drillLeft, setDrillLeft] = useState(60);
  const isDrill = session.mode === "drill";
  const quiet = session.mode !== "practice"; // simulation + drill: no per-answer reveal

  const dict = useDictation((finals, interim) => setAnswer(finals + interim));
  const [voiceOn, setVoiceOn] = useState(() => localStorage.getItem("studio-tts") !== "off");

  const toggleVoice = () => {
    const next = !voiceOn;
    setVoiceOn(next);
    localStorage.setItem("studio-tts", next ? "on" : "off");
    if (!next) speechSynthesis.cancel();
  };

  useEffect(() => { speak(followup || q.question); return () => speechSynthesis.cancel(); },
    [q.question, followup]);

  useEffect(() => {
    if (!isDrill || reveal) return;
    setDrillLeft(60);
    const t = setInterval(() => setDrillLeft((s) => Math.max(0, s - 1)), 1000);
    return () => clearInterval(t);
  }, [qIndex, isDrill, reveal]);
  useEffect(() => {
    if (isDrill && drillLeft === 0 && !grading && !reveal) submit(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drillLeft]);

  const submit = async (skip = false) => {
    dict.stop(); setGrading(true); setError("");
    try {
      const res = await interviewApi.answer(session.session_id, {
        question_id: q.id, answer: skip ? "" : answer.trim(), skip,
        predicted_score: session.mode === "practice" && !skip ? predicted : null,
      });
      setGrading(false);
      if (res.followup_question) {
        setFollowup(res.followup_question); setAnswer(""); return;
      }
      if (quiet || skip) { advance(res); return; }
      setReveal(res);
    } catch (e) { setGrading(false); setError((e as Error).message); }
  };

  const advance = async (res: InterviewAnswerResponse | null) => {
    const fresh = await interviewApi.get(session.session_id);
    if (res?.session_complete || fresh.complete) onDone(fresh);
    else { setAnswer(""); setFollowup(null); setReveal(null); onNext(fresh, firstOpen(fresh)); }
  };

  const evaluation: AnswerEvaluation | null =
    reveal?.evaluation ?? null;
  const flipped = !!reveal && !quiet;
  const vs = evaluation ? (VERDICT_STYLE[evaluation.verdict] || VERDICT_STYLE.adequate) : null;

  return (
    <div className="room-wrap room-col">
      <div className={"orb" + (grading ? " speaking" : "")} />
      {isDrill && !reveal && (
        <div className="drill-ring">
          <svg width="54" height="54" viewBox="0 0 54 54">
            <circle cx="27" cy="27" r="23" fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth="5" />
            <circle cx="27" cy="27" r="23" fill="none"
              stroke={drillLeft <= 10 ? "var(--redpen)" : "var(--teal)"} strokeWidth="5"
              strokeLinecap="round" strokeDasharray="145"
              strokeDashoffset={145 - (145 * drillLeft) / 60} />
          </svg>
          <span className="t">{drillLeft}</span>
        </div>
      )}
      <div className="qcard">
        <span className="tag">
          Question {qIndex + 1} of {session.questions.length} · {session.mode}
          {followup ? " · follow-up" : ""}
        </span>
        {followup || q.question}
      </div>

      {!flipped && (
        <>
          <textarea
            className="notepad" value={answer} rows={4}
            onChange={(e) => setAnswer(e.target.value)}
            placeholder={dict.supported
              ? "Tap the mic and speak, or type your answer here…"
              : "Type your answer here…"}
          />
          {session.mode === "practice" && !followup && (
            <div className="predict-row">
              Your call before the verdict:
              <input type="range" min={0} max={10} value={predicted}
                onChange={(e) => setPredicted(+e.target.value)} />
              <b className="mono" style={{ color: "var(--teal)" }}>{predicted}/10</b>
            </div>
          )}
          <div className="mic-row">
            {dict.supported && (
              <button className={"mic-btn" + (dict.recording ? " recording" : "")}
                aria-label={dict.recording ? "Stop dictation" : "Answer by voice"}
                onClick={() => (dict.recording ? dict.stop() : dict.start())}>
                ◉
              </button>
            )}
            <button className="btn" onClick={() => submit(false)}
              disabled={grading || !answer.trim()}>
              {grading ? "The grader is checking the bar…" : followup ? "Answer the follow-up" : "Submit answer"}
            </button>
            <button className="btn btn-quiet" onClick={() => submit(true)} disabled={grading}>Skip</button>
            <button className="btn btn-quiet" onClick={toggleVoice}
              aria-pressed={voiceOn}>{voiceOn ? "Voice on" : "Voice off"}</button>
            <button className="btn btn-quiet" onClick={onQuit}>Leave the room</button>
          </div>
        </>
      )}
      {error && <div className="errbox" style={{ margin: "0.8rem 0" }}>{error}</div>}

      <div className={"rubric-flip" + (flipped ? " flipped" : "")}>
        <div className="rubric-inner">
          <div className="rubric-face rubric-back-face">
            <div>
              <span className="seal">SEALED · THE BAR</span>
              Written before you answered.<br />
              {quiet ? "In this mode it stays down until the session ends." : "Flips when your answer closes; the grader cannot move it."}
            </div>
          </div>
          <div className="rubric-face rubric-front-face">
            {evaluation && (
              <>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span className="tag">The bar, revealed</span>
                  <span className="verdict-chip" style={{ color: vs!.color, background: vs!.bg }}>
                    {evaluation.score}/10 {evaluation.verdict}
                    {reveal!.question.predicted_score != null && ` · you said ${reveal!.question.predicted_score}`}
                  </span>
                </div>
                <ul>
                  {(reveal!.question.rubric_key_points || []).map((p, i) => {
                    const hit = !evaluation.gaps.some((g) => g.toLowerCase().includes(p.slice(0, 24).toLowerCase()));
                    return <li key={i} className={hit ? "" : "miss"}>{p}</li>;
                  })}
                </ul>
                {evaluation.strengths[0] && (
                  <p style={{ fontSize: "0.72rem", color: "#1A7A4E", marginTop: "0.3rem" }}>✓ {evaluation.strengths[0]}</p>
                )}
                {evaluation.gaps[0] && (
                  <p style={{ fontSize: "0.72rem", color: "#B0472F", marginTop: "0.2rem" }}>△ {evaluation.gaps[0]}</p>
                )}
                <button className="btn" style={{ marginTop: "0.7rem" }}
                  onClick={() => advance(reveal)}>
                  {reveal!.session_complete ? "See the whole session" : "Next question"}
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── Summary ── */

function Summary({ session, onBack }: { session: InterviewSessionPublic; onBack: () => void }) {
  const s = session.summary!;
  const tone = scoreTone((s.average_score ?? 0) * 10);
  const goJobRoom = () => { window.location.hash = "/job"; };
  return (
    <div className="room-wrap room-col">
      <h1 className="room-title bar-tick-left">Session review</h1>
      <p className="room-sub">{session.topic} · {session.mode} · {s.answered} answered, {s.skipped} skipped</p>
      <div className="panel score-panel" style={{ marginBottom: "1rem" }}>
        <Dial score={Math.round((s.average_score ?? 0) * 10)} tone={tone} />
        <div>
          <div className="verdict" style={{ color: tone }}>{s.average_score ?? "-"}/10 average</div>
          <div className="score-sub">
            {s.calibration_gap != null && `calibration gap ${s.calibration_gap} · `}
            the rubric was sealed before every answer
          </div>
        </div>
      </div>
      {s.debrief && (
        <div className="panel" style={{ marginBottom: "1rem" }}>
          <p className="eyebrow" style={{ color: "var(--teal)" }}>The interviewer's debrief</p>
          <p style={{ fontSize: "0.84rem", lineHeight: 1.6, color: "var(--text-dim)" }}>{s.debrief}</p>
        </div>
      )}
      {s.scorecard && (
        <div className="panel" style={{ marginBottom: "1rem" }}>
          <p style={{ fontSize: "0.82rem" }}>
            This was a job screen; the full marked report card lives in the Job Room.
          </p>
          <button className="btn" style={{ marginTop: "0.6rem" }} onClick={goJobRoom}>Open the report card</button>
        </div>
      )}
      <div className="panel" style={{ marginBottom: "1rem" }}>
        <p className="eyebrow">Question by question</p>
        {session.questions.map((q, i) => (
          <div className="review-q" key={q.id}>
            <b>{i + 1}. {q.question}</b>
            <div style={{ color: "var(--text-dim)", marginTop: "0.2rem" }}>
              {q.status === "skipped" ? "skipped" : `${q.final_score}/10`}
              {q.final_evaluation?.gaps[0] && ` · gap: ${q.final_evaluation.gaps[0]}`}
            </div>
            {q.model_answer && (
              <details style={{ marginTop: "0.25rem" }}>
                <summary style={{ cursor: "pointer", color: "var(--teal)", fontSize: "0.72rem" }}>
                  The sealed model answer
                </summary>
                <p style={{ fontSize: "0.74rem", color: "var(--text-dim)", marginTop: "0.25rem" }}>{q.model_answer}</p>
              </details>
            )}
          </div>
        ))}
      </div>
      {s.weak_sections.length > 0 && (
        <p className="classic-note">Re-read before next time: {s.weak_sections.join(" · ")}</p>
      )}
      <button className="btn" onClick={onBack}>Back to the table</button>
    </div>
  );
}
