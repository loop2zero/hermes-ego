#!/usr/bin/env python3
"""Link smoke test. CI has no ego lite, so --fake-ego stands in for ego-browser.

The test starts the real relay and the real Mac daemon. The daemon's
ego_browser path points at this file's --fake-ego mode, which speaks the
stream contract (version on stdout; nodejs JSON on stderr, as real ego does)
and keeps a tiny tab list on disk.

Checks:
  * all five MCP tools, including a verbatim save under EGO_SAVE_ROOT
  * yitang /fs-doc authored markdown (not the snapshot) and the 20k snapshot cap
  * 401 on a bad bearer, 403 on Origin, 405 on GET /mcp
  * WebSocket close 1008 on a bad hello token
  * daemon reconnects after the relay process is killed and started again
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FANCY_URL = 'https://example.com/\u00e4?q="hi"&x=</script>'
YITANG_URL = "https://yitang.top/fs-doc/token/demo-lesson"
AUTHORED_BODY = "课" * 25000


def authored_data_json() -> dict:
    """Small fsdoc tree plus a body longer than the snapshot cap."""
    return {
        "blockId": "page1",
        "type": 1,
        "sum": "不要写出这份摘要",
        "blockAttr": {"page": {"elements": [{"text_run": {"content": "重新理解一堂五步法"}}]}},
        "childrens": [
            {
                "blockId": "h",
                "type": 4,
                "blockAttr": {"heading2": {"elements": [{"text_run": {"content": "需求"}}]}},
                "childrens": [],
            },
            {
                "blockId": "body",
                "type": 2,
                "blockAttr": {"text": {"elements": [{"text_run": {"content": AUTHORED_BODY}}]}},
                "childrens": [],
            },
        ],
    }


def yitang_page(url: str) -> bool:
    host = url.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0].lower()
    return (host == "yitang.top" or host.endswith(".yitang.top")) and "/fs-doc/" in url


def emit_nodejs_json(payload: dict) -> None:
    """ego-browser nodejs writes console.log to stderr. stdout stays empty.

    A notice follows the object, so the daemon must take the last JSON object
    rather than the last line. --version still prints on stdout.
    """
    print("ego-browser: result", file=sys.stderr)
    print(json.dumps(payload), file=sys.stderr)
    print("[ego-browser:notice] smoke", file=sys.stderr)


def fake_ego_main() -> None:
    state_path = Path(os.environ["HERMES_EGO_FAKE_STATE"])
    if "--version" in sys.argv:
        print("ego-browser 0.6.2.8")
        return
    if "nodejs" not in sys.argv:
        print("fake ego-browser: expected --version or nodejs", file=sys.stderr)
        sys.exit(2)
    script = sys.stdin.read()
    if 'taskSpace("hermes-ego")' not in script:
        print("script does not use task space hermes-ego", file=sys.stderr)
        sys.exit(1)
    if "finish(" in script:
        print("v0 script must not call finish()", file=sys.stderr)
        sys.exit(1)
    op = ""
    for line in script.splitlines():
        if line.startswith("// hermes-ego-op: "):
            op = line.split(": ", 1)[1].strip()
    marker = "const params = "
    at = script.find(marker)
    if at < 0:
        print("script has no params literal", file=sys.stderr)
        sys.exit(1)
    params, _end = json.JSONDecoder().raw_decode(script[at + len(marker):])
    state = {"tabs": [], "scripts": []}
    if state_path.exists():
        state = json.loads(state_path.read_text())
    state.setdefault("tabs", [])
    state.setdefault("scripts", [])
    state["scripts"].append({"op": op, "params": params, "script": script})
    state["scripts"] = state["scripts"][-20:]

    if op == "probe":
        state_path.write_text(json.dumps(state))
        emit_nodejs_json({"ok": True})
        return
    if op == "list_tabs":
        state_path.write_text(json.dumps(state))
        listed = [
            {"label": tab["label"], "title": tab["title"], "url": tab["url"], "active": bool(tab["active"])}
            for tab in state["tabs"]
            if tab.get("label") and tab.get("openedBy") == "agent"
        ]
        emit_nodejs_json({"tabs": listed, "spaceId": 7})
        return
    if op == "navigate":
        url = params.get("url")
        if not isinstance(url, str):
            print("navigate missing url", file=sys.stderr)
            sys.exit(1)
        host = url.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]
        tab = {"label": "p1", "title": host, "url": url, "active": True, "openedBy": "agent"}
        state["tabs"] = [tab]
        state_path.write_text(json.dumps(state))
        emit_nodejs_json({"label": "p1", "title": host, "url": url, "spaceId": 7})
        return
    if op == "get_page_text":
        label = params.get("label")
        tabs = state["tabs"]
        if label:
            tab = next((item for item in tabs if item["label"] == label), None)
            if tab is None:
                print("label is not an agent-created page in task space hermes-ego", file=sys.stderr)
                state_path.write_text(json.dumps(state))
                sys.exit(1)
        else:
            tab = next((item for item in tabs if item.get("active")), None) or (tabs[0] if tabs else None)
            if tab is None:
                print("no agent-created page in task space hermes-ego", file=sys.stderr)
                state_path.write_text(json.dumps(state))
                sys.exit(1)
        if "page.snapshot" not in script or "page.evaluate" not in script or "fsdoc" not in script:
            print("get_page_text script is missing the snapshot or yitang path", file=sys.stderr)
            state_path.write_text(json.dumps(state))
            sys.exit(1)
        mode = params.get("mode") or "auto"
        if mode not in ("auto", "snapshot", "yitang", "generic"):
            print("mode must be auto, snapshot, yitang, or generic", file=sys.stderr)
            state_path.write_text(json.dumps(state))
            sys.exit(1)
        use_authored = mode == "yitang" or (mode == "auto" and yitang_page(tab["url"]))
        state_path.write_text(json.dumps(state))
        if use_authored:
            emit_nodejs_json({
                "label": tab["label"],
                "title": tab["title"],
                "url": tab["url"],
                "dataJson": authored_data_json(),
            })
            return
        if mode != "snapshot":
            emit_nodejs_json({
                "label": tab["label"],
                "title": tab["title"],
                "url": tab["url"],
                "html": (
                    "<!doctype html><html><head><title>Example Domain</title></head><body>"
                    "<nav>Home</nav><article><h1>Example Domain</h1>"
                    "<p>This domain is for use in illustrative examples in documents.</p>"
                    "<img src=\"https://cdn.example/example.png\" alt=\"example\">"
                    "</article></body></html>"
                ),
                "truncated": False,
            })
            return
        text = f"SNAPSHOT {tab['url']}\n" + ("x" * 25000)
        emit_nodejs_json({
            "label": tab["label"], "title": tab["title"], "url": tab["url"], "text": text, "truncated": False,
        })
        return
    print(f"unknown op {op!r}", file=sys.stderr)
    sys.exit(1)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_call(port: int, method: str, headers: dict[str, str], body: bytes | None = None) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def wait_relay(port: int, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if http_call(port, "POST", {"Authorization": "Bearer nope", "Content-Type": "application/json"}, b"{}") == 401:
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise RuntimeError("relay did not start answering 401")


class Relay:
    def __init__(
        self, port: int, token: str, log_path: Path, extra_env: dict[str, str] | None = None,
    ) -> None:
        self.port = port
        self.token = token
        self.log_path = log_path
        self.extra_env = extra_env or {}
        self.proc: subprocess.Popen | None = None
        self.logf = open(log_path, "a", buffering=1)

    def start(self) -> None:
        env = os.environ.copy()
        env["EGO_RELAY_TOKEN"] = self.token
        env["EGO_RELAY_HOST"] = "127.0.0.1"
        env["EGO_RELAY_PORT"] = str(self.port)
        env["PYTHONUNBUFFERED"] = "1"
        env.update(self.extra_env)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "relay"],
            cwd=ROOT,
            env=env,
            stdout=self.logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            wait_relay(self.port)
        except Exception:
            self.dump()
            raise

    def stop(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        self.logf.flush()

    def dump(self) -> None:
        self.logf.flush()
        text = self.log_path.read_text(errors="replace") if self.log_path.exists() else ""
        print("--- relay log ---")
        print(text[-4000:])


def tool_text(result) -> str:
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        bits = []
        for block in getattr(result, "content", []) or []:
            bits.append(getattr(block, "text", str(block)))
        raise AssertionError("tool error: " + " ".join(bits))
    parts = []
    for block in result.content:
        parts.append(getattr(block, "text", str(block)))
    return "".join(parts)


def tool_error(result) -> str:
    bits = []
    for block in getattr(result, "content", []) or []:
        bits.append(getattr(block, "text", str(block)))
    return " ".join(bits)


@asynccontextmanager
async def open_session(port: int, token: str):
    """MCP session whose enter and exit run in the caller's task.

    The streamable HTTP client and ClientSession each enter an anyio cancel
    scope. Exiting that scope from another task raises, and on Python 3.14 the
    resulting CancelledError stays stuck on the caller. Nested `async with`
    keeps the enter and the exit on one task.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    try:
        import httpx2 as httpx
    except ImportError:
        import httpx

    url = f"http://127.0.0.1:{port}/mcp"
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(30.0, read=60.0),
    ) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


