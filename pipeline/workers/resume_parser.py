"""Extract plain text from an uploaded resume (PDF, DOCX, or plain text).

The parser is deliberately dumb: no layout reconstruction, no section
detection — the LLM downstream reads the raw text better than any
heuristic re-formatter. The only jobs here are (1) getting text out of
binary containers and (2) failing with an ACTIONABLE message when we
can't (the classic case: a scanned-image PDF with no text layer).
"""
from __future__ import annotations

import io
import multiprocessing
import time
import zipfile

# Limits on an uploaded document. A resume is a page or two of text; these
# are far above any real one and far below what would hurt the machine.
MAX_FILE_BYTES = 5_000_000          # the file as uploaded
MAX_EXPANDED_BYTES = 40_000_000     # a .docx is a zip: what it unpacks to
MAX_ARCHIVE_ENTRIES = 2_000
MAX_PDF_PAGES = 40
PARSE_SECONDS = 20.0

# ~30k chars ≈ 7-8k tokens: generous for any real resume (even academic
# CVs), small enough to leave prompt room for the JD and instructions.
_MAX_CHARS = 30_000

# Below this many extracted characters a PDF is almost certainly a scan
# (image-only pages) rather than a text document.
_SCAN_SUSPECT_CHARS = 40


class ResumeParseError(ValueError):
    """Raised when a resume can't be turned into usable text. The message
    is shown to the user verbatim — keep it actionable."""


def parse_resume(data: bytes, filename: str) -> str:
    """Return the resume's plain text, capped at _MAX_CHARS."""
    if not data:
        raise ResumeParseError("The uploaded file is empty.")
    name = (filename or "").lower()

    if name.endswith(".pdf"):
        text = _parse_pdf(data)
    elif name.endswith(".docx"):
        text = _parse_docx(data)
    elif name.endswith((".txt", ".md", ".text")):
        text = data.decode("utf-8", errors="replace")
    else:
        raise ResumeParseError(
            "Unsupported file type. Upload a PDF, DOCX, or TXT file — or "
            "paste the resume text directly."
        )

    text = text.strip()
    if len(text) < _SCAN_SUSPECT_CHARS:
        raise ResumeParseError(
            "Almost no text could be extracted — this PDF looks like a "
            "scanned image. Paste the resume text directly instead."
        )
    return text[:_MAX_CHARS]


def _parse_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ResumeParseError(
                "This PDF is password-protected. Remove the password or "
                "paste the text directly."
            )
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ResumeParseError:
        raise
    except Exception:
        raise ResumeParseError(
            "Could not read this PDF. Try re-exporting it, or paste the "
            "resume text directly."
        )


def _parse_docx(data: bytes) -> str:
    try:
        from docx import Document

        document = Document(io.BytesIO(data))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts)
    except Exception:
        raise ResumeParseError(
            "Could not read this DOCX file. Try re-saving it, or paste the "
            "resume text directly."
        )


# ── Bounded parsing ─────────────────────────────────────────────────────────
# parse_resume() above trusts its input. An uploaded file has earned no
# trust: it may be the wrong type, may unpack to gigabytes, or may be
# built to keep a parser busy for ever. Everything below is what stands
# between an upload and parse_resume().

_last_child = None


def last_child_alive() -> bool:
    return bool(_last_child is not None and _last_child.is_alive())


def _check_type(data: bytes, filename: str) -> None:
    """The first bytes, not the file name: a name is whatever the sender
    typed."""
    name = (filename or "").lower()
    if name.endswith(".pdf") and not data.lstrip()[:5] == b"%PDF-":
        raise ResumeParseError(
            "That file does not look like a PDF, whatever it is called. "
            "Export it again, or paste the resume text directly.")
    if name.endswith(".docx") and data[:4] != b"PK\x03\x04":
        raise ResumeParseError(
            "That file does not look like a Word document, whatever it is "
            "called. Save it again as .docx, or paste the text directly.")


