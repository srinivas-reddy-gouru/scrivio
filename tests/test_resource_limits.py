"""Bounds on requests, parsing, concurrency, and provider calls (R11).

These are the LOCAL limits: what one person's install needs so that a
bad file or a stuck provider cannot take the machine down with it.
Per-user and cross-worker quotas are a hosting matter and are not here.

All inputs are synthetic and built in memory.
"""
import asyncio
import base64
import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from api import limits, server
from pipeline.workers import resume_parser
from pipeline.workers.resume_parser import ResumeParseError, parse_resume_bounded


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def docx_bytes(paragraphs: list[str]) -> bytes:
    import docx
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


RESUME = ["Sam Okafor", "Software Engineer at Initrode, Feb 2020 to Present",
          "Maintained 12 services behind the internal gateway"] * 4


# ── Request bodies ───────────────────────────────────────────────────

def test_an_oversize_body_is_refused_by_its_declared_length():
    client = TestClient(server.app)
    huge = "x" * (limits.DEFAULT_BODY_BYTES + 1000)

    r = client.post("/interviews", json={"topic": huge})

    assert r.status_code == 413
    assert "too large" in r.json()["detail"].lower()


def test_an_oversize_body_with_no_declared_length_is_cut_off_while_streaming():
    """Chunked uploads carry no Content-Length, so a check that only reads
    the header waves them through and buffers whatever arrives."""
    received = []

    async def app(scope, receive, send):
        while True:
            message = await receive()
            received.append(len(message.get("body", b"")))
            if not message.get("more_body"):
                break

    chunks = [b"x" * 100_000] * 50               # 5 MB, in pieces
    sent = []

    async def receive():
        body = chunks.pop() if chunks else b""
        return {"type": "http.request", "body": body, "more_body": bool(chunks)}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/interviews", "headers": []}
    asyncio.run(limits.BodyLimit(app)(scope, receive, send))

    assert sent[0]["status"] == 413
    assert sum(received) <= limits.DEFAULT_BODY_BYTES + 100_000, \
        "reading must stop at the limit, not after the whole upload"


def test_uploads_get_a_larger_allowance_than_ordinary_requests():
    assert limits.limit_for("/resumes") > limits.limit_for("/interviews")
    assert limits.limit_for("/transcribe") > limits.limit_for("/resumes")
    assert limits.limit_for("/settings") == limits.DEFAULT_BODY_BYTES


def test_reads_are_not_subject_to_a_body_limit():
    assert TestClient(server.app).get("/health").status_code == 200


# ── Uploaded documents ───────────────────────────────────────────────

def test_a_decoded_file_over_the_limit_is_refused_before_parsing(monkeypatch):
    monkeypatch.setattr(resume_parser, "MAX_FILE_BYTES", 10_000)
    parsed = []
    monkeypatch.setattr(resume_parser, "_parse_in_child",
                        lambda *a, **k: parsed.append(1) or "text")

    with pytest.raises(ResumeParseError, match="larger than"):
        parse_resume_bounded(b"%PDF-1.7\n" + b"0" * 20_000, "resume.pdf")

    assert parsed == []


@pytest.mark.parametrize("filename, content", [
    ("resume.pdf", b"PK\x03\x04 this is a zip, not a pdf"),
    ("resume.pdf", b"<html><body>not a pdf</body></html>"),
    ("resume.docx", b"%PDF-1.7 this is a pdf, not a docx"),
    ("resume.docx", b"MZ\x90\x00 an executable"),
])
def test_a_file_that_is_not_what_its_name_says_is_refused(filename, content):
    with pytest.raises(ResumeParseError, match="does not look like"):
        parse_resume_bounded(content + b" " * 200, filename)


def test_a_document_that_expands_enormously_is_refused(monkeypatch):
    """A .docx is a zip. A few kilobytes can declare gigabytes."""
    monkeypatch.setattr(resume_parser, "MAX_EXPANDED_BYTES", 1_000_000)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", b"0" * 30_000_000)
    bomb = out.getvalue()
    assert len(bomb) < 100_000

    with pytest.raises(ResumeParseError, match="expands"):
        parse_resume_bounded(bomb, "resume.docx")