async def call_json(session, name: str, args: dict | None = None) -> dict:
    result = await session.call_tool(name, args or {})
    return json.loads(tool_text(result))


async def bad_hello(port: int) -> None:
    import websockets
    from websockets.exceptions import ConnectionClosed

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/agent", open_timeout=5) as ws:
            await ws.send(json.dumps({"type": "hello", "token": "not-the-token", "device": "intruder"}))
            try:
                msg = await asyncio.wait_for(ws.recv(), 5)
            except ConnectionClosed as exc:
                if exc.code != 1008:
                    raise AssertionError(f"bad hello closed with {exc.code}, expected 1008") from exc
                return
            raise AssertionError(f"bad hello got a message: {msg!r}")
    except ConnectionClosed as exc:
        if exc.code != 1008:
            raise AssertionError(f"bad hello closed with {exc.code}, expected 1008") from exc


async def wait_online(port: int, token: str, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    last: dict | str = "no response"
    while time.monotonic() < deadline:
        try:
            async with open_session(port, token) as session:
                last = await call_json(session, "ego_status")
                if isinstance(last, dict) and last.get("online") is True:
                    return last
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last = str(exc)
        await asyncio.sleep(0.2)
    raise AssertionError(f"daemon did not come online: {last}")


async def run() -> None:
    import logging
    import secrets

    # Importing the relay in the unit tests drops the root logger to INFO, and the
    # MCP client's httpx lines then bury the PASS lines. The smoke process used
    # WARNING before those imports.
    logging.getLogger().setLevel(logging.WARNING)

    token = secrets.token_urlsafe(24)
    port = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="hermes-ego-smoke-"))
    home = tmp / "home"
    state_path = tmp / "ego-state.json"
    wrapper = tmp / "ego-browser"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {ROOT / 'smoke_test.py'} --fake-ego \"$@\"\n")
    wrapper.chmod(0o755)
    config_dir = home / ".config" / "hermes-ego"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        "\n".join([
            f'relay_url = "ws://127.0.0.1:{port}/agent"',
            f'token = "{token}"',
            'device = "smoke-mac"',
            f'ego_browser = "{wrapper}"',
            "",
        ])
    )
    save_root = tmp / "notes"
    save_root.mkdir()
    relay = Relay(port, token, tmp / "relay.log", extra_env={
        "EGO_SAVE_ROOT": str(save_root),
        "EGO_SAVE_TZ": "Europe/Berlin",
    })
    daemon_log = open(tmp / "daemon.err", "w", buffering=1)
    daemon: subprocess.Popen | None = None
    try:
        print(f"relay 127.0.0.1:{port}")
        relay.start()

        missing = http_call(port, "POST", {"Content-Type": "application/json"}, b"{}")
        assert missing == 401, missing
        bad = http_call(port, "POST", {"Authorization": "Bearer wrong", "Content-Type": "application/json"}, b"{}")
        assert bad == 401, bad
        origin = http_call(
            port, "POST",
            {"Authorization": f"Bearer {token}", "Origin": "https://evil.test", "Content-Type": "application/json"},
            b"{}",
        )
        assert origin == 403, origin
        get_status = http_call(port, "GET", {"Authorization": f"Bearer {token}"})
        assert get_status == 405, get_status
        print("PASS http 401 / 403 / 405")

        await bad_hello(port)
        print("PASS websocket rejects bad hello")

        env = os.environ.copy()
        env["HOME"] = str(home)
        env["HERMES_EGO_FAKE_STATE"] = str(state_path)
        env["PYTHONUNBUFFERED"] = "1"
        daemon = subprocess.Popen(
            [sys.executable, str(ROOT / "agent" / "hermes_ego_daemon.py")],
            cwd=ROOT,
            env=env,
            stdout=daemon_log,
            stderr=subprocess.STDOUT,
        )
        status = await wait_online(port, token)
        assert status["device"] == "smoke-mac", status
        assert status["egoVersion"] == "0.6.2.8", status
        assert status["online"] is True, status
        assert status["egoReachable"] is True, status
        assert status["egoError"] is None, status
        print("PASS ego_status", status)

        await bad_hello(port)
        still = await wait_online(port, token, timeout=5)
        assert still["online"] is True
        print("PASS bad hello does not drop the daemon")

        async with open_session(port, token) as session:
            listed = await session.list_tools()
            names = sorted(tool.name for tool in listed.tools)
            assert names == [
                "ego_get_page_text", "ego_list_tabs", "ego_navigate", "ego_save_page_markdown", "ego_status",
            ], names
            text_tool = next(tool for tool in listed.tools if tool.name == "ego_get_page_text")
            schema = text_tool.input_schema
            assert "url" in schema.get("properties", {}), schema
            assert "label" in schema.get("properties", {}), schema
            assert "mode" in schema.get("properties", {}), schema
            assert "url" not in schema.get("required", []), schema
            assert "label" not in schema.get("required", []), schema
            assert "mode" not in schema.get("required", []), schema
            text_desc = (text_tool.description or "").lower()
            assert "serial" in text_desc, text_tool.description
            assert "not atomic" in text_desc, text_tool.description
            nav_tool = next(tool for tool in listed.tools if tool.name == "ego_navigate")
            assert "url" in nav_tool.input_schema.get("required", []), nav_tool.input_schema
            nav_desc = (nav_tool.description or "").lower()
            assert "serial" in nav_desc, nav_tool.description
            assert "ego_get_page_text with url" in nav_desc, nav_tool.description
            save_tool = next(tool for tool in listed.tools if tool.name == "ego_save_page_markdown")
            save_schema = save_tool.input_schema
            save_props = save_schema.get("properties", {})
            for key in ("subdir", "slug", "url", "label", "title", "mode"):
                assert key in save_props, save_schema
            save_required = set(save_schema.get("required") or [])
            assert "subdir" in save_required and "slug" in save_required, save_schema
            assert "url" not in save_required and "label" not in save_required, save_schema
            assert "title" not in save_required and "mode" not in save_required, save_schema
            save_desc = (save_tool.description or "").lower()
            assert "write_file" in save_desc, save_tool.description
            assert "not returned" in save_desc, save_tool.description

            empty = await call_json(session, "ego_list_tabs")
            assert empty["tabs"] == [], empty
            assert empty["spaceId"] == 7, empty

            opened = await call_json(session, "ego_navigate", {"url": "https://example.com/"})
            assert opened["label"] == "p1", opened
            assert opened["url"] == "https://example.com/", opened
            assert opened["spaceId"] == 7, opened

            tabs = await call_json(session, "ego_list_tabs")
            assert tabs["tabs"] == [{
                "label": "p1", "title": "example.com", "url": "https://example.com/", "active": True,
            }], tabs

            page = await call_json(session, "ego_get_page_text")
            assert page["label"] == "p1", page
            assert page["url"] == "https://example.com/", page
            assert page["truncated"] is False, page
            assert "illustrative examples" in page["text"], page["text"]
            assert "html" not in page
            assert "SNAPSHOT" not in page["text"]

            labeled = await call_json(session, "ego_get_page_text", {"label": "p1"})
            assert "illustrative examples" in labeled["text"], labeled["text"]

            refused = await session.call_tool("ego_get_page_text", {"label": "p2"})
            assert getattr(refused, "is_error", False) or getattr(refused, "isError", False), refused
            assert "not an agent-created page" in tool_error(refused), tool_error(refused)

            bad_url = await session.call_tool("ego_navigate", {"url": "javascript:alert(1)"})
            assert getattr(bad_url, "is_error", False) or getattr(bad_url, "isError", False), bad_url
            assert "http" in tool_error(bad_url).lower(), tool_error(bad_url)

            quoted = await call_json(session, "ego_navigate", {"url": FANCY_URL})
            assert quoted["url"] == FANCY_URL, quoted
            state = json.loads(state_path.read_text())
            nav_scripts = [item for item in state["scripts"] if item["op"] == "navigate"]
            assert nav_scripts, state["scripts"]
            last = nav_scripts[-1]
            assert last["params"]["url"] == FANCY_URL
            embedded = json.dumps({"url": FANCY_URL}, ensure_ascii=True)
            assert embedded in last["script"], last["script"]
            assert 'taskSpace("hermes-ego")' in last["script"]
            assert 'openedBy === "agent"' in last["script"]
            assert "finish(" not in last["script"]
            assert "newPage" in last["script"]
            for item in state["scripts"]:
                assert "finish(" not in item["script"]
                assert 'taskSpace("hermes-ego")' in item["script"]
            print("PASS tools, truncation, quote-safe params, agent-only pages")

            opened = await call_json(session, "ego_navigate", {"url": YITANG_URL})
            assert opened["url"] == YITANG_URL, opened
            lesson = await call_json(session, "ego_get_page_text")
            assert lesson["url"] == YITANG_URL, lesson
            assert lesson["truncated"] is False, (lesson["truncated"], len(lesson["text"]))
            assert len(lesson["text"]) > 20000, len(lesson["text"])
            assert lesson["text"].startswith("# 重新理解一堂五步法"), lesson["text"][:80]
            assert "## 需求" in lesson["text"]
            assert AUTHORED_BODY in lesson["text"]
            assert "不要写出这份摘要" not in lesson["text"]
            assert "SNAPSHOT" not in lesson["text"]
            assert "dataJson" not in lesson
            forced = await call_json(session, "ego_get_page_text", {"label": "p1", "mode": "yitang"})
            assert forced["text"] == lesson["text"]
            assert forced["truncated"] is False
            shot = await call_json(session, "ego_get_page_text", {"mode": "snapshot"})
            assert shot["truncated"] is True, shot
            assert len(shot["text"]) == 20000, len(shot["text"])
            assert shot["text"].startswith(f"SNAPSHOT {YITANG_URL}"), shot["text"][:80]
            bad_mode = await session.call_tool("ego_get_page_text", {"mode": "click"})
            assert getattr(bad_mode, "is_error", False) or getattr(bad_mode, "isError", False), bad_mode
            assert "mode" in tool_error(bad_mode), tool_error(bad_mode)
            atomic_url = "https://example.com/atomic-read"
            atomic = await call_json(session, "ego_get_page_text", {"url": atomic_url})
            assert atomic["url"] == atomic_url, atomic
            assert atomic["label"] == "p1", atomic
            assert "illustrative examples" in atomic["text"], atomic["text"]
            state = json.loads(state_path.read_text())
            recent = [(item["op"], item["params"]) for item in state["scripts"]]
            nav_at = max(
                index for index, (op, params) in enumerate(recent)
                if op == "navigate" and params.get("url") == atomic_url
            )
            assert any(op == "get_page_text" for op, _params in recent[nav_at + 1:]), recent[nav_at:]
            both = await session.call_tool("ego_get_page_text", {"url": atomic_url, "label": "p1"})
            assert getattr(both, "is_error", False) or getattr(both, "isError", False), both
            assert "not both" in tool_error(both), tool_error(both)
            state = json.loads(state_path.read_text())
            yitang_calls = [
                item for item in state["scripts"]
                if item["op"] == "get_page_text" and item["params"].get("mode") == "yitang"
            ]
            assert yitang_calls, state["scripts"]
            embedded = json.dumps({"label": "p1", "mode": "yitang"}, ensure_ascii=True)
            assert embedded in yitang_calls[-1]["script"], yitang_calls[-1]["script"][:400]
            assert "page.evaluate" in yitang_calls[-1]["script"]
            assert "fsdoc" in yitang_calls[-1]["script"]
            print("PASS yitang authored markdown past 20k, snapshot mode still capped")

            from datetime import datetime
            from zoneinfo import ZoneInfo

            from relay.save_page import split_frontmatter

            day = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d")
            offset = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%z")
            offset_text = offset[:3] + ":" + offset[3:]
            before_scripts = len(json.loads(state_path.read_text())["scripts"])
            save_result = await session.call_tool("ego_save_page_markdown", {
                "subdir": "lessons",
                "slug": "demo",
                "url": YITANG_URL,
                "title": "重新理解一堂五步法",
                "mode": "yitang",
            })
            raw_save = tool_text(save_result)
            assert "课" * 40 not in raw_save, "save returned the page body"
            saved = json.loads(raw_save)
            assert set(saved) == {
                "path", "chars", "images_ok", "images_failed",
                "image_refs", "image_refs_ok", "image_refs_failed",
                "images_via_daemon", "images_via_relay", "images_pending",
                "failures", "bytes", "elapsed_ms", "truncated",
            }, saved
            assert saved["images_ok"] == 0 and saved["images_failed"] == 0, saved
            assert saved["images_pending"] == 0, saved
            assert saved["images_via_daemon"] == 0 and saved["images_via_relay"] == 0, saved
            assert saved["image_refs"] == 0 and saved["failures"] == {}, saved
            assert saved["truncated"] is False, saved
            assert isinstance(saved["elapsed_ms"], int) and saved["elapsed_ms"] >= 0, saved
            note = Path(saved["path"])
            assert note.is_file() and not note.is_symlink(), note
            assert note.resolve().is_relative_to(save_root.resolve()), note
            assert saved["bytes"] == note.stat().st_size, saved
            fields, body = split_frontmatter(note.read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text())
            fresh = [(item["op"], item["params"]) for item in state["scripts"][before_scripts:]]
            navs = [params for op, params in fresh if op == "navigate"]
            reads = [params for op, params in fresh if op == "get_page_text"]
            assert navs and navs[0].get("url") == YITANG_URL, fresh
            assert any(params.get("mode") == "yitang" for params in reads), fresh
            reread = await call_json(session, "ego_get_page_text", {"mode": "yitang"})
            assert body == reread["text"]
            assert body.encode("utf-8") == note.read_bytes()[-len(body.encode("utf-8")):]
            assert saved["chars"] == len(reread["text"]) == len(body)
            assert AUTHORED_BODY in body
            assert json.loads(fields["title"]) == "重新理解一堂五步法"
            assert json.loads(fields["source_url"]) == YITANG_URL
            assert json.loads(fields["device"]) == "smoke-mac"
            assert json.loads(fields["mode"]) == "yitang"
            assert fields["chars"] == str(len(body))
            assert fields["images"] == "0"
            assert fields["truncated"] == "false"
            captured = json.loads(fields["captured_at"])
            assert captured.endswith(offset_text), captured
            assert note.name == f"{day}-demo.md", note.name
            first_bytes = note.read_bytes()

            second = await call_json(session, "ego_save_page_markdown", {
                "subdir": "lessons",
                "slug": "demo",
                "mode": "yitang",
            })
            assert second["path"] != saved["path"]
            assert Path(second["path"]).name == f"{day}-demo-2.md", second
            assert note.read_bytes() == first_bytes

            for bad_args, needle in (
                ({"subdir": "../etc", "slug": "x"}, "EGO_SAVE_ROOT"),
                ({"subdir": "/etc", "slug": "x"}, "EGO_SAVE_ROOT"),
                ({"subdir": "lessons", "slug": "a/b"}, "slug"),
                ({"subdir": "lessons", "slug": "ok", "url": "javascript:alert(1)"}, "http"),
                ({"subdir": "lessons", "slug": "ok", "url": YITANG_URL, "label": "p1"}, "not both"),
            ):
                refused = await session.call_tool("ego_save_page_markdown", bad_args)
                assert getattr(refused, "is_error", False) or getattr(refused, "isError", False), bad_args
                assert needle.lower() in tool_error(refused).lower(), (bad_args, tool_error(refused))
            assert note.read_bytes() == first_bytes
            print("PASS save_page_markdown verbatim, collision suffix, path rejected")

        relay.logf.flush()
        audit = relay.log_path.read_text()
        lines = audit.splitlines()
        for name in ("ego_status", "ego_list_tabs", "ego_navigate", "ego_get_page_text", "ego_save_page_markdown"):
            assert any(f"tool={name}" in line and "status=ok" in line for line in lines), audit[-2000:]
        assert "save configured" in audit
        assert "课" * 40 not in audit
        print("PASS audit log")

        daemon_log.flush()
        assert daemon.poll() is None, "daemon exited early"
        print("stopping relay to force a daemon reconnect")
        relay.stop()
        await asyncio.sleep(0.3)
        relay.start()
        again = await wait_online(port, token, timeout=20)
        assert again["egoVersion"] == "0.6.2.8", again
        async with open_session(port, token) as session:
            tabs = await call_json(session, "ego_list_tabs")
        assert tabs["tabs"][0]["url"] == YITANG_URL, tabs
        relay.logf.flush()
        assert relay.log_path.read_text().count("agent connected") >= 2
        assert daemon.poll() is None
        local_log = home / ".local" / "state" / "hermes-ego" / "daemon.log"
        assert local_log.is_file(), "daemon did not write a local command log"
        logged = local_log.read_text()
        assert "command navigate" in logged and " ok" in logged, logged
        assert token not in logged, "daemon log contains the relay token"
        relay.logf.flush()
        assert token not in relay.log_path.read_text(), "relay log contains the token"
        print("PASS reconnect after relay disconnect")
        print("smoke_test: all checks passed")
    except Exception:
        print("--- daemon stderr ---")
        daemon_log.flush()
        print((tmp / "daemon.err").read_text(errors="replace")[-4000:])
        relay.dump()
        raise
    finally:
        if daemon is not None and daemon.poll() is None:
            daemon.send_signal(signal.SIGTERM)
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
        relay.stop()
        relay.logf.close()
        daemon_log.close()


def run_unit_tests() -> None:
    import unittest

    sys.path.insert(0, str(ROOT / "agent"))
    import test_yitang_text

    import test_ego_status

    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromModule(test_yitang_text))
    suite.addTests(loader.loadTestsFromModule(test_ego_status))
    import test_save_page
    import test_image_fetch
    import test_install_doctor
    import test_article_text

    suite.addTests(loader.loadTestsFromModule(test_save_page))
    suite.addTests(loader.loadTestsFromModule(test_image_fetch))
    suite.addTests(loader.loadTestsFromModule(test_install_doctor))
    suite.addTests(loader.loadTestsFromModule(test_article_text))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("PASS unit tests")


def main() -> int:
    if "--fake-ego" in sys.argv:
        fake_ego_main()
        return 0
    try:
        run_unit_tests()
        asyncio.run(run())
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
