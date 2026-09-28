/** The Back Office: the utility room. Engines, model tiers, keys.
 * No metaphor theatrics; honest states and one save per edit batch. */
import { useEffect, useState } from "react";
import { api, dataApi, settingsApi } from "../api";
import type { DataOverview, ModeStatus, SettingsFull } from "../types";

const PROVIDER_NAMES: Record<string, string> = {
  anthropic: "Anthropic API", openai: "OpenAI API", demo: "Canned demo output (no model)",
  none: "Nothing configured",
};
const describe = (provider: string, mode: ModeStatus): string =>
  provider === "claude-cli"
    ? `${mode.cli.cli} subscription (command line)`
    : PROVIDER_NAMES[provider] ?? provider;

const ENGINES: Array<{ id: string; name: string; blurb: string }> = [
  { id: "", name: "Auto", blurb: "Whatever is available wins: single key uses that provider; subscription CLI fills the gap." },
  { id: "claude-cli", name: "Claude subscription", blurb: "Your Claude Code CLI login. No API key, no metered bill; slower per call." },
  { id: "anthropic", name: "Anthropic API", blurb: "Metered API key. Fastest Claude path." },
  { id: "openai", name: "OpenAI API", blurb: "Metered API key. GPT models across the pipeline." },
];

const MODEL_KNOBS: Array<{ env: string; label: string; blurb: string; options: string[] }> = [
  { env: "ANTHROPIC_STRONG_MODEL", label: "Anthropic · strong", blurb: "Drafting, tailoring, grading",
    options: ["claude-sonnet-4-5", "claude-opus-4-6", "claude-haiku-4-5"] },
  { env: "ANTHROPIC_LIGHT_MODEL", label: "Anthropic · light", blurb: "Routing, extraction, quick checks",
    options: ["claude-haiku-4-5", "claude-sonnet-4-5"] },
  { env: "OPENAI_STRONG_MODEL", label: "OpenAI · strong", blurb: "Drafting when OpenAI is the engine",
    options: ["gpt-5.2", "gpt-5.2-mini"] },
  { env: "OPENAI_LIGHT_MODEL", label: "OpenAI · light", blurb: "Routing when OpenAI is the engine",
    options: ["gpt-5.2-mini", "gpt-5.2"] },
  { env: "CLI_STRONG_MODEL", label: "Subscription · strong", blurb: "CLI alias for the heavy stages",
    options: ["sonnet", "opus", "haiku"] },
  { env: "CLI_LIGHT_MODEL", label: "Subscription · light", blurb: "CLI alias for the quick stages",
    options: ["haiku", "sonnet"] },
];

