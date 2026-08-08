/** The Job Room: every target is a dossier (resume + JD clipped
 * together), and every completed screen is a marked report card. The
 * voice screen itself still happens in the classic Interview Room until
 * migration step 3. */
import { useEffect, useMemo, useState } from "react";
import { api, fmtElapsed } from "../api";
import type {
  InterviewDetail, InterviewSessionItem, JobProfileDetail, JobProfileSummary,
} from "../types";

const EVIDENCE_COLOR: Record<string, string> = {
  strong: "var(--green)", partial: "var(--amber)", missing: "var(--redpen)",
};

export function JobRoom() {
  const [profiles, setProfiles] = useState<JobProfileSummary[]>([]);
  const [detail, setDetail] = useState<JobProfileDetail | null>(null);
  const [screens, setScreens] = useState<InterviewSessionItem[]>([]);
  const [scorecard, setScorecard] = useState<InterviewDetail | null>(null);
  const [creating, setCreating] = useState(false);

  const refresh = () => {
    api.listJobProfiles().then(setProfiles).catch(() => {});
    api.listInterviews()
      .then((all) => setScreens(all.filter((s) => s.mode === "job")))
      .catch(() => {});
  };
  useEffect(refresh, []);

  const openProfile = (id: string) =>
    api.getJobProfile(id).then((d) => { setDetail(d); setScorecard(null); }).catch(() => {});

  const openScorecard = (sessionId: string) =>
    api.getInterview(sessionId).then(setScorecard).catch(() => {});

  if (creating) {
    return (
      <NewDossier
        onDone={(d) => { setCreating(false); setDetail(d); refresh(); }}
        onCancel={() => setCreating(false)}
      />
    );
  }

  if (detail) {
    return (
      <Dossier
        detail={detail} screens={screens} scorecard={scorecard}
        onBack={() => { setDetail(null); setScorecard(null); }}
        onOpenScorecard={openScorecard}
      />
    );
  }

  return (
    <div className="room-wrap">
      <h1 className="room-title bar-tick-left">The Job Room</h1>
      <p className="room-sub">Every target is a dossier: your resume and the JD, clipped together and worked.</p>
      <div style={{ maxWidth: 700 }}>
        {profiles.map((p, i) => (
          <button key={p.profile_id} className="dossier-card"
            style={{ "--i": i } as React.CSSProperties}
            onClick={() => openProfile(p.profile_id)}>
            <span>
              <b>{p.role_title}{p.company ? ` @ ${p.company}` : ""}</b>
              <span className="meta">
                {[p.seniority, p.location].filter(Boolean).join(" · ") || "no details"} ·{" "}
                {screens.filter((s) => s.topic.includes(p.role_title)).length} screen(s) taken
              </span>
            </span>
            <span className="glyph" style={{ color: "var(--text-faint)" }}>→</span>
          </button>
        ))}
        {profiles.length === 0 && (
          <p className="classic-note">No dossiers yet. Open your first target below.</p>
        )}
        <button className="btn" style={{ marginTop: "0.8rem" }} onClick={() => setCreating(true)}>
          + Open a new dossier
        </button>
      </div>
    </div>
  );
}

/* ── The dossier: clip header, Fit / Screens tabs, report card ── */

