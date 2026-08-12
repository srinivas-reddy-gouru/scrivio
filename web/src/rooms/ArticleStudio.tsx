/** Article Studio: the assignment slip, the press run (real SSE stages
 * beside a ghost-lined manuscript), and the reading paper. The library
 * is the shelf below the slip. */
import { useEffect, useMemo, useRef, useState } from "react";
import { marked } from "marked";
import { api, articleApi, fmtElapsed, interviewApi, openSession } from "../api";
import type { ArticleSummary, ClarificationQuestion, ProgressEvent, SettingsInfo } from "../types";

const STAGES: Array<[string, string, string]> = [
  ["brief", "The brief", "editorial angle and scope"],
  ["relevance_check", "Relevance check", "is the angle still on-topic"],
  ["search", "Evidence search", "sources gathered, trust-scored"],
  ["planning", "Outline", "sections planned against evidence"],
  ["gap_fill", "Gap fill", "holes in the evidence chased down"],
  ["verification", "Fact-check", "claims verified; weak ones dropped"],
  ["drafting", "Drafting", "sections written from evidence"],
  ["editor", "Editor", "flags raised, fixes demanded"],
  ["polish", "Polish", "voice pass, code fences protected"],
  ["critic", "The critic", "final read before it ships"],
];

/** Render article markdown. Our own pipeline wrote it; trusted content.
 * Mermaid fences pass through as code blocks that useMermaid() then
 * renders into real diagrams, exactly like the classic studio did. */
function renderMarkdown(md: string): string {
  return marked.parse(md, { async: false }) as string;
}

let mermaidReady = false;
/** Turn `pre > code.language-mermaid` blocks inside the container into
 * live mermaid diagrams. Re-runs whenever the html changes. */
function useMermaid(ref: React.RefObject<HTMLElement | null>, html: string) {
  useEffect(() => {
    const host = ref.current;
    if (!host) return;
    const blocks = host.querySelectorAll("pre code.language-mermaid");
    if (!blocks.length) return;
    let cancelled = false;
    import("mermaid").then(({ default: mermaid }) => {
      if (cancelled) return;
      if (!mermaidReady) {
        mermaid.initialize({ startOnLoad: false, theme: "default", securityLevel: "loose" });
        mermaidReady = true;
      }
      const nodes: HTMLElement[] = [];
      blocks.forEach((code) => {
        const div = document.createElement("div");
        div.className = "mermaid";
        div.textContent = code.textContent || "";
        code.closest("pre")?.replaceWith(div);
        nodes.push(div);
      });
      mermaid.run({ nodes }).catch(() => {
        // A malformed diagram stays as its source text; never break the page.
      });
    });
    return () => { cancelled = true; };
  }, [ref, html]);
}

type View =
  | { kind: "compose" }
  | { kind: "clarify"; questions: ClarificationQuestion[]; defaultNote: string }
  | { kind: "press"; jobId: string }
  | { kind: "read"; articleId: string; level?: string };

interface Brief {
  topic: string; level: string; preset: string;
  web_search: boolean; include_diagrams: boolean;
}

export function ArticleStudio() {
  const [view, setView] = useState<View>({ kind: "compose" });
  const [brief, setBrief] = useState<Brief>({
    topic: "", level: "intermediate", preset: "balanced",
    web_search: true, include_diagrams: true,
  });
  const [error, setError] = useState("");

  const submit = async (extra: Record<string, unknown> = {}) => {
    setError("");
    try {
      const res = await articleApi.generate({
        topic: brief.topic.trim(), explanation_level: brief.level,
        model_preset: brief.preset, web_search: brief.web_search,
        include_diagrams: brief.include_diagrams, ...extra,
      });
      if (res.clarification_required) {
        setView({ kind: "clarify", questions: res.questions, defaultNote: res.default_if_skipped });
      } else if (res.job_id) {
        setView({ kind: "press", jobId: res.job_id });
      }
    } catch (e) { setError((e as Error).message); }
  };

  if (view.kind === "read") {
    return <Reading articleId={view.articleId} level={view.level}
      onBack={() => setView({ kind: "compose" })}
      onLevel={(l) => setView({ kind: "read", articleId: view.articleId, level: l })} />;
  }
  if (view.kind === "press") {
    return <PressRun jobId={view.jobId} topic={brief.topic}
      onDone={(articleId) => setView({ kind: "read", articleId })}
      onFail={(msg) => { setError(msg); setView({ kind: "compose" }); }} />;
  }
  if (view.kind === "clarify") {
    return <Clarify questions={view.questions} defaultNote={view.defaultNote}
      onSubmit={(answers) => submit({ clarification_answers: answers })}
      onSkip={() => submit({ skip_clarification: true })}
      onBack={() => setView({ kind: "compose" })} />;
  }
  return <Compose brief={brief} setBrief={setBrief} error={error}
    onGo={() => submit()} onOpen={(id) => setView({ kind: "read", articleId: id })} />;
}

