"""The first five minutes, for someone who has never seen it (R23).

Run in demo mode on a store with nothing in it, which is what a new
install looks like. The sample resume and posting are invented, so
nothing here needs a personal document.
"""
import pytest

from conftest import paired_page

PAGES = ["", "#/desk", "#/job", "#/interview", "#/newsroom", "#/settings"]

# Everything a person can operate, and the name a screen reader would
# announce for it. An element with no name is announced as "button".
UNNAMED = """
() => {
  const named = (el) => {
    const by = (el.getAttribute("aria-labelledby") || "").split(/\\s+/)
      .map((id) => document.getElementById(id)?.textContent || "").join(" ");
    const label = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    return [el.getAttribute("aria-label"), by, label?.textContent,
            el.closest("label")?.textContent, el.textContent, el.getAttribute("title"),
            el.getAttribute("alt"), el.getAttribute("placeholder"),
            el.tagName === "INPUT" && ["submit", "button"].includes(el.type) ? el.value : ""]
      .some((t) => (t || "").trim().length > 0);
  };
  const shown = (el) => {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return box.width > 0 && box.height > 0 && style.visibility !== "hidden";
  };
  return [...document.querySelectorAll(
      "button, a[href], input:not([type=hidden]), select, textarea, [role=button], [tabindex]")]
    .filter(shown).filter((el) => !named(el))
    .map((el) => el.outerHTML.slice(0, 140));
}
"""


@pytest.fixture
def fresh(browser, tmp_path):
    """A server of its own with nothing stored, because a first visit is
    only a first visit once: the sample is offered on the home page until
    there is something of the user's to show instead."""
    from conftest import DIST
    from live_server import LiveServer

    if not DIST.is_file():
        pytest.skip("web/dist is not built: run `npm ci && npm run build` in web/")
    live = LiveServer(tmp_path / "first-run", demo=True).start()
    context, page = paired_page(browser, live, live.base)
    try:
        yield page, live.base
    finally:
        context.close()
        live.stop()


@pytest.fixture
def anywhere(browser, demo_server):
    context, page = paired_page(browser, demo_server, demo_server.base)
    yield page, demo_server.base
    context.close()


def test_a_first_visit_says_who_it_is_for_and_offers_a_sample(fresh):
    page, base = fresh
    page.goto(base + "/")
    page.get_by_role("heading", level=1).wait_for()
    said = page.locator("main").inner_text()

    assert "For people applying to jobs" in said
    assert "resume" in said.lower() and "posting" in said.lower()
    assert page.get_by_role("button", name="Try it with a sample resume").is_visible()
    # The order the page puts them in is the order of the work.
    names = page.locator(".studio-tile b").all_inner_texts()
    assert names == ["Resume", "Job prep", "Interviews", "Articles"]


def test_the_sample_runs_end_to_end_and_is_labelled_throughout(fresh):
    page, base = fresh
    page.goto(base + "/")
    page.get_by_role("button", name="Try it with a sample resume").click()

    resume = page.get_by_label("Your resume text", exact=True)
    resume.wait_for()
    assert "jordan@example.com" in resume.input_value()
    assert "Payments Platform" in page.get_by_label("Job description text", exact=True).input_value()
    note = page.locator(".sample-note")
    assert "invented" in note.inner_text()
    assert "canned" in note.inner_text(), "in demo mode it says the results are examples too"

    page.get_by_role("button", name="Read my resume").click()
    page.get_by_role("button", name="Tailor it to this JD").wait_for(timeout=20000)
    assert page.locator(".mode-banner.demo").is_visible()


def test_the_sample_does_not_come_back_over_what_was_typed(fresh):
    page, base = fresh
    page.goto(base + "/")
    page.get_by_role("button", name="Try it with a sample resume").click()
    resume = page.get_by_label("Your resume text", exact=True)
    resume.wait_for()
    resume.fill("My own resume text")

    page.goto(base + "/#/interview")
    page.goto(base + "/#/desk")
    page.get_by_label("Your resume text", exact=True).wait_for()

    assert "jordan@example.com" not in page.get_by_label("Your resume text", exact=True).input_value()


def test_the_sample_can_be_reached_and_started_from_the_keyboard(fresh):
    page, base = fresh
    page.goto(base + "/")
    wanted = page.get_by_role("button", name="Try it with a sample resume")
    wanted.wait_for()

    for _ in range(60):
        page.keyboard.press("Tab")
        if wanted.evaluate("el => el === document.activeElement"):
            break
    else:
        pytest.fail("sixty presses of Tab never reached the sample button")
    page.keyboard.press("Enter")

    page.get_by_label("Your resume text", exact=True).wait_for()
    assert "Jordan Rivera" in page.get_by_label("Your resume text", exact=True).input_value()


@pytest.mark.parametrize("where", PAGES)
def test_a_phone_width_screen_does_not_scroll_sideways(anywhere, where):
    page, base = anywhere
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{base}/{where}")
    page.wait_for_timeout(700)

    wide, shown = page.evaluate(
        "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert wide <= shown + 1, f"{where or 'home'} is {wide}px wide on a {shown}px screen"


@pytest.mark.parametrize("where", PAGES)
def test_everything_that_can_be_operated_has_a_name(anywhere, where):
    page, base = anywhere
    page.goto(f"{base}/{where}")
    page.wait_for_timeout(700)

    assert page.evaluate(UNNAMED) == []


def test_once_there_is_work_of_your_own_the_sample_moves_out_of_the_way(fresh):
    """The home page stops offering it; the resume page still has it."""
    page, base = fresh
    page.goto(base + "/")
    page.get_by_role("button", name="Try it with a sample resume").click()
    page.get_by_role("button", name="Read my resume").click()
    page.get_by_role("button", name="Tailor it to this JD").wait_for(timeout=20000)

    page.goto(base + "/")
    page.get_by_role("heading", level=1).wait_for()
    page.locator(".recent-row").first.wait_for()
    assert page.get_by_role("button", name="Try it with a sample resume").count() == 0


def test_the_job_target_form_names_every_field(anywhere):
    """It sits behind a button, so the page-by-page check above never
    sees it. Its labels were text beside the fields, attached to nothing:
    the last field and the file picker were announced with no name."""
    page, base = anywhere
    page.goto(f"{base}/#/job")
    page.get_by_role("button", name="Add a job target").click()
    page.get_by_role("heading", name="Add a job target").wait_for()

    assert page.evaluate(UNNAMED) == []
    for name in ("Role title", "Company", "Job posting URL", "Job description text",
                 "Your resume text", "Resume file", "Anything else"):
        assert page.get_by_label(name, exact=True).count() == 1, name


def test_the_job_target_form_fits_a_phone(anywhere):
    page, base = anywhere
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{base}/#/job")
    page.get_by_role("button", name="Add a job target").click()
    page.get_by_role("heading", name="Add a job target").wait_for()

    wide, shown = page.evaluate(
        "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert wide <= shown + 1
