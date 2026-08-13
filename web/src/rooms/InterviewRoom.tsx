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

/* ── Voice capability probe ──────────────────────────────────────────
 * One OPENAI_API_KEY gates both directions of voice: /transcribe for the
 * candidate and /speak for the interviewer. Asking once at mount means
 * the mic never has to discover mid-recording that the good path was
 * never available, which would throw away what was just said. */
type VoiceOption = { name: string; description: string };
type VoiceConfig = { voices: VoiceOption[]; default: string; available: boolean };
let voiceConfig: VoiceConfig | null = null;
let voiceProbe: Promise<VoiceConfig | null> | null = null;

function loadVoiceConfig(): Promise<VoiceConfig | null> {
  if (!voiceProbe) {
    voiceProbe = fetch("/speak/voices")
      .then((r) => (r.ok ? (r.json() as Promise<VoiceConfig>) : null))
      .then((c) => (voiceConfig = c))
      .catch(() => (voiceConfig = null));
  }
  return voiceProbe;
}

export const chosenVoice = () => localStorage.getItem("studio-voice") || voiceConfig?.default || "sage";

/* ── Answering by voice ──────────────────────────────────────────────
 * Record and send to Whisper (/transcribe): accurate, punctuated, and
 * working in every browser that has a microphone. The React port shipped
 * with only browser SpeechRecognition, which is missing or blocked in
 * most browsers and fails through a silent onerror, so the mic button
 * appeared to do nothing at all. Dictation stays as the fallback for
 * servers with no key, and every failure now says so out loud. */
type MicState = "idle" | "recording" | "thinking";

function recorderMime(): string {
  const supported = (window.MediaRecorder as unknown as {
    isTypeSupported?: (t: string) => boolean } | undefined)?.isTypeSupported;
  for (const m of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"]) {
    if (supported?.(m)) return m;
  }
  return "";
}

type SpeechRec = {
  continuous: boolean; interimResults: boolean; lang: string;
  onresult: (e: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void;
  onend: () => void; onerror: (e: { error?: string }) => void;
  start: () => void; stop: () => void;
};

function useVoiceAnswer(handlers: { append: (t: string) => void; live: (t: string) => void }) {
  const h = useRef(handlers); h.current = handlers;
  const [state, setState] = useState<MicState>("idle");
  const [error, setError] = useState("");
  const stream = useRef<MediaStream | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<BlobPart[]>([]);
  const mime = useRef("audio/webm");
  const dictation = useRef<SpeechRec | null>(null);

  const [supported] = useState(() =>
    !!navigator.mediaDevices?.getUserMedia
    || "webkitSpeechRecognition" in window || "SpeechRecognition" in window);

  const release = useCallback(() => {
    stream.current?.getTracks().forEach((t) => t.stop());
    stream.current = null;
  }, []);

  const fail = useCallback((message: string) => {
    setError(message); setState("idle"); release();
  }, [release]);

  const transcribe = useCallback(async () => {
    const blob = new Blob(chunks.current, { type: mime.current });
    chunks.current = [];
    release();
    if (!blob.size) { setState("idle"); setError("Nothing was recorded. Try again."); return; }
    try {
      const b64 = await new Promise<string>((resolve, reject) => {
        const fr = new FileReader();
        fr.onload = () => resolve(String(fr.result).split(",")[1] || "");
        fr.onerror = reject;
        fr.readAsDataURL(blob);
      });
      const res = await fetch("/transcribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ audio_b64: b64, mime_type: mime.current }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        fail(d.detail || `HTTP ${res.status}`); return;
      }
      const { text } = await res.json();
      if (!text?.trim()) { setState("idle"); setError("That came through silent. Try again."); return; }
      h.current.append(text.trim());
      setState("idle");
    } catch {
      fail("Could not reach the transcriber. Type your answer instead.");
    }
  }, [fail, release]);

  const startDictation = useCallback(() => {
    const Ctor = (window as unknown as Record<string, unknown>).SpeechRecognition
      || (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
    if (!Ctor) { fail("This browser cannot record. Type your answer instead."); return; }
    const rec = new (Ctor as new () => SpeechRec)();
    rec.continuous = true; rec.interimResults = true; rec.lang = "en-US";
    let finals = "";
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) finals += r[0].transcript + " ";
        else interim += r[0].transcript;
      }
      h.current.live(finals + interim);
    };
    rec.onend = () => setState("idle");
    rec.onerror = (e) => fail(
      e.error === "not-allowed" || e.error === "service-not-allowed"
        ? "Microphone blocked. Allow it in the browser, or type your answer."
        : "Browser dictation is unavailable here. Type your answer instead.");
    try { rec.start(); } catch { fail("Dictation could not start. Type your answer instead."); return; }
    dictation.current = rec; setState("recording");
  }, [fail]);

  const start = useCallback(async () => {
    setError("");
    await loadVoiceConfig();
    if (!voiceConfig?.available || !navigator.mediaDevices?.getUserMedia) {
      startDictation(); return;
    }
    let media: MediaStream;
    try {
      media = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      fail("Microphone blocked. Allow it in the browser, or type your answer.");
      return;
    }
    stream.current = media;
    if (!window.MediaRecorder) { release(); startDictation(); return; }
    const type = recorderMime();
    const mr = type ? new MediaRecorder(media, { mimeType: type }) : new MediaRecorder(media);
    mime.current = mr.mimeType || type || "audio/webm";
    chunks.current = [];
    mr.ondataavailable = (e) => { if (e.data?.size) chunks.current.push(e.data); };
    mr.onstop = () => { setState("thinking"); void transcribe(); };
    recorder.current = mr;
    mr.start(1000); // timeslices, so audio survives a tab hiccup
    setState("recording");
  }, [fail, release, startDictation, transcribe]);

  const stop = useCallback(() => {
    if (dictation.current) {
      try { dictation.current.stop(); } catch { /* already ended */ }
      dictation.current = null; setState("idle"); return;
    }
    if (recorder.current?.state === "recording") recorder.current.stop();
    recorder.current = null;
  }, []);

  useEffect(() => () => {
    try { dictation.current?.stop(); } catch { /* ignore */ }
    if (recorder.current?.state === "recording") recorder.current.stop();
    stream.current?.getTracks().forEach((t) => t.stop());
  }, []);

  return { supported, state, error, start, stop, clearError: () => setError("") };
}

