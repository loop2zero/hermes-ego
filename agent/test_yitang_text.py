"""Offline flatten of a synthetic yitang dataJson. No browser, no relay."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from yitang_text import (
    AUTHORED_MAX_TEXT,
    SNAPSHOT_MAX_TEXT,
    flatten_data_json,
    is_yitang_doc_url,
    page_text_from_ego,
)


def _cell(block_id: str, text: str) -> dict:
    return {
        "blockId": block_id,
        "type": 32,
        "childrens": [
            {
                "blockId": block_id + "t",
                "type": 2,
                "blockAttr": {"text": {"elements": [{"text_run": {"content": text}}]}},
                "childrens": [],
            }
        ],
    }


DOC = {
    "blockId": "page",
    "type": 1,
    "sum": "不要把摘要再写一遍",
    "blockAttr": {"page": {"elements": [{"text_run": {"content": "重新理解一堂五步法"}}]}},
    "childrens": [
        {
            "blockId": "h",
            "type": 3,
            "blockAttr": {"heading1": {"elements": [{"text_run": {"content": "需求"}}]}},
            "childrens": [],
        },
        {
            "blockId": "p",
            "type": 2,
            "sum": "子段全文",
            "blockAttr": {
                "text": {
                    "elements": [
                        {"text_run": {"content": "先写"}, "text_1": "重复"},
                        {"mention_doc": {"title": "用户"}},
                        {"text_run": {"content": "。"}},
                        {"inline_block": {"block_id": "f"}},
                    ]
                }
            },
            "childrens": [
                {
                    "blockId": "f",
                    "type": 23,
                    "blockAttr": {"file": {"name": "画布.pdf", "token": "FILETOKEN"}},
                    "childrens": [],
                }
            ],
        },
        {
            "blockId": "b",
            "type": 12,
            "blockAttr": {"bullet": {"elements": [{"text_run": {"content": "要点"}}]}},
            "childrens": [
                {
                    "blockId": "b2",
                    "type": 12,
                    "blockAttr": {"bullet": {"elements": [{"text_run": {"content": "子点"}}]}},
                    "childrens": [],
                }
            ],
        },
        {
            "blockId": "img",
            "type": 27,
            "blockAttr": {
                "image": {"token": "SECRETTOKEN"},
                "textArea": [{"text": "图上的字：关键假设"}, {"text": "  "}],
            },
            "childrens": [],
        },
        {
            "blockId": "code",
            "type": 14,
            "blockAttr": {
                "code": {
                    "style": {"language": 49},
                    "elements": [{"text_run": {"content": "print('hi')"}}],
                }
            },
            "childrens": [],
        },
        {"blockId": "hr", "type": 22, "blockAttr": {}, "childrens": []},
        {
            "blockId": "tbl",
            "type": 31,
            "blockAttr": {
                "table": {
                    "property": {"row_size": 2, "column_size": 2},
                    "cells": ["c1", "c2", "c3", "c4"],
                }
            },
            "childrens": [
                _cell("c1", "A"),
                _cell("c2", "B"),
                _cell("c3", "C"),
                _cell("c4", "D"),
            ],
        },
        {
            "blockId": "call",
            "type": 19,
            "blockAttr": {"callout": {"emoji_id": "1"}},
            "childrens": [
                {
                    "blockId": "ct",
                    "type": 2,
                    "blockAttr": {"text": {"elements": [{"text_run": {"content": "高亮句"}}]}},
                    "childrens": [],
                }
            ],
        },
    ],
}


EXPECTED = """# 重新理解一堂五步法

# 需求

先写用户。

画布.pdf

- 要点

  - 子点

![image](SECRETTOKEN)

图上的字：关键假设

```python
print('hi')
```

---

| A | B |
| --- | --- |
| C | D |

