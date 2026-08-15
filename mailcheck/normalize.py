"""MIME text -> clean plain text.

This stage directly controls both LLM cost and accuracy: everything it strips
is a token not spent, and quoted history is the main source of misclassification
(a reply to a rejection still contains the original rejection text).
"""

from __future__ import annotations

import re

import html2text

from .models import NormalizedMessage, RawMessage

_HTML_HINT = re.compile(r"<(?:html|body|div|table|p|br|a)\b", re.I)

# Where quoted history begins. Everything from the first hit onward is dropped.
_QUOTE_MARKERS = [
    re.compile(r"^\s*On .{0,200}\bwrote:\s*$", re.I | re.M),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.I | re.M),
    re.compile(r"^\s*-{3,}\s*Forwarded message\s*-{3,}\s*$", re.I | re.M),
    re.compile(r"^\s*_{10,}\s*$", re.M),
    re.compile(r"^\s*From:.*\n\s*Sent:.*\n", re.I | re.M),
    re.compile(r"^\s*El .{0,200}\bescribió:\s*$", re.I | re.M),
]

# Footer boilerplate. Only cut if it appears late in the body, so an email whose
# whole point is "unsubscribe" is not reduced to nothing.
_FOOTER_MARKERS = [
    re.compile(r"\bunsubscribe\b", re.I),
    re.compile(r"you (?:are )?receiv(?:ed|ing) this (?:email|message)", re.I),
    re.compile(r"this (?:e-?mail|message) was sent to\b", re.I),
    re.compile(r"confidentialit(?:y|é) notice", re.I),
    re.compile(r"this (?:e-?mail|message) (?:and any attachments )?(?:is|are) confidential", re.I),
    re.compile(r"^\s*sent from my (?:iphone|ipad|android|samsung)", re.I | re.M),
]

_URL = re.compile(r"https?://\S+")
_MULTI_BLANK = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+$", re.M)


def _html_to_text(html: str) -> str:
    h = html2text.HTML2Text()
    h.body_width = 0          # no hard wrapping - it fragments sentences
    h.ignore_images = True
    h.ignore_links = False    # Calendly / HackerRank links carry real signal
    h.ignore_emphasis = True
    h.skip_internal_links = True
    return h.handle(html)


def _strip_quotes(text: str) -> str:
    cut = len(text)
    for marker in _QUOTE_MARKERS:
        m = marker.search(text)
        if m and m.start() < cut:
            cut = m.start()
    text = text[:cut]
    # Drop residual `>` quote lines.
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines)


def _strip_footer(text: str) -> str:
    # Only cut a marker that sits late in the body AND leaves real content
    # behind, so a short email whose whole point is "unsubscribe" survives.
    threshold = max(120, int(len(text) * 0.5))
    if len(text) <= threshold:
        return text
    cut = len(text)
    for marker in _FOOTER_MARKERS:
        for m in marker.finditer(text):
            if m.start() >= threshold:
                cut = min(cut, m.start())
                break
    return text[:cut]


def _shorten_urls(text: str, max_len: int = 90) -> str:
    def repl(m: re.Match[str]) -> str:
        url = m.group(0)
        if len(url) <= max_len:
            return url
        # Keep scheme+host+start of path; tracking query strings are pure burn.
        return url[:max_len] + "..."

    return _URL.sub(repl, text)


def clean_text(raw: str) -> str:
    text = raw or ""
    if _HTML_HINT.search(text):
        text = _html_to_text(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("‌", "").replace(" ", " ")  # zero-width, nbsp
    text = _strip_quotes(text)
    text = _strip_footer(text)
    text = _shorten_urls(text)
    text = _TRAILING_WS.sub("", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()


def normalize(
    raw: RawMessage, *, account_id: int, account_label: str, max_body_chars: int = 1200
) -> NormalizedMessage:
    body = clean_text(raw.text)
    if len(body) > max_body_chars:
        # Keep the head: job mail states its intent in the first paragraph.
        body = body[:max_body_chars].rstrip() + "\n[...truncated]"
    return NormalizedMessage(
        account_id=account_id,
        account_label=account_label,
        message_id=raw.message_id,
        uid=raw.uid,
        folder=raw.folder,
        from_addr=raw.from_addr,
        from_name=raw.from_name,
        subject=raw.subject,
        date_utc=raw.date_utc,
        body=body,
        provider_url=raw.provider_url,
    )
