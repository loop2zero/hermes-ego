"""Flatten a yitang.top /fs-doc manuscript into markdown.

The lesson page is a Vue 2 app. The readable document is the `fsdoc` component's
`dataJson` tree (`childrens` in source order), not the accessibility tree and not
`document.body.innerText`: the on-screen list virtualizes and only mounts a window
of blocks. Checked against the prod bundles `main-c8cb65e20e.js` and
`login-0cdb44c937.js`:

- block type numbers match the app's `St` map (`1` page, `2` text, `3` heading1, …)
- prose is `blockAttr.<type>.elements[].text_run.content` (and `mention_doc.title`)
- type 27 is the only image block in the prod `St` map (`img-block` in
  `main-c8cb65e20e.js`). There is no separate figure type. The renderer uses
  `blockAttr.image.url` when `proxy` is set, otherwise `blockAttr.cdnUrl`,
  otherwise it synthesizes `/api/feishu/media/<image.token>`. That media URL
  is not in the tree, so a placeholder keeps the token (or a real http(s) URL)
  and does not invent the path. Slide overlay copy is `blockAttr.textArea[].text`
- `sum` is not rendered; emitting it duplicates the children

Own text is emitted, then `childrens`. Parent text is not copied from children.
"""

from __future__ import annotations

import json
import urllib.parse

SNAPSHOT_MAX_TEXT = 20_000
AUTHORED_MAX_TEXT = 1_000_000
PAGE_TEXT_MODES = frozenset({"auto", "snapshot", "yitang", "generic"})

# Feishu-style block types used by the yitang fs-doc renderer.
TYPE_NAMES = {
    1: "page",
    2: "text",
    3: "heading1",
    4: "heading2",
    5: "heading3",
    6: "heading4",
    7: "heading5",
    8: "heading6",
    9: "heading7",
    10: "heading8",
    11: "heading9",
    12: "bullet",
    13: "ordered",
    14: "code",
    15: "quote",
    19: "callout",
    22: "divider",
    23: "file",
    24: "grid",
    25: "grid_column",
    27: "image",
    31: "table",
    32: "table_cell",
    33: "view",
    34: "quote_container",
}

# Subset of the renderer's language ids. Unknown ids stay an unlabeled fence.
CODE_LANG = {
    7: "bash",
    22: "go",
    28: "json",
    29: "java",
    30: "javascript",
    36: "lua",
    39: "markdown",
    43: "php",
    49: "python",
    52: "ruby",
    53: "rust",
    56: "sql",
    60: "shell",
    61: "swift",
    63: "typescript",
    67: "yaml",
}


def is_yitang_doc_url(url: object) -> bool:
    """True for a yitang.top lesson document (/fs-doc/<acl>/<id>)."""
    if not isinstance(url, str) or not url:
        return False
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host != "yitang.top" and not host.endswith(".yitang.top"):
        return False
    return "/fs-doc/" in (parts.path or "").lower()


def flatten_data_json(data: object) -> str:
    """Block order → markdown. `data` is a fsdoc `dataJson` object (or a list of blocks)."""
    parts: list[str] = []
    seen: set[str] = set()
    if isinstance(data, dict):
        _walk(data, parts, seen, 0, 0)
    elif isinstance(data, list):
        for item in data:
            _walk(item, parts, seen, 0, 0)
    else:
        raise RuntimeError("yitang dataJson must be an object")
    kept: list[str] = []
    for part in parts:
        if not isinstance(part, str) or not part.strip():
            continue
        # List items carry a leading indent. strip() would flatten nesting.
        kept.append(part.rstrip() if part[:1] in (" ", "\t") else part.strip())
    return "\n\n".join(kept).strip()


def page_text_from_ego(result: dict) -> dict:
    """Turn an ego get_page_text payload into {label, title, url, text, truncated}.

    A `dataJson` payload is the authored tree and uses the manuscript cap.
    Anything else is a snapshot and keeps the 20k cap.
    """
    if not isinstance(result, dict):
        return result
    if "dataJson" in result:
        text = flatten_data_json(_coerce_data_json(result.get("dataJson")))
        truncated = len(text) > AUTHORED_MAX_TEXT
        if truncated:
            text = text[:AUTHORED_MAX_TEXT]
        return {
            "label": result.get("label"),
            "title": result.get("title"),
            "url": result.get("url"),
            "text": text,
            "truncated": truncated,
        }
    text = result.get("text")
    if isinstance(text, str) and len(text) > SNAPSHOT_MAX_TEXT:
        return {**result, "text": text[:SNAPSHOT_MAX_TEXT], "truncated": True}
    return result


