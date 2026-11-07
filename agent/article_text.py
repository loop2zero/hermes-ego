"""Turn a page's HTML into article markdown.

The daemon reads `document.documentElement.outerHTML` from ego, so a logged-in
page is not fetched again. Trafilatura picks the main column. Chrome (nav,
footer, header, aside, cookie and consent blocks) is removed first so a short
page is not flattened together with the menu.

When ego cannot return HTML, the daemon sends the accessibility snapshot with
`fallback: "snapshot"`. `clean_accessibility_text` drops obvious chrome lines.
That text is still the accessibility tree, only shorter.
"""

from __future__ import annotations

import re
import urllib.parse

import trafilatura
from lxml import html as lhtml

_CHROME_XPATH = (
    "//script|//style|//noscript|//iframe|//svg|//form"
    "|//nav|//footer|//header|//aside"
    "|//*[@role='navigation' or @role='banner' or @role='contentinfo' or @role='complementary']"
)
_CHROME_ATTR = re.compile(
    r"cookie|consent|sidebar|newsletter|popup|modal|breadcrumb|gdpr",
    re.IGNORECASE,
)
_CHROME_LINE = re.compile(
    r"^(cookie|accept all|accept cookies|reject all|we use cookies|"
    r"privacy policy|terms of (use|service)|sign in|log in|subscribe|newsletter)\b",
    re.IGNORECASE,
)
_LANDMARK_LINE = frozenset({
    "navigation", "banner", "contentinfo", "complementary", "footer", "nav",
})


def article_from_html(html: str, url: str | None = None) -> str:
    """Main column as markdown. Image destinations stay http(s) URLs."""
    cleaned = _strip_chrome(html)
    page_url = url if isinstance(url, str) and url else None
    text = trafilatura.extract(
        cleaned,
        url=page_url,
        output_format="markdown",
        include_images=True,
        include_links=True,
        include_tables=True,
        include_comments=False,
    )
    if not isinstance(text, str) or not text.strip():
        text = _html_to_markdown(cleaned, page_url)
    return text.strip()


def clean_accessibility_text(text: str) -> str:
    """Drop chrome lines from an accessibility snapshot. The rest is kept."""
    kept: list[str] = []
    blank = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            blank += 1
            if blank <= 1:
                kept.append("")
            continue
        blank = 0
        if stripped.lower() in _LANDMARK_LINE or _CHROME_LINE.match(stripped):
            continue
        kept.append(line.rstrip())
    return "\n".join(kept).strip()


def _strip_chrome(html: str) -> str:
    try:
        doc = lhtml.fromstring(html)
    except Exception:
        return html
    for element in list(doc.xpath(_CHROME_XPATH)):
        if _inside_content(element):
            continue
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
    for element in list(doc.xpath("//*[@id or @class]")):
        blob = f"{element.get('id') or ''} {element.get('class') or ''}"
        if not _CHROME_ATTR.search(blob) or _inside_content(element):
            continue
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
    return lhtml.tostring(doc, encoding="unicode")


def _inside_content(element) -> bool:
    parent = element.getparent()
    while parent is not None:
        if getattr(parent, "tag", None) in ("article", "main"):
            return True
        parent = parent.getparent()
    return False


def _html_to_markdown(html: str, url: str | None) -> str:
    try:
        doc = lhtml.fromstring(html)
    except Exception:
        return ""
    nodes = doc.xpath("//article|//main")
    root = nodes[0] if nodes else doc
    parts: list[str] = []
    _walk(root, parts, url, ordered=False)
    return "\n\n".join(part for part in parts if part.strip())


def _walk(element, parts: list[str], url: str | None, *, ordered: bool) -> None:
    tag = getattr(element, "tag", None)
    if not isinstance(tag, str):
        return
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(tag[1])
        text = _inline(element, url)
        if text:
            parts.append(f"{'#' * level} {text}")
        return
    if tag == "p":
        text = _inline(element, url)
        if text:
            parts.append(text)
        return
    if tag == "li":
        text = _inline(element, url)
        if text:
            prefix = "1. " if ordered else "- "
            parts.append(prefix + text)
        return
    if tag == "img":
        image = _image(element, url)
        if image:
            parts.append(image)
        return
    child_ordered = tag == "ol"
    for child in element:
        _walk(child, parts, url, ordered=child_ordered)


def _inline(element, url: str | None) -> str:
    chunks: list[str] = []
    if element.text:
        chunks.append(element.text)
    for child in element:
        tag = getattr(child, "tag", None)
        if tag == "a":
            label = "".join(child.itertext()).strip()
            href = _absolute(child.get("href") or "", url)
            if label and href:
                chunks.append(f"[{label}]({href})")
            elif label:
                chunks.append(label)
        elif tag == "img":
            image = _image(child, url)
            if image:
                chunks.append(image)
        else:
            chunks.append("".join(child.itertext()))
        if child.tail:
            chunks.append(child.tail)
    return " ".join(" ".join(chunks).split())


def _image(element, url: str | None) -> str:
    src = _absolute(element.get("src") or "", url)
    if not src.startswith(("http://", "https://")):
        return ""
    alt = " ".join((element.get("alt") or "image").split()) or "image"
    return f"![{alt}]({src})"


def _absolute(value: str, url: str | None) -> str:
    value = value.strip()
    if not value or not url:
        return value
    return urllib.parse.urljoin(url, value)
