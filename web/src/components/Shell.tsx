/** The Studio shell: sidebar of rooms + the command palette.
 * Hash-routed (#/floor, #/desk, …) with no router dependency. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { ArticleSummary, InterviewSessionItem, ResumeSummaryItem } from "../types";

export type RoomId = "floor" | "newsroom" | "interview" | "job" | "desk" | "office";

export const ROOMS: Array<{ id: RoomId; label: string; glyph: string }> = [
  { id: "floor", label: "The Floor", glyph: "⌂" },
  { id: "newsroom", label: "Article Studio", glyph: "✎" },
  { id: "interview", label: "Interview Room", glyph: "◉" },
  { id: "job", label: "Job Room", glyph: "▤" },
  { id: "desk", label: "The Desk", glyph: "≡" },
];

export function useHashRoom(): [RoomId, (r: RoomId) => void] {
  const read = (): RoomId => {
    const h = window.location.hash.replace(/^#\/?/, "");
    return (ROOMS.some((r) => r.id === h) || h === "office" ? h : "floor") as RoomId;
  };
  const [room, setRoom] = useState<RoomId>(read);
  useEffect(() => {
    const onHash = () => setRoom(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const go = useCallback((r: RoomId) => { window.location.hash = `/${r}`; }, []);
  return [room, go];
}

interface PaletteItem {
  title: string; kind: string; glyph: string;
  action: () => void;
}

export function Shell({ room, go, children }: {
  room: RoomId;
  go: (r: RoomId) => void;
  children: React.ReactNode;
}) {
  const [health, setHealth] = useState<{ label: string; up: boolean } | null>(null);
  const [palOpen, setPalOpen] = useState(false);

  useEffect(() => {
    api.settings()
      .then((s) => setHealth({
        up: true,
        label: s.resolved_provider === "claude-cli"
          ? `subscription (${s.active_cli || "claude"})`
          : `${s.resolved_provider} api`,
      }))
      .catch(() => setHealth({ up: false, label: "backend unreachable" }));
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault(); setPalOpen((o) => !o);
      }
      if (e.key === "Escape") setPalOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="studio">
      <nav className="side">
        <div className="brand-block">
          <div className="brand-name">Scrivio</div>
          <div className="health">
            <i className={health?.up ? "" : "down"} /> {health?.label ?? "checking…"}
          </div>
        </div>
        {ROOMS.map((r) => (
          <button key={r.id} className={"room-btn" + (room === r.id ? " on" : "")}
            onClick={() => go(r.id)}>
            <span className="glyph">{r.glyph}</span> {r.label}
          </button>
        ))}
        <div className="spacer" />
        <button className={"room-btn" + (room === "office" ? " on" : "")}
          onClick={() => go("office")}>
          <span className="glyph">⚙</span> Back Office
        </button>
        <div className="kbd-hint"><kbd>⌘K</kbd> jump anywhere</div>
      </nav>
      <main className="room-main">{children}</main>
      {palOpen && <Palette go={(r) => { go(r); setPalOpen(false); }} close={() => setPalOpen(false)} />}
    </div>
  );
}

function Palette({ go, close }: { go: (r: RoomId) => void; close: () => void }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const [articles, setArticles] = useState<ArticleSummary[]>([]);
  const [resumes, setResumes] = useState<ResumeSummaryItem[]>([]);
  const [sessions, setSessions] = useState<InterviewSessionItem[]>([]);

  useEffect(() => {
    api.listArticles().then(setArticles).catch(() => {});
    api.listResumes().then(setResumes).catch(() => {});
    api.listInterviews().then(setSessions).catch(() => {});
  }, []);

  const items = useMemo<PaletteItem[]>(() => {
    const rooms: PaletteItem[] = [...ROOMS, { id: "office" as RoomId, label: "Back Office", glyph: "⚙" }]
      .map((r) => ({ title: r.label, kind: "room", glyph: r.glyph, action: () => go(r.id) }));
    const acts: PaletteItem[] = [
      { title: "Check a resume", kind: "action", glyph: "▶", action: () => go("desk") },
      { title: "Write an article (classic studio)", kind: "action", glyph: "▶",
        action: () => { window.location.href = "/"; } },
      { title: "Practice an interview (classic studio)", kind: "action", glyph: "▶",
        action: () => { window.location.href = "/"; } },
    ];
    const art: PaletteItem[] = articles.slice(0, 6).map((a) => ({
      title: a.title || a.topic, kind: "article", glyph: "▤",
      action: () => { window.location.href = "/"; },
    }));
    const res: PaletteItem[] = resumes.slice(0, 6).map((r) => ({
      title: `Resume: ${r.name || "report"}${r.jd_label ? ` vs ${r.jd_label}` : ""}`,
      kind: "report", glyph: "≡", action: () => go("desk"),
    }));
    const ses: PaletteItem[] = sessions.slice(0, 6).map((s) => ({
      title: `Session: ${s.topic} (${s.mode})`, kind: "interview", glyph: "◉",
      action: () => { window.location.href = "/"; },
    }));
    return [...rooms, ...acts, ...res, ...art, ...ses];
  }, [articles, resumes, sessions, go]);

  const hits = useMemo(() => {
    const needle = q.toLowerCase();
    return items.filter((i) => i.title.toLowerCase().includes(needle)).slice(0, 9);
  }, [items, q]);

  return (
    <div className="palette" onClick={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div className="pal-box">
        <input
          autoFocus value={q} placeholder="Jump anywhere: rooms, papers, actions…"
          onChange={(e) => { setQ(e.target.value); setSel(0); }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { setSel((s) => Math.min(s + 1, hits.length - 1)); e.preventDefault(); }
            if (e.key === "ArrowUp") { setSel((s) => Math.max(s - 1, 0)); e.preventDefault(); }
            if (e.key === "Enter" && hits[sel]) hits[sel].action();
          }}
        />
        <div className="pal-list">
          {hits.map((h, i) => (
            <button key={`${h.kind}:${h.title}`} className={"pal-item" + (i === sel ? " sel" : "")}
              onClick={h.action}>
              <span className="glyph">{h.glyph}</span>
              {h.title}
              <span className="kind">{h.kind}</span>
            </button>
          ))}
          {hits.length === 0 && <div className="pal-item">Nothing on the shelves for that.</div>}
        </div>
      </div>
    </div>
  );
}