function Dossier({ detail, screens, scorecard, onBack, onOpenScorecard }: {
  detail: JobProfileDetail;
  screens: InterviewSessionItem[];
  scorecard: InterviewDetail | null;
  onBack: () => void;
  onOpenScorecard: (id: string) => void;
}) {
  const [tab, setTab] = useState<"fit" | "screens">("fit");
  const { profile, analysis } = detail;
  const mine = useMemo(
    () => screens.filter((s) => s.topic.includes(profile.role_title)),
    [screens, profile.role_title],
  );

  return (
    <div className="room-wrap">
      <button className="btn btn-quiet" style={{ marginBottom: "1rem" }} onClick={onBack}>
        ← All dossiers
      </button>
      <div className="dossier">
        <div className="clip">
          <b>{profile.role_title}{profile.company ? ` @ ${profile.company}` : ""}</b>
          <span className="meta">
            resume + JD clipped · {mine.length} screen(s) taken
            {profile.seniority ? ` · ${profile.seniority}` : ""}
          </span>
          <a className="btn btn-quiet" href="/">Take the screen → (classic room)</a>
        </div>
        <div className="tabs">
          <button className={"tab" + (tab === "fit" ? " on" : "")} onClick={() => setTab("fit")}>Fit</button>
          <button className={"tab" + (tab === "screens" ? " on" : "")} onClick={() => setTab("screens")}>
            Screens ({mine.length})
          </button>
        </div>
        {tab === "fit" ? (
          <div className="dossier-body">
            {analysis.competencies.map((c) => (
              <div className="comp-row" key={c.name}>
                <span className="m" style={{ background: EVIDENCE_COLOR[c.evidence_in_resume] || "var(--stroke)" }} />
                <span>
                  <b style={{ fontSize: "0.8rem" }}>{c.name}</b>
                  <span style={{ color: "var(--text-dim)", fontSize: "0.72rem", display: "block" }}>
                    {c.why_it_matters}
                  </span>
                </span>
                <span className="note">{c.evidence_in_resume} · {c.probe_note}</span>
              </div>
            ))}
            {analysis.gaps.length > 0 && (
              <p className="classic-note">The interviewer will probe: {analysis.gaps.join(" · ")}</p>
            )}
          </div>
        ) : (
          <div className="dossier-body">
            {mine.map((s) => (
              <button key={s.session_id} className="dossier-card" onClick={() => onOpenScorecard(s.session_id)}>
                <span>
                  <b>{new Date(s.created_at).toLocaleDateString()} · {s.answered}/{s.total} answered</b>
                  <span className="meta">
                    {s.complete
                      ? `complete · avg ${s.average_score ?? "-"}/10`
                      : "in progress (finish it in the classic room)"}
                  </span>
                </span>
                <span className="glyph" style={{ color: "var(--text-faint)" }}>
                  {s.complete ? "open scorecard →" : "→"}
                </span>
              </button>
            ))}
            {mine.length === 0 && (
              <p className="classic-note">No screens yet for this target. Take the first one from the clip above.</p>
            )}
          </div>
        )}
      </div>

      {scorecard?.summary?.scorecard && (
        <ReportCard detail={scorecard} />
      )}
    </div>
  );
}

/* ── The marked report card ── */

function ReportCard({ detail }: { detail: InterviewDetail }) {
  const sc = detail.summary!.scorecard!;
  const barColor = (score: number | null) =>
    score == null ? "var(--stroke)"
      : score >= 8 ? "var(--green)" : score >= 6 ? "var(--amber)" : "var(--redpen)";
  const byCompetency = useMemo(() => {
    const groups = new Map<string, typeof sc.study_plan>();
    for (const r of sc.study_plan) {
      groups.set(r.competency, [...(groups.get(r.competency) ?? []), r]);
    }
    return [...groups.entries()];
  }, [sc.study_plan]);

  return (
    <div className="report-card">
      <div className="rc-head">
        <div>
          <h1 className="font-display" style={{ fontSize: "1.15rem" }}>Interview scorecard</h1>
          <span style={{ fontSize: "0.7rem", color: "var(--ink-dim)" }}>
            {detail.topic} · {detail.summary!.answered} answered · avg {detail.summary!.average_score ?? "-"}/10
          </span>
        </div>
        {sc.hire_signal && <span className="hire-stamp">{sc.hire_signal}</span>}
      </div>

      <h2>Competencies</h2>
      {sc.competency_scores.map((c) => (
        <div className="comp-line" key={c.name} title={[...c.evidence, ...c.gaps].join(" · ")}>
          <span className="nm">{c.name}</span>
          <span className="bar"><i style={{ width: `${(c.score ?? 0) * 10}%`, background: barColor(c.score) }} /></span>
          <span className="sc">{c.score != null ? c.score.toFixed(1) : "n/a"}</span>
        </div>
      ))}

      {sc.debrief && (
        <>
          <h2>The panel's notes</h2>
          <p className="pen-note">“{sc.debrief}”</p>
        </>
      )}

      {sc.requirement_coverage.length > 0 && (
        <>
          <h2>JD requirement coverage</h2>
          {sc.requirement_coverage.map((r) => (
            <div className="cover-row" key={r.requirement}>
              <span className={r.status === "met" ? "ok" : r.status === "partial" ? "part" : "miss"}>
                {r.status === "met" ? "✓" : r.status === "partial" ? "◐" : "✗"}
              </span>
              {r.requirement}
              <span style={{ marginLeft: "auto", fontSize: "0.68rem", color: "var(--ink-dim)" }}>{r.status}</span>
            </div>
          ))}
        </>
      )}

      {byCompetency.length > 0 && (
        <>
          <h2>Study plan · trust-ranked</h2>
          {byCompetency.map(([name, resources]) => (
            <div className="study-row" key={name}>
              <b>{name}:</b>{" "}
              {resources.map((r, i) => (
                <span key={r.url}>
                  {i > 0 && " · "}
                  <a href={r.url} target="_blank" rel="noopener">{r.title}</a>{" "}
                  <span className="trust-tick">[{r.trust_score.toFixed(2)}]</span>
                </span>
              ))}
            </div>
          ))}
        </>
      )}
    </div>
  );
}

