"""A real demo server, fully configured, watched from outside (F01).

tests/test_demo_isolation.py watches from inside the process. This
starts the server the way a person does, with every provider key set
and a command-line assistant installed, and watches the only two ways
out that can be seen from outside it:

  the network   every proxy variable points at a recorder on loopback,
                which writes down where each connection was going and
                refuses it
  the assistant a program named `claude` is first on the PATH, and
                writes down that it was started

All the credentials are invented, and the recorder refuses everything,
so neither this test nor the control below can reach a real service.
"""
import os
import socketserver
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_server import LiveServer  # noqa: E402

RESUME = """Jordan Rivera
Backend Engineer
jordan@example.com | +1 555 010 1234 | Austin, TX

Experience
Software Engineer, Acme Corp
Jan 2021 - Present
- Built Kafka pipelines processing 2M events/day

Education
B.S. in Computer Science, State University, 2015 - 2019

Skills
Python, Kafka
"""
POSTING = ("Senior Backend Engineer. Run Kafka pipelines on Kubernetes and work "
           "across teams. Requirements: Python, Kafka, Kubernetes. ") * 4


class Recorder:
    """Stands where a proxy would. Records the first line of whatever
    arrives ("CONNECT api.example.com:443 HTTP/1.1") and refuses it."""

    def __init__(self):
        self.seen: list[str] = []
        recorder = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                line = self.rfile.readline(4096).decode("latin-1").strip()
                if line:
                    recorder.seen.append(line)
                self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                                 b"Connection: close\r\n\r\n")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def hosts(self) -> list[str]:
        return sorted({line.split()[1].split(":")[0].replace("https://", "").split("/")[0]
                       for line in self.seen if len(line.split()) > 1})

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def recorder():
    made = Recorder()
    yield made
    made.close()


def everything_configured(tmp_path, recorder) -> tuple[dict, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    started = tmp_path / "assistant-was-started"
    cli = bin_dir / "claude"
    cli.write_text(f"#!/bin/sh\necho \"$@\" >> '{started}'\necho '{{\"result\": \"[]\"}}'\n")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    environment = {
        "ANTHROPIC_API_KEY": "sk-ant-invented-for-a-test",
        "OPENAI_API_KEY": "sk-invented-for-a-test",
        "TAVILY_API_KEY": "tvly-invented-for-a-test",
        "BRAVE_SEARCH_API_KEY": "brave-invented-for-a-test",
        "EXA_API_KEY": "exa-invented-for-a-test",
        "JINA_API_KEY": "jina-invented-for-a-test",
        "LLM_CLI": "claude",
        "CLAUDE_CLI_PATH": str(cli),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        environment[name] = environment[name.lower()] = recorder.address
    return environment, started


def finished(server, job_id, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status, job = server.json("GET", f"/jobs/{job_id}")
        if status == 200 and job.get("status") not in ("running", "pending", None):
            return job
        time.sleep(0.3)
    raise AssertionError("the article run did not finish")


def test_a_configured_demo_server_reaches_nobody(tmp_path, recorder):
    environment, assistant_started = everything_configured(tmp_path, recorder)
    server = LiveServer(tmp_path / "demo", demo=True, configured=environment).start()
    try:
        status, mode = server.json("GET", "/mode")
        assert status == 200 and mode["demo"] is True

        # An article, with web search asked for.
        status, started = server.json("POST", "/generate", {
            "topic": "How Kafka consumer group rebalancing works",
            "skip_clarification": True, "web_search": True})
        assert status == 200, started
        assert finished(server, started["job_id"])["status"] == "complete"

        # A topic interview, and an answer to grade.
        status, session = server.json("POST", "/interviews", {
            "topic": "Kafka consumer groups", "num_questions": 3})
        assert status == 200, session
        status, graded = server.json(
            "POST", f"/interviews/{session['session_id']}/answers",
            {"question_id": session["questions"][0]["id"],
             "answer": "A consumer group rebalances when membership changes, and "
                       "consumption pauses while partitions are reassigned."})
        assert status == 200, graded

        # A job target, and the interview for it.
        status, profile = server.json("POST", "/job-profiles", {
            "role_title": "Senior Backend Engineer", "company": "Example Payments Inc",
            "job_description": POSTING, "resume_text": RESUME})
        assert status == 200, profile
        status, screen = server.json("POST", "/interviews", {
            "mode": "job", "job_profile_id": profile["profile"]["profile_id"],
            "duration_minutes": 30})
        assert status == 200, screen

        # Voice, both directions.
        status, voices = server.json("GET", "/speak/voices")
        assert status == 200 and voices["available"] is False
        status, spoken = server.json("POST", "/speak", {"text": "Tell me about it."})
        assert status == 503 and "demo" in spoken["detail"].lower()
        status, heard = server.json("POST", "/transcribe", {
            "audio_b64": "GkXfo24gYXVkaW8=", "mime_type": "audio/webm"})
        assert status == 503 and "demo" in heard["detail"].lower()

        # A posting given as an address.
        for path, body in (
            ("/resumes", {"resume_text": RESUME,
                          "jd_url": "https://jobs.example.com/posting/1"}),
            ("/job-profiles", {"role_title": "Backend Engineer", "resume_text": RESUME,
                               "jd_url": "https://jobs.example.com/posting/1"}),
        ):
            status, refused = server.json("POST", path, body)
            assert status == 422 and "demo" in refused["detail"].lower(), refused
    finally:
        server.stop()

    assert recorder.seen == []
    assert not assistant_started.exists(), assistant_started.read_text()


def test_the_same_setup_outside_demo_mode_is_seen_leaving(tmp_path, recorder):
    """The control. If the recorder could not see a real-mode server
    reaching for a search provider, its silence above would mean
    nothing. Everything it sees here it refuses."""
    environment, _ = everything_configured(tmp_path, recorder)
    server = LiveServer(tmp_path / "real", configured=environment).start()
    try:
        # It cannot succeed: the recorder refuses every connection. What
        # it answers does not matter, only where it tried to go first.
        server.request("POST", "/interviews", {
            "topic": "Kafka consumer groups", "num_questions": 3}, timeout=60)
    finally:
        server.stop()

    assert "api.tavily.com" in recorder.hosts()
