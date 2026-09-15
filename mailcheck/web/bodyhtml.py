"""Turn a stored message body into readable HTML.

Bodies reach us as plain text: `normalize.clean_text` has already run the
original HTML through html2text, stripped quoted history and footers, and
shortened long URLs. Showing that in a monospace block treated a letter like a
stack trace — one wall of code-font text, paragraphs indistinguishable from
line wraps, and every link dead.

A Markdown parser would be the wrong tool. Measured across 800 stored bodies:
96% separate paragraphs with a blank line, 58% contain a URL, 32% carry an
image's alt text on a line of its own, and 12% carry the pipes html2text emits
for a table — while Markdown link syntax appears in 12%, bullets in 8%,
headings in 4% and bold in 1%. The readability problem is in every message;
Markdown syntax is in a corner of them. So this renders the structure that is
actually there, and cleans up the artefacts html2text leaves behind, which
would otherwise read as punctuation noise.

Safety: the input is whatever a stranger emailed. Every span of text is escaped
here and the only tags in the output are the ones this module writes, so the
result is safe to mark as markup. Only ``http``, ``https`` and ``mailto`` ever
become a link — a `javascript:` or `data:` address stays inert text.
"""

from __future__ import annotations

import html
import re

from markupsafe import Markup

#: `[label](url)`, optionally `[label](url "title")`. The label is often empty —
#: html2text writes `[ ](…)` for an anchor wrapped around an image — so the
#: title, and then the address itself, stand in for it.
_MD_LINK = (
    r"\[(?P<label>[^\]\n]*)\]\((?P<md_url>(?:https?://|mailto:)[^)\s]+)"
    r'(?:\s+"(?P<title>[^"\n]*)")?\)'
)
#: The same thing, with the address cut short by ``clean_text`` — which takes
#: the closing bracket with it, so the pattern above can never match. Both are
#: common, and together they are common enough to matter: without this the
#: label leaks into the page as literal `[…](` syntax around a broken URL.
_MD_LINK_CUT = r"\[(?P<cut_label>[^\]\n]*)\]\((?P<cut_url>https?://[^)\s]*\.\.\.)"
#: `<url>` as html2text writes a bare link, and an unwrapped one.
_ANGLE_URL = r"<(?P<angle_url>(?:https?://|mailto:)[^>\s]+)>"
_BARE_URL = r"(?P<bare_url>(?:https?://|mailto:)[^\s<>()\[\]]+)"
_TOKEN = re.compile(f"{_MD_LINK}|{_MD_LINK_CUT}|{_ANGLE_URL}|{_BARE_URL}")

_BULLET = re.compile(r"^\s*[*\-•]\s+(?P<item>.+)$")
_HEADING = re.compile(r"^\s*#{1,6}\s+(?P<text>.+?)\s*#*$")
_RULE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
#: A line that is only `[Something]` — html2text's placeholder for an image it
#: was told to ignore. Common in job-board digests, and pure noise to read.
_IMG_ALT = re.compile(r"^\s*\[(?P<alt>[^\]]{1,80})\]\s*$")

#: html2text renders every table as pipe-delimited rows — and HTML email lays
#: itself out in tables, so this is almost never tabular data. The pipes are
#: stripped and the text kept; a row holding nothing but pipes is dropped.
_PIPE_ROW = re.compile(r"^\s*\|")
#: The `---|---|---` under a table's first row.
_TABLE_SEP = re.compile(r"^[\s|:-]*-{3,}[\s|:-]*$")

#: Emphasis typed by the sender. Each edge must sit against a non-space
#: character, so `5 * 3 * 2` and a bullet's leading star are not emphasis.
_STRONG = re.compile(r"\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*")
_EM = re.compile(r"\*(?=\S)([^*\n]+?)(?<=\S)\*")

#: Trailing sentence punctuation swallowed by the URL pattern.
_TRAILING = ".,;:!?"

#: Longer than this and a URL is shown as its host instead. The full address
#: still goes in the href — this only decides what the reader has to look at.
_URL_DISPLAY_MAX = 52


def _compact(url: str) -> str:
    """A long URL shown whole is a paragraph of noise. Show where it goes."""
    if url.startswith("mailto:"):
        return url[len("mailto:") :]        # the address is the readable part
    if len(url) <= _URL_DISPLAY_MAX:
        return url
    # Stop at the path, the query *or* the fragment. Matching only on "/" let a
    # tracking query masquerade as the hostname, so a link to a bare domain
    # came out as `example.com?utm_campaign=…&utm_source=…/…`.
    host = re.match(r"https?://([^/?#]+)", url)
    return f"{host.group(1)}/…" if host else url[:_URL_DISPLAY_MAX] + "…"


def _dead(label: str) -> str:
    """Text that looked like a link but cannot be followed."""
    return (
        '<span class="rb-dead" title="This address was shortened when the message '
        f'was stored, so it is no longer complete">{html.escape(label)}</span>'
    )


