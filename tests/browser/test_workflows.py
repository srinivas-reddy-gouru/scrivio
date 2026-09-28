"""The main workflows, driven through a real browser (R16, R18).

Demo mode supplies the model output, so these need no provider and spend
nothing. What they exercise is everything else: the interface, the API,
the boundary, the stream, the export gate, and the files on disk.

The same workflows are run twice: against the built interface served by
the API, which is how a user runs it, and against the Vite development
server, which is how a contributor does.
"""
import json

import pytest

from conftest import paired_page

RESUME = """Jordan Rivera
Backend Engineer
jordan@example.com | +1 555 010 1234 | Austin, TX

Summary
Backend engineer focused on event-driven systems.

Experience
Software Engineer, Acme Corp
Jan 2021 - Present
- Built Kafka pipelines processing 2M events/day
- Cut p99 latency 40% by rewriting the consumer group logic
- Mentored two junior engineers

Education
B.S. in Computer Science, State University, 2015 - 2019

Skills
Python, Go, Kafka, PostgreSQL
"""
JD = "Senior Backend Engineer.\n\nRequirements:\n- Python\n- Kafka\n- Kubernetes\n- PostgreSQL\n"


@pytest.fixture(params=["built", "dev"])
def app(request, browser, demo_server):
    """(page, base address) for each way of serving the interface."""
    if request.param == "built":
        base = demo_server.base
    else:
        base = request.getfixturevalue("dev_server")
    context, page = paired_page(browser, demo_server, base)
    yield page, base
    assert page.html_instead_of_data == [], \
        f"these API requests were answered with a web page: {page.html_instead_of_data}"
    context.close()


def tailored(page, base):
    page.goto(f"{base}/#/desk")
    page.get_by_label("Your resume text").fill(RESUME)
    page.get_by_label("Job description text").fill(JD)
    page.get_by_role("button", name="Read my resume").click()
    page.get_by_role("button", name="Tailor it to this JD").click(timeout=20000)
    page.locator(".metric-chip").first.wait_for(timeout=20000)


def test_resume_import_review_edit_and_export(app):
    page, base = app

    tailored(page, base)
    assert page.locator(".mode-banner.demo").is_visible(), "demo output is labelled"

    # A number is still a placeholder, so there is nothing to send yet.
    page.get_by_role("button", name="Send").click()
    assert page.get_by_role("heading", name="Not ready to send yet").is_visible()
    assert page.get_by_role("button", name="Download PDF").count() == 0

    # Supply it, save, and the finished export opens.
    page.get_by_role("button", name="Tailor").click()
    # With the mouse, as a user would. The line the chip sits on carries an
    # honesty note, which makes the whole line a button: a click meant for
    # the chip used to open the note, and the number could not be typed.
    chip = page.locator(".metric-chip").first
    chip.click()
    page.keyboard.type("35")
    page.keyboard.press("Tab")                    # the chip reports on blur
    page.get_by_role("button", name="Save 1 number").click()
    page.locator(".metric-chip").first.wait_for(state="detached", timeout=10000)
    page.get_by_role("button", name="Send").click()
    page.get_by_role("heading", name="Ready to send").wait_for(timeout=10000)

    with page.expect_download() as started:
        page.get_by_role("button", name="Markdown").click()
    download = started.value
    text = open(download.path(), encoding="utf-8").read()

    assert "DRAFT" not in download.suggested_filename
    assert "Jordan Rivera" in text and "35" in text and "[METRIC]" not in text


def test_a_mock_interview_from_start_to_feedback(app):
    page, base = app

    page.goto(f"{base}/#/interview")
    page.get_by_label("Interview topic").fill("Kafka consumer groups")
    page.get_by_role("button", name="Take a seat").click()
    answer = page.get_by_label("Your answer")
    answer.wait_for(timeout=20000)
    answer.fill(
        "A consumer group rebalances when its membership changes. Consumption "
        "pauses while partitions are reassigned to the members that remain.")
    page.get_by_role("button", name="Submit answer").click()

    page.get_by_text("You correctly identified the core concept.").wait_for(timeout=20000)
    assert page.locator(".mode-banner.demo").is_visible()
    assert "DEMO MODE" in page.locator("main").inner_text(), \
        "the canned feedback says what it is, not only the banner above it"


def test_an_article_run_is_followed_over_the_stream_to_the_reader(app):
    page, base = app

    page.goto(f"{base}/#/newsroom")
    page.get_by_label("Article topic or question").fill(
        "How Kafka consumer group rebalancing works, and what pauses during it")
    page.get_by_role("button", name="Generate").click()

    page.locator("article.prose h1").wait_for(timeout=60000)
    streams = [u for u in page.requested if "/jobs/" in u and u.endswith("/stream")]
    assert streams, "the run was followed over its event stream"
    assert "DEMO MODE" in page.locator("article.prose").inner_text()


def test_every_page_loads_without_a_script_error(app):
    page, base = app

    for room in ("floor", "newsroom", "interview", "job", "desk", "office"):
        page.goto(f"{base}/#/{room}")
        page.wait_for_selector("main#studio-main")
        page.wait_for_timeout(300)

    assert page.console_errors == []


# ── Error states ─────────────────────────────────────────────────────

def test_with_no_provider_the_interface_says_why_nothing_runs(browser, server):
    """`server` is a real-mode server with nothing configured."""
    context, page = paired_page(browser, server)

    page.goto(f"{server.base}/#/interview")
    page.get_by_label("Interview topic").fill("Kafka")
    page.get_by_role("button", name="Take a seat").click()

    page.locator(".errbox").first.wait_for(timeout=10000)
    shown = page.locator(".errbox").first.inner_text()
    assert "No model provider is configured" in shown
    assert page.locator(".mode-banner.blocked").is_visible()
    context.close()


def test_an_unpaired_browser_is_asked_to_pair_and_shown_nothing(browser, demo_server):
    context = browser.new_context()
    page = context.new_page()

    page.goto(f"{demo_server.base}/#/desk")

    page.get_by_role("heading", name="Pair this browser").wait_for(timeout=10000)
    assert page.locator("main#studio-main").count() == 0
    context.close()


def test_a_session_that_ends_mid_use_returns_to_the_pairing_screen(browser, demo_server):
    context, page = paired_page(browser, demo_server)
    page.goto(f"{demo_server.base}/#/desk")
    page.get_by_role("button", name="Read my resume").wait_for()

    context.clear_cookies()
    page.get_by_label("Your resume text").fill(RESUME)
    page.get_by_role("button", name="Read my resume").click()

    page.get_by_role("heading", name="Pair this browser").wait_for(timeout=10000)
    context.close()


def test_a_request_the_server_refuses_is_shown_not_swallowed(browser, demo_server):
    context, page = paired_page(browser, demo_server)
    page.goto(f"{demo_server.base}/#/desk")

    page.get_by_label("Your resume text").fill("x" * 40)
    page.get_by_label("Job posting URL").fill("http://169.254.169.254/latest/meta-data/")
    page.get_by_role("button", name="Read my resume").click()

    page.locator(".errbox").first.wait_for(timeout=10000)
    assert "was not fetched" in page.locator(".errbox").first.inner_text()
    context.close()
