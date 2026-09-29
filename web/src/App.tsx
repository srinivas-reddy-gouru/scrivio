import { useState } from "react";
import type { ResumeDoc } from "./types";
import { Shell, useHashRoom } from "./components/Shell";
import type { RoomId } from "./components/Shell";
import { Floor } from "./rooms/Floor";
import { JobRoom } from "./rooms/JobRoom";
import { InterviewRoom } from "./rooms/InterviewRoom";
import { ArticleStudio } from "./rooms/ArticleStudio";
import { BackOffice } from "./rooms/BackOffice";
import { ReportStation, SendStation, TailorStation, TargetStation } from "./components/stations";

const STATIONS = ["Target", "Report", "Tailor", "Send"] as const;

/** The Desk room: the four-station resume flow (formerly the whole app). */
function DeskRoom() {
  const [station, setStation] = useState(1);
  const [doc, setDocState] = useState<ResumeDoc | null>(null);

  // Responses do not arrive in the order they were asked for. A status
  // poll sent before a save can land after it, carrying the resume as it
  // was, and used to replace the saved one on screen: a number just typed
  // turned back into a placeholder. The server stamps every save, so an
  // older copy of the same resume is recognisable and is dropped.
  const setDoc = (next: ResumeDoc | null) => setDocState((current) => {
    if (!next || !current || next.resume_id !== current.resume_id) return next;
    if (next.updated_at && current.updated_at && next.updated_at < current.updated_at) {
      return current;
    }
    return next;
  });

  const openDoc = (d: ResumeDoc) => {
    setDoc(d);
    if (d.status !== "analyzing") setStation(2);
  };
  const openTailored = (d: ResumeDoc) => { setDoc(d); setStation(3); };

  const unlocked = (n: number) =>
    n === 1 || (n === 2 && !!doc) || ((n === 3 || n === 4) && !!doc?.tailored);

  return (
    <div className="room-wrap">
      <h1 className="room-title bar-tick-left">Resume</h1>
      <p className="room-sub">A checklist report you can verify, tailoring that is checked against your original, and exports in four formats.</p>
      <header style={{ position: "static", background: "none", border: "none", padding: "0 0 1rem", justifyContent: "center" }}>
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
      <div key={station}>
        {station === 1 && <TargetStation onDoc={openDoc} />}
        {station === 2 && doc && (
          <ReportStation doc={doc} onDoc={setDoc} onTailored={openTailored} />
        )}
        {station === 3 && doc?.tailored && (
          <TailorStation doc={doc} onDoc={setDoc} onSend={() => setStation(4)} />
        )}
        {station === 4 && doc?.tailored && <SendStation doc={doc} />}
      </div>
    </div>
  );
}

export default function App() {
  const [room, go] = useHashRoom();
  const body: Record<RoomId, React.ReactNode> = {
    floor: <Floor go={go} />,
    desk: <DeskRoom />,
    newsroom: <ArticleStudio />,
    interview: <InterviewRoom />,
    job: <JobRoom />,
    office: <BackOffice />,
  };
  return <Shell room={room} go={go}>{body[room]}</Shell>;
}
