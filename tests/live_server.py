"""Start a real Scrivio server process for a test, on synthetic data.

Used where a TestClient cannot answer the question: what a browser
executes, and what a server finds on disk after the previous one was
killed. The process gets its own output directory, its own state
directory, a settings file that does not exist, and no provider keys,
so it cannot read the developer's work or spend their money.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, root: Path, *, demo: bool = False, launcher: str | None = None,
                 configured: dict[str, str] | None = None):
        """`configured` is put into the server's environment last, after
        the real keys have been taken out. It is how a test gives the
        server invented credentials: a demo-mode promise that holds only
        when nothing is configured has not been tested."""
        self.configured = dict(configured or {})
        self.root = Path(root)
        self.output, self.state = self.root / "output", self.root / "state"
        self.output.mkdir(parents=True, exist_ok=True)
        self.demo, self.launcher = demo, launcher
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen | None = None

    @property
    def data(self) -> Path:
        """Where this server keeps its records."""
        return self.output / "demo-mode" if self.demo else self.output

    def _environment(self) -> dict[str, str]:
        env = {
            k: v for k, v in os.environ.items()
            if not k.endswith("_API_KEY") and k not in ("LLM_PROVIDER", "SCRIVIO_DEMO")
        }
        env.update({
            "PORT": str(self.port), "SCRIVIO_HOST": "127.0.0.1",
            "ARTICLE_OUTPUT_DIR": str(self.output),
            "SCRIVIO_STATE_DIR": str(self.state),
            "SCRIVIO_ENV_FILE": str(self.root / "no-such.env"),
            "LLM_CLI": "qwen",               # a CLI that is not installed here
            "PYTHONPATH": str(REPO),
        })
        if self.demo:
            env["SCRIVIO_DEMO"] = "1"
        env.update(self.configured)
        return env

    def start(self) -> "LiveServer":
        argv = [sys.executable, self.launcher] if self.launcher else [sys.executable, "-m", "api"]
        # What it says on the way down is the only account of why it went
        # down. It is kept beside the server's own records, which are
        # synthetic, and quoted when startup fails.
        self.log = self.root / "server.log"
        with open(self.log, "ab") as log:
            self.process = subprocess.Popen(
                argv, cwd=REPO, env=self._environment(),
                stdout=subprocess.DEVNULL, stderr=log)
        for _ in range(150):
            if self.process.poll() is not None:
                raise RuntimeError(
                    "the server exited during startup:\n" + self._last_words())
            try:
                urllib.request.urlopen(f"{self.base}/health", timeout=1).read()
                return self
            except Exception:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("the server did not start")

    def _last_words(self, lines: int = 15) -> str:
        try:
            text = self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(it left no log)"
        return "\n".join(text.splitlines()[-lines:]) or "(it said nothing)"

    def kill(self) -> None:
        """SIGKILL: no shutdown handlers, no chance to tidy up. What is on
        disk afterwards is what a crash or a pulled plug leaves."""
        if self.process is not None:
            self.process.kill()
            self.process.wait(timeout=10)
            self.process = None

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

    # ── talking to it, as a paired browser ─────────────────────────────
    def cookie_value(self) -> str:
        from api import boundary
        previous = os.environ.get("SCRIVIO_STATE_DIR")
        os.environ["SCRIVIO_STATE_DIR"] = str(self.state)
        try:
            return boundary.mint_session()
        finally:
            if previous is None:
                os.environ.pop("SCRIVIO_STATE_DIR", None)
            else:
                os.environ["SCRIVIO_STATE_DIR"] = previous

    def session_cookie(self) -> dict:
        from api import boundary
        return {"name": boundary.COOKIE_NAME, "value": self.cookie_value(), "url": self.base}

    def request(self, method: str, path: str, body: dict | None = None,
                headers: dict | None = None, timeout: float = 20):
        from api import boundary
        request = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json",
                     "Cookie": f"{boundary.COOKIE_NAME}={self.cookie_value()}",
                     **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")

    def json(self, method: str, path: str, body: dict | None = None, **kwargs):
        status, text = self.request(method, path, body, **kwargs)
        return status, (json.loads(text) if text else None)