def _anchor(url: str, label: str | None = None) -> str:
    """One link — or a dead one, rendered honestly as text.

    ``clean_text`` truncates URLs past 90 characters and marks the cut with an
    ellipsis, so a third of stored bodies hold an address that no longer
    resolves. Presenting one of those as a link would be a button that goes
    nowhere; it becomes plain text instead, and says why on hover.
    """
    if url.endswith("..."):
        return _dead(_compact(url))
    return (
        f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">'
        f"{html.escape(label if label else _compact(url))}</a>"
    )


def _emphasize(escaped: str) -> str:
    """`*word*` -> emphasis, on text that has already been escaped.

    `ignore_emphasis` is on when html2text runs, but plenty of senders type the
    asterisks themselves, and they arrive as punctuation sprayed through a
    sentence — "*You have an upcoming interview with* *OKG* *.*". Both patterns
    demand a non-space character on each inside edge, so arithmetic and bullet
    leaders are left alone. Safe after escaping: the captured text cannot carry
    a tag, and the only ones added here are these.
    """
    escaped = _STRONG.sub(r"<strong>\1</strong>", escaped)
    return _EM.sub(r"<em>\1</em>", escaped)


def _inline(text: str) -> str:
    """Escape a line, and turn the links in it into anchors."""
    out: list[str] = []
    pos = 0
    for m in _TOKEN.finditer(text):
        before = text[pos : m.start()]

        if m.group("bare_url") or m.group("cut_url"):
            # A `<https://…>` whose URL was shortened lost its closing bracket
            # with the tail that was cut, so the opener never matched as part
            # of a link and would dangle in front of it. Same for the `[` of an
            # image or link whose address ran past the limit.
            before = re.sub(r"!?[<\[]$", "", before)

        if m.group("angle_url"):
            # html2text writes an anchor as `text <href>`, so an address that
            # links to itself arrives twice — `a@b.com<mailto:a@b.com>`. Show
            # it once.
            shown = _compact(m.group("angle_url"))
            if before.rstrip().endswith(shown):
                before = before.rstrip()[: -len(shown)]

        out.append(_emphasize(html.escape(before)))

        if m.group("md_url"):
            label = (m.group("label") or "").strip() or (m.group("title") or "").strip()
            out.append(_anchor(m.group("md_url"), label or None))
        elif m.group("cut_url"):
            # The address is unusable, but the label is the part worth reading:
            # keep the words, mark them as something you cannot follow.
            out.append(_dead((m.group("cut_label") or "").strip() or _compact(m.group("cut_url"))))
        elif m.group("angle_url"):
            out.append(_anchor(m.group("angle_url")))
        else:
            url = m.group("bare_url")
            # Give back a sentence's full stop, which the pattern above ate.
            # Never the ellipsis of a shortened URL: that is part of the mark.
            trail = ""
            while len(url) > 1 and url[-1] in _TRAILING and not url.endswith("..."):
                trail = url[-1] + trail
                url = url[:-1]
            out.append(_anchor(url))
            out.append(html.escape(trail))
        pos = m.end()
    out.append(_emphasize(html.escape(text[pos:])))
    return "".join(out)


def render_body(text: str | None) -> Markup:
    """Plain-text body -> paragraphs, lists and links.

    Returns empty markup for an empty body; the caller decides what to say in
    that case, because "no body was stored" and "this email was blank" are not
    the same thing to a reader.
    """
    if not text or not text.strip():
        return Markup("")

    blocks: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush_para() -> None:
        if para:
            # Single newlines inside a paragraph are real: signatures and
            # addresses depend on them. Blank lines separate ideas.
            blocks.append("<p>" + "<br>".join(_inline(ln) for ln in para) + "</p>")
            para.clear()

    def flush_list() -> None:
        if items:
            blocks.append(
                "<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>"
            )
            items.clear()

    def flush_all() -> None:
        flush_list()
        flush_para()

    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            flush_all()
            continue

        # A table's own separator row is scaffolding, never content.
        if "|" in line and _TABLE_SEP.match(line):
            continue
        if _PIPE_ROW.match(line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            joined = "  ".join(c for c in cells if c)
            if not joined:
                continue                    # a spacer row from a layout table
            line = joined

        # Order matters: `---` is a rule, not a bullet with nothing after it.
        if _RULE.match(line):
            flush_all()
            blocks.append("<hr>")
        elif m := _HEADING.match(line):
            flush_all()
            blocks.append(f'<p class="rb-head">{_inline(m.group("text"))}</p>')
        elif m := _IMG_ALT.match(line):
            flush_all()
            blocks.append(f'<p class="rb-alt">{_inline(m.group("alt"))}</p>')
        elif m := _BULLET.match(line):
            flush_para()
            items.append(m.group("item"))
        else:
            flush_list()
            para.append(line)

    flush_all()
    return Markup("".join(_tidy_rules(blocks)))


def _tidy_rules(blocks: list[str]) -> list[str]:
    """Drop horizontal rules that separate nothing.

    Stripping a layout table's scaffolding leaves its rules behind — stacked
    against each other, or stranded at the top and bottom of the message. A
    rule only means anything between two pieces of content.
    """
    out: list[str] = []
    for block in blocks:
        if block == "<hr>" and (not out or out[-1] == "<hr>"):
            continue
        out.append(block)
    while out and out[-1] == "<hr>":
        out.pop()
    return out
