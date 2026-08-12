/** The four stations of the desk. All data is the real API's; all the
 * play is in service of it: papers settle, numbers count, marks pulse,
 * the stamp slaps. Everything stills under prefers-reduced-motion. */
import { useEffect, useMemo, useRef, useState } from "react";
import type { DragEvent } from "react";
import { api, fmtElapsed, useDocWatch } from "../api";
import { countMetrics } from "../marks";
import type { ChatTurn, JobProfileSummary, ResumeDoc, ResumeSummaryItem } from "../types";
import { Paper } from "./Paper";

/* ── Shared bits ── */

const reducedMotion = () =>
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Ease-out count-up that lands exactly on target. rAF pauses in hidden
 * tabs, so a timeout guarantees the landing regardless. */
function useCountUp(target: number, ms = 900) {
  const [value, setValue] = useState(reducedMotion() ? target : 0);
  useEffect(() => {
    if (reducedMotion()) { setValue(target); return; }
    let raf = 0;
    const t0 = performance.now();
    const tick = (t: number) => {
      const p = Math.min(1, (t - t0) / ms);
      setValue(Math.round(target * (1 - Math.pow(1 - p, 3))));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    const land = window.setTimeout(() => setValue(target), ms + 150);
    return () => { cancelAnimationFrame(raf); clearTimeout(land); };
  }, [target, ms]);
  return value;
}

export function Dial({ score, tone }: { score: number; tone: string }) {
  const shown = useCountUp(score);
  return (
    <div className="dial">
      <svg width="84" height="84" viewBox="0 0 84 84">
        <circle cx="42" cy="42" r="36" fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth="8" />
        <circle
          cx="42" cy="42" r="36" fill="none" stroke={tone} strokeWidth="8"
          strokeLinecap="round" strokeDasharray="226"
          strokeDashoffset={226 - (226 * shown) / 100}
        />
      </svg>
      <div className="num">{shown}</div>
    </div>
  );
}

export const scoreTone = (s: number) =>
  s >= 80 ? "var(--green)" : s >= 60 ? "var(--amber)" : "var(--redpen)";
export const scoreVerdict = (s: number) =>
  s >= 80 ? "Strong shape" : s >= 60 ? "Getting there" : "Needs work";

/** Honest staged copy: checks are instant, the AI passes take the time. */
const READ_STAGES: Array<[number, string]> = [
  [0, "Deterministic checks: instant ✓"],
  [4, "Mapping structure: sections, roles, dates…"],
  [30, "The recruiter is reading against your JD…"],
  [75, "Taking longer than usual; subscription providers queue under load…"],
];

function Progress({ title, elapsed, estimate }: { title: string; elapsed: number; estimate: number }) {
  const pct = Math.min(94, 2 + (elapsed / estimate) * 92);
  const stage = [...READ_STAGES].reverse().find(([at]) => elapsed >= at)!;
  return (
    <div className="working">
      <p className="line font-display" style={{ fontSize: "1.1rem" }}>{title}</p>
      <div className="progressbar"><div style={{ width: `${pct}%` }} /></div>
      <p className="line mono">
        {fmtElapsed(elapsed)} <span style={{ color: "var(--text-faint)" }}>/ ~{fmtElapsed(estimate)} est.</span>
      </p>
      <p className="line" style={{ fontSize: "0.76rem" }}>{stage[1]}</p>
    </div>
  );
}

/* ── Full-page text editor: pasted text expands to the whole screen ── */

function FullEditor({ title, value, onSave, onClose }: {
  title: string; value: string;
  onSave: (v: string) => void; onClose: () => void;
}) {
  const [draft, setDraft] = useState(value);
  const boxRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { boxRef.current?.focus(); }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fulledit-overlay" role="dialog" aria-label={title}>
      <div className="fulledit-card">
        <div className="fulledit-head">
          <p className="eyebrow" style={{ marginBottom: 0 }}>{title}</p>
          <span style={{ display: "flex", gap: "0.5rem" }}>
            <button className="btn" onClick={() => { onSave(draft); onClose(); }}>Save</button>
            <button className="btn btn-quiet" onClick={onClose}>Cancel</button>
          </span>
        </div>
        <textarea ref={boxRef} value={draft} spellCheck={false}
          onChange={(e) => setDraft(e.target.value)} />
        <p className="hint" style={{ marginTop: "0.5rem" }}>
          Esc cancels. Save writes back to the box; nothing is analyzed until you press "Read my resume".
        </p>
      </div>
    </div>
  );
}

