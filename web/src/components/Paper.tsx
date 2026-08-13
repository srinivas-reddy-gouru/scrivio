/** The paper: a StructuredResume rendered as the document it will become.
 * Report mode derives red/amber marks from the live AtsReport; tailored
 * mode marks changed lines teal (from the change log's where-paths) and
 * renders [METRIC] placeholders as editable chips in place. */
import { Fragment, useRef, useState } from "react";
import type { AtsReport, ResumeChange, StructuredResume } from "../types";
import { changeIndex, displayNote, markForHighlight, METRIC_TOKEN } from "../marks";
import type { PaperNote } from "../marks";

interface PaperProps {
  resume: StructuredResume;
  mode: "report" | "tailored";
  report: AtsReport | null;
  changes?: ResumeChange[];
  litFinding?: string | null;
  metricValues?: Map<number, string>;
  onMetric?: (index: number, value: string) => void;
  /** Edit mode: prose fields become directly editable; blur reports the
   * new text by where-path. Metric chips render as plain text so the
   * whole sentence (placeholder included) is the user's to change. */
  onEdit?: (path: string, value: string) => void;
  /** Honesty notes placed on the lines they name: amber until answered,
   * then the line goes teal like any other change. */
  notes?: Map<string, PaperNote[]>;
  activeNote?: string | null;
  onNote?: (path: string) => void;
  answerSlot?: (path: string, notes: PaperNote[]) => React.ReactNode;
  /** Adding, as opposed to rewriting. Present only in edit mode, and only
   * ever driven by the user: the studio will rephrase what is on the
   * resume, but it will never put something new there. */
  onAdd?: (payload: AddPayload) => void;
  onRemoveEntry?: (path: string, label: string) => void;
}

export type AddPayload =
  | { kind: "bullet"; parent: string; text: string }
  | { kind: "certificate"; text: string }
  | { kind: "skill"; parent: string; text: string }
  | { kind: "work" | "projects" | "education" | "custom"; fields: Record<string, unknown> };

/** A quiet "add a line" that becomes an input in place. Deliberately not a
 * modal: adding a bullet is the most common thing here, and it should cost
 * one click and a return key. */
function AddLine({ label, onSave }: { label: string; onSave: (text: string) => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const save = () => {
    const v = text.trim();
    if (v) onSave(v);
    setText(""); setOpen(false);
  };
  if (!open) {
    return (
      <button className="add-line" onClick={() => setOpen(true)}>+ {label}</button>
    );
  }
  return (
    <div className="add-line-form">
      <textarea autoFocus value={text} rows={2}
        placeholder="Write it the way you would say it out loud, with the number first."
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save(); }
          if (e.key === "Escape") { setText(""); setOpen(false); }
        }} />
      <div className="add-line-actions">
        <button className="btn btn-sm" onClick={save} disabled={!text.trim()}>Add</button>
        <button className="btn btn-sm btn-quiet" onClick={() => { setText(""); setOpen(false); }}>
          Cancel
        </button>
      </div>
    </div>
  );
}

const ENTRY_FIELDS: Record<string, { key: string; label: string; wide?: boolean }[]> = {
  work: [
    { key: "name", label: "Employer" }, { key: "position", label: "Job title" },
    { key: "startDate", label: "Start (e.g. Mar 2023)" }, { key: "endDate", label: "End or Present" },
    { key: "summary", label: "Tech stack (optional)", wide: true },
  ],
  projects: [
    { key: "name", label: "Project" }, { key: "url", label: "Link (optional)" },
    { key: "description", label: "What it is", wide: true },
  ],
  education: [
    { key: "institution", label: "School" }, { key: "area", label: "Field" },
    { key: "studyType", label: "Degree (e.g. B.S.)" }, { key: "endDate", label: "Year" },
  ],
  custom: [{ key: "name", label: "Section name, e.g. Publications", wide: true }],
};

/** The form for a whole new entry. Employer, title, and dates are typed by
 * the user here, which is the one place in the studio that is allowed:
 * these are facts about their life, not text a model is generating. */