def _check_archive(data: bytes) -> None:
    """Sizes are read from the archive's directory, without unpacking."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile:
        raise ResumeParseError(
            "Could not read this DOCX file. Try re-saving it, or paste the "
            "resume text directly.")
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise ResumeParseError(
            f"That document has {len(entries)} parts inside it, which is not "
            "what a resume looks like. Paste the text directly instead.")
    expanded = sum(entry.file_size for entry in entries)
    if expanded > MAX_EXPANDED_BYTES:
        raise ResumeParseError(
            f"That document expands to {expanded // 1_000_000} MB when "
            "opened, which is not what a resume looks like. Paste the text "
            "directly instead.")


def _check_pdf_pages(data: bytes, limit: int) -> None:
    from pypdf import PdfReader
    try:
        pages = len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return                       # unreadable: parse_resume says so properly
    if pages > limit:
        raise ResumeParseError(
            f"That PDF has {pages} pages; the limit is {limit}. "
            "Upload only the resume, or paste the text directly.")


def _limit_child(seconds: float) -> None:
    """Ceilings on the child itself, where the platform enforces them."""
    try:
        import resource
        cpu = int(seconds) + 2
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_AS, (2_000_000_000, 2_000_000_000))
    except Exception:
        pass


def _child_main(conn, data: bytes, filename: str, bounds: dict) -> None:
    """Runs in a fresh interpreter, so the limits in force arrive as an
    argument: module values changed in the parent are not visible here."""
    _limit_child(bounds["seconds"])
    try:
        if (filename or "").lower().endswith(".pdf"):
            _check_pdf_pages(data, bounds["pages"])
        conn.send(("ok", parse_resume(data, filename)))
    except ResumeParseError as exc:
        conn.send(("refused", str(exc)))
    except BaseException:
        conn.send(("refused", "Could not read that file. Paste the resume "
                              "text directly instead."))
    finally:
        conn.close()


def _sleep_forever(conn, data: bytes, filename: str, bounds: dict) -> None:
    """Stands in for a parser that never returns. Used by the tests."""
    while True:
        time.sleep(1)


_CHILD_TARGET = _child_main


def _parse_in_child(data: bytes, filename: str) -> str:
    """Parse in a separate process, so it can be stopped.

    A thread cannot be: one stuck inside a PDF parser keeps a core busy
    after the request that started it has long since timed out."""
    global _last_child
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    bounds = {"pages": MAX_PDF_PAGES, "seconds": PARSE_SECONDS}
    child = context.Process(
        target=_CHILD_TARGET, args=(sender, data, filename, bounds), daemon=True)
    _last_child = child
    child.start()
    sender.close()
    try:
        if not receiver.poll(PARSE_SECONDS):
            raise ResumeParseError(
                "Reading that file took too long and was stopped. Paste the "
                "resume text directly instead.")
        try:
            outcome, payload = receiver.recv()
        except EOFError:
            raise ResumeParseError(
                "Could not read that file. Paste the resume text directly instead.")
    finally:
        receiver.close()
        if child.is_alive():
            child.terminate()
            child.join(2)
            if child.is_alive():
                child.kill()
                child.join(2)
    if outcome != "ok":
        raise ResumeParseError(payload)
    return payload


def parse_resume_bounded(data: bytes, filename: str) -> str:
    """parse_resume(), for a file that came from outside.

    Blocks until the parse finishes or is stopped, so call it from a
    worker thread when on an event loop."""
    if not data:
        raise ResumeParseError("The uploaded file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise ResumeParseError(
            f"That file is larger than the {MAX_FILE_BYTES // 1_000_000} MB "
            "limit for a resume. Paste the text directly instead.")
    name = (filename or "").lower()
    if name.endswith((".txt", ".md", ".text")):
        return parse_resume(data, filename)          # nothing to unpack
    _check_type(data, filename)
    if name.endswith(".docx"):
        _check_archive(data)
    return _parse_in_child(data, filename)
