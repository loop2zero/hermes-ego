"""Generic article extraction. Yitang flattening stays in test_yitang_text."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
for path in (str(ROOT), str(AGENT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from article_text import article_from_html, clean_accessibility_text
from relay.server import cap_text
from yitang_text import flatten_data_json

SIMPLE = """<!doctype html>
<html><head><title>How potatoes grow</title></head>
<body>
<article>
  <h1>How potatoes grow</h1>
  <p>Potatoes grow underground in cool soil.</p>
  <h2>Steps</h2>
  <ul><li>Plant the seed</li><li>Water weekly</li></ul>
  <p>Read the <a href="https://example.com/guide">full guide</a>.</p>
  <p><img src="https://cdn.example/potato.png" alt="a potato"></p>
</article>
</body></html>"""

NOISY = """<!doctype html>
<html><head><title>A quieter web</title></head><body>
<header><nav><a href="/">Home</a><a href="/login">Log in</a></nav></header>
<div id="cookie-banner" class="cookie">This site uses cookies <button>Accept all</button></div>
<aside class="sidebar"><h2>Related</h2><ul><li>Ad one</li><li>Newsletter signup</li></ul></aside>
<main>
  <h1>A quieter web</h1>
  <p>The main column explains how to read a page without the chrome.</p>
  <ol><li>Open the article</li><li>Ignore the rail</li></ol>
  <p>See <a href="/docs/more">more docs</a>.</p>
  <img src="/images/diagram.png" alt="diagram">
</main>
<footer><p>Privacy Policy Terms Example</p></footer>
</body></html>"""


class ArticleTests(unittest.TestCase):
    def test_simple_article_keeps_structure_and_image(self) -> None:
        text = article_from_html(SIMPLE, "https://example.com/potatoes")
        self.assertIn("# How potatoes grow", text)
        self.assertIn("Potatoes grow underground in cool soil.", text)
        self.assertIn("## Steps", text)
        self.assertIn("Plant the seed", text)
        self.assertIn("[full guide](https://example.com/guide)", text)
        self.assertIn("![a potato](https://cdn.example/potato.png)", text)

    def test_noisy_page_drops_chrome_and_keeps_the_image(self) -> None:
        text = article_from_html(NOISY, "https://news.example/posts/quiet")
        self.assertIn("# A quieter web", text)
        self.assertIn("without the chrome", text)
        self.assertIn("[more docs](https://news.example/docs/more)", text)
        self.assertIn("![diagram](https://news.example/images/diagram.png)", text)
        for chrome in ("Accept all", "Privacy Policy", "Newsletter signup", "Log in", "Ad one"):
            self.assertNotIn(chrome, text)

    def test_snapshot_fallback_drops_chrome_lines(self) -> None:
        raw = "\n".join([
            "navigation",
            "Accept all cookies",
            "heading How potatoes grow",
            "Potatoes grow underground in cool soil.",
            "Privacy Policy",
            "",
            "",
            "Water weekly",
        ])
        text = clean_accessibility_text(raw)
        self.assertIn("How potatoes grow", text)
        self.assertIn("Water weekly", text)
        self.assertNotIn("Accept all", text)
        self.assertNotIn("Privacy Policy", text)
        self.assertNotIn("navigation", text.lower())

    def test_relay_extracts_html_and_hides_it(self) -> None:
        result = cap_text({
            "label": "p1",
            "title": "How potatoes grow",
            "url": "https://example.com/potatoes",
            "html": SIMPLE,
            "truncated": False,
        })
        self.assertNotIn("html", result)
        self.assertIn("![a potato](https://cdn.example/potato.png)", result["text"])
        self.assertIs(result["truncated"], False)

    def test_relay_cleans_a_snapshot_fallback(self) -> None:
        result = cap_text({
            "label": "p1",
            "title": "page",
            "url": "https://example.com/",
            "text": "Accept all\nPotatoes grow underground in cool soil.",
            "truncated": False,
            "fallback": "snapshot",
        })
        self.assertNotIn("fallback", result)
        self.assertNotIn("Accept all", result["text"])
        self.assertIn("Potatoes grow underground", result["text"])

    def test_yitang_flatten_is_unchanged(self) -> None:
        doc = {
            "blockId": "p",
            "type": 1,
            "blockAttr": {"page": {"elements": [{"text_run": {"content": "课"}}]}},
            "childrens": [
                {
                    "blockId": "t",
                    "type": 2,
                    "blockAttr": {"text": {"elements": [{"text_run": {"content": "正文"}}]}},
                    "childrens": [],
                },
            ],
        }
        self.assertEqual(flatten_data_json(doc), "# 课\n\n正文")
        capped = cap_text({
            "label": "p1",
            "title": "课",
            "url": "https://yitang.top/fs-doc/token/demo",
            "text": "# 课\n\n正文",
            "truncated": False,
        })
        self.assertEqual(capped["text"], "# 课\n\n正文")
        self.assertIs(capped["truncated"], False)


if __name__ == "__main__":
    unittest.main()