高亮句"""


class FlattenTests(unittest.TestCase):
    def test_manuscript_markdown(self) -> None:
        text = flatten_data_json(DOC)
        self.assertEqual(text, EXPECTED)
        self.assertEqual(text.count("画布.pdf"), 1)
        self.assertEqual(text.count("先写用户。"), 1)
        self.assertNotIn("不要把摘要再写一遍", text)
        self.assertNotIn("重复", text)
        self.assertNotIn("FILETOKEN", text)
        self.assertNotIn("/api/feishu/media/", text)
        self.assertNotIn("[图片]", text)
        self.assertIn("![image](SECRETTOKEN)", text)

    def test_image_placeholders_follow_source_order(self) -> None:
        """Type 27 only. URL beats token; a bare image is still marked; no invented media URL."""
        doc = {
            "blockId": "page",
            "type": "1",
            "blockAttr": {"page": {"elements": [{"text_run": {"content": "课"}}]}},
            "childrens": [
                {
                    "blockId": "before",
                    "type": 2,
                    "blockAttr": {"text": {"elements": [{"text_run": {"content": "前文"}}]}},
                    "childrens": [],
                },
                {
                    "blockId": "cdn",
                    "type": 27,
                    "blockAttr": {
                        "cdnUrl": "https://cdn.example/slide.png",
                        "image": {"token": "HIDDEN_TOKEN", "width": 800, "height": 450},
                        "textArea": [{"text": "封面标题"}, {"text": "  "}],
                    },
                    "childrens": [],
                },
                {
                    "blockId": "proxy",
                    "type": "27",
                    "blockAttr": {
                        "proxy": 1,
                        "cdnUrl": "https://cdn.example/ignored.png",
                        "image": {"url": "https://img.example/proxy.png", "token": "ALSO_HIDDEN"},
                    },
                    "childrens": [],
                },
                {
                    "blockId": "tok",
                    "type": 27,
                    "blockAttr": {
                        "cdnUrl": "/api/feishu/media/boxcnToken123",
                        "image": {"token": "boxcnToken123", "width": 10},
                    },
                    "childrens": [],
                },
                {
                    "blockId": "bullet",
                    "type": 12,
                    "blockAttr": {"bullet": {"elements": [{"text_run": {"content": "条目"}}]}},
                    "childrens": [
                        {
                            "blockId": "nested",
                            "type": 27,
                            "blockAttr": {"image": {"height": 80, "width": 100}, "textArea": [{"text": "只有叠字"}]},
                            "childrens": [],
                        }
                    ],
                },
                {
                    "blockId": "after",
                    "type": 2,
                    "blockAttr": {"text": {"elements": [{"text_run": {"content": "后文"}}]}},
                    "childrens": [],
                },
                {
                    "blockId": "diagram",
                    "type": 21,
                    "blockAttr": {"diagram": {"token": "NOT_AN_IMAGE"}},
                    "childrens": [],
                },
            ],
        }
        text = flatten_data_json(doc)
        self.assertEqual(
            text,
            "\n\n".join([
                "# 课",
                "前文",
                "![image](https://cdn.example/slide.png)\n\n封面标题",
                "![image](https://img.example/proxy.png)",
                "![image](boxcnToken123)",
                "- 条目",
                "[图片]\n\n只有叠字",
                "后文",
            ]),
        )
        self.assertNotIn("HIDDEN_TOKEN", text)
        self.assertNotIn("ALSO_HIDDEN", text)
        self.assertNotIn("NOT_AN_IMAGE", text)
        self.assertNotIn("/api/feishu/media/", text)
        self.assertNotIn("ignored.png", text)

    def test_historical_text_star_fallback(self) -> None:
        doc = {
            "blockId": "p",
            "type": 2,
            "blockAttr": {"text": {"elements": [{"text_1": "历史字段"}]}},
            "childrens": [],
        }
        self.assertEqual(flatten_data_json(doc), "历史字段")

    def test_quote_ordered_and_grid(self) -> None:
        doc = {
            "blockId": "page",
            "type": 1,
            "blockAttr": {"page": {"elements": []}},
            "childrens": [
                {
                    "blockId": "q",
                    "type": 15,
                    "blockAttr": {"quote": {"elements": [{"text_run": {"content": "引用"}}]}},
                    "childrens": [],
                },
                {
                    "blockId": "o",
                    "type": 13,
                    "blockAttr": {"ordered": {"elements": [{"text_run": {"content": "第一步"}}]}},
                    "childrens": [],
                },
                {
                    "blockId": "g",
                    "type": 24,
                    "blockAttr": {"grid": {}},
                    "childrens": [
                        {
                            "blockId": "gt",
                            "type": 2,
                            "blockAttr": {"text": {"elements": [{"text_run": {"content": "栏内"}}]}},
                            "childrens": [],
                        }
                    ],
                },
            ],
        }
        self.assertEqual(flatten_data_json(doc), "> 引用\n\n1. 第一步\n\n栏内")

    def test_authored_cap_is_above_snapshot_cap(self) -> None:
        long = "课" * (SNAPSHOT_MAX_TEXT + 1)
        snap = page_text_from_ego({
            "label": "p1",
            "title": "example",
            "url": "https://example.com/",
            "text": long,
            "truncated": False,
        })
        self.assertTrue(snap["truncated"])
        self.assertEqual(len(snap["text"]), SNAPSHOT_MAX_TEXT)

        authored = page_text_from_ego({
            "label": "p1",
            "title": "lesson",
            "url": "https://yitang.top/fs-doc/token/demo",
            "dataJson": {
                "blockId": "p",
                "type": 2,
                "blockAttr": {"text": {"elements": [{"text_run": {"content": long}}]}},
                "childrens": [],
            },
        })
        self.assertFalse(authored["truncated"])
        self.assertEqual(authored["text"], long)
        self.assertNotIn("dataJson", authored)

        huge = "课" * (AUTHORED_MAX_TEXT + 5)
        capped = page_text_from_ego({
            "label": "p1",
            "title": "lesson",
            "url": "https://yitang.top/fs-doc/token/demo",
            "dataJson": {
                "blockId": "p",
                "type": 2,
                "blockAttr": {"text": {"elements": [{"text_run": {"content": huge}}]}},
                "childrens": [],
            },
        })
        self.assertTrue(capped["truncated"])
        self.assertEqual(len(capped["text"]), AUTHORED_MAX_TEXT)

    def test_url_detection(self) -> None:
        self.assertTrue(is_yitang_doc_url("https://yitang.top/fs-doc/token/abc"))
        self.assertTrue(is_yitang_doc_url("https://yitang.top/fs-doc/token/abc?fromAcl=token"))
        self.assertTrue(is_yitang_doc_url("https://www.yitang.top/fs-doc/a/b"))
        self.assertFalse(is_yitang_doc_url("https://yitang.top/elective-lesson"))
        self.assertFalse(is_yitang_doc_url("https://example.com/fs-doc/a"))
        self.assertFalse(is_yitang_doc_url("https://notyitang.top/fs-doc/a"))
        self.assertFalse(is_yitang_doc_url("https://yitang.top.evil.com/fs-doc/a"))
        self.assertFalse(is_yitang_doc_url("https://evil.example/fs-doc/?next=https://yitang.top/fs-doc/a"))

    def test_get_page_text_script_parses(self) -> None:
        from hermes_ego_daemon import TEXT_JS, render

        script = render(TEXT_JS, {"mode": "auto", "label": "p1"})
        self.assertIn("page.snapshot", script)
        self.assertIn("page.evaluate", script)
        self.assertIn("outerHTML", script)
        self.assertIn("generic", script)
        self.assertIn("fsdoc", script)
        self.assertIn("dataJson", script)
        self.assertNotIn("finish(", script)
        self.assertNotIn("__HERMES_EGO_PARAMS__", script)
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "get_page_text.mjs"
            path.write_text(script, encoding="utf-8")
            proc = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
