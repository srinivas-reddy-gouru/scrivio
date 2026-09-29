"""What the interface asks of machines other than this one, and what
happens when one of them does not answer.

This began as an investigation of one browser test that timed out once,
after 30 seconds, loading a page. Two things were established and one
was not.

  Established: the pages load their fonts from Google Fonts, through a
  stylesheet in the head of the page, which the browser will not draw
  the page without. If that request is never answered, nothing is
  drawn, and loading the page times out with exactly the error that
  was seen.

  Established: the Vite development server building its dependency
  cache from nothing is not the cause. That takes about a second.

  NOT established: that the font request is what happened on the day.
  The failure left no record of what the page was waiting for. It
  does now (see conftest.py), so a second occurrence will say.

Nothing here retries, and nothing here is given longer to finish.
"""
import json

import pytest

from conftest import account_of, paired_page, write_account

FONT_HOSTS = {"fonts.googleapis.com", "fonts.gstatic.com"}


def hosts_asked(page) -> set[str]:
    return set(account_of(page)["hosts"])


@pytest.mark.parametrize("where", ["", "#/desk", "#/job", "#/interview", "#/newsroom"])
def test_the_current_interface_asks_nothing_of_anyone_but_the_font_hosts(
        browser, demo_server, where):
    """A list of who is asked, so that an addition to it is a decision
    and not an accident. The requests are made whether or not they are
    answered, so this does not depend on the network being there."""
    context, page = paired_page(browser, demo_server)
    own = demo_server.base.split("//", 1)[1]

    page.goto(f"{demo_server.base}/{where}", wait_until="commit")
    page.wait_for_selector("main#studio-main", timeout=20000)
    page.wait_for_timeout(500)

    assert hosts_asked(page) - {own} <= FONT_HOSTS
    context.close()


def held(context) -> None:
    """The font host, reached and never answering."""
    context.route("**://fonts.googleapis.com/**", lambda route: None)


def test_a_font_host_that_never_answers_gives_the_failure_that_was_seen(browser, demo_server):
    """The mechanism, shown. With a limit of 3 seconds here, where the
    test that failed had 30: what matters is that every request to this
    machine has finished and the page is still not loaded."""
    context, page = paired_page(browser, demo_server)
    held(context)

    with pytest.raises(Exception, match="Timeout 3000ms exceeded"):
        page.goto(f"{demo_server.base}/#/desk", timeout=3000)

    account = account_of(page)
    own = demo_server.base.split("//", 1)[1]
    assert account["hosts"][own]["never_finished"] == 0
    assert [w["what"] for w in account["still_waiting_for"]] == ["fonts.googleapis.com/css2"]
    context.close()


@pytest.mark.xfail(strict=True, reason=(
    "A defect in the product, recorded and not fixed. The font stylesheet is in the head of "
    "the page and the browser will not draw the page until it has been answered. A font "
    "host that is slow holds up the whole interface, and one that never answers holds it "
    "up until the browser gives up. Fixing it means serving the fonts from this machine "
    "or loading them without blocking, and both are the owner's to decide (A12)."))
def test_the_interface_is_drawn_without_waiting_for_the_font_host(browser, demo_server):
    context, page = paired_page(browser, demo_server)
    held(context)

    page.goto(f"{demo_server.base}/#/desk", wait_until="commit")

    try:
        page.wait_for_selector("main#studio-main", timeout=5000)
    finally:
        context.close()


def test_a_font_host_that_refuses_at_once_holds_nothing_up(browser, demo_server):
    """Which is what being offline is. The interface works with the
    fonts the machine has."""
    context, page = paired_page(browser, demo_server)
    for host in FONT_HOSTS:
        context.route(f"**://{host}/**", lambda route: route.abort())

    page.goto(f"{demo_server.base}/#/desk", timeout=10000)

    page.wait_for_selector("main#studio-main", timeout=5000)
    context.close()


def test_a_failure_leaves_an_account_of_what_the_page_was_waiting_for(
        browser, demo_server, tmp_path, monkeypatch):
    import conftest

    monkeypatch.setattr(conftest, "DIAGNOSTICS", tmp_path / "diagnostics")
    context, page = paired_page(browser, demo_server)
    held(context)
    try:
        page.goto(f"{demo_server.base}/#/desk", timeout=2000)
    except Exception as error:
        failure = str(error)

    written = write_account("tests/browser/example.py::test_example[dev]", failure, [page])

    kept = json.loads(written.read_text(encoding="utf-8"))
    assert kept["test"].endswith("test_example[dev]")
    assert any("Timeout 2000ms exceeded" in line for line in kept["failure"])
    assert kept["pages"][0]["still_waiting_for"][0]["what"] == "fonts.googleapis.com/css2"
    assert kept["pages"][0]["hosts"]["fonts.googleapis.com"]["never_finished"] == 1
    assert "the API server" in kept["logs"]
    context.close()


def test_the_development_server_keeps_its_log(browser, demo_server, dev_server):
    import conftest

    context, page = paired_page(browser, demo_server, dev_server)
    page.goto(f"{dev_server}/#/desk", timeout=20000)
    context.close()

    log = conftest._LOGS["the Vite development server"]
    assert log.is_file() and "VITE" in log.read_text(encoding="utf-8", errors="replace")