function NewEntry({ kind, onSave, onCancel }: {
  kind: "work" | "projects" | "education" | "custom";
  onSave: (fields: Record<string, unknown>) => void; onCancel: () => void;
}) {
  const [fields, setFields] = useState<Record<string, string>>({});
  const [first, setFirst] = useState("");
  const set = (k: string, v: string) => setFields((f) => ({ ...f, [k]: v }));
  const required = kind === "education" ? "institution" : "name";
  const firstLabel = kind === "custom" ? "First line of the section" : "First bullet (optional)";

  return (
    <div className="new-entry">
      <div className="new-entry-grid">
        {ENTRY_FIELDS[kind].map((f) => (
          <label key={f.key} className={f.wide ? "wide" : ""}>
            <span>{f.label}</span>
            <input value={fields[f.key] ?? ""} onChange={(e) => set(f.key, e.target.value)} />
          </label>
        ))}
        <label className="wide">
          <span>{firstLabel}</span>
          <textarea rows={2} value={first} onChange={(e) => setFirst(e.target.value)} />
        </label>
      </div>
      <div className="add-line-actions">
        <button className="btn btn-sm" disabled={!(fields[required] ?? "").trim()}
          onClick={() => {
            const payload: Record<string, unknown> = { ...fields };
            if (first.trim()) {
              payload[kind === "custom" ? "items" : "highlights"] = [first.trim()];
            }
            onSave(payload);
          }}>Add to the resume</button>
        <button className="btn btn-sm btn-quiet" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

function EditableText({ path, text, onEdit, as: Tag = "p", className = "" }: {
  path: string; text: string; onEdit: (path: string, value: string) => void;
  as?: "p" | "li" | "div"; className?: string;
}) {
  return (
    <Tag
      className={(className + " editable-field").trim()}
      contentEditable
      suppressContentEditableWarning
      role="textbox"
      aria-label={`Edit ${path}`}
      onBlur={(e: React.FocusEvent<HTMLElement>) => {
        const v = (e.currentTarget.textContent || "").replace(/\s+/g, " ").trim();
        if (v !== text.trim()) onEdit(path, v);
      }}
    >
      {text}
    </Tag>
  );
}

/** Renders text, replacing each [METRIC] with an editable chip. The
 * counter object keeps occurrence numbering aligned with the server's
 * canonical traversal across the whole document render. */
function MetricText({ text, counter, values, onMetric }: {
  text: string;
  counter: { n: number };
  values?: Map<number, string>;
  onMetric?: (index: number, value: string) => void;
}) {
  if (!text.includes(METRIC_TOKEN) || !onMetric) return <>{text}</>;
  const parts = text.split(METRIC_TOKEN);
  return (
    <>
      {parts.map((part, i) => {
        if (i === parts.length - 1) return <Fragment key={i}>{part}</Fragment>;
        const index = counter.n++;
        const value = values?.get(index) ?? "";
        return (
          <Fragment key={i}>
            {part}
            <span
              className={"metric-chip" + (value ? " filled" : "")}
              contentEditable
              suppressContentEditableWarning
              role="textbox"
              aria-label="Fill in your real number"
              onFocus={(e) => {
                if (e.currentTarget.textContent === METRIC_TOKEN)
                  e.currentTarget.textContent = "";
              }}
              onBlur={(e) => {
                const v = (e.currentTarget.textContent || "").trim();
                if (!v) e.currentTarget.textContent = METRIC_TOKEN;
                onMetric(index, v);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") { e.preventDefault(); (e.target as HTMLElement).blur(); }
              }}
            >
              {value || METRIC_TOKEN}
            </span>
          </Fragment>
        );
      })}
    </>
  );
}

export function Paper({
  resume, mode, report, changes = [], litFinding, metricValues, onMetric, onEdit,
  notes, activeNote, onNote, answerSlot, onAdd, onRemoveEntry,
}: PaperProps) {
  const [adding, setAdding] = useState<"work" | "projects" | "education" | "custom" | null>(null);
  /** The remove control sits on the entry it removes, and only in edit
   * mode, so a stray click on a read-only paper cannot delete a job. */
  const removeBtn = (path: string, label: string) => onRemoveEntry && (
    <button className="entry-remove" title={`Remove ${label}`}
      aria-label={`Remove ${label}`}
      onClick={() => onRemoveEntry(path, label)}>✕</button>
  );
  const counter = useRef({ n: 0 });
  counter.current.n = 0; // occurrence numbering restarts every render
  const idx = changeIndex(changes);
  const b = resume.basics;

  /** Props that turn any line into a note flag when a note names it.
   * Amber outranks teal: an open question matters more than a done edit. */
  const flag = (path: string) => {
    const hits = notes?.get(path);
    if (!hits?.length) return null;
    return {
      className: "marked mark-amber note-flag" + (activeNote === path ? " open" : ""),
      "data-note": displayNote(hits[0].text),
      role: "button" as const,
      tabIndex: 0,
      title: "Answer this honesty note",
      onClick: () => onNote?.(path),
      onKeyDown: (e: React.KeyboardEvent) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onNote?.(path); }
      },
    };
  };
  const slot = (path: string) => {
    const hits = notes?.get(path);
    return activeNote === path && hits?.length ? answerSlot?.(path, hits) : null;
  };

  const lit = (findingIds: string[], tone: string) =>
    litFinding && findingIds.includes(litFinding) ? ` lit-${tone}` : "";

  const metric = (text: string) => (
    <MetricText text={text} counter={counter.current} values={metricValues} onMetric={onMetric} />
  );

  return (
    <div className="paper">
      <h1>{b.name || "Your name"}</h1>
      {b.label && (
        <div
          className={"headline" + (mode === "tailored" && idx.byField.has("basics.label") ? " marked mark-teal note-tip" : "")}
          data-note={mode === "tailored" ? idx.byField.get("basics.label")?.what : undefined}
        >
          {b.label}
        </div>
      )}
      <div className={"contact marked mark-teal" + lit(["contact-info"], "teal")} data-finding="contact-info">
        {[b.email, b.phone, b.location, b.url].filter(Boolean).join(" · ")}
      </div>

      {b.summary && (
        <>
          <h2>Summary</h2>
          {onEdit ? (
            <EditableText path="basics.summary" text={b.summary} onEdit={onEdit} />
          ) : (
            <>
              <p {...(flag("basics.summary") ?? {
                className: mode === "tailored" && idx.byField.has("basics.summary") ? "marked mark-teal note-tip" : "",
                "data-note": mode === "tailored" ? idx.byField.get("basics.summary")?.what : undefined,
              })}>
                {metric(b.summary)}
              </p>
              {slot("basics.summary")}
            </>
          )}
        </>
      )}

      {resume.work.length > 0 && (
        <>
          <h2 data-finding="section-headers">Experience</h2>
          {resume.work.map((w, wi) => (
            <Fragment key={wi}>
              <h3>
                {[w.position, w.name].filter(Boolean).join(", ")}
                {removeBtn(`work[${wi}]`, w.name || "this job")}
              </h3>
              <div className="dates" data-finding="dates">
                {[w.startDate, w.endDate].filter(Boolean).join(" - ")}
              </div>
              {w.summary && (onEdit
                ? <EditableText path={`work[${wi}].summary`} text={w.summary} onEdit={onEdit} />
                : <>
                    <p {...(flag(`work[${wi}].summary`) ?? {})}>{metric(w.summary)}</p>
                    {slot(`work[${wi}].summary`)}
                  </>)}
              <ul>
                {w.highlights.map((h, hi) => {
                  if (onEdit) {
                    return (
                      <EditableText key={hi} as="li"
                        path={`work[${wi}].highlights[${hi}]`} text={h} onEdit={onEdit} />
                    );
                  }
                  let cls = ""; let underline: string | undefined;
                  if (mode === "report") {
                    const mark = markForHighlight(h, report);
                    if (mark) {
                      cls = `marked mark-${mark.tone}` + lit(mark.findings, mark.tone);
                      underline = mark.underline;
                    }
                  } else if (idx.byHighlight.has(`${wi}:${hi}`)) {
                    cls = "marked mark-teal";
                  }
                  const findingId = mode === "report" && cls.includes("mark-red")
                    ? "weak-language"
                    : cls.includes("mark-amber") ? "quantification" : undefined;
                  const path = `work[${wi}].highlights[${hi}]`;
                  const noteFlag = flag(path);
                  const note = mode === "tailored" ? idx.byHighlight.get(`${wi}:${hi}`)?.what : undefined;
                  return (
                    <li key={hi} {...(noteFlag ?? {
                      className: cls + (note ? " note-tip" : ""),
                      "data-finding": findingId, "data-note": note,
                    })}>
                      {underline ? (
                        <UnderlinedText text={h} phrase={underline} />
                      ) : (
                        metric(h)
                      )}
                      {slot(path)}
                    </li>
                  );
                })}
              </ul>
              {onAdd && (
                <AddLine label="Add a bullet"
                  onSave={(text) => onAdd({ kind: "bullet", parent: `work[${wi}]`, text })} />
              )}
            </Fragment>
          ))}
        </>
      )}

      {resume.projects.length > 0 && (
        <>
          <h2>Projects</h2>
          {resume.projects.map((p, pi) => (
            <Fragment key={pi}>
              <h3>
                {p.name || "Project"}
                {removeBtn(`projects[${pi}]`, p.name || "this project")}
              </h3>
              {p.description && (onEdit
                ? <EditableText path={`projects[${pi}].description`} text={p.description} onEdit={onEdit} />
                : <>
                    <p {...(flag(`projects[${pi}].description`) ?? {})}>{metric(p.description)}</p>
                    {slot(`projects[${pi}].description`)}
                  </>)}
              <ul>
                {p.highlights.map((h, hi) => onEdit ? (
                  <EditableText key={hi} as="li"
                    path={`projects[${pi}].highlights[${hi}]`} text={h} onEdit={onEdit} />
                ) : (
                  <li key={hi} {...(flag(`projects[${pi}].highlights[${hi}]`) ?? {})}>
                    {metric(h)}
                    {slot(`projects[${pi}].highlights[${hi}]`)}
                  </li>
                ))}
              </ul>
              {onAdd && (
                <AddLine label="Add a bullet"
                  onSave={(text) => onAdd({ kind: "bullet", parent: `projects[${pi}]`, text })} />
              )}
            </Fragment>
          ))}
        </>
      )}

      {resume.education.length > 0 && (
        <>
          <h2>Education</h2>
          {resume.education.map((e, ei) => (
            <Fragment key={ei}>
              <h3>
                {[[e.studyType, e.area].filter(Boolean).join(" in "), e.institution]
                  .filter(Boolean).join(", ")}
                {removeBtn(`education[${ei}]`, e.institution || "this entry")}
              </h3>
              <div className="dates">
                {[[e.startDate, e.endDate].filter(Boolean).join(" - "), e.score]
                  .filter(Boolean).join(" · ")}
              </div>
            </Fragment>
          ))}
        </>
      )}

      {resume.skills.length > 0 && (
        <>
          <h2>Skills</h2>
          <p
            className={
              mode === "report"
                ? "marked mark-amber" + lit(["keywords", "skills-section"], "amber")
                : idx.skills ? "marked mark-teal" : ""
            }
            data-finding="keywords"
          >
            {resume.skills.map((sk, i) => (
              <Fragment key={i}>
                {i > 0 && <> &nbsp;·&nbsp; </>}
                {sk.name && <b>{sk.name}: </b>}
                {sk.keywords.join(", ")}
              </Fragment>
            ))}
          </p>
        </>
      )}

      {(resume.certificates.length > 0 || onAdd) && (
        <>
          <h2>Certifications</h2>
          <ul>
            {resume.certificates.map((c, i) => onEdit ? (
              <EditableText key={i} as="li" path={`certificates[${i}]`} text={c} onEdit={onEdit} />
            ) : <li key={i}>{c}</li>)}
          </ul>
          {onAdd && (
            <AddLine label="Add a certification"
              onSave={(text) => onAdd({ kind: "certificate", text })} />
          )}
        </>
      )}

      {(resume.custom ?? []).map((section, ci) => (
        <Fragment key={ci}>
          <h2>
            {section.name || "Additional"}
            {removeBtn(`custom[${ci}]`, section.name || "this section")}
          </h2>
          <ul>
            {section.items.map((item, ii) => onEdit ? (
              <EditableText key={ii} as="li"
                path={`custom[${ci}].items[${ii}]`} text={item} onEdit={onEdit} />
            ) : <li key={ii}>{metric(item)}</li>)}
          </ul>
          {onAdd && (
            <AddLine label="Add a line"
              onSave={(text) => onAdd({ kind: "bullet", parent: `custom[${ci}]`, text })} />
          )}
        </Fragment>
      ))}

      {onAdd && (
        <div className="paper-add">
          {adding ? (
            <NewEntry kind={adding} onCancel={() => setAdding(null)}
              onSave={(fields) => { onAdd({ kind: adding, fields }); setAdding(null); }} />
          ) : (
            <>
              <span className="paper-add-label">Missing something?</span>
              <button className="btn btn-sm btn-quiet" onClick={() => setAdding("work")}>+ Job</button>
              <button className="btn btn-sm btn-quiet" onClick={() => setAdding("projects")}>+ Project</button>
              <button className="btn btn-sm btn-quiet" onClick={() => setAdding("education")}>+ Education</button>
              <button className="btn btn-sm btn-quiet" onClick={() => setAdding("custom")}>+ Section</button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function UnderlinedText({ text, phrase }: { text: string; phrase: string }) {
  const at = text.toLowerCase().indexOf(phrase.toLowerCase());
  if (at === -1) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <span className="pen">{text.slice(at, at + phrase.length)}</span>
      {text.slice(at + phrase.length)}
    </>
  );
}
