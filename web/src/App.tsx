import { useState } from "react";
import type { ResumeDoc } from "./types";
import { ReportStation, SendStation, TailorStation, TargetStation } from "./components/stations";

const STATIONS = ["Target", "Report", "Tailor", "Send"] as const;

export default function App() {
  const [station, setStation] = useState(1);
  const [doc, setDoc] = useState<ResumeDoc | null>(null);

  const openDoc = (d: ResumeDoc) => {
    setDoc(d);
    if (d.status !== "analyzing") setStation(2);
  };
  const openTailored = (d: ResumeDoc) => { setDoc(d); setStation(3); };

  const unlocked = (n: number) =>
    n === 1 || (n === 2 && !!doc) || ((n === 3 || n === 4) && !!doc?.tailored);

  return (
    <>
      <header>
        <div className="brand"><b>Scrivio</b><span>Resume Desk</span></div>
        <nav className="stations" aria-label="Steps">
          {STATIONS.map((label, i) => {
            const n = i + 1;
            return (
              <span key={label} style={{ display: "flex", alignItems: "center" }}>
                {i > 0 && <span className="station-rule" />}
                <button
                  className={"station" + (station === n ? " active" : station > n ? " done" : "")}
                  disabled={!unlocked(n)}
                  onClick={() => setStation(n)}
                >
                  <span className="dot">{station > n ? "✓" : n}</span>
                  {label}
                </button>
              </span>
            );
          })}
        </nav>
      </header>

      <main className="stage" key={station}>
        {station === 1 && <TargetStation onDoc={openDoc} />}
        {station === 2 && doc && (
          <ReportStation doc={doc} onDoc={setDoc} onTailored={openTailored} />
        )}
        {station === 3 && doc?.tailored && (
          <TailorStation doc={doc} onDoc={setDoc} onSend={() => setStation(4)} />
        )}
        {station === 4 && doc?.tailored && <SendStation doc={doc} />}
      </main>
    </>
  );
}