def _coerce_data_json(data: object) -> dict:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise RuntimeError("yitang dataJson was not valid JSON") from exc
    if not isinstance(data, dict) or not (data.get("blockId") or data.get("childrens")):
        raise RuntimeError("yitang page has no authored dataJson")
    return data


def _walk(block: object, parts: list[str], seen: set[str], list_depth: int, depth: int) -> None:
    if depth > 80 or not isinstance(block, dict):
        return
    block_id = block.get("blockId")
    if isinstance(block_id, str) and block_id:
        if block_id in seen:
            return
        seen.add(block_id)
    type_name = _type_name(block.get("type"))
    if type_name == "table":
        rendered = _table(block, seen, depth)
        if rendered:
            parts.append(rendered)
        return
    rendered = _format_block(block, type_name, list_depth)
    if rendered:
        parts.append(rendered)
    children = block.get("childrens")
    if not isinstance(children, list):
        return
    child_depth = list_depth + 1 if type_name in ("bullet", "ordered") else list_depth
    for child in children:
        _walk(child, parts, seen, child_depth, depth + 1)


def _type_name(type_id: object) -> str:
    if isinstance(type_id, str) and type_id.lstrip("-").isdigit():
        type_id = int(type_id)
    if isinstance(type_id, int):
        return TYPE_NAMES.get(type_id, "")
    return ""


def _format_block(block: dict, type_name: str, list_depth: int) -> str:
    if type_name == "divider":
        return "---"
    if type_name == "file":
        return _file_name(block)
    if type_name == "image":
        return _image_markdown(block)
    if type_name == "code":
        body = _elements_of(block, type_name, strip=False)
        if not body.strip():
            return ""
        return _fence(block, body)
    text = _elements_of(block, type_name, strip=True)
    if not text:
        return ""
    if type_name == "page":
        return "# " + _one_line(text)
    if type_name.startswith("heading") and type_name[7:].isdigit():
        level = max(1, min(int(type_name[7:]), 6))
        return f"{'#' * level} {_one_line(text)}"
    if type_name == "bullet":
        return f"{'  ' * min(list_depth, 8)}- {text}"
    if type_name == "ordered":
        return f"{'  ' * min(list_depth, 8)}1. {text}"
    if type_name == "quote":
        return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())
    return text


def _elements_of(block: dict, type_name: str, strip: bool) -> str:
    attr = block.get("blockAttr")
    if not isinstance(attr, dict):
        return ""
    if type_name:
        node = attr.get(type_name)
        raw = _elements_text(node.get("elements")) if isinstance(node, dict) else ""
    else:
        raw = ""
        for val in attr.values():
            if isinstance(val, dict) and isinstance(val.get("elements"), list):
                raw = _elements_text(val.get("elements"))
                break
    return raw.strip() if strip else raw


def _elements_text(elements: object) -> str:
    """Visible inline text. Skips inline_block (that child is walked on its own)."""
    if not isinstance(elements, list):
        return ""
    parts: list[str] = []
    for el in elements:
        if isinstance(el, str):
            parts.append(el)
            continue
        if not isinstance(el, dict):
            continue
        run = el.get("text_run")
        if isinstance(run, dict) and isinstance(run.get("content"), str):
            parts.append(run["content"])
            continue
        if isinstance(run, str):
            parts.append(run)
            continue
        mention = el.get("mention_doc")
        if isinstance(mention, dict) and isinstance(mention.get("title"), str):
            parts.append(mention["title"])
            continue
        if "inline_block" in el:
            continue
        for key, val in el.items():
            if key.startswith("text_") and isinstance(val, str):
                parts.append(val)
                break
    return "".join(parts)


def _file_name(block: dict) -> str:
    attr = block.get("blockAttr")
    node = attr.get("file") if isinstance(attr, dict) else None
    name = node.get("name") if isinstance(node, dict) else None
    return name.strip() if isinstance(name, str) else ""


