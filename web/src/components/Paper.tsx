/** The paper: a StructuredResume rendered as the document it will become.
 * Report mode derives red/amber marks from the live AtsReport; tailored
 * mode marks changed lines teal (from the change log's where-paths) and
 * renders [METRIC] placeholders as editable chips in place. */
import { Fragment, useRef } from "react";
import type { AtsReport, ResumeChange, StructuredResume } from "../types";
import { changeIndex, markForHighlight, METRIC_TOKEN } from "../marks";

interface PaperProps {
  resume: StructuredResume;
  mode: "report" | "tailored";
  report: AtsReport | null;
  changes?: ResumeChange[];
  litFinding?: string | null;
  metricValues?: Map<number, string>;
  onMetric?: (index: number, value: string) => void;
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
  resume, mode, report, changes = [], litFinding, metricValues, onMetric,
}: PaperProps) {
  const counter = useRef({ n: 0 });
  counter.current.n = 0; // occurrence numbering restarts every render
  const idx = changeIndex(changes);
  const b = resume.basics;

  const lit = (findingIds: string[], tone: string) =>
    litFinding && findingIds.includes(litFinding) ? ` lit-${tone}` : "";

  const metric = (text: string) => (
    <MetricText text={text} counter={counter.current} values={metricValues} onMetric={onMetric} />
  );

  return (
    <div className="paper">
      <h1>{b.name || "Your name"}</h1>
      {b.label && (
        <div className={"headline" + (mode === "tailored" && idx.byField.has("basics.label") ? " marked mark-teal" : "")}>
          {b.label}
        </div>
      )}
      <div className={"contact marked mark-teal" + lit(["contact-info"], "teal")} data-finding="contact-info">
        {[b.email, b.phone, b.location, b.url].filter(Boolean).join(" · ")}
      </div>

      {b.summary && (
        <>
          <h2>Summary</h2>
          <p className={mode === "tailored" && idx.byField.has("basics.summary") ? "marked mark-teal" : ""}>
            {metric(b.summary)}
          </p>
        </>
      )}

      {resume.work.length > 0 && (
        <>
          <h2 data-finding="section-headers">Experience</h2>
          {resume.work.map((w, wi) => (
            <Fragment key={wi}>
              <h3>{[w.position, w.name].filter(Boolean).join(", ")}</h3>
              <div className="dates" data-finding="dates">
                {[w.startDate, w.endDate].filter(Boolean).join(" - ")}
              </div>
              {w.summary && <p>{metric(w.summary)}</p>}
              <ul>
                {w.highlights.map((h, hi) => {
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
                  return (
                    <li key={hi} className={cls} data-finding={findingId} title={
                      mode === "tailored" ? idx.byHighlight.get(`${wi}:${hi}`)?.what : undefined
                    }>
                      {underline ? (
                        <UnderlinedText text={h} phrase={underline} />
                      ) : (
                        metric(h)
                      )}
                    </li>
                  );
                })}
              </ul>
            </Fragment>
          ))}
        </>
      )}

      {resume.projects.length > 0 && (
        <>
          <h2>Projects</h2>
          {resume.projects.map((p, pi) => (
            <Fragment key={pi}>
              <h3>{p.name || "Project"}</h3>
              {p.description && <p>{metric(p.description)}</p>}
              <ul>
                {p.highlights.map((h, hi) => <li key={hi}>{metric(h)}</li>)}
              </ul>
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

      {resume.certificates.length > 0 && (
        <>
          <h2>Certifications</h2>
          <ul>
            {resume.certificates.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        </>
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
