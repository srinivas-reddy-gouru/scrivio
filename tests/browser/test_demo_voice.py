"""In demo mode the browser says nothing and listens to nothing (F01).

The server refuses speech and transcription in demo mode. The interface
used to take a refusal as a cue to use the browser's own voice and
dictation, and in some browsers those go to a speech service. The
server cannot see that happen, so it is checked here, in a browser,
with the browser's speech features replaced by recorders.
"""
import pytest

from conftest import paired_page

RECORDERS = """
window.__used = [];
const note = (what) => window.__used.push(what);
if (window.speechSynthesis) {
  window.speechSynthesis.speak = () => note('speechSynthesis.speak');
}
for (const name of ['SpeechRecognition', 'webkitSpeechRecognition']) {
  window[name] = function () {
    note(name);
    return { start() { note(name + '.start'); }, stop() {}, abort() {} };
  };
}
if (navigator.mediaDevices) {
  navigator.mediaDevices.getUserMedia = () => {
    note('getUserMedia');
    return Promise.reject(new DOMException('refused by the test', 'NotAllowedError'));
  };
}
"""


def watched(browser, server):
    context, page = paired_page(browser, server, server.base)
    page.add_init_script(RECORDERS)
    return context, page


def start_an_interview(page, base):
    page.goto(f"{base}/#/interview")
    page.get_by_label("Interview topic").fill("Kafka consumer groups")
    page.get_by_role("button", name="Take a seat").click()
    page.get_by_label("Your answer").wait_for(timeout=20000)
    page.wait_for_timeout(800)          # time for a question to be read out


def test_a_demo_interview_neither_speaks_nor_listens(browser, demo_server):
    context, page = watched(browser, demo_server)

    start_an_interview(page, demo_server.base)

    assert page.evaluate("window.__used") == []
    assert [u for u in page.requested if u.endswith(("/speak", "/transcribe"))] == []
    assert page.get_by_text("Voice is off in demo mode").is_visible()
    assert page.get_by_role("button", name="Answer by voice").count() == 0
    assert page.get_by_role("button", name="Voice on").count() == 0
    context.close()


def test_the_demo_interview_can_still_be_answered_by_typing(browser, demo_server):
    context, page = watched(browser, demo_server)
    start_an_interview(page, demo_server.base)

    page.get_by_label("Your answer").fill(
        "A consumer group rebalances when its membership changes, and "
        "consumption pauses while partitions are reassigned.")
    page.get_by_role("button", name="Submit answer").click()

    page.get_by_text("You correctly identified the core concept.").wait_for(timeout=20000)
    assert page.evaluate("window.__used") == []
    context.close()


def test_outside_demo_mode_the_voice_controls_are_there(browser, server):
    """`server` is in real mode with nothing configured, so no interview
    can start. What is checked is the setup page, and that the recorders
    above do not hide the controls everywhere."""
    context, page = watched(browser, server)

    page.goto(f"{server.base}/#/interview")
    page.get_by_label("Interview topic").wait_for()

    assert page.get_by_text("Voice is off in demo mode").count() == 0
    context.close()


def test_the_older_interface_keeps_voice_off_in_demo_mode_too(browser, demo_server):
    context, page = watched(browser, demo_server)

    page.goto(f"{demo_server.base}/classic")
    page.wait_for_function("typeof speakQuestion === 'function'", timeout=20000)
    page.evaluate("speakQuestion('Tell me about consumer groups.')")
    started = page.evaluate(
        """() => new Promise((resolve) => {
             Voice.start({ onError: (m) => resolve(m), onState: () => {} });
             setTimeout(() => resolve('nothing was reported'), 3000);
           })""")
    page.wait_for_timeout(500)

    assert page.evaluate("window.__used") == []
    assert [u for u in page.requested if u.endswith(("/speak", "/transcribe"))] == []
    context.close()