/* ── Station 1: Target ── */

interface PickedFile { name: string; sizeKb: number; b64: string; }

const readFile = (file: File) =>
  new Promise<PickedFile>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({
      name: file.name,
      sizeKb: Math.round(file.size / 1024),
      b64: String(reader.result).split(",")[1] || "",
    });
    reader.onerror = () => reject(new Error("Could not read the file."));
    reader.readAsDataURL(file);
  });

export function TargetStation({ onDoc }: { onDoc: (d: ResumeDoc) => void }) {
  const [resumeText, setResumeText] = useState("");
  const [file, setFile] = useState<PickedFile | null>(null);
  const [dragging, setDragging] = useState(false);
  const [jdText, setJdText] = useState("");
  const [jdUrl, setJdUrl] = useState("");
  const [profileId, setProfileId] = useState("");
  const [profiles, setProfiles] = useState<JobProfileSummary[]>([]);
  const [past, setPast] = useState<ResumeSummaryItem[]>([]);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<"" | "resume" | "jd">("");
  const fileInput = useRef<HTMLInputElement>(null);

  const refreshLists = () => {
    api.listJobProfiles().then(setProfiles).catch(() => {});
    api.listResumes().then(setPast).catch(() => {});
  };
  useEffect(refreshLists, []);

  const elapsed = useDocWatch(pendingId, pendingId !== null, (doc) => {
    if (doc.status !== "analyzing") { setPendingId(null); onDoc(doc); }
  });

  const pick = async (f: File | undefined) => {
    if (!f) return;
    setError("");
    try { setFile(await readFile(f)); setResumeText(""); }
    catch (e) { setError((e as Error).message); }
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault(); setDragging(false);
    pick(e.dataTransfer.files?.[0]);
  };

  const analyze = async () => {
    setError("");
    try {
      const doc = await api.createResume({
        resume_text: file ? "" : resumeText,
        resume_file_b64: file?.b64,
        resume_filename: file?.name,
        jd_text: jdText,
        jd_url: jdUrl,
        job_profile_id: profileId || null,
      });
      onDoc(doc);
      if (doc.status === "analyzing") setPendingId(doc.resume_id);
    } catch (e) { setError((e as Error).message); }
  };

  if (pendingId) return <Progress title="The recruiter is reading…" elapsed={elapsed} estimate={90} />;

  return (
    <div>
      <div className="trays">
        <h1 className="bar-tick">Put your resume <em>on the desk</em></h1>
        <p className="sub">Scrivio reads it like a recruiter, marks it like an editor, and never writes a word that is not true.</p>
        <div className="tray-grid">
          <div
            className={"tray" + (dragging ? " dragging" : "")}
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
          >
            <div className="tray-head">
              <p className="eyebrow" style={{ marginBottom: 0 }}>Step 1 · Your resume</p>
              {resumeText.trim() && !file && (
                <button className="expand-btn" onClick={() => setExpanded("resume")}
                  aria-label="Edit your resume text full-screen" title="Edit full-screen">
                  ⤢
                </button>
              )}
            </div>
            <p className="hint" style={{ marginTop: "0.3rem" }}>
              Paste the full text below (you can keep editing it there), or drop a PDF/DOCX file anywhere in this box.
            </p>
            {file ? (
              <>
                <div className="mini-paper">
                  <b>{file.name}</b>
                  <span className="thin">{file.sizeKb} KB · landed on the desk ✓</span>
                </div>
                <p className="hint">
                  Drop another file to replace it, or{" "}
                  <a href="#" style={{ textDecoration: "underline" }}
                    onClick={(e) => { e.preventDefault(); setFile(null); }}>
                    switch to pasting text
                  </a>.
                </p>
              </>
            ) : (
              <>
                <textarea
                  value={resumeText}
                  onChange={(e) => setResumeText(e.target.value)}
                  onClick={() => { if (resumeText.trim()) setExpanded("resume"); }}
                  placeholder={dragging ? "Drop it right here…" : "Drag a PDF/DOCX here, or paste your resume text…"}
                  spellCheck={false}
                  title={resumeText.trim() ? "Click to edit full-screen" : undefined}
                />
                <p className="hint">
                  PDF, DOCX, TXT, or JSON Resume ·{" "}
                  <a href="#" style={{ textDecoration: "underline" }}
                    onClick={(e) => { e.preventDefault(); fileInput.current?.click(); }}>
                    browse files
                  </a>
                </p>
                <input
                  ref={fileInput} type="file" hidden
                  accept=".pdf,.docx,.txt,.md,.json"
                  onChange={(e) => pick(e.target.files?.[0])}
                />
              </>
            )}
          </div>
          <div className="tray">
            <div className="tray-head">
              <p className="eyebrow" style={{ marginBottom: 0 }}>Step 2 · The job description (optional)</p>
              {jdText.trim() && (
                <button className="expand-btn" onClick={() => setExpanded("jd")}
                  aria-label="Edit the job description full-screen" title="Edit full-screen">
                  ⤢
                </button>
              )}
            </div>
            <p className="hint" style={{ marginTop: "0.1rem" }}>
              Add the posting one of three ways: pick a saved job target, paste the posting's URL, or paste its text. With a JD you get a keyword match score and can tailor; without one you still get the full ATS report.
            </p>
            <select value={profileId} onChange={(e) => setProfileId(e.target.value)}
              aria-label="Saved job target">
              <option value="">Saved job target: none</option>
              {profiles.map((p) => (
                <option key={p.profile_id} value={p.profile_id}>
                  {p.role_title}{p.company ? ` @ ${p.company}` : ""}
                </option>
              ))}
            </select>
            <input
              type="text" value={jdUrl} onChange={(e) => setJdUrl(e.target.value)}
              placeholder="Posting URL: https://… (Scrivio fetches it)"
            />
            <textarea
              className="jd-box"
              value={jdText}
              onChange={(e) => setJdText(e.target.value)}
              onClick={() => { if (jdText.trim()) setExpanded("jd"); }}
              placeholder="Or paste the job description text here…"
              spellCheck={false}
              title={jdText.trim() ? "Click to edit full-screen" : undefined}
            />
          </div>
        </div>
        {error && <div className="errbox" style={{ marginTop: "1rem" }}>{error}</div>}
        <div className="go-row">
          <button className="btn" onClick={analyze}
            disabled={!resumeText.trim() && !file && !profileId}>
            Read my resume
          </button>
        </div>
      </div>

      {expanded === "resume" && (
        <FullEditor title="Edit your resume text" value={resumeText}
          onSave={setResumeText} onClose={() => setExpanded("")} />
      )}
      {expanded === "jd" && (
        <FullEditor title="Edit the job description" value={jdText}
          onSave={setJdText} onClose={() => setExpanded("")} />
      )}

      {past.length > 0 && (
        <div className="past">
          <p className="eyebrow">Past reports</p>
          {past.map((item, i) => (
            <div className="past-card" key={item.resume_id}
              style={{ animation: "rise .35s both", animationDelay: `${i * 50}ms` }}>
              <div>
                <b>{item.name || "Resume"}</b>
                <div className="meta">{item.jd_label || "no target JD"}</div>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
                {item.score != null && (
                  <span className="scorepill" style={{ color: scoreTone(item.score), background: "rgba(255,255,255,0.05)" }}>
                    {item.score}{item.tailored_score != null ? ` → ${item.tailored_score}` : ""}
                  </span>
                )}
                <button className="btn btn-quiet"
                  onClick={() => api.getResume(item.resume_id).then(onDoc).catch(() => {})}>
                  Open
                </button>
                <button className="btn btn-quiet" aria-label={`Delete ${item.name}`}
                  onClick={() => api.deleteResume(item.resume_id).then(refreshLists)}>
                  ✕
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Station 2: Report ── */

export function ReportStation({ doc, onDoc, onTailored }: {
  doc: ResumeDoc;
  onDoc: (d: ResumeDoc) => void;
  onTailored: (d: ResumeDoc) => void;
}) {
  const [lit, setLit] = useState<string | null>(null);
  const [tailoring, setTailoring] = useState(doc.tailor_status === "tailoring");
  const [error, setError] = useState("");
  const report = doc.report;

  // A doc opened mid-analysis keeps filling in live.
  const analyzing = doc.status === "analyzing";
  const elapsed = useDocWatch(doc.resume_id, tailoring || analyzing, (fresh) => {
    if (analyzing && fresh.status !== "analyzing") { onDoc(fresh); return; }
    if (tailoring && fresh.tailor_status !== "tailoring") {
      setTailoring(false);
      if (fresh.tailor_status === "error") { setError(fresh.tailor_error); onDoc(fresh); }
      else onTailored(fresh);
    }
  });

  const findings = useMemo(() => {
    const checks = report ? [...report.checks] : [];
    checks.sort((a, b) => Number(a.passed) - Number(b.passed) || b.weight - a.weight);
    return checks;
  }, [report]);

  const light = (id: string) => {
    setLit(id);
    document.querySelector(`[data-finding="${id}"]`)
      ?.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => setLit((cur) => (cur === id ? null : cur)), 2600);
  };

  const startTailor = async () => {
    setError("");
    try { await api.tailor(doc.resume_id); setTailoring(true); }
    catch (e) { setError((e as Error).message); }
  };

  if (!doc.structured || !report) {
    return <div className="errbox">{doc.error || "Structure extraction failed. Re-analyze from the Target station."}</div>;
  }

  return (
    <div className="desk-grid">
      <Paper resume={doc.structured} mode="report" report={report} litFinding={lit} />
      <aside className="rail">
        <div className="panel score-panel">
          <Dial score={report.score} tone={scoreTone(report.score)} />
          <div>
            <div className="verdict" style={{ color: scoreTone(report.score) }}>
              {scoreVerdict(report.score)}{analyzing ? " · provisional" : ""}
            </div>
            <div className="score-sub">
              {doc.jd_label ? `vs ${doc.jd_label}` : "no target JD"}<br />
              {report.keyword_coverage ? "70% checks + 30% keywords" : "weighted checks"}
            </div>
          </div>
        </div>

        <div className="panel">
          <p className="eyebrow">The pen's marks · tap one to see it on the paper</p>
          {findings.map((c, i) => (
            <button key={c.id} className={"finding" + (lit === c.id ? " lit" : "")}
              style={{ "--i": i } as React.CSSProperties} onClick={() => light(c.id)}>
              <span className="sig" style={{ background: c.passed ? "var(--green)" : "var(--redpen)" }} />
              <span>
                <b>{c.label} {c.passed ? "✓" : `· weight ${c.weight}`}</b>
                <p>{c.detail}</p>
              </span>
            </button>
          ))}
        </div>

        {report.keyword_coverage && (
          <div className="panel">
            <p className="eyebrow">Keyword match · {report.keyword_coverage.percent}%</p>
            {report.keyword_coverage.found.map((k, i) => (
              <span key={k} className="kchip found" style={{ "--i": i } as React.CSSProperties}>{k}</span>
            ))}
            {report.keyword_coverage.missing.map((k, i) => (
              <span key={k} className="kchip missing"
                style={{ "--i": report.keyword_coverage!.found.length + i } as React.CSSProperties}>
                {k}
              </span>
            ))}
          </div>
        )}

        {doc.review ? (
          <div className="panel">
            <p className="eyebrow" style={{ color: "var(--teal)" }}>Recruiter's read</p>
            <p style={{ fontSize: "0.8rem", lineHeight: 1.55, color: "var(--text-dim)" }}>{doc.review.summary}</p>
          </div>
        ) : analyzing && (
          <div className="panel" style={{ borderStyle: "dashed" }}>
            <p className="eyebrow">Recruiter's read</p>
            <p style={{ fontSize: "0.78rem", color: "var(--text-dim)" }}>
              Being written now ({fmtElapsed(elapsed)})… the checklist above is already final.
            </p>
          </div>
        )}

        {error && <div className="errbox">{error}</div>}

        <div className="panel cta-panel">
          {tailoring ? (
            <div style={{ textAlign: "center" }}>
              <p style={{ fontSize: "0.82rem", fontWeight: 600 }}>Rewriting honestly…</p>
              <p className="mono" style={{ fontSize: "0.78rem", color: "var(--teal)", marginTop: "0.25rem" }}>
                {fmtElapsed(elapsed)} <span style={{ color: "var(--text-faint)" }}>/ ~2:10 est.</span>
              </p>
            </div>
          ) : doc.tailored ? (
            <button className="btn" style={{ width: "100%" }} onClick={() => onTailored(doc)}>
              ↓ Your tailored resume is ready
            </button>
          ) : (
            <>
              <button className="btn" style={{ width: "100%" }} onClick={startTailor}
                disabled={!doc.jd_text || analyzing}>
                Tailor it to this JD
              </button>
              <p style={{ fontSize: "0.72rem", color: "var(--text-faint)", marginTop: "0.5rem", textAlign: "center" }}>
                {doc.jd_text
                  ? "Employers, titles, and dates cannot change."
                  : "Add a JD on the Target station to unlock tailoring."}
              </p>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

/* ── Station 3: Tailor ── */

export function TailorStation({ doc, onDoc, onSend }: {
  doc: ResumeDoc;
  onDoc: (d: ResumeDoc) => void;
  onSend: () => void;
}) {
  const [values, setValues] = useState<Map<number, string>>(new Map());
  const [saving, setSaving] = useState(false);
  const [peek, setPeek] = useState(false);
  const [editMode, setEditMode] = useState(false);
  const [pendingEdits, setPendingEdits] = useState<Map<string, string>>(new Map());
  const [savingEdits, setSavingEdits] = useState(false);
  const [undoing, setUndoing] = useState(false);
  const [error, setError] = useState("");
  const t = doc.tailored!;
  const remaining = countMetrics(t.resume);
  const typed = [...values.values()].filter((v) => v.trim()).length;

  // The package gate: nothing leaves the desk with unfinished numbers
  // or unsaved work on the paper.
  const blockers: string[] = [];
  if (remaining > 0) blockers.push(
    `${remaining} [METRIC] placeholder${remaining > 1 ? "s" : ""} still on the paper: type your real numbers into the amber chips, then press Save`);
  else if (typed > 0) blockers.push("You typed numbers but have not saved them yet");
  if (pendingEdits.size > 0) blockers.push(
    `${pendingEdits.size} text edit${pendingEdits.size > 1 ? "s" : ""} not saved yet`);
  const gateTip = blockers.join(". ");

  const save = async () => {
    setSaving(true); setError("");
    try {
      const max = Math.max(...values.keys()) + 1;
      const list = Array.from({ length: max }, (_, i) => values.get(i) ?? "");
      const fresh = await api.fillMetrics(doc.resume_id, list);
      setValues(new Map());
      onDoc(fresh);
    } catch (e) { setError((e as Error).message); }
    finally { setSaving(false); }
  };

  const saveEdits = async () => {
    setSavingEdits(true); setError("");
    try {
      const edits = [...pendingEdits.entries()].map(([path, value]) => ({ path, value }));
      const fresh = await api.editTailored(doc.resume_id, edits);
      setPendingEdits(new Map());
      setEditMode(false);
      onDoc(fresh);
    } catch (e) { setError((e as Error).message); }
    finally { setSavingEdits(false); }
  };

  const undo = async () => {
    setUndoing(true); setError("");
    try {
      const fresh = await api.undoTailored(doc.resume_id);
      setValues(new Map()); setPendingEdits(new Map()); setEditMode(false);
      onDoc(fresh);
    } catch (e) { setError((e as Error).message); }
    finally { setUndoing(false); }
  };

  return (
    <div>
      <div className="peek-row">
        <button className={"peek-btn" + (!peek && !editMode ? " on" : "")}
          onClick={() => { setPeek(false); setEditMode(false); }}>
          Tailored
        </button>
        <button className={"peek-btn" + (peek ? " on" : "")}
          onClick={() => { setPeek(true); setEditMode(false); }}>
          Peek at the original
        </button>
        <button className={"peek-btn" + (editMode ? " on" : "")}
          title="Click any sentence on the paper and type; blur to stage the edit"
          onClick={() => { setPeek(false); setEditMode(true); }}>
          ✎ Edit the text
        </button>
        {(doc.tailored_history?.length ?? 0) > 0 && (
          <button className="peek-btn" onClick={undo} disabled={undoing}
            title={`Step back to the version before the last change (${doc.tailored_history?.length} step${(doc.tailored_history?.length ?? 0) > 1 ? "s" : ""} available)`}>
            {undoing ? "Undoing…" : "↶ Undo"}
          </button>
        )}
      </div>
      {editMode && (
        <p className="edit-hint">
          Editing directly: click a sentence, change it, click away. Bullets emptied out are deleted.
          Employers, titles, and dates stay locked, that is the honesty contract.
        </p>
      )}
      <div className="desk-grid">
        {/* key remount replays the paper-settle animation on flip */}
        {peek && doc.structured ? (
          <Paper key="original" resume={doc.structured} mode="report" report={doc.report} />
        ) : editMode ? (
          <Paper
            key="editable"
            resume={t.resume} mode="tailored" report={doc.tailored_report}
            changes={t.changes}
            onEdit={(path, value) => setPendingEdits((m) => new Map(m).set(path, value))}
          />
        ) : (
          <Paper
            key="tailored"
            resume={t.resume} mode="tailored" report={doc.tailored_report}
            changes={t.changes} metricValues={values}
            onMetric={(i, v) => setValues((m) => new Map(m).set(i, v))}
          />
        )}
        <aside className="rail">
          <div className="panel score-panel" style={{ justifyContent: "space-between" }}>
            <div className="delta">
              <span className="pill from">{doc.report?.score ?? "-"}</span>
              <span className="arrow">→</span>
              <span className="pill to">{doc.tailored_report?.score ?? "-"}</span>
            </div>
            <div style={{ textAlign: "right" }}>
              <div className="verdict" style={{ color: scoreTone(doc.tailored_report?.score ?? 0) }}>
                {scoreVerdict(doc.tailored_report?.score ?? 0)}
              </div>
              <div className="fill-count">
                {remaining === 0 ? "All numbers filled ✓"
                  : `${remaining} number${remaining > 1 ? "s" : ""} to fill on the paper`}
              </div>
            </div>
          </div>

          {remaining > 0 && (
            <div className="panel" style={{ borderColor: "rgba(229,176,76,0.4)" }}>
              <p className="eyebrow" style={{ color: "var(--amber)" }}>Finish it on the paper</p>
              <p style={{ fontSize: "0.76rem", color: "var(--text-dim)", lineHeight: 1.5 }}>
                The amber chips are your numbers to type, right where they will print.
                Scrivio never invents metrics; blanks ship as [METRIC] until you fill them.
              </p>
              <button className="btn" style={{ width: "100%", marginTop: "0.7rem" }} onClick={save}
                disabled={saving || typed === 0}>
                {saving ? "Saving…" : `Save ${typed || ""} number${typed === 1 ? "" : "s"}`}
              </button>
            </div>
          )}

          <div className="panel">
            <p className="eyebrow">What changed, and why</p>
            {t.changes.map((c, i) => (
              <div className="change-note" key={i} style={{ "--i": i } as React.CSSProperties}>
                <span className="where">{c.where}</span> {c.what}
              </div>
            ))}
          </div>

          {t.warnings.length > 0 && (
            <div className="panel" style={{ borderColor: "rgba(229,176,76,0.4)" }}>
              <p className="eyebrow" style={{ color: "var(--amber)" }}>Honesty notes</p>
              {t.warnings.map((w, i) => (
                <p key={i} style={{ fontSize: "0.74rem", color: "var(--text-dim)", lineHeight: 1.5, padding: "0.2rem 0" }}>
                  • {w}
                </p>
              ))}
            </div>
          )}

          {error && <div className="errbox">{error}</div>}

          <div className="panel cta-panel">
            <button className="btn" style={{ width: "100%" }} onClick={onSend}
              disabled={blockers.length > 0}
              title={gateTip || "Package the finished resume"}>
              Looks right, package it
            </button>
            {blockers.length > 0 && (
              <p className="gate-note">{blockers[0]}.</p>
            )}
          </div>
        </aside>
      </div>

      {/* Staged work follows you: the save bar floats over any scroll position. */}
      {(pendingEdits.size > 0 || typed > 0) && (
        <div className="float-save" role="status">
          <span className="msg">
            {pendingEdits.size > 0
              ? <><b>{pendingEdits.size} edit{pendingEdits.size > 1 ? "s" : ""}</b> staged on the paper</>
              : <><b>{typed} number{typed > 1 ? "s" : ""}</b> typed, not saved</>}
          </span>
          {pendingEdits.size > 0 ? (
            <>
              <button className="btn" onClick={saveEdits} disabled={savingEdits}>
                {savingEdits ? "Saving…" : "Save edits"}
              </button>
              <button className="btn btn-quiet" onClick={() => { setPendingEdits(new Map()); setEditMode(false); }}>
                Discard
              </button>
            </>
          ) : (
            <button className="btn" onClick={save} disabled={saving}>
              {saving ? "Saving…" : "Save numbers"}
            </button>
          )}
        </div>
      )}

      <CoachDock doc={doc} onDoc={onDoc} />
    </div>
  );
}

/* ── The coach dock: ask questions, or ask for an edit — reachable
 * from any scroll position, chat kept warm while closed ── */

function CoachDock({ doc, onDoc }: {
  doc: ResumeDoc;
  onDoc: (d: ResumeDoc) => void;
}) {
  const [open, setOpen] = useState(false);
  const [unread, setUnread] = useState(false);
  const [log, setLog] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState<"" | "ask" | "edit">("");
  const logRef = useRef<HTMLDivElement>(null);
  const openRef = useRef(open);
  openRef.current = open;

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [log, busy, open]);

  const push = (turn: ChatTurn) => {
    setLog((l) => [...l, turn]);
    if (turn.role === "assistant" && !openRef.current) setUnread(true);
  };

  const ask = async () => {
    const q = input.trim();
    if (!q || busy) return;
    setInput(""); push({ role: "user", content: q }); setBusy("ask");
    try {
      const { answer } = await api.adviseResume(doc.resume_id, q, log.slice(-8));
      push({ role: "assistant", content: answer });
    } catch (e) {
      push({ role: "assistant", content: (e as Error).message });
    } finally { setBusy(""); }
  };

  const requestEdit = async () => {
    const q = input.trim();
    if (busy || (!q && log.length === 0)) return;
    const transcript = log.slice(-8);
    setInput("");
    push({ role: "user", content: q ? `Edit: ${q}` : "Edit: apply what you recommended above" });
    setBusy("edit");
    try {
      const before = doc.tailored?.changes.length ?? 0;
      const beforeWarnings = doc.tailored?.warnings.length ?? 0;
      const fresh = await api.requestEdit(doc.resume_id, q, transcript);
      onDoc(fresh);
      // Say exactly what was edited: the change entries this request added.
      const added = (fresh.tailored?.changes ?? []).slice(before);
      const addedWarnings = (fresh.tailored?.warnings ?? []).slice(beforeWarnings);
      const lines = added.slice(0, 4).map((c) => `• ${c.where}: ${c.what}`);
      if (added.length > 4) lines.push(`…and ${added.length - 4} more (full list in "What changed, and why").`);
      push({
        role: "assistant",
        content: added.length === 0
          ? "I did not change anything. " +
            (addedWarnings[0] || "The paper already matched the instruction, or it asked for something the honesty rules refuse.")
          : `Done. What changed:\n${lines.join("\n")}` +
            (addedWarnings.length ? `\n\nNote: ${addedWarnings.join(" ")}` : "") +
            "\n\nThe changed lines are marked teal on the paper. Undo is at the top if it went too far.",
      });
    } catch (e) {
      push({ role: "assistant", content: (e as Error).message });
    } finally { setBusy(""); }
  };

  return (
    <>
      {open && (
        <div className="coach-dock" role="dialog" aria-label="The coach">
          <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
            <p className="eyebrow" style={{ marginBottom: "0.3rem" }}>The coach</p>
            <button className="btn btn-quiet" style={{ padding: "0.2rem 0.6rem" }}
              aria-label="Close the coach" onClick={() => setOpen(false)}>✕</button>
          </div>
          <p style={{ fontSize: "0.72rem", color: "var(--text-dim)", lineHeight: 1.5 }}>
            Ask about metrics or phrasing, or describe an edit and let the coach make it. It will not invent facts for you.
          </p>
          <div className="coach-log" ref={logRef}>
            {log.length === 0 && !busy && (
              <p style={{ color: "var(--text-faint)", fontSize: "0.78rem", lineHeight: 1.6, margin: "auto 0" }}>
                Try: "Which of my numbers would an interviewer challenge?" or
                "Make the summary lead with event-driven systems." Answers land here.
              </p>
            )}
            {log.map((m, i) => (
              <div key={i} className={"coach-msg " + m.role}>{m.content}</div>
            ))}
            {busy && (
              <div className="coach-msg assistant thinking">
                {busy === "ask" ? "Thinking…" : "Editing the paper… (about half a minute)"}
              </div>
            )}
          </div>
          <textarea
            className="coach-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={'e.g. "What metric fits the deploy bullet?" or "Make the summary lead with Kafka"'}
            rows={2}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); }
            }}
          />
          <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem" }}>
            <button className="btn btn-quiet" style={{ flex: 1 }} onClick={ask}
              disabled={!input.trim() || !!busy}>
              Ask
            </button>
            <button className="btn" style={{ flex: 1 }} onClick={requestEdit}
              disabled={!!busy || (!input.trim() && log.length === 0)}
              title={input.trim()
                ? "The coach edits the tailored resume as instructed; honesty guard applies and Undo is one click"
                : "Empty box: applies what the coach recommended in this conversation"}>
              {input.trim() || log.length === 0 ? "Make this edit" : "Apply the advice"}
            </button>
          </div>
        </div>
      )}
      <button className="coach-fab" onClick={() => { setOpen((o) => !o); setUnread(false); }}
        aria-expanded={open} aria-label="Open the coach">
        {unread && <span className="dot" aria-label="New reply" />}
        ◉ Coach{busy ? "…" : ""}
      </button>
    </>
  );
}

/* ── Station 4: Send ── */

export function SendStation({ doc }: { doc: ResumeDoc }) {
  const t = doc.tailored!;
  const remaining = countMetrics(t.resume);
  const dl = (fmt: string) => api.downloadUrl(doc.resume_id, fmt, "tailored");
  return (
    <div className="send-wrap">
      <h1 className="font-display bar-tick">Ready to send</h1>
      <div className="package">
        <span className="stamp">TAILORED · HONEST</span>
        <b style={{ fontSize: "0.62rem" }}>{t.resume.basics.name}</b><br />
        <span style={{ color: "var(--ink-dim)" }}>{t.resume.basics.label}</span>
        <hr style={{ border: "none", borderTop: "1px solid #D8D2C2", margin: "0.4rem 0" }} />
        {t.resume.work[0]?.highlights.slice(0, 3).map((h, i) => (
          <span key={i}>{h.slice(0, 60)}…<br /></span>
        ))}
      </div>
      {remaining > 0 && (
        <p className="fill-count" style={{ marginBottom: "0.6rem" }}>
          ⚠ {remaining} [METRIC] still unfilled; downloads include the placeholders.
        </p>
      )}
      <div className="dl-row">
        <a className="btn" href={dl("pdf")} download>Download PDF</a>
        <a className="btn btn-quiet" href={dl("docx")} download>Word</a>
        <a className="btn btn-quiet" href={dl("md")} download>Markdown</a>
        <a className="btn btn-quiet" href={dl("json")} download>JSON Resume</a>
      </div>
      <div className="recap">
        <div style={{ "--i": 0 } as React.CSSProperties}>
          <span className="mono" style={{ color: "var(--green)" }}>
            {doc.report?.score} → {doc.tailored_report?.score}
          </span>
          <span className="lbl">ATS readiness</span>
        </div>
        <div style={{ "--i": 1 } as React.CSSProperties}>
          <span className="mono" style={{ color: "var(--teal)" }}>{t.changes.length}</span>
          <span className="lbl">honest rewrites</span>
        </div>
        <div style={{ "--i": 2 } as React.CSSProperties}>
          <span className="mono" style={{ color: "var(--amber)" }}>0</span>
          <span className="lbl">facts invented</span>
        </div>
      </div>
      <p style={{ fontSize: "0.74rem", color: "var(--text-faint)", marginTop: "1.6rem" }}>
        Every employer, title, and date on this page is byte-identical to your original. That is the point.
      </p>
    </div>
  );
}
