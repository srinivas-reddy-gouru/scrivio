import { useState } from "react";
import type { ResumeDoc } from "./types";
import { Shell, useHashRoom } from "./components/Shell";
import type { RoomId } from "./components/Shell";
import { Floor } from "./rooms/Floor";
import { JobRoom } from "./rooms/JobRoom";
import { ReportStation, SendStation, TailorStation, TargetStation } from "./components/stations";

const STATIONS = ["Target", "Report", "Tailor", "Send"] as const;

/** The Desk room: the four-station resume flow (formerly the whole app). */
function DeskRoom() {
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
    <div className="room-wrap">
      <header style={{ position: "static", background: "none", border: "none", padding: "0 0 1rem" }}>
        <div className="brand"><b>The Desk</b><span>resume studio</span></div>
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

/** Rooms that still live in the classic studio during the migration. */
function ClassicRoom({ title, sub }: { title: string; sub: string }) {
  return (
    <div className="room-wrap">
      <h1 className="room-title bar-tick-left">{title}</h1>
      <p className="room-sub">{sub}</p>
      <a className="btn" href="/">Open in the classic studio</a>
      <p className="classic-note" style={{ maxWidth: 420 }}>
        This room is next in the migration queue; it moves in here with the
        full studio treatment. Your data is shared either way.
      </p>
    </div>
  );
}

const CLASSIC: Record<string, { title: string; sub: string }> = {
  newsroom: { title: "The Newsroom", sub: "Articles: the press run lands here soon." },
  interview: { title: "The Interview Room", sub: "Practice: the sealed rubric card lands here soon." },
  job: { title: "The Job Room", sub: "Dossiers and marked report cards land here soon." },
  office: { title: "The Back Office", sub: "Engine, models, and keys land here soon." },
};

export default function App() {
  const [room, go] = useHashRoom();
  const body: Record<RoomId, React.ReactNode> = {
    floor: <Floor go={go} />,
    desk: <DeskRoom />,
    newsroom: <ClassicRoom {...CLASSIC.newsroom} />,
    interview: <ClassicRoom {...CLASSIC.interview} />,
    job: <JobRoom />,
    office: <ClassicRoom {...CLASSIC.office} />,
  };
  return <Shell room={room} go={go}>{body[room]}</Shell>;
}