/* ── New dossier ── */

function NewDossier({ onDone, onCancel }: {
  onDone: (d: JobProfileDetail) => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState({
    role_title: "", company: "", location: "", seniority: "senior",
    extra_notes: "", job_description: "", jd_url: "", resume_text: "",
  });
  const [file, setFile] = useState<{ name: string; b64: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState("");
  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }));

  useEffect(() => {
    if (!busy) return;
    const t0 = Date.now();
    const t = setInterval(() => setElapsed(Math.floor((Date.now() - t0) / 1000)), 1000);
    return () => clearInterval(t);
  }, [busy]);

  const submit = async () => {
    setBusy(true); setError("");
    try {
      const d = await api.createJobProfile({
        ...form,
        resume_file_b64: file?.b64,
        resume_filename: file?.name,
        resume_text: file ? "" : form.resume_text,
      });
      onDone(d);
    } catch (e) { setError((e as Error).message); setBusy(false); }
  };

  if (busy) {
    return (
      <div className="room-wrap">
        <div className="working">
          <p className="line font-display" style={{ fontSize: "1.1rem" }}>Reading the dossier…</p>
          <p className="line mono">{fmtElapsed(elapsed)} <span style={{ color: "var(--text-faint)" }}>/ ~1:00 est.</span></p>
          <p className="line" style={{ fontSize: "0.74rem" }}>
            Deriving the competency rubric from the JD and mapping your resume's evidence against it.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="room-wrap jr-form" style={{ maxWidth: 700 }}>
      <h1 className="room-title bar-tick-left">Open a dossier</h1>
      <p className="room-sub">The role, the JD, and your resume. Scrivio derives the rubric from the JD itself.</p>
      <div className="row2">
        <div><label>Role title *</label>
          <input type="text" value={form.role_title} onChange={(e) => set("role_title", e.target.value)}
            placeholder="Senior Backend Engineer" /></div>
        <div><label>Company</label>
          <input type="text" value={form.company} onChange={(e) => set("company", e.target.value)}
            placeholder="Stripe" /></div>
        <div><label>Location</label>
          <input type="text" value={form.location} onChange={(e) => set("location", e.target.value)}
            placeholder="Remote / Austin, TX" /></div>
        <div><label>Seniority</label>
          <select value={form.seniority} onChange={(e) => set("seniority", e.target.value)}>
            <option value="">Not specified</option>
            <option value="junior">Junior</option><option value="mid">Mid-level</option>
            <option value="senior">Senior</option><option value="staff">Staff / Principal</option>
            <option value="lead">Lead / Manager</option>
          </select></div>
      </div>
      <label>Job description * (paste, or a posting URL)</label>
      <input type="text" value={form.jd_url} onChange={(e) => set("jd_url", e.target.value)}
        placeholder="https://… (fetches the posting)" />
      <textarea value={form.job_description} onChange={(e) => set("job_description", e.target.value)}
        placeholder="…or paste the job description" />
      <label>Resume * (paste, or attach)</label>
      <input type="file" accept=".pdf,.docx,.txt,.md"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (!f) return;
          const reader = new FileReader();
          reader.onload = () => setFile({ name: f.name, b64: String(reader.result).split(",")[1] || "" });
          reader.readAsDataURL(f);
        }} />
      {file
        ? <p className="classic-note">{file.name} attached ✓</p>
        : <textarea value={form.resume_text} onChange={(e) => set("resume_text", e.target.value)}
            placeholder="…or paste your resume text" />}
      <label>Anything else? (recruiter hints, round focus)</label>
      <input type="text" value={form.extra_notes} onChange={(e) => set("extra_notes", e.target.value)} />
      {error && <div className="errbox" style={{ marginBottom: "0.8rem" }}>{error}</div>}
      <div style={{ display: "flex", gap: "0.7rem" }}>
        <button className="btn" onClick={submit}
          disabled={!form.role_title.trim() || (!form.job_description.trim() && !form.jd_url.trim())
            || (!form.resume_text.trim() && !file)}>
          Analyze my fit
        </button>
        <button className="btn btn-quiet" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