export function BackOffice() {
  const [s, setS] = useState<SettingsFull | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [status, setStatus] = useState<{ msg: string; ok: boolean } | null>(null);
  const [saving, setSaving] = useState(false);
  const [mode, setMode] = useState<ModeStatus | null>(null);

  const load = () => {
    api.mode().then(setMode).catch(() => setMode(null));
    return settingsApi.full().then(setS).catch(() => setStatus({ msg: "Settings could not be loaded. Is the server still running?", ok: false }));
  };
  useEffect(() => { load(); }, []);

  const stage = (key: string, value: string) => {
    setEdits((e) => ({ ...e, [key]: value }));
    setStatus(null);
  };

  const save = async () => {
    if (!Object.keys(edits).length) return;
    setSaving(true); setStatus(null);
    try {
      const trimmed = Object.fromEntries(
        Object.entries(edits).map(([k, v]) => [k, v.trim()]));
      await settingsApi.patch(trimmed);
      setEdits({}); setEditingKey(null);
      await load();
      setStatus({ msg: "Saved. New runs pick this up; runs already on the floor keep their setup.", ok: true });
    } catch (e) {
      setStatus({ msg: (e as Error).message, ok: false });
    }
    setSaving(false);
  };

  if (!s) {
    return <div className="room-wrap"><h1 className="room-title bar-tick-left">Settings</h1>
      <p className="room-sub">{status ? status.msg : "Opening the ledger…"}</p></div>;
  }

  const keyValue = (env: string) => edits[env] ?? s.keys.find((k) => k.key === env)?.masked_value ?? "";
  const currentEngine = edits["LLM_PROVIDER"] ?? (s.provider_auto ? "" : s.provider_preference);
  const dirty = Object.keys(edits).length > 0;

  const keyGroups: Array<[string, string[]]> = [
    ["Providers", ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]],
    ["Search + extras", s.keys.map((k) => k.key).filter((k) => !["ANTHROPIC_API_KEY", "OPENAI_API_KEY"].includes(k) && !k.endsWith("_MODEL") && k !== "LLM_PROVIDER" && k !== "LLM_CLI")],
  ];

  return (
    <div className="room-wrap">
      <h1 className="room-title bar-tick-left">Settings</h1>
      <p className="room-sub">
        {mode?.demo ? "Demo mode: canned examples, no model is called."
          : mode && !mode.ready ? "No provider is configured yet, so nothing can run."
          : <>Running on {s.resolved_provider === "claude-cli" ? `the ${s.active_cli || "claude"} subscription CLI` : `the ${s.resolved_provider} API`}
            {s.provider_auto ? " (auto-resolved)" : " (pinned)"}.</>}
      </p>
      {mode && (
        <div className="panel what-runs" aria-label="What will run">
          <p className="eyebrow">What will run</p>
          <dl>
            <dt>Writing, grading, tailoring</dt><dd>{describe(mode.writing, mode)}</dd>
            <dt>Fact-checking claims</dt><dd>{describe(mode.fact_checking, mode)}</dd>
            <dt>Command-line assistant</dt>
            <dd>{mode.cli.cli}: {mode.cli.state}
              {mode.cli.checked_at ? ` (as of ${mode.cli.checked_at.replace("T", " ")})` : ""}</dd>
          </dl>
          {mode.problem && <p className="office-note" role="alert">{mode.problem}</p>}
          <p className="office-note">
            An installed command-line assistant is not the same as a signed-in one. Whether it
            is signed in is learned from the last real call, never by spending one to check.
          </p>
        </div>
      )}
      <div className="office-grid">
        <div className="panel">
          <p className="eyebrow">The engine</p>
          {ENGINES.map((e) => {
            const active = currentEngine === e.id;
            const resolved = s.resolved_provider === (e.id || s.resolved_provider);
            return (
              <button key={e.id} className={"engine-row" + (active ? " on" : "")}
                onClick={() => stage("LLM_PROVIDER", e.id)}>
                <span className={
                  (e.id === "anthropic" && s.has_anthropic) ||
                  (e.id === "openai" && s.has_openai) ||
                  (e.id === "claude-cli" && s.has_claude_cli) ||
                  e.id === "" ? "dot-live" : "dot-idle"} />
                <span>
                  <b>{e.name}</b>
                  <p>{e.blurb}</p>
                </span>
                {active && resolved && <span className="engine-badge">selected</span>}
              </button>
            );
          })}
          <p className="office-note">
            Detected CLIs: {s.detected_clis.length ? s.detected_clis.join(", ") : "none"}.
            An engine with a grey dot has no key or login; picking it falls back to auto at run time.
          </p>
        </div>

        <div className="panel">
          <p className="eyebrow">Model tiers</p>
          {MODEL_KNOBS.map((m) => {
            const current = keyValue(m.env);
            const custom = current !== "" && !m.options.includes(current);
            return (
              <div className="model-row" key={m.env}>
                <span><b>{m.label}</b><p>{m.blurb}</p></span>
                {custom ? (
                  <input type="text" value={current} aria-label={m.label}
                    onChange={(e) => stage(m.env, e.target.value)} />
                ) : (
                  <select value={current || m.options[0]} aria-label={m.label}
                    onChange={(e) => e.target.value === "__other" ? stage(m.env, " ") : stage(m.env, e.target.value)}>
                    {m.options.map((o) => <option key={o} value={o}>{o}</option>)}
                    <option value="__other">Other…</option>
                  </select>
                )}
              </div>
            );
          })}
          <div className="model-row">
            <span><b>Subscription · CLI</b><p>Which installed CLI runs subscription calls</p></span>
            <select value={keyValue("LLM_CLI") || s.active_cli || "claude"}
              aria-label="Subscription CLI"
              onChange={(e) => stage("LLM_CLI", e.target.value)}>
              {[...new Set([...(s.detected_clis.length ? s.detected_clis : ["claude"]), keyValue("LLM_CLI")].filter(Boolean))]
                .map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <p className="office-note">Strong writes and grades; light routes and extracts. Defaults are sane; change these only if you know why.</p>
        </div>
      </div>

      <div className="panel room-col" style={{ marginTop: "1.1rem" }}>
        <p className="eyebrow">Keys and connections</p>
        <div className="key-grid">
          {keyGroups.map(([group, keys]) => (
            <div key={group}>
              <p style={{ fontSize: "0.68rem", color: "var(--text-faint)", marginBottom: "0.3rem" }}>{group}</p>
              {keys.map((env) => {
                const k = s.keys.find((x) => x.key === env);
                if (!k) return null;
                const editing = editingKey === env || edits[env] !== undefined;
                return (
                  <div className="key-row" key={env} title={k.description}>
                    <span className={k.present ? "dot-live" : "dot-idle"} />
                    {env}
                    {editing ? (
                      <input autoFocus type="text" aria-label={env} placeholder="paste the new value"
                        value={edits[env] ?? ""}
                        onChange={(e) => stage(env, e.target.value)} />
                    ) : (
                      <>
                        <span className="masked">{k.present ? k.masked_value : "not set"}</span>
                        <button className="btn btn-quiet key-edit" onClick={() => setEditingKey(env)}>edit</button>
                      </>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
        <p className="office-note">
          Keys are kept in the settings file on this machine, and each is sent only to the
          provider it belongs to, as its credential. Values are shown masked.
        </p>
      </div>

      <DataPanel />

      <div className="room-col" style={{ display: "flex", gap: "0.7rem", alignItems: "center", marginTop: "1.1rem", flexWrap: "wrap" }}>
        <button className="btn" onClick={save} disabled={!dirty || saving}>
          {saving ? "Writing the ledger…" : dirty ? `Save ${Object.keys(edits).length} change${Object.keys(edits).length > 1 ? "s" : ""}` : "Nothing to save"}
        </button>
        {dirty && <button className="btn btn-quiet" onClick={() => { setEdits({}); setEditingKey(null); }}>Discard</button>}
        {status && (
          <span style={{ fontSize: "0.76rem", color: status.ok ? "var(--green)" : "var(--redpen)" }}>{status.msg}</span>
        )}
      </div>
    </div>
  );
}


const KIND_LABELS: Record<string, string> = {
  resumes: "Resumes", job_targets: "Job targets", interviews: "Interview sessions",
  articles: "Articles", run_records: "Records of article runs",
  set_aside: "Damaged files set aside", stage_cache: "Cached article stages",
};
const CONFIRM = "delete everything";

/** What is held, where it goes, and the two things a person should be
 * able to do with their own data without reading the source: take a copy,
 * and remove it. */
function DataPanel() {
  const [data, setData] = useState<DataOverview | null>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState<{ msg: string; ok: boolean } | null>(null);

  const load = () => dataApi.overview().then(setData).catch(() => setData(null));
  useEffect(() => { load(); }, []);
  if (!data) return null;

  const run = async (what: string, work: () => Promise<unknown>, done: string) => {
    setBusy(what); setNote(null);
    try { await work(); setNote({ msg: done, ok: true }); await load(); }
    catch (e) { setNote({ msg: (e as Error).message, ok: false }); }
    finally { setBusy(""); }
  };
  const total = Object.values(data.stored).reduce((n, k) => n + k.count, 0);

  return (
    <div className="panel data-panel" aria-labelledby="data-title">
      <p className="eyebrow" id="data-title">Your data</p>
      <p className="data-statement">{data.processed_by.statement}</p>

      <table className="data-table">
        <caption className="sr-only">What is stored on this machine</caption>
        <thead><tr><th scope="col">Stored here</th><th scope="col">How many</th><th scope="col">Where</th></tr></thead>
        <tbody>
          {Object.entries(data.stored).map(([kind, k]) => (
            <tr key={kind}>
              <th scope="row">{KIND_LABELS[kind] ?? kind}</th>
              <td className="mono">{k.count}</td>
              <td className="mono folder">{k.folder}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {!data.processed_by.local && (
        <details className="fold">
          <summary>What is sent to your provider ({data.processed_by.provider})</summary>
          <dl className="sent-list">
            {data.processed_by.what_is_sent.map((row) => (
              <div key={row.studio}><dt>{row.studio}</dt><dd>{row.sent}</dd></div>
            ))}
          </dl>
        </details>
      )}
      <p className="office-note">{data.retention}</p>

      <div className="data-actions">
        <button className="btn" disabled={!!busy || total === 0}
          onClick={() => run("export", dataApi.exportAll, "Your export was saved. It contains your resumes and answers: keep it private.")}>
          {busy === "export" ? "Preparing…" : "Export everything"}
        </button>
      </div>

      <details className="fold danger">
        <summary>Delete everything</summary>
        <p>
          Removes every resume, job target, interview, article, and cached stage from this
          machine. It cannot be undone. Your settings and keys are kept.
        </p>
        <ul>
          {data.not_covered_by_delete.map((line) => <li key={line}>{line}</li>)}
        </ul>
        <label htmlFor="confirm-delete">To confirm, type <b>{CONFIRM}</b></label>
        <input id="confirm-delete" type="text" value={typed} autoComplete="off"
          onChange={(e) => setTyped(e.target.value)} />
        <button className="btn btn-danger"
          disabled={!!busy || typed.trim().toLowerCase() !== CONFIRM}
          onClick={() => run("delete", () => dataApi.deleteAll(typed), "Everything was deleted.")
            .then(() => setTyped(""))}>
          {busy === "delete" ? "Deleting…" : "Delete everything"}
        </button>
      </details>
      {note && (
        <p role="status" style={{ fontSize: "0.8rem", marginTop: "0.7rem",
          color: note.ok ? "var(--green)" : "var(--redpen)" }}>{note.msg}</p>
      )}
    </div>
  );
}