def test_a_document_with_too_many_parts_is_refused(monkeypatch):
    monkeypatch.setattr(resume_parser, "MAX_ARCHIVE_ENTRIES", 50)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for i in range(200):
            archive.writestr(f"word/part{i}.xml", "<x/>")

    with pytest.raises(ResumeParseError, match="parts"):
        parse_resume_bounded(out.getvalue(), "resume.docx")


def test_a_pdf_with_too_many_pages_is_refused(monkeypatch):
    from pypdf import PdfWriter

    monkeypatch.setattr(resume_parser, "MAX_PDF_PAGES", 5)
    writer = PdfWriter()
    for _ in range(12):
        writer.add_blank_page(width=200, height=200)
    out = io.BytesIO()
    writer.write(out)

    with pytest.raises(ResumeParseError, match="pages"):
        parse_resume_bounded(out.getvalue(), "resume.pdf")


def test_a_parse_that_never_finishes_is_killed(monkeypatch):
    """Run in a child process, because a thread that is stuck in a parser
    cannot be stopped and goes on using a core after the request is gone."""
    monkeypatch.setattr(resume_parser, "PARSE_SECONDS", 1.0)
    monkeypatch.setattr(resume_parser, "_CHILD_TARGET", resume_parser._sleep_forever)

    started = time.monotonic()
    with pytest.raises(ResumeParseError, match="too long"):
        parse_resume_bounded(docx_bytes(RESUME), "resume.docx")

    assert time.monotonic() - started < 6
    assert resume_parser.last_child_alive() is False, "the child must be gone"


def test_an_ordinary_document_still_parses():
    text = parse_resume_bounded(docx_bytes(RESUME), "resume.docx")

    assert "Sam Okafor" in text and "12 services" in text


def test_the_upload_endpoint_uses_the_bounded_parser(monkeypatch):
    monkeypatch.setattr(resume_parser, "MAX_FILE_BYTES", 2_000)

    r = TestClient(server.app).post("/resumes", json={
        "resume_file_b64": b64(docx_bytes(RESUME * 40)), "resume_filename": "resume.docx"})

    assert r.status_code == 422 and "larger than" in r.json()["detail"]


# ── Job admission ────────────────────────────────────────────────────

def test_a_burst_of_jobs_is_refused_once_the_limit_is_running():
    gate = limits.Gate("article generation", 2)

    first, second = gate.enter(), gate.enter()
    with pytest.raises(limits.Busy) as refused:
        gate.enter()

    assert "2" in str(refused.value) and "article generation" in str(refused.value)
    first.leave()
    gate.enter()                       # a slot freed is a slot available
    second.leave()


def test_leaving_twice_does_not_free_a_slot_that_was_never_taken():
    gate = limits.Gate("tailoring", 1)
    slot = gate.enter()
    slot.leave()
    slot.leave()

    gate.enter()
    with pytest.raises(limits.Busy):
        gate.enter()


def test_the_api_answers_429_when_generation_is_full(monkeypatch):
    monkeypatch.setattr(server, "ARTICLE_GATE", limits.Gate("article generation", 0))

    r = TestClient(server.app).post(
        "/generate", json={"topic": "Kafka", "must_cover": ["rebalancing"]})

    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_a_finished_job_gives_its_slot_back(monkeypatch):
    gate = limits.Gate("article generation", 1)
    monkeypatch.setattr(server, "ARTICLE_GATE", gate)
    client = TestClient(server.app)

    for _ in range(3):
        r = client.post("/generate", json={"topic": "Kafka", "must_cover": ["rebalancing"]})
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        deadline = time.monotonic() + 20
        while client.get(f"/jobs/{job_id}").json().get("status") == "running":
            assert time.monotonic() < deadline
            time.sleep(0.05)

    assert gate.running == 0


