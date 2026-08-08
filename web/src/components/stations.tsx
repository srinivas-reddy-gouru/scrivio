/** The four stations of the desk. All data is the real API's. */
import { useEffect, useMemo, useState } from "react";
import { api, fmtElapsed, useDocWatch } from "../api";
import { countMetrics } from "../marks";
import type { JobProfileSummary, ResumeDoc, ResumeSummaryItem } from "../types";
import { Paper } from "./Paper";

/* ── Shared bits ── */

export function Dial({ score, tone }: { score: number; tone: string }) {
  return (
    <div className="dial">
      <svg width="84" height="84" viewBox="0 0 84 84">
        <circle cx="42" cy="42" r="36" fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth="8" />
        <circle
          cx="42" cy="42" r="36" fill="none" stroke={tone} strokeWidth="8"
          strokeLinecap="round" strokeDasharray="226"
          strokeDashoffset={226 - (226 * score) / 100}
          style={{ transition: "stroke-dashoffset .8s ease-out" }}
        />
      </svg>
      <div className="num">{score}</div>
    </div>
  );
}

export const scoreTone = (s: number) =>
  s >= 80 ? "var(--green)" : s >= 60 ? "var(--amber)" : "var(--redpen)";
export const scoreVerdict = (s: number) =>
  s >= 80 ? "Strong shape" : s >= 60 ? "Getting there" : "Needs work";

function Progress({ title, elapsed, estimate }: { title: string; elapsed: number; estimate: number }) {
  const pct = Math.min(94, 2 + (elapsed / estimate) * 92);
  return (
    <div className="working">
      <p className="line font-display" style={{ fontSize: "1.1rem" }}>{title}</p>
      <div className="progressbar"><div style={{ width: `${pct}%` }} /></div>
      <p className="line mono">
        {fmtElapsed(elapsed)} <span style={{ color: "var(--text-faint)" }}>/ ~{fmtElapsed(estimate)} est.</span>
      </p>
      <p className="line" style={{ fontSize: "0.72rem", color: "var(--text-faint)" }}>
        The checklist is computed instantly; the AI passes take the time. Estimate depends on your provider.
      </p>
    </div>
  );
}

/* ── Station 1: Target ── */