/** Server TTS is the good voice; the browser's is the fallback nobody
 * enjoys. The classic room asked /speak first and only degraded when the
 * server had no key, and the React port shipped with just the fallback,
 * so a configured OPENAI_API_KEY was being ignored. One latch: once the
 * server says it cannot speak, stop asking for the rest of the session. */
let serverVoiceDown = false;
let currentAudio: HTMLAudioElement | null = null;

function browserSpeak(text: string) {
  try {
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.04;
    speechSynthesis.speak(u);
  } catch { /* voice is garnish */ }
}

export function stopSpeaking() {
  try { speechSynthesis.cancel(); } catch { /* ignore */ }
  if (currentAudio) { currentAudio.pause(); currentAudio = null; }
}

const speak = async (text: string, style = "interviewer") => {
  if (localStorage.getItem("studio-tts") === "off") return;
  stopSpeaking();
  if (serverVoiceDown) { browserSpeak(text); return; }
  let url = "";
  try {
    const res = await fetch("/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice: chosenVoice(), style }),
    });
    // Only the request failing means the server cannot speak. A refused
    // play() is the browser's autoplay rule, and latching on it would
    // demote a working key to the robot voice for the whole session.
    if (!res.ok) { serverVoiceDown = true; browserSpeak(text); return; }
    url = URL.createObjectURL(await res.blob());
    const audio = new Audio(url);
    currentAudio = audio;
    audio.onended = audio.onerror = () => URL.revokeObjectURL(url);
    await audio.play();
  } catch {
    if (url) URL.revokeObjectURL(url);
    browserSpeak(text);
  }
};

/** Which voice asks the questions is a matter of taste, and taste is not
 * something to guess at from a config file. The picker previews on the
 * spot with the real question, because a list of adjectives tells you
 * nothing about whether you want to hear it for twenty minutes. */