/* ── Compose + library ── */

function Compose({ brief, setBrief, error, onGo, onOpen }: {
  brief: Brief; setBrief: (b: Brief) => void; error: string;
  onGo: () => void; onOpen: (id: string) => void;
}) {
  const [library, setLibrary] = useState<ArticleSummary[]>([]);
  const [settings, setSettings] = useState<SettingsInfo | null>(null);
  useEffect(() => {
    api.listArticles().then(setLibrary).catch(() => {});
    api.settings().then(setSettings).catch(() => {});
  }, []);

  const providerLabel = settings
    ? (settings.resolved_provider === "claude-cli"
      ? `subscription (${settings.active_cli || "claude"})`
      : `${settings.resolved_provider} api`)
    : "checking…";

  return (
    <div className="room-wrap">
      <div className="compose-hero">
        <h1>What should we <span className="grad-text">write</span> today?</h1>
        <div className="bar" />
        <p>Research-backed technical articles: sourced, verified, drafted and polished by a multi-agent pipeline.</p>
      </div>
      <div className="composer-card">
        <textarea value={brief.topic}
          aria-label="Article topic or question"
          placeholder={'Describe a topic or ask a question… e.g. "How does Kafka handle backpressure?"'}
          onChange={(e) => setBrief({ ...brief, topic: e.target.value })}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && brief.topic.trim()) onGo();
          }}
        />
        {error && <div className="errbox" style={{ margin: "0 1.1rem 0.8rem" }}>{error}</div>}
        <div className="composer-row">
          <label className="pill-select" title="Reading level">
            <span aria-hidden="true">⌂</span>
            <select value={brief.level} onChange={(e) => setBrief({ ...brief, level: e.target.value })}>
              {["basic", "intermediate", "advanced"].map((l) => (
                <option key={l} value={l}>{l[0].toUpperCase() + l.slice(1)}</option>
              ))}
            </select>
          </label>
          <label className="pill-select" title="Speed and model quality">
            <span aria-hidden="true">☆</span>
            <select value={brief.preset}
              onChange={(e) => setBrief({ ...brief, preset: e.target.value as typeof brief.preset })}>
              {["fast", "balanced", "best"].map((p) => (
                <option key={p} value={p}>{p[0].toUpperCase() + p.slice(1)}</option>
              ))}
            </select>
          </label>
          <label className="pill-select" title="Search the live web for evidence">
            <span aria-hidden="true">🔍</span>
            <select value={brief.web_search ? "on" : "off"}
              onChange={(e) => setBrief({ ...brief, web_search: e.target.value === "on" })}>
              <option value="on">Live web</option>
              <option value="off">No web</option>
            </select>
          </label>
          <button className={"pill-toggle" + (brief.include_diagrams ? " on" : "")}
            title="Include mermaid diagrams"
            onClick={() => setBrief({ ...brief, include_diagrams: !brief.include_diagrams })}>
            ▦ Diagrams
          </button>
          <button className="gen-btn" onClick={onGo} disabled={!brief.topic.trim()}>
            ⚡ Generate
          </button>
        </div>
      </div>
      <div className="status-chips">
        <span className="status-chip"><i /> {providerLabel}</span>
        {settings?.has_search && <span className="status-chip"><i /> Web search ready</span>}
      </div>

      {library.length > 0 && (
        <div style={{ marginTop: "1.8rem" }}>
          <p className="eyebrow">The library · {library.length} article{library.length > 1 ? "s" : ""}</p>
          <div className="lib-grid">
            {library.map((a, i) => (
              <button key={a.id} className="lib-card" style={{ "--i": Math.min(i, 12) } as React.CSSProperties}
                onClick={() => onOpen(a.id)}>
                <b>{a.title || a.topic}</b>
                <span className="meta">
                  {a.level} · {a.available_levels.length} level{a.available_levels.length > 1 ? "s" : ""}
                  {a.version > 1 ? ` · v${a.version}` : ""}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Clarification: the editor asks before the press rolls ── */

function Clarify({ questions, defaultNote, onSubmit, onSkip, onBack }: {
  questions: ClarificationQuestion[]; defaultNote: string;
  onSubmit: (answers: Record<string, string>) => void;
  onSkip: () => void; onBack: () => void;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const set = (id: string, v: string) => setAnswers((a) => ({ ...a, [id]: v }));

  return (
    <div className="room-wrap room-col">
      <h1 className="room-title bar-tick-left">The editor has questions</h1>
      <p className="room-sub">A broad topic writes a mushy article. Thirty seconds here buys a sharper one.</p>
      {questions.map((q) => (
        <div className="panel" key={q.id} style={{ marginBottom: "0.9rem" }}>
          <p style={{ fontSize: "0.86rem", fontWeight: 600, marginBottom: "0.6rem" }}>{q.question}</p>
          {q.options.map((o) => (
            <button key={o} className={"clarify-opt" + (answers[q.id] === o ? " on" : "")}
              onClick={() => set(q.id, o)}>{o}</button>
          ))}
          <input type="text" className="clarify-opt" style={{ cursor: "text" }}
            placeholder="Or answer in your own words…"
            value={q.options.includes(answers[q.id] || "") ? "" : answers[q.id] || ""}
            onChange={(e) => set(q.id, e.target.value)} />
        </div>
      ))}
      <div style={{ display: "flex", gap: "0.6rem", flexWrap: "wrap" }}>
        <button className="btn" onClick={() => onSubmit(answers)}
          disabled={Object.values(answers).every((v) => !v.trim())}>
          Answer and start the press
        </button>
        <button className="btn btn-quiet" onClick={onSkip}>Skip; use the default angle</button>
        <button className="btn btn-quiet" onClick={onBack}>Back</button>
      </div>
      <p className="classic-note" style={{ marginTop: "0.8rem" }}>If skipped: {defaultNote}</p>
    </div>
  );
}

/* ── The press run: real SSE stages beside the assembling paper ── */

type StageState = { status: "idle" | "live" | "done"; note: string };

function PressRun({ jobId, topic, onDone, onFail }: {
  jobId: string; topic: string;
  onDone: (articleId: string) => void; onFail: (msg: string) => void;
}) {
  const [stages, setStages] = useState<Record<string, StageState>>(
    () => Object.fromEntries(STAGES.map(([id]) => [id, { status: "idle", note: "" }])));
  const [elapsed, setElapsed] = useState(0);
  const [modelsNote, setModelsNote] = useState("");
  const doneRef = useRef(false);

  useEffect(() => {
    const startedAt = Date.now();
    const clock = setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    const es = new EventSource(articleApi.streamUrl(jobId));
    es.onmessage = (msg) => {
      let ev: ProgressEvent;
      try { ev = JSON.parse(msg.data); } catch { return; }
      if (ev.type === "pipeline_info") {
        const models = ev.data?.models as Record<string, string> | undefined;
        if (models) setModelsNote([...new Set(Object.values(models))].join(" · "));
        return;
      }
      if (ev.type === "stage_started" || ev.type === "stage_completed") {
        setStages((s) => ({
          ...s,
          [ev.stage]: {
            status: ev.type === "stage_started" ? "live" : "done",
            note: ev.message || s[ev.stage]?.note || "",
          },
        }));
      }
      if (ev.type === "complete" && !doneRef.current) {
        doneRef.current = true; es.close();
        const dir = String(ev.data?.output_dir || "");
        const id = dir.split("/").filter(Boolean).pop() || "";
        if (id) onDone(id);
        else onFail("The press finished but the paper's shelf id was missing.");
      }
      if (ev.type === "error" || ev.type === "cancelled") {
        doneRef.current = true; es.close();
        onFail(ev.message || "The press stopped mid-run.");
      }
    };
    es.onerror = () => { /* EventSource auto-reconnects; terminal states close above */ };
    return () => { clearInterval(clock); es.close(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  const liveIdx = STAGES.findIndex(([id]) => stages[id].status === "live");
  const doneCount = STAGES.filter(([id]) => stages[id].status === "done").length;

  return (
    <div className="room-wrap">
      <h1 className="room-title bar-tick-left">The press is running</h1>
      <p className="room-sub">
        {topic || "Your article"} · {fmtElapsed(elapsed)} on the clock
        {modelsNote && ` · ${modelsNote}`}
      </p>
      <div className="press-grid">
        <div className="manuscript">
          <h1>{topic || "…"}</h1>
          {Array.from({ length: 3 + doneCount * 2 }).map((_, i) => (
            <div key={i} className="ghost-line"
              style={{ width: `${[92, 78, 96, 64, 88, 71, 95, 82][i % 8]}%` }} />
          ))}
          <p style={{ fontSize: "0.72rem", color: "var(--ink-dim)", marginTop: "1rem" }}>
            The paper fills as stages close. A full run takes minutes, not seconds; that is the fact-checking.
          </p>
        </div>
        <aside className="panel">
          <p className="eyebrow">The floor, stage by stage</p>
          {STAGES.map(([id, label, sub], i) => {
            const st = stages[id];
            return (
              <div key={id} className={`stage-row ${st.status === "idle" ? "" : st.status}`}>
                <span className="dot">{st.status === "done" ? "✓" : i + 1}</span>
                <span>
                  <b>{label}</b>
                  <p>{st.note || (st.status === "live" ? "working…" : sub)}</p>
                </span>
              </div>
            );
          })}
          {liveIdx === -1 && doneCount === STAGES.length && (
            <p className="classic-note">Binding the paper…</p>
          )}
        </aside>
      </div>
    </div>
  );
}

/* ── Reading view ── */

function Reading({ articleId, level, onBack, onLevel }: {
  articleId: string; level?: string;
  onBack: () => void; onLevel: (l: string) => void;
}) {
  const [detail, setDetail] = useState<Awaited<ReturnType<typeof articleApi.detail>> | null>(null);
  const [error, setError] = useState("");
  const [practicing, setPracticing] = useState(false);
  const [practiceError, setPracticeError] = useState("");
  const proseRef = useRef<HTMLElement>(null);

  useEffect(() => {
    setDetail(null);
    articleApi.detail(articleId, level)
      .then((d) => ("markdown" in d ? setDetail(d) : setError("That paper is not on the shelf.")))
      .catch((e) => setError((e as Error).message));
  }, [articleId, level]);

  const html = useMemo(() => (detail ? renderMarkdown(detail.markdown) : ""), [detail]);
  useMermaid(proseRef, html);

  const practice = async () => {
    setPracticing(true); setPracticeError("");
    try {
      const s = await interviewApi.create({ article_id: articleId, level: detail?.level });
      openSession(s.session_id);
    } catch (e) {
      setPracticeError((e as Error).message); setPracticing(false);
    }
  };

  if (error) {
    return <div className="room-wrap"><div className="errbox">{error}</div>
      <button className="btn btn-quiet" style={{ marginTop: "0.8rem" }} onClick={onBack}>Back to the studio</button></div>;
  }
  if (!detail) {
    return <div className="room-wrap"><p className="room-sub">Pulling the paper off the shelf…</p></div>;
  }
  return (
    <div className="room-wrap">
      <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", flexWrap: "wrap", marginBottom: "1rem" }}>
        <button className="btn btn-quiet" onClick={onBack}>← Studio</button>
        {detail.available_levels.map((l) => (
          <button key={l} className={"seg-pill" + (l === detail.level ? " on" : "")}
            onClick={() => onLevel(l)}>{l}</button>
        ))}
        <span style={{ marginLeft: "auto", display: "flex", gap: "0.5rem", alignItems: "center" }}>
          <button className="btn" disabled={practicing}
            title="A practice interview grounded in this article: questions and rubrics written from its verified claims"
            onClick={practice}>
            {practicing ? "Writing the rubric…" : "◉ Practice interview"}
          </button>
          <button className="btn btn-quiet"
            onClick={() => {
              const blob = new Blob([detail.markdown], { type: "text/markdown" });
              const a = document.createElement("a");
              a.href = URL.createObjectURL(blob);
              a.download = `${(detail.title || detail.topic || "article").replace(/[^\w.-]+/g, "_")}_${detail.level}.md`;
              a.click(); URL.revokeObjectURL(a.href);
            }}>Download .md</button>
        </span>
      </div>
      {practiceError && <div className="errbox" style={{ marginBottom: "0.8rem" }}>{practiceError}</div>}
      <div className="read-paper">
        <article className="prose" ref={proseRef}
          dangerouslySetInnerHTML={{ __html: html }} />
      </div>
    </div>
  );
}
