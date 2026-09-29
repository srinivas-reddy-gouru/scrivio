/** The door. Scrivio's API answers only a browser that has been paired,
 * and pairing means typing the code the server printed in its own
 * terminal: whoever can read that terminal is, for a tool that runs on
 * your machine, you.
 *
 * The code travels in a request body and comes back as an HttpOnly
 * cookie, so it is never in a URL, in history, or readable by a script. */
import { useEffect, useRef, useState } from "react";

const UNAUTHENTICATED = "scrivio-unauthenticated";

/** Every request in the app goes through window.fetch, from a dozen call
 * sites that each handle their own errors. Rather than teach all of them
 * about sessions, the one thing they share is watched: a 401 anywhere
 * means the session is gone, and the gate below takes over. */
export function watchForSignOut(): void {
  const original = window.fetch.bind(window);
  window.fetch = async (...args: Parameters<typeof fetch>) => {
    const res = await original(...args);
    const url = typeof args[0] === "string" ? args[0] : (args[0] as Request).url ?? "";
    if (res.status === 401 && !url.includes("/auth/")) {
      window.dispatchEvent(new Event(UNAUTHENTICATED));
    }
    return res;
  };
}

type Gate = "checking" | "open" | "closed" | "unreachable";

export function PairingGate({ children }: { children: React.ReactNode }) {
  const [gate, setGate] = useState<Gate>("checking");

  const check = () => {
    fetch("/auth/status")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((s: { authenticated: boolean }) => setGate(s.authenticated ? "open" : "closed"))
      .catch(() => setGate("unreachable"));
  };

  useEffect(() => {
    check();
    const close = () => setGate("closed");
    window.addEventListener(UNAUTHENTICATED, close);
    return () => window.removeEventListener(UNAUTHENTICATED, close);
  }, []);

  if (gate === "open") return <>{children}</>;
  if (gate === "checking") return <div className="pair-wrap" aria-busy="true" />;
  if (gate === "unreachable") {
    return (
      <main className="pair-wrap">
        <div className="pair-card">
          <h1>Scrivio is not answering</h1>
          <p>The page loaded but the server behind it did not respond. Check that it is still
            running in your terminal, then try again.</p>
          <button className="btn" onClick={() => { setGate("checking"); check(); }}>Try again</button>
        </div>
      </main>
    );
  }
  return <PairingForm onPaired={() => setGate("open")} />;
}

function PairingForm({ onPaired }: { onPaired: () => void }) {
  const [code, setCode] = useState("");
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => { input.current?.focus(); }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true); setProblem("");
    try {
      const res = await fetch("/auth/pair", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      if (res.ok) { onPaired(); return; }
      const body = await res.json().catch(() => ({}));
      setProblem(body.detail || "That did not work. Check the code and try again.");
      setCode("");
      input.current?.focus();
    } catch {
      setProblem("Could not reach the server. Is it still running?");
    } finally { setBusy(false); }
  };

  return (
    <main className="pair-wrap">
      <form className="pair-card" onSubmit={submit} aria-labelledby="pair-title">
        <h1 id="pair-title">Pair this browser</h1>
        <p>Your resumes and interview answers stay on this machine, so Scrivio only talks to a
          browser you have paired. The code is printed in the terminal where the server is
          running.</p>
        <label htmlFor="pair-code">Pairing code</label>
        <input
          id="pair-code" ref={input} value={code} required
          onChange={(e) => setCode(e.target.value.toUpperCase())}
          placeholder="XXXX-XXXX" autoComplete="off" autoCapitalize="characters"
          spellCheck={false} inputMode="text" maxLength={16}
          aria-describedby={problem ? "pair-problem" : undefined}
          aria-invalid={!!problem}
        />
        <button className="btn" type="submit" disabled={busy || !code.trim()}>
          {busy ? "Pairing…" : "Pair"}
        </button>
        <div id="pair-problem" role="alert" className="pair-problem">{problem}</div>
      </form>
    </main>
  );
}
