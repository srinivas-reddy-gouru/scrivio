/** Article markdown becomes DOM here, and only here.
 *
 * An article is written by a model that read the open web, so it is not
 * trusted HTML, whatever the pipeline that produced it. marked is explicit
 * that it does not sanitize: `<img src=x onerror=...>` in the markdown is
 * `<img src=x onerror=...>` in the output.
 *
 * The boundary is at the TOKEN level, not the string level. The markdown
 * parser already knows which spans are raw HTML and which are link
 * targets; asking it is reliable, and pattern-matching its output for
 * dangerous-looking text is the classic way to get this wrong.
 *
 *   raw HTML     is shown as the text it is, never parsed as markup
 *   link targets must be http, https, or mailto, or an in-page anchor
 *   image sources must be http or https
 *
 * Everything else marked emits is its own escaped output. The response's
 * Content-Security-Policy is the second line: it forbids inline script
 * and handlers, so a gap here would be a rendering bug, not an execution.
 */
import { Marked } from "marked";
import type { Tokens } from "marked";

const LINK_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);
const IMAGE_PROTOCOLS = new Set(["http:", "https:"]);

const ESCAPES: Record<string, string> = {
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
};
export const escapeHtml = (text: string): string =>
  (text ?? "").replace(/[&<>"']/g, (ch) => ESCAPES[ch]);

/** The URL if its scheme is on the list, otherwise null.
 *
 * Browsers ignore whitespace and control characters inside a scheme, so
 * "java\tscript:" is javascript: to them. Those characters are removed
 * before the scheme is read, and the URL is parsed rather than matched. */
export function allowedUrl(href: string, protocols: Set<string>, anchors = false): string | null {
  // eslint-disable-next-line no-control-regex
  const compact = (href ?? "").replace(/[\u0000- \u007f-\u009f​-‍﻿]+/g, "");
  if (!compact) return null;
  if (anchors && compact.startsWith("#")) return compact;
  let parsed: URL;
  try { parsed = new URL(compact); } catch { return null; }   // relative: refuse
  return protocols.has(parsed.protocol) ? parsed.href : null;
}

const engine = new Marked({
  async: false,
  gfm: true,
  renderer: {
    html(token: Tokens.HTML | Tokens.Tag): string {
      return escapeHtml(token.raw ?? token.text ?? "");
    },
    link(this: { parser: { parseInline(t: Tokens.Generic[]): string } }, token: Tokens.Link): string {
      const text = this.parser.parseInline(token.tokens ?? []);
      const href = allowedUrl(token.href, LINK_PROTOCOLS, true);
      if (!href) return text;                       // the words stay, the link goes
      const title = token.title ? ` title="${escapeHtml(token.title)}"` : "";
      const external = href.startsWith("#") ? "" : ' target="_blank" rel="noopener noreferrer nofollow"';
      return `<a href="${escapeHtml(href)}"${title}${external}>${text}</a>`;
    },
    image(token: Tokens.Image): string {
      const src = allowedUrl(token.href, IMAGE_PROTOCOLS);
      const alt = escapeHtml(token.text ?? "");
      if (!src) return alt;
      const title = token.title ? ` title="${escapeHtml(token.title)}"` : "";
      return `<img src="${escapeHtml(src)}" alt="${alt}"${title} loading="lazy" referrerpolicy="no-referrer">`;
    },
  },
});

/** Markdown to HTML that is safe to put in the page. */
export function renderMarkdown(markdown: string): string {
  return engine.parse(markdown ?? "") as string;
}