export function TargetStation({ onDoc }: { onDoc: (d: ResumeDoc) => void }) {
  const [resumeText, setResumeText] = useState("");
  const [jdText, setJdText] = useState("");
  const [profileId, setProfileId] = useState("");
  const [profiles, setProfiles] = useState<JobProfileSummary[]>([]);
  const [past, setPast] = useState<ResumeSummaryItem[]>([]);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.listJobProfiles().then(setProfiles).catch(() => {});
    api.listResumes().then(setPast).catch(() => {});
  }, []);

  const elapsed = useDocWatch(pendingId, pendingId !== null, (doc) => {
    if (doc.status !== "analyzing") { setPendingId(null); onDoc(doc); }
  });

  const analyze = async () => {
    setError("");
    try {
      const doc = await api.createResume({
        resume_text: resumeText,
        jd_text: jdText,
        job_profile_id: profileId || null,
      });
      onDoc(doc); // deterministic report exists already; App stays on 1 until ready
      if (doc.status === "analyzing") setPendingId(doc.resume_id);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (pendingId) return <Progress title="The recruiter is reading…" elapsed={elapsed} estimate={90} />;

  return (
    <div>
      <div className="trays">
        <h1 className="bar-tick">Put your resume <em>on the desk</em></h1>
        <p className="sub">Scrivio reads it like a recruiter, marks it like an editor, and never writes a word that is not true.</p>
        <div className="tray-grid">
          <div className="tray">
            <p className="eyebrow">Your resume</p>
            <textarea
              value={resumeText}
              onChange={(e) => setResumeText(e.target.value)}
              placeholder="Paste your resume text here…"
              spellCheck={false}
            />
            <p className="hint">Paste for now; PDF/DOCX upload arrives with the full port.</p>
          </div>
          <div className="tray">
            <p className="eyebrow">The job it must win</p>
            <select value={profileId} onChange={(e) => setProfileId(e.target.value)}>
              <option value="">No saved job target</option>
              {profiles.map((p) => (
                <option key={p.profile_id} value={p.profile_id}>
                  {p.role_title}{p.company ? ` @ ${p.company}` : ""}
                </option>
              ))}
            </select>
            <textarea
              value={jdText}
              onChange={(e) => setJdText(e.target.value)}
              placeholder="…or paste the job description here (optional)"
              spellCheck={false}
            />
            <p className="hint">A JD unlocks keyword match and tailoring.</p>
          </div>
        </div>
        {error && <div className="errbox" style={{ marginTop: "1rem" }}>{error}</div>}
        <div className="go-row">
          <button className="btn" onClick={analyze} disabled={!resumeText.trim() && !profileId}>
            Read my resume
          </button>
        </div>
      </div>

      {past.length > 0 && (
        <div className="past">
          <p className="eyebrow">Past reports</p>
          {past.map((item) => (
            <div className="past-card" key={item.resume_id}>
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
                <button
                  className="btn btn-quiet"
                  onClick={() => api.getResume(item.resume_id).then(onDoc).catch(() => {})}
                >
                  Open
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

  const elapsed = useDocWatch(doc.resume_id, tailoring, (fresh) => {
    if (fresh.tailor_status === "tailoring") return;
    setTailoring(false);
    if (fresh.tailor_status === "error") setError(fresh.tailor_error);
    else onTailored(fresh);
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
    try {
      await api.tailor(doc.resume_id);
      setTailoring(true);
    } catch (e) { setError((e as Error).message); }
  };

  if (!doc.structured || !report) {
    return <div className="stage"><div className="errbox">{doc.error || "Structure extraction failed. Re-analyze from the Target station."}</div></div>;
  }

  return (
    <div className="desk-grid">
      <Paper resume={doc.structured} mode="report" report={report} litFinding={lit} />
      <aside className="rail">
        <div className="panel score-panel">
          <Dial score={report.score} tone={scoreTone(report.score)} />
          <div>
            <div className="verdict" style={{ color: scoreTone(report.score) }}>
              {scoreVerdict(report.score)}
            </div>
            <div className="score-sub">
              {doc.jd_label ? `vs ${doc.jd_label}` : "no target JD"}<br />
              {report.keyword_coverage ? "70% checks + 30% keywords" : "weighted checks"}
            </div>
          </div>
        </div>

        <div className="panel">
          <p className="eyebrow">The pen's marks · tap one to see it on the paper</p>
          {findings.map((c) => (
            <button key={c.id} className={"finding" + (lit === c.id ? " lit" : "")} onClick={() => light(c.id)}>
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
            {report.keyword_coverage.found.map((k) => <span key={k} className="kchip found">{k}</span>)}
            {report.keyword_coverage.missing.map((k) => <span key={k} className="kchip missing">{k}</span>)}
          </div>
        )}

        {doc.review && (
          <div className="panel">
            <p className="eyebrow" style={{ color: "var(--teal)" }}>Recruiter's read</p>
            <p style={{ fontSize: "0.8rem", lineHeight: 1.55, color: "var(--text-dim)" }}>{doc.review.summary}</p>
          </div>
        )}
        {doc.status === "analyzing" && (
          <div className="panel" style={{ borderStyle: "dashed" }}>
            <p className="eyebrow">Recruiter's read</p>
            <p style={{ fontSize: "0.78rem", color: "var(--text-dim)" }}>Being written now; the checklist above is already final.</p>
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
              <button
                className="btn" style={{ width: "100%" }}
                onClick={startTailor}
                disabled={!doc.jd_text || doc.status === "analyzing"}
              >
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
        <span style={{ display: "none" }}>{void onDoc}</span>
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
  const [error, setError] = useState("");
  const t = doc.tailored!;
  const remaining = countMetrics(t.resume);
  const typed = [...values.values()].filter((v) => v.trim()).length;

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

  return (
    <div className="desk-grid">
      <Paper
        resume={t.resume} mode="tailored" report={doc.tailored_report}
        changes={t.changes} metricValues={values}
        onMetric={(i, v) => setValues((m) => new Map(m).set(i, v))}
      />
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
            <div className="change-note" key={i}>
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
          <button className="btn" style={{ width: "100%" }} onClick={onSend}>
            Looks right, package it
          </button>
        </div>
      </aside>
    </div>
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
        <div>
          <span className="mono" style={{ color: "var(--green)" }}>
            {doc.report?.score} → {doc.tailored_report?.score}
          </span>
          <span className="lbl">ATS readiness</span>
        </div>
        <div>
          <span className="mono" style={{ color: "var(--teal)" }}>{t.changes.length}</span>
          <span className="lbl">honest rewrites</span>
        </div>
        <div>
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
