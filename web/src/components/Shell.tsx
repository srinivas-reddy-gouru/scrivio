/** The Studio shell: sidebar of rooms + the command palette.
 * Hash-routed (#/floor, #/desk, …) with no router dependency. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, openSession } from "../api";
import type { ArticleSummary, InterviewSessionItem, ResumeSummaryItem } from "../types";

export type RoomId = "floor" | "newsroom" | "interview" | "job" | "desk" | "office";

// Labels say what the page does. The workplace flavor lives inside the
// rooms; navigation is not the place to make people decode a metaphor.
export const ROOMS: Array<{ id: RoomId; label: string; glyph: string }> = [
  { id: "floor", label: "Home", glyph: "⌂" },
  { id: "newsroom", label: "Articles", glyph: "✎" },
  { id: "interview", label: "Interviews", glyph: "◉" },
  { id: "job", label: "Job prep", glyph: "▤" },
  { id: "desk", label: "Resume", glyph: "≡" },
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

/** Post a message to the app-wide polite live region. Anything async
 * that changes state visually should also say so aloud. */
export function announce(message: string) {
  window.dispatchEvent(new CustomEvent("studio-announce", { detail: message }));
}

function Announcer() {
  const [msg, setMsg] = useState("");
  useEffect(() => {
    const on = (e: Event) => setMsg((e as CustomEvent<string>).detail);
    window.addEventListener("studio-announce", on);
    return () => window.removeEventListener("studio-announce", on);
  }, []);
  return <div className="sr-only" role="status" aria-live="polite">{msg}</div>;
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
  const [theme, setTheme] = useState<"dark" | "light">(() =>
    (localStorage.getItem("studio-theme") as "dark" | "light" | null)
    ?? (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("studio-theme", theme);
  }, [theme]);

  // The tab reflects where you are; screen readers hear room changes.
  useEffect(() => {
    const label = room === "office" ? "Settings"
      : ROOMS.find((r) => r.id === room)?.label ?? "Home";
    document.title = `${label} · Scrivio`;
    announce(`${label} page`);
  }, [room]);

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
      <a className="skip-link" href="#studio-main">Skip to content</a>
      <Announcer />
      <nav className="side" aria-label="Studio navigation">
        <div className="brand-block">
          <div className="brand-name">
            <span className="brand-mark" aria-hidden="true" />
            Scrivio
            <button className="theme-btn"
              aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
              title={theme === "dark" ? "Light theme" : "Dark theme"}
              onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}>
              {theme === "dark" ? "☀" : "☾"}
            </button>
          </div>
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
          <span className="glyph">⚙</span> Settings
        </button>
        <div className="kbd-hint"><kbd>⌘K</kbd> jump anywhere</div>
      </nav>
      <main className="room-main" id="studio-main" tabIndex={-1}>{children}</main>
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
      { title: "Write an article", kind: "action", glyph: "▶", action: () => go("newsroom") },
      { title: "Practice an interview", kind: "action", glyph: "▶", action: () => go("interview") },
      { title: "New job dossier", kind: "action", glyph: "▶", action: () => go("job") },
    ];
    const art: PaletteItem[] = articles.slice(0, 6).map((a) => ({
      title: a.title || a.topic, kind: "article", glyph: "▤",
      action: () => go("newsroom"),
    }));
    const res: PaletteItem[] = resumes.slice(0, 6).map((r) => ({
      title: `Resume: ${r.name || "report"}${r.jd_label ? ` vs ${r.jd_label}` : ""}`,
      kind: "report", glyph: "≡", action: () => go("desk"),
    }));
    const ses: PaletteItem[] = sessions.slice(0, 6).map((s) => ({
      title: `Session: ${s.topic} (${s.mode})`, kind: "interview", glyph: "◉",
      action: () => openSession(s.session_id),
    }));
    return [...rooms, ...acts, ...res, ...art, ...ses];
  }, [articles, resumes, sessions, go]);

  const hits = useMemo(() => {
    const needle = q.toLowerCase();
    return items.filter((i) => i.title.toLowerCase().includes(needle)).slice(0, 9);
  }, [items, q]);

  return (
    <div className="palette" onClick={(e) => { if (e.target === e.currentTarget) close(); }}
      role="dialog" aria-modal="true" aria-label="Jump anywhere">
      <div className="pal-box">
        <input
          autoFocus value={q} placeholder="Jump anywhere: rooms, papers, actions…"
          aria-label="Search rooms, papers, and actions"
          role="combobox" aria-expanded={hits.length > 0}
          aria-controls="pal-list" aria-activedescendant={hits[sel] ? `pal-opt-${sel}` : undefined}
          onChange={(e) => { setQ(e.target.value); setSel(0); }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { setSel((s) => Math.min(s + 1, hits.length - 1)); e.preventDefault(); }
            if (e.key === "ArrowUp") { setSel((s) => Math.max(s - 1, 0)); e.preventDefault(); }
            if (e.key === "Enter" && hits[sel]) hits[sel].action();
          }}
        />
        <div className="pal-list" id="pal-list" role="listbox" aria-label="Results">
          {hits.map((h, i) => (
            <button key={`${h.kind}:${h.title}`} className={"pal-item" + (i === sel ? " sel" : "")}
              id={`pal-opt-${i}`} role="option" aria-selected={i === sel}
              onClick={h.action}>
              <span className="glyph" aria-hidden="true">{h.glyph}</span>
              {h.title}
              <span className="kind">{h.kind}</span>
            </button>
          ))}
          {hits.length === 0 && <div className="pal-item" role="option" aria-selected="false">Nothing on the shelves for that.</div>}
        </div>
      </div>
    </div>
  );
}