def test_health_stays_responsive_while_a_document_is_being_parsed(monkeypatch):
    """The parse runs outside the event loop, so a slow file cannot make
    the whole application look dead."""
    monkeypatch.setattr(resume_parser, "PARSE_SECONDS", 3.0)
    monkeypatch.setattr(resume_parser, "_CHILD_TARGET", resume_parser._sleep_forever)

    async def scenario():
        import httpx
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            upload = asyncio.create_task(c.post("/resumes", json={
                "resume_file_b64": b64(docx_bytes(RESUME)),
                "resume_filename": "resume.docx"}))
            await asyncio.sleep(0.3)
            started = time.monotonic()
            health = await c.get("/health")
            waited = time.monotonic() - started
            return health.status_code, waited, (await upload).status_code

    status, waited, upload_status = asyncio.run(scenario())

    assert status == 200 and waited < 1.0
    assert upload_status == 422


# ── Provider calls ───────────────────────────────────────────────────

def test_provider_clients_are_built_with_a_timeout_and_bounded_retries(monkeypatch):
    """The SDK default is a ten minute wait. A stalled provider would hold
    a job, and the slot it occupies, for all of it."""
    from pipeline.providers import clients

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")

    for client in (clients.anthropic_client(), clients.openai_client()):
        assert client.timeout.read == clients.PROVIDER_SECONDS <= 180
        assert client.timeout.connect == clients.PROVIDER_CONNECT_SECONDS
        assert client.max_retries == clients.PROVIDER_RETRIES <= 2


def test_every_real_client_in_the_codebase_is_built_that_way():
    """A client constructed directly somewhere would quietly get the ten
    minute default back."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    direct = []
    for path in [*root.glob("pipeline/**/*.py"), *root.glob("api/*.py"), root / "main.py"]:
        if path.name == "clients.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\b(AsyncAnthropic|AsyncOpenAI|Anthropic|OpenAI)\(", line):
                direct.append(f"{path.relative_to(root)}:{number}")

    assert direct == []


def test_a_cancelled_cli_call_does_not_leave_the_process_running(monkeypatch):
    """Cancelling the job cancelled the wait and left the assistant
    running, still spending the subscription on an answer nobody wants."""
    from pipeline.providers import claude_cli_adapter

    killed = []

    class Process:
        returncode = None

        async def communicate(self, _input):
            await asyncio.sleep(30)

        def kill(self):
            killed.append(True)

        async def wait(self):
            return -9

    async def spawn(*argv, **kwargs):
        return Process()

    monkeypatch.setattr(claude_cli_adapter, "_find_cli", lambda: "/usr/local/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def scenario():
        call = asyncio.create_task(claude_cli_adapter.ClaudeCLIAdapter().messages.create(
            model="sonnet", messages=[{"role": "user", "content": "hello"}]))
        await asyncio.sleep(0.2)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(scenario())

    assert killed == [True]


def test_cli_output_beyond_the_limit_stops_the_call(monkeypatch):
    from pipeline.providers import claude_cli_adapter

    monkeypatch.setattr(claude_cli_adapter, "MAX_OUTPUT_BYTES", 10_000)
    killed = []

    class Stream:
        def __init__(self, data):
            self._data = data

        async def read(self, n=-1):
            chunk, self._data = self._data[:n], self._data[n:]
            return chunk

    class Process:
        returncode = None
        stdout, stderr = Stream(b"x" * 1_000_000), Stream(b"")
        stdin = None

        def kill(self):
            killed.append(True)

        async def wait(self):
            return 0

    async def spawn(*argv, **kwargs):
        return Process()

    monkeypatch.setattr(claude_cli_adapter, "_find_cli", lambda: "/usr/local/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(claude_cli_adapter.ClaudeCLIError, match="more output"):
        asyncio.run(claude_cli_adapter.ClaudeCLIAdapter().messages.create(
            model="sonnet", messages=[{"role": "user", "content": "hello"}]))
    assert killed == [True]
