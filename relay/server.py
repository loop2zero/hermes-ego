"""Relay between the Mac ego daemon (WebSocket /agent) and Hermes (MCP /mcp).

The daemon connects outward. The Mac opens no inbound port. Tool calls are
forwarded on that socket and correlated by id on the same connection.

Derived from hermes-tabs (MIT, Copyright (c) 2026 ai-ag2026). See NOTICE.
The browser-extension /ext endpoint, tab overlay, and admin UI are not part of
this relay: a native daemon speaks /agent and authenticates with a first-message
hello, so the token never appears in the URL.

Environment:
  EGO_RELAY_TOKEN  required bearer token for /mcp and the daemon hello
  EGO_RELAY_HOST   bind address, default 127.0.0.1
  EGO_RELAY_PORT   port, default 8767
  EGO_SAVE_ROOT    absolute directory ego_save_page_markdown may write under.
                   Unset disables that tool; the others keep working.
  EGO_SAVE_TZ      IANA timezone for the note's date prefix and captured_at.
                   Default UTC.
  EGO_SAVE_UID     optional numeric uid for notes, asset files, and directories
                   this tool creates. Default: the uid of EGO_SAVE_ROOT.
  EGO_SAVE_GID     optional numeric gid. Default: the gid of EGO_SAVE_ROOT.
  EGO_IMAGE_FETCH  relay (default), auto, or daemon. relay downloads during
                   the save, within EGO_IMAGE_BUDGET_MS, then one background
                   Mac pass for the leftovers. auto still asks the Mac first.
  EGO_IMAGE_BUDGET_MS
                   synchronous relay image budget. Default 90000.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import itertools
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

# The daemon file lives next to this package and is also run as a script.
_AGENT = Path(__file__).resolve().parent.parent / "agent"
if str(_AGENT) not in sys.path:
    sys.path.insert(0, str(_AGENT))
from article_text import article_from_html, clean_accessibility_text
from yitang_text import AUTHORED_MAX_TEXT, PAGE_TEXT_MODES, SNAPSHOT_MAX_TEXT, is_yitang_doc_url

from .save_page import (
    DaemonCapabilityError,
    SaveError,
    check_save_config,
    image_fetch_mode,
    run_backfill,
    save_markdown,
)
from image_fetch import FETCH_CAPABILITY, ImageAssembler

TIMEOUT = 60
HELLO_TIMEOUT = 10
LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

TOKEN = ""
HOST = "127.0.0.1"
PORT = 8767

server = MCPServer(
    "hermes-ego",
    instructions=(
        "Drives ego lite on the user's Mac through one task space named hermes-ego. "
        "You may only navigate and read pages this agent created in that space. "
        "You cannot click, type, or read the user's own tabs. "
        "There is one shared agent tab. "
        "To open a page and read it, call ego_get_page_text with url. "
        "That call holds the tab lock across the navigation and the read, so two "
        "callers cannot receive each other's page. "
        "ego_navigate and a later ego_get_page_text without url are separate calls "
        "and are not atomic: another navigation can land between them and the read "
        "can return the other page. "
        "On a yitang.top/fs-doc page, ego_get_page_text returns the lesson's authored markdown. "
        "On any other page, auto extracts the article from the page HTML. "
        "snapshot returns the accessibility tree. "
        "To keep a page, call ego_save_page_markdown with url, a relative subdir, and a slug. "
        "That save holds the same lock across navigate and read. "
        "It writes the markdown on the relay host and returns only the path and counts. "
        "Do not retype the page with write_file or apply_patch. "
        "ego_list_tabs lists agent pages already open in that space. "
        "ego_status.online means the daemon is connected. egoReachable means ego lite "
        "answered a probe; if it is false, start ego lite before the other tools. "
        "ego_status does not take the tab lock. "
        "If the daemon is offline, ask the user to start it on the Mac. "
        "A call that failed because the daemon disconnected has an unknown result: "
        "do not retry it automatically; check ego_status, then look at the page."
    ),
)

# One Mac daemon. A newer hello replaces the previous socket.
agent: dict[str, Any] | None = None
# id -> ("once", future, ws) or ("stream", queue, ws)
pending: dict[int, tuple] = {}
ids = itertools.count(1)
IMAGE_STREAM_IDLE_S = 75.0
IMAGE_STREAM_TOTAL_S = 180.0
_backfill_tasks: set[asyncio.Task] = set()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def token_ok(got: object) -> bool:
    if not isinstance(got, str):
        return False
    a = got.encode()
    b = TOKEN.encode()
    if len(a) != len(b):
        return False
    return secrets.compare_digest(a, b)


def bearer_ok(header: bytes) -> bool:
    expected = b"Bearer " + TOKEN.encode()
    if len(header) != len(expected):
        return False
    return secrets.compare_digest(header, expected)


def clean_device(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = "".join(c for c in value if c.isprintable()).strip()
    if not text or len(text) > 80:
        return None
    return text


def check_url(url: object) -> str:
    if not isinstance(url, str) or not url or len(url) > 2000:
        raise ToolError("url must be an absolute http or https URL")
    if any(ord(c) < 32 for c in url):
        raise ToolError("url must be an absolute http or https URL")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ToolError("url must be an absolute http or https URL")
    return url


def redact_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if not (parts.username or parts.password):
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urllib.parse.urlunsplit(parts._replace(netloc=host))


def summarize(params: dict) -> str:
    shown: dict[str, Any] = {}
    for key, value in params.items():
        if key == "url" and isinstance(value, str):
            value = redact_url(value)
        if isinstance(value, str) and len(value) > 160:
            value = value[:160] + "…"
        shown[key] = value
    return json.dumps(shown, ensure_ascii=False, default=str)[:300]


def audit(tool: str, params: dict, dur_ms: float, ok: bool) -> None:
    status = "ok" if ok else "err"
    print(f"{now()} tool={tool} params={summarize(params)} duration_ms={dur_ms:.0f} status={status}", flush=True)


def tool(fn):
    @functools.wraps(fn)
    async def run(*args, **kwargs):
        started = time.perf_counter()
        ok = True
        try:
            return await fn(*args, **kwargs)
        except Exception:
            ok = False
            raise
        finally:
            shown = {key: value for key, value in kwargs.items() if value is not None}
            audit(fn.__name__, shown, (time.perf_counter() - started) * 1000, ok)

    return server.tool(structured_output=False)(run)


def _page_fields(result: dict, text: str, truncated: bool) -> dict:
    return {
        "label": result.get("label"),
        "title": result.get("title"),
        "url": result.get("url"),
        "text": text,
        "truncated": truncated,
    }


def cap_text(result: Any) -> Any:
    """Safety cap. The daemon already slices snapshots to 20k and manuscripts to 1e6.

    HTML from a generic read is extracted here and then uses the manuscript cap.
    A snapshot fallback is cleaned and keeps the 20k cap. A yitang lesson URL
    keeps the manuscript cap so this hop does not cut the export back to 20k.
    """
    if not isinstance(result, dict):
        return result
    html = result.get("html")
    if isinstance(html, str) and html.strip():
        url = result.get("url") if isinstance(result.get("url"), str) else None
        text = article_from_html(html, url)
        truncated = result.get("truncated") is True or len(text) > AUTHORED_MAX_TEXT
        if len(text) > AUTHORED_MAX_TEXT:
            text = text[:AUTHORED_MAX_TEXT]
        return _page_fields(result, text, truncated)
    if result.get("fallback") == "snapshot" and isinstance(result.get("text"), str):
        text = clean_accessibility_text(result["text"])
        truncated = result.get("truncated") is True or len(text) > SNAPSHOT_MAX_TEXT
        if len(text) > SNAPSHOT_MAX_TEXT:
            text = text[:SNAPSHOT_MAX_TEXT]
        return _page_fields(result, text, truncated)
    text = result.get("text")
    if not isinstance(text, str):
        return result
    limit = AUTHORED_MAX_TEXT if is_yitang_doc_url(result.get("url")) else SNAPSHOT_MAX_TEXT
    if len(text) > limit:
        return {**result, "text": text[:limit], "truncated": True}
    return result


async def call(method: str, params: dict | None = None, timeout: float = TIMEOUT) -> Any:
    """Send one command on the daemon socket and wait. No automatic retry."""
    conn_state = agent
    if conn_state is None:
        raise ToolError("No ego daemon is connected to the relay.")
    conn: WebSocket = conn_state["ws"]
    i = next(ids)
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    pending[i] = ("once", fut, conn)
    body = {"id": i, "method": method, "params": params or {}}
    try:
        async with conn_state["send_lock"]:
            await conn.send_text(json.dumps(body))
        msg = await asyncio.wait_for(fut, timeout)
    except TimeoutError:
        raise ToolError(
            f"No answer from the ego daemon after {timeout:.0f}s — result unknown, "
            "do not retry automatically, check ego_status first"
        ) from None
    except Exception as exc:
        raise ToolError(f"ego daemon connection failed: {exc}") from exc
    finally:
        pending.pop(i, None)
    if not isinstance(msg, dict):
        raise ToolError("malformed response from the ego daemon")
    if msg.get("error"):
        raise ToolError(str(msg["error"]))
    if "result" not in msg:
        raise ToolError("ego daemon response missing result")
    return cap_text(msg["result"])


def dump(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False)


_tab_gate: asyncio.Lock | None = None


@contextlib.asynccontextmanager
async def tab_gate():
    """One navigate or read at a time on this relay.

    ego_get_page_text with a url, and ego_save_page_markdown, hold the gate
    across their own navigate and get_page_text, so another navigation cannot
    land between them. ego_navigate and a later ego_get_page_text without a url
    each take the gate on their own. ego_status does not take it.
    ego_list_tabs does not either: listing does not change the page.
    """
    global _tab_gate
    if _tab_gate is None:
        _tab_gate = asyncio.Lock()
    async with _tab_gate:
        yield


def _one_line(text: str, limit: int = 220) -> str:
    flat = " ".join(text.split())
    return flat[:limit]


def status_body(*, online: bool, device: str | None, result: dict | None, error: str | None) -> dict:
    """Shape ego_status. online is the daemon socket. egoReachable is the Mac probe."""
    if not online:
        return {
            "online": False,
            "device": None,
            "egoVersion": None,
            "egoReachable": False,
            "egoError": None,
        }
    if error is not None or not isinstance(result, dict):
        message = _one_line(error or "ego status failed")
        return {
            "online": True,
            "device": device,
            "egoVersion": None,
            "egoReachable": False,
            "egoError": message,
            "error": message,
        }
    version = result.get("egoVersion")
    if not isinstance(version, str) or not version.strip():
        version = None
    reachable = result.get("egoReachable") is True
    ego_error = result.get("egoError")
    if reachable:
        ego_error = None
    elif not isinstance(ego_error, str) or not ego_error.strip():
        ego_error = "ego lite is not reachable"
    else:
        ego_error = _one_line(ego_error)
    return {
        "online": True,
        "device": device,
        "egoVersion": version,
        "egoReachable": reachable,
        "egoError": ego_error,
    }


@tool
async def ego_status() -> str:
    """Report the daemon socket and whether ego lite itself answered a probe.

    online is true when the Mac daemon's WebSocket is up. That does not mean ego lite
    is running: `ego-browser --version` can succeed while the bootstrap server is down.
    egoReachable is the probe result. When it is false, egoError is a short reason.
    capabilities is the list from the daemon hello (fetch_images when image backfill
    can run). A probe is cached on the Mac for a few seconds. This call returns while a
    navigate or read is still in flight. Returns JSON.
    """
    conn_state = agent
    if conn_state is None:
        return dump(status_body(online=False, device=None, result=None, error=None))
    device = conn_state["device"]
    try:
        result = await call("status", {})
    except ToolError as exc:
        body = status_body(online=True, device=device, result=None, error=str(exc))
    else:
        if not isinstance(result, dict):
            body = status_body(online=True, device=device, result=None, error="malformed status")
        else:
            body = status_body(online=True, device=device, result=result, error=None)
    caps = conn_state.get("capabilities")
    body["capabilities"] = sorted(caps) if isinstance(caps, frozenset) else []
    return dump(body)


@tool
async def ego_list_tabs() -> str:
    """List agent-created tabs in the hermes-ego task space.

    Each tab has label, title, url, and active. The user's own tabs are not listed.
    List, navigate, and read share one tab and run one at a time, so this waits
    behind an in-flight navigate or read. Returns JSON: {"tabs": [...], "spaceId": ...}.
    """
    return dump(await call("list_tabs", {}))


@tool
async def ego_navigate(url: str) -> str:
    """Open the shared agent page in the hermes-ego task space and go to url.

    url must be absolute http or https. The daemon reuses the active agent page
    (else the first) and only calls newPage when none exists. Navigations are
    serialized on that one tab. This call does not read the page. To open a url
    and read that same page, call ego_get_page_text with url, or
    ego_save_page_markdown with url. Those hold the tab lock across navigate and
    read. This call followed by ego_get_page_text without url is not atomic:
    another navigation can run in the gap and the read can return the other page.
    Shares the lock with those tools. ego_status does not take it.
    Returns JSON with label, title, url, and spaceId.
    """
    checked = check_url(url)
    async with tab_gate():
        return dump(await call("navigate", {"url": checked}))


@tool
async def ego_get_page_text(
    url: str | None = None,
    label: str | None = None,
    mode: str | None = None,
) -> str:
    """Read the shared agent page, optionally opening url first under one lock.

    Pass url (absolute http or https) to open that page and read it as one call.
    The tab lock is held across the navigation and the read. Concurrent calls
    with different urls are serialized, and each returns its own page.
    Omit url to read the page already open. Pass label (for example p1) or omit
    it for the active agent page. Do not pass both url and label.
    ego_navigate and a later call without url are not atomic: another navigation
    can run in the gap. Use this url argument, or ego_save_page_markdown with url.
    mode is auto (default), generic, snapshot, or yitang.
    auto reads the authored lesson when the page is yitang.top and the path
    contains /fs-doc/: the daemon loads the Vue fsdoc dataJson and flattens
    blocks in order to markdown. That export is kept up to 1000000 characters
    and sets truncated when the manuscript is longer. Any other page uses
    generic: the daemon reads the page HTML and this relay extracts the article
    (title, headings, paragraphs, lists, links, and images). snapshot forces
    the accessibility tree, truncated at 20000 characters. yitang forces the
    authored export. generic forces the article extract. If the daemon cannot
    read HTML, generic falls back to a cleaned accessibility snapshot.
    ego_status does not take this lock. Returns JSON with label, title, url,
    text, and truncated.
    """
    if url is not None and label is not None:
        raise ToolError("pass url or label, not both")
    checked = check_url(url) if url is not None else None
    if label is not None and not LABEL_RE.fullmatch(label):
        raise ToolError("label must be 1-64 characters of letters, digits, '_' or '-'")
    if mode is not None and mode not in PAGE_TEXT_MODES:
        raise ToolError("mode must be auto, snapshot, yitang, or generic")
    result, _device, _navigate_ms, _read_ms = await _read_page(checked, label, mode)
    return dump(result)


async def _read_page(url: str | None, label: str | None, mode: str | None) -> tuple[Any, str, int, int]:
    """Navigate when url is set, then read, holding the tab gate the whole time.

    Callers validate url and label first. The last two numbers are navigate_ms
    and read_ms. ego_get_page_text and ego_save_page_markdown both use this so
    an open-and-read stays one critical section on the shared tab.
    """
    params: dict[str, Any] = {}
    if label is not None:
        params["label"] = label
    if mode is not None:
        params["mode"] = mode
    async with tab_gate():
        current = agent
        device = current.get("device") if isinstance(current, dict) else ""
        if not isinstance(device, str):
            device = ""
        nav_started = time.perf_counter()
        if url is not None:
            await call("navigate", {"url": url})
        navigate_ms = int(round((time.perf_counter() - nav_started) * 1000)) if url is not None else 0
        read_started = time.perf_counter()
        result = await call("get_page_text", params)
        read_ms = int(round((time.perf_counter() - read_started) * 1000))
    return result, device, navigate_ms, read_ms


async def fetch_images_from_daemon(urls: list[str]) -> tuple[dict[str, tuple[bytes, str]], dict[str, str]]:
    """Stream image bytes from the Mac. Raises DaemonCapabilityError if it cannot."""
    conn_state = agent
    if conn_state is None:
        raise DaemonCapabilityError("no ego daemon is connected")
    caps = conn_state.get("capabilities") or frozenset()
    if FETCH_CAPABILITY not in caps:
        raise DaemonCapabilityError("daemon has no fetch_images capability")
    conn: WebSocket = conn_state["ws"]
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    i = next(ids)
    pending[i] = ("stream", queue, conn)
    assembler = ImageAssembler(set(urls))
    try:
        async with conn_state["send_lock"]:
            await conn.send_text(json.dumps({
                "id": i, "method": "fetch_images", "params": {"urls": urls},
            }))
        deadline = loop.time() + IMAGE_STREAM_TOTAL_S
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(queue.get(), min(IMAGE_STREAM_IDLE_S, remaining))
            except TimeoutError:
                break
            if not isinstance(msg, dict):
                continue
            if msg.get("event") == "image" and isinstance(msg.get("seq"), int):
                try:
                    async with conn_state["send_lock"]:
                        await conn.send_text(json.dumps({
                            "id": i,
                            "event": "ack",
                            "url": msg.get("url"),
                            "seq": msg["seq"],
                        }))
                except Exception:
                    break
            try:
                finished = assembler.feed(msg)
            except DaemonCapabilityError:
                raise
            if finished:
                return assembler.ok, assembler.failed
        for url in urls:
            if url not in assembler.ok and url not in assembler.failed:
                assembler.failed[url] = "disconnect"
        return assembler.ok, assembler.failed
    except DaemonCapabilityError:
        raise
    except Exception:
        for url in urls:
            if url not in assembler.ok and url not in assembler.failed:
                assembler.failed[url] = "disconnect"
        return assembler.ok, assembler.failed
    finally:
        pending.pop(i, None)


@tool
async def ego_save_page_markdown(
    subdir: str,
    slug: str,
    url: str | None = None,
    label: str | None = None,
    title: str | None = None,
    mode: str | None = None,
) -> str:
    """Save the agent page as markdown on the relay host. Do not retype it.

    Use this instead of write_file or apply_patch. The body is the
    ego_get_page_text string plus YAML frontmatter, with http(s) image links
    rewritten to local files when the download works. The result is metadata
    only: path, chars, images_ok and images_failed (unique URLs), image_refs,
    image_refs_ok, image_refs_failed (markdown occurrences), images_via_daemon
    and images_via_relay for this call, images_pending (unique URLs left for a
    background pass), failures (counts by reason, no URLs), bytes, elapsed_ms,
    truncated. The body is not returned.
    The synchronous image phase runs on the relay and stops starting new
    downloads after EGO_IMAGE_BUDGET_MS (default 90s). Images that miss that
    budget, or fail, keep their original URL and a marker in the note. When
    the Mac daemon advertises fetch_images, one background pass fills those
    in after this tool returns. images_pending is 0 when that pass will not
    run. Created files are mode 0644 and
    created directories are mode 0755, owned by EGO_SAVE_ROOT's uid and gid
    unless EGO_SAVE_UID and EGO_SAVE_GID override that.

    subdir is a relative directory under EGO_SAVE_ROOT. Pass `.` for the root
    itself. Absolute paths, `..`, and symlinks are rejected. The tool is
    disabled, with an error, when EGO_SAVE_ROOT is unset.
    slug is the filename stem: 1-64 letters, digits, `_`, or `-`, not a path.
    The file is named YYYY-MM-DD-<slug>.md in EGO_SAVE_TZ (default UTC).
    An existing file is never overwritten. The next free name gets -2, -3,
    through -100.
    url, when set, opens that http(s) page and reads it before another
    navigation can run. Omit url to read the current agent page, or pass label.
    Do not pass both url and label.
    title overrides the note title. The default is the page title, then the slug.
    mode is auto (default), generic, snapshot, or yitang, the same as ego_get_page_text.
    """
    started = time.perf_counter()
    try:
        if url is not None and label is not None:
            raise ToolError("pass url or label, not both")
        checked = check_url(url) if url is not None else None
        if label is not None and not LABEL_RE.fullmatch(label):
            raise ToolError("label must be 1-64 characters of letters, digits, '_' or '-'")
        if title is not None and not isinstance(title, str):
            raise ToolError("title must be a string")
        if mode is not None and mode not in PAGE_TEXT_MODES:
            raise ToolError("mode must be auto, snapshot, yitang, or generic")
        check_save_config(subdir, slug)
        fetch_mode = image_fetch_mode()
        page, device, navigate_ms, read_ms = await _read_page(checked, label, mode)
        if not isinstance(page, dict):
            raise ToolError("ego daemon returned no page text")
        caps = agent.get("capabilities") if isinstance(agent, dict) else frozenset()
        capable = isinstance(caps, frozenset) and FETCH_CAPABILITY in caps
        daemon_fetch = fetch_images_from_daemon if fetch_mode in ("auto", "daemon") and capable else None
        meta = await save_markdown(
            page,
            device=device,
            subdir=subdir,
            slug=slug,
            title=title,
            mode=mode or "auto",
            daemon_fetch=daemon_fetch,
            image_fetch_mode_name=fetch_mode,
            allow_backfill=fetch_mode == "relay" and capable,
        )
    except SaveError as exc:
        raise ToolError(str(exc)) from exc
    images_ms = int(meta.pop("_images_ms", 0))
    write_ms = int(meta.pop("_write_ms", 0))
    job = meta.pop("_backfill", None)
    meta["elapsed_ms"] = int(round((time.perf_counter() - started) * 1000))
    print(
        f"{now()} save phases navigate_ms={navigate_ms} read_ms={read_ms} "
        f"images_ms={images_ms} write_ms={write_ms} total_ms={meta['elapsed_ms']} "
        f"via_daemon={meta.get('images_via_daemon', 0)} via_relay={meta.get('images_via_relay', 0)} "
        f"pending={meta.get('images_pending', 0)}",
        flush=True,
    )
    if isinstance(job, dict):
        _schedule_backfill(job)
    return dump(meta)


def _schedule_backfill(job: dict) -> None:
    """One daemon pass after the tool response. The model does not wait for it."""

    async def runner() -> None:
        try:
            current = agent
            caps = current.get("capabilities") if isinstance(current, dict) else frozenset()
            fetch = fetch_images_from_daemon if isinstance(caps, frozenset) and FETCH_CAPABILITY in caps else None
            await run_backfill(job, fetch)
        except Exception as exc:
            print(f"{now()} backfill error {exc}", flush=True)

    task = asyncio.create_task(runner())
    _backfill_tasks.add(task)
    task.add_done_callback(_backfill_tasks.discard)


def _abort_message() -> dict:
    return {
        "error": (
            "Connection to the ego daemon aborted — result unknown, "
            "do not retry automatically, check ego_status first"
        ),
    }


def fail_pending(ws: WebSocket) -> None:
    for kind, slot, conn in list(pending.values()):
        if conn is not ws:
            continue
        if kind == "once" and not slot.done():
            slot.set_result(_abort_message())
        elif kind == "stream":
            slot.put_nowait(_abort_message())


async def agent_endpoint(ws: WebSocket) -> None:
    """Daemon socket. First message is {type:hello, token, device}. Bad token closes."""
    global agent
    await ws.accept()
    hello = await read_hello(ws)
    if hello is None:
        with contextlib.suppress(Exception):
            await ws.close(code=1008)
        return
    device, capabilities = hello
    fresh = {
        "ws": ws,
        "device": device,
        "capabilities": capabilities,
        "send_lock": asyncio.Lock(),
        "connectedAt": now(),
    }
    old = agent
    agent = fresh
    shown = ",".join(sorted(capabilities)) if capabilities else "-"
    print(f"{now()} agent connected device={device} capabilities={shown}", flush=True)
    if old is not None:
        with contextlib.suppress(Exception):
            await old["ws"].close(code=1012)
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "ping":
                continue
            waiter = pending.get(msg.get("id"))
            if waiter is None or waiter[2] is not ws:
                continue
            kind, slot, _conn = waiter
            if kind == "once" and not slot.done():
                slot.set_result(msg)
            elif kind == "stream":
                slot.put_nowait(msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        with contextlib.suppress(Exception):
            await ws.close(code=1008)
    finally:
        if agent is not None and agent.get("ws") is ws:
            agent = None
            print(f"{now()} agent disconnected device={device}", flush=True)
        fail_pending(ws)


def clean_capabilities(value: object) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str) and item and len(item) <= 64)


async def read_hello(ws: WebSocket) -> tuple[str | None, frozenset[str]] | None:
    try:
        raw = await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT)
        msg = json.loads(raw)
    except Exception:
        return None
    if not isinstance(msg, dict) or msg.get("type") != "hello" or not token_ok(msg.get("token")):
        print(f"{now()} agent hello rejected", flush=True)
        return None
    return clean_device(msg.get("device")), clean_capabilities(msg.get("capabilities"))


def guarded(app):
    """Bearer token on /mcp. Reject any request that carries an Origin header.

    MCP clients do not send Origin. A browser page does, which is the DNS-rebinding
    case the streamable HTTP transport wants closed. GET /mcp is 405: this server
    is stateless and answers JSON, not an SSE event stream.
    """

    async def deny(send, status: int) -> None:
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def asgi(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if scope.get("path") == "/mcp":
                if b"origin" in headers:
                    return await deny(send, 403)
                if scope.get("method") == "GET":
                    return await deny(send, 405)
                if not TOKEN or not bearer_ok(headers.get(b"authorization", b"")):
                    return await deny(send, 401)
        await app(scope, receive, send)

    return asgi


def build_app():
    app = server.streamable_http_app(
        host=HOST,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.router.routes.append(WebSocketRoute("/agent", agent_endpoint))
    return guarded(app)


def load_env_file() -> None:
    """Load KEY=VALUE lines when launchd points HERMES_EGO_ENV at relay.env.

    systemd uses EnvironmentFile and does not need this. Existing process
    environment wins, so a shell export is not overwritten. Symlinks are refused.
    """
    raw = os.environ.get("HERMES_EGO_ENV", "").strip()
    if not raw:
        return
    path = Path(raw)
    if path.is_symlink() or not path.is_file():
        sys.exit(f"HERMES_EGO_ENV is not a regular file: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value


def main() -> None:
    global TOKEN, HOST, PORT
    load_env_file()
    TOKEN = os.environ.get("EGO_RELAY_TOKEN", "")
    if not TOKEN:
        sys.exit(
            "EGO_RELAY_TOKEN is not set. Use a long random string, for example "
            "EGO_RELAY_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
        )
    HOST = os.environ.get("EGO_RELAY_HOST", "127.0.0.1")
    PORT = int(os.environ.get("EGO_RELAY_PORT", "8767"))
    print(f"{now()} relay listening http://{HOST}:{PORT}/mcp  ws://{HOST}:{PORT}/agent", flush=True)
    root = os.environ.get("EGO_SAVE_ROOT", "").strip()
    tz_name = os.environ.get("EGO_SAVE_TZ", "UTC").strip() or "UTC"
    if root:
        print(f"{now()} save configured root={root} tz={tz_name}", flush=True)
    else:
        print(f"{now()} save disabled: EGO_SAVE_ROOT is not set", flush=True)
    # Ping timeout is longer than uvicorn's 20s default. Image frames are small
    # and acked, but an SSH tunnel can still pause a pong for more than 20s.
    uvicorn.run(
        build_app(),
        host=HOST,
        port=PORT,
        access_log=False,
        log_level="warning",
        ws_ping_interval=20.0,
        ws_ping_timeout=60.0,
    )


if __name__ == "__main__":
    main()