function VoicePicker({ enabled, sample }: { enabled: boolean; sample: string }) {
  const [voices, setVoices] = useState<VoiceOption[]>([]);
  const [voice, setVoice] = useState(chosenVoice);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    void loadVoiceConfig().then((c) => {
      if (c?.available) { setVoices(c.voices); setVoice(chosenVoice()); }
    });
  }, []);

  if (!voices.length) return null;   // no key: the browser voice has no options
  const pick = (name: string) => {
    setVoice(name);
    localStorage.setItem("studio-voice", name);
    localStorage.setItem("studio-tts", "on");
    void speak(sample.slice(0, 180));
  };

  return (
    <span className="voice-pick">
      <button className="btn btn-quiet" onClick={() => setOpen(!open)}
        aria-expanded={open} disabled={!enabled}
        title={enabled ? "Choose the interviewer's voice" : "Turn the voice on first"}>
        Voice: {voice}
      </button>
      {open && (
        <div className="voice-menu" role="listbox">
          <p className="voice-menu-hint">Pick one and hear this question in it.</p>
          {voices.map((v) => (
            <button key={v.name} role="option" aria-selected={v.name === voice}
              className={"voice-opt" + (v.name === voice ? " on" : "")}
              onClick={() => pick(v.name)}>
              <b>{v.name}</b><span>{v.description}</span>
            </button>
          ))}
        </div>
      )}
    </span>
  );
}

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
    if (mode === "coding" && !topic.trim()) {
      setError("A coding round needs a topic, for example 'arrays and intervals' or 'graphs'.");
      return;
    }
    setBusy(true); setError("");
    try {
      const s = await interviewApi.create({
        topic: topic.trim() || undefined, level, mode, num_questions: count,
        ...(mode === "coding" ? { language: "python" } : {}),
      });
      onStart(s);
    } catch (e) { setError((e as Error).message); setBusy(false); }
  };

  const MODES: Array<[string, string]> = [
    ["practice", "Practice · coach at the table"],
    ["simulation", "Simulation · cards stay down"],
    ["drill", "Drill · 60s a question"],
    ["coding", "Coding · one problem, four phases"],
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
          <input type="text" value={topic} aria-label="Interview topic"
            onChange={(e) => setTopic(e.target.value)}
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
  const coding = session.mode === "coding" ? session.coding_problem ?? null : null;
  const PHASES = ["Clarify", "Approach", "Code", "Defend"];
  const phaseIndex = session.questions.findIndex((q) => q.id === session.questions[qIndex]?.id);
  // The code phase gets a real editor: monospace, tabs that indent, and the
  // signature pinned above it so it is never guessed from memory.
  const isCodePhase = !!coding && phaseIndex === 2;
  const quiet = session.mode !== "practice"; // simulation + drill: no per-answer reveal

  // Dictation writes over the answer as it goes, so it needs the text that
  // was already typed as its base; Whisper hands back one finished block
  // and appends to whatever is there.
  const typedBase = useRef("");
  const mic = useVoiceAnswer({
    append: (t) => setAnswer((a) => (a.trim() ? a.trimEnd() + " " : "") + t),
    live: (t) => setAnswer(typedBase.current + t),
  });
  const [voiceOn, setVoiceOn] = useState(() => localStorage.getItem("studio-tts") !== "off");

  const toggleVoice = () => {
    const next = !voiceOn;
    setVoiceOn(next);
    localStorage.setItem("studio-tts", next ? "on" : "off");
    // Turning it on used to take effect only at the NEXT question, which
    // read as a dead button. Switch it on and you hear this one.
    if (next) void speak(followup || q.question); else stopSpeaking();
  };

  useEffect(() => { speak(followup || q.question); return () => stopSpeaking(); },
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
    mic.stop(); setGrading(true); setError("");
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
      {coding && (
        <div className="code-problem">
          <div className="cp-head">
            <span className="eyebrow eyebrow-accent">{coding.title}</span>
            <span className="phase-rail" aria-label="Round phases">
              {PHASES.map((p, i) => (
                <span key={p} className={"phase" + (i === phaseIndex ? " on" : i < phaseIndex ? " done" : "")}>
                  {p}
                </span>
              ))}
            </span>
          </div>
          <p className="cp-statement">{coding.statement}</p>
          <code className="cp-signature">{coding.signature}</code>
          {coding.stated_constraints.length > 0 && (
            <ul className="cp-constraints">
              {coding.stated_constraints.map((c) => <li key={c}>{c}</li>)}
            </ul>
          )}
          <p className="cp-note">
            Some constraints are deliberately unstated. Asking about the ones
            that would change your implementation is what the clarify phase
            scores.
          </p>
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
            className={"notepad" + (isCodePhase ? " code-pad" : "")}
            value={answer} rows={isCodePhase ? 16 : 4}
            spellCheck={!isCodePhase}
            aria-label={isCodePhase ? "Your code" : "Your answer"}
            onChange={(e) => setAnswer(e.target.value)}
            onKeyDown={isCodePhase ? (e) => {
              // Tab indents rather than leaving the editor: nobody writes
              // Python with the browser's focus order.
              if (e.key === "Tab") {
                e.preventDefault();
                const el = e.currentTarget;
                const at = el.selectionStart;
                setAnswer(answer.slice(0, at) + "    " + answer.slice(el.selectionEnd));
                requestAnimationFrame(() => { el.selectionStart = el.selectionEnd = at + 4; });
              }
            } : undefined}
            placeholder={isCodePhase
              ? `${coding?.signature ?? ""}\n    # your implementation`
              : mic.supported
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
            {mic.supported && (
              <button className={"mic-btn" + (mic.state === "recording" ? " recording" : "")}
                disabled={mic.state === "thinking"}
                aria-label={mic.state === "recording" ? "Stop recording" : "Answer by voice"}
                onClick={() => {
                  if (mic.state === "recording") { mic.stop(); return; }
                  stopSpeaking();            // never record the interviewer
                  typedBase.current = answer ? answer.trimEnd() + " " : "";
                  void mic.start();
                }}>
                ◉
              </button>
            )}
            {mic.state !== "idle" && (
              <span className="mic-state">
                {mic.state === "recording" ? "Listening, tap again when you are done" : "Writing down what you said…"}
              </span>
            )}
            <button className="btn" onClick={() => submit(false)}
              disabled={grading || !answer.trim()}>
              {grading ? "The grader is checking the bar…" : followup ? "Answer the follow-up" : "Submit answer"}
            </button>
            <button className="btn btn-quiet" onClick={() => submit(true)} disabled={grading}>Skip</button>
            <button className="btn btn-quiet" onClick={toggleVoice}
              aria-pressed={voiceOn}>{voiceOn ? "Voice on" : "Voice off"}</button>
            <VoicePicker enabled={voiceOn} sample={q.question} />
            <button className="btn btn-quiet" onClick={onQuit}>Leave the room</button>
          </div>
          {mic.error && <div className="errbox" style={{ margin: "0.6rem 0" }}>{mic.error}</div>}
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