def _http_url(value: object) -> str:
    """An absolute http(s) URL stored on the block. Relative paths are not usable."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > 2000:
        return ""
    if any(ord(ch) < 33 or ch.isspace() for ch in text):
        return ""
    if text.startswith("https://") and len(text) > len("https://"):
        return text
    if text.startswith("http://") and len(text) > len("http://"):
        return text
    return ""


def _proxy_on(value: object) -> bool:
    """Match the bundle's `blockAttr.proxy || 0`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip() not in ("", "0")
    return False


def _image_token(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > 512 or any(ord(ch) < 33 or ch.isspace() for ch in text):
        return ""
    return text


def _image_target(block: dict) -> str:
    """URL or token actually present on the image block. Never a synthesized media URL."""
    attr = block.get("blockAttr")
    if not isinstance(attr, dict):
        return ""
    image = attr.get("image") if isinstance(attr.get("image"), dict) else {}
    if _proxy_on(attr.get("proxy")):
        url = _http_url(image.get("url"))
        if url:
            return url
    cdn = _http_url(attr.get("cdnUrl"))
    if cdn:
        return cdn
    token = _image_token(image.get("token"))
    if token:
        return _http_url(token) or token
    return _http_url(image.get("url"))


def _image_markdown(block: dict) -> str:
    target = _image_target(block)
    marker = f"![image]({target})" if target else "[图片]"
    notes = _image_notes(block)
    if notes:
        return f"{marker}\n\n{notes}"
    return marker


def _image_notes(block: dict) -> str:
    attr = block.get("blockAttr")
    areas = attr.get("textArea") if isinstance(attr, dict) else None
    if not isinstance(areas, list):
        return ""
    lines: list[str] = []
    for area in areas:
        if isinstance(area, dict) and isinstance(area.get("text"), str):
            text = area["text"].strip()
            if text:
                lines.append(text)
        elif isinstance(area, str) and area.strip():
            lines.append(area.strip())
    return "\n".join(lines)


def _fence(block: dict, body: str) -> str:
    attr = block.get("blockAttr")
    code = attr.get("code") if isinstance(attr, dict) else None
    style = code.get("style") if isinstance(code, dict) else None
    lang_id = style.get("language") if isinstance(style, dict) else None
    if isinstance(lang_id, str) and lang_id.isdigit():
        lang_id = int(lang_id)
    lang = CODE_LANG.get(lang_id, "") if isinstance(lang_id, int) else ""
    if not body.endswith("\n"):
        body += "\n"
    return f"```{lang}\n{body}```"


def _table(block: dict, seen: set[str], depth: int) -> str:
    attr = block.get("blockAttr") if isinstance(block.get("blockAttr"), dict) else {}
    table = attr.get("table") if isinstance(attr.get("table"), dict) else {}
    prop = table.get("property") if isinstance(table.get("property"), dict) else {}
    cols = _pos_int(prop.get("column_size"))
    rows = _pos_int(prop.get("row_size"))
    cells = table.get("cells") if isinstance(table.get("cells"), list) else []
    kids = block.get("childrens") if isinstance(block.get("childrens"), list) else []
    by_id: dict[str, dict] = {}
    for kid in kids:
        if isinstance(kid, dict) and isinstance(kid.get("blockId"), str):
            by_id[kid["blockId"]] = kid
    if cols <= 0 or rows <= 0 or len(cells) < rows * cols:
        parts: list[str] = []
        for kid in kids:
            _walk(kid, parts, seen, 0, depth + 1)
        return "\n\n".join(part for part in parts if part.strip())
    lines: list[str] = []
    for row in range(rows):
        rendered: list[str] = []
        for col in range(cols):
            cid = cells[row * cols + col]
            cell = by_id.get(cid) if isinstance(cid, str) else None
            rendered.append(_cell_text(cell, seen, depth) if cell else "")
        lines.append("| " + " | ".join(rendered) + " |")
        if row == 0:
            lines.append("| " + " | ".join("---" for _ in rendered) + " |")
    return "\n".join(lines)


def _cell_text(cell: dict, seen: set[str], depth: int) -> str:
    parts: list[str] = []
    _walk(cell, parts, seen, 0, depth + 1)
    text = " ".join(part.replace("\n", " ").strip() for part in parts if part.strip())
    return " ".join(text.split()).replace("|", "\\|")


def _pos_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _one_line(text: str) -> str:
    return " ".join(text.split())
