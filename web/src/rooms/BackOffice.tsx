/** The Back Office: the utility room. Engines, model tiers, keys.
 * No metaphor theatrics; honest states and one save per edit batch. */
import { useEffect, useState } from "react";
import { settingsApi } from "../api";
import type { SettingsFull } from "../types";

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

  const load = () => settingsApi.full().then(setS).catch(() => setStatus({ msg: "The office is unreachable; is the backend up?", ok: false }));
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
    return <div className="room-wrap"><h1 className="room-title bar-tick-left">Back Office</h1>
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
      <h1 className="room-title bar-tick-left">Back Office</h1>
      <p className="room-sub">
        Running on {s.resolved_provider === "claude-cli" ? `the ${s.active_cli || "claude"} subscription CLI` : `the ${s.resolved_provider} API`}
        {s.provider_auto ? " (auto-resolved)" : " (pinned)"}.
      </p>
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
                  <input type="text" value={current}
                    onChange={(e) => stage(m.env, e.target.value)} />
                ) : (
                  <select value={current || m.options[0]}
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
              onChange={(e) => stage("LLM_CLI", e.target.value)}>
              {[...new Set([...(s.detected_clis.length ? s.detected_clis : ["claude"]), keyValue("LLM_CLI")].filter(Boolean))]
                .map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <p className="office-note">Strong writes and grades; light routes and extracts. Defaults are sane; change these only if you know why.</p>
        </div>
      </div>

      <div className="panel" style={{ marginTop: "1.1rem", maxWidth: 1020 }}>
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
                      <input autoFocus type="text" placeholder="paste the new value"
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
        <p className="office-note">Keys live in your local .env; they never leave this machine. Values shown masked.</p>
      </div>

      <div style={{ display: "flex", gap: "0.7rem", alignItems: "center", marginTop: "1.1rem", flexWrap: "wrap" }}>
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
