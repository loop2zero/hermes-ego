#!/usr/bin/env python3
"""Mac daemon for hermes-ego. Outbound WebSocket only; no listening port.

Config: ~/.config/hermes-ego/config.toml (override with HERMES_EGO_CONFIG).
Each relay request runs one `ego-browser` process. Scripts are ESM and do not
inherit env or cwd, so parameters are embedded as JSON literals.
`nodejs` writes console.log JSON to stderr (stdout is empty); `--version`
prints on stdout and does not prove ego lite is running. Status also probes
`task.tabs()` with a 5s cap. The daemon takes the last JSON object across both streams.

One task space, "hermes-ego". Only pages with openedBy "agent" are used.
finish() is not called: it drops page labels, and the next call resumes by name.
Those pages are one shared agent tab. list, navigate, and read take one lock so
they run one at a time. The lock is per command: a navigate and a later read
are not one transaction. The relay joins them when ego_get_page_text or
ego_save_page_markdown is called with a url. ego_status bypasses that lock.
A tab command whose reachability cache is missing or stale runs the 5s probe
first and fails fast
when ego lite is down. fetch_images downloads http(s) bytes for the relay and
does not touch the browser or the note vault.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sys
import time
import tomllib
import urllib.parse
from pathlib import Path

import websockets
from websockets.exceptions import ConnectionClosed

from image_fetch import (
    FETCH_CAPABILITY,
    IMAGE_CONCURRENCY,
    IMAGE_MAX_BYTES,
    MAX_UNIQUE_IMAGES,
    ImageFetchError,
    fetch_url_with_retries,
    image_frames,
    normalize_image_url,
)
from yitang_text import PAGE_TEXT_MODES, page_text_from_ego

HEARTBEAT_S = 20
# WebSocket protocol pings, separate from the JSON {"type":"ping"} heartbeat.
# The relay allows 60s for a pong so a slow SSH tunnel can drain one frame.
WS_PING_INTERVAL_S = 20
WS_PING_TIMEOUT_S = 60
# At most two 256 KiB frames on the wire. The daemon waits for a relay ack
# before exceeding that, so a ping is never stuck behind a multi-MB backlog.
IMAGE_FRAME_WINDOW = 2
IMAGE_ACK_TIMEOUT_S = 30
_ack_queues: dict[int, asyncio.Queue] = {}
EGO_TIMEOUT_S = 45
# --version does not open the ego lite bootstrap server. The probe does, and it
# is killed on its own clock so a down GUI cannot stall ego_status for ~30s.
PROBE_TIMEOUT_S = 5
PROBE_CACHE_S = 10
BACKOFF_MAX_S = 30
PARAMS = "__HERMES_EGO_PARAMS__"
LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# One shared agent tab, so one lock, per command. status is intentionally absent.
# A navigate and a later read from two socket messages are not one section.
TAB_METHODS = frozenset({"list_tabs", "navigate", "get_page_text"})
LOG_PATH: Path | None = None
_BOOTSTRAP_MARKERS = ("bootstrap", "ego_cli", "failed to connect", "launch ego lite")
# at, reachable, error, version. None until the first probe or bootstrap failure.
_probe_cache: dict | None = None
# Created on first use so it belongs to the running loop's lifetime, not import.
_tab_command_lock: asyncio.Lock | None = None

NAV_JS = """
// hermes-ego-op: navigate
const params = __HERMES_EGO_PARAMS__;
const task = await taskSpace("hermes-ego");
const tabs = await task.tabs();
const agent = (tabs || []).filter((t) => t && t.label && t.openedBy === "agent");
const current = agent.find((t) => t.active) || agent[0];
const page = current ? task.page(current.label) : await task.newPage();
await page.goto(params.url);
await page.waitForLoadState();
console.log(JSON.stringify({
  label: page.label, title: await page.title(), url: await page.url(), spaceId: task.spaceId,
}));
""".strip()

LIST_JS = """
// hermes-ego-op: list_tabs
const params = __HERMES_EGO_PARAMS__;
const task = await taskSpace("hermes-ego");
const tabs = await task.tabs();
const listed = (tabs || []).filter((t) => t && t.label && t.openedBy === "agent").map((t) => ({
  label: t.label, title: t.title ?? null, url: t.url ?? null, active: Boolean(t.active),
}));
console.log(JSON.stringify({ tabs: listed, spaceId: task.spaceId }));
""".strip()

PROBE_JS = """
// hermes-ego-op: probe
// Contacts the ego lite bootstrap server. --version does not.
const params = __HERMES_EGO_PARAMS__;
const task = await taskSpace("hermes-ego");
await task.tabs();
console.log(JSON.stringify({ ok: true }));
""".strip()

TEXT_JS = """
// hermes-ego-op: get_page_text
// auto: yitang.top /fs-doc/ returns fsdoc.dataJson; every other page returns HTML
// for the relay's generic article extract. snapshot stays the accessibility tree.
const params = __HERMES_EGO_PARAMS__;
const task = await taskSpace("hermes-ego");
const tabs = await task.tabs();
const agent = (tabs || []).filter((t) => t && t.label && t.openedBy === "agent");
const wanted = params.label || null;
const tab = wanted
  ? agent.find((t) => t.label === wanted)
  : (agent.find((t) => t.active) || agent[0]);
if (!tab) {
  throw new Error(wanted
    ? "label is not an agent-created page in task space hermes-ego"
    : "no agent-created page in task space hermes-ego");
}
const page = task.page(tab.label);
const mode = params.mode || "auto";
if (mode !== "auto" && mode !== "snapshot" && mode !== "yitang" && mode !== "generic") {
  throw new Error("mode must be auto, snapshot, yitang, or generic");
}
const pageUrl = await page.url();
const title = await page.title();

function yitangDocUrl(raw) {
  try {
    const u = new URL(raw);
    const host = u.hostname.toLowerCase();
    const hostOk = host === "yitang.top" || host.endsWith(".yitang.top");
    return hostOk && u.pathname.toLowerCase().includes("/fs-doc/");
  } catch (err) {
    return false;
  }
}

function readAuthored(flag) {
  function ready(data) {
    return !!(data && typeof data === "object" && (
      data.blockId || (Array.isArray(data.childrens) && data.childrens.length)
    ));
  }
  function walk(vm, depth, pred) {
    if (!vm || depth > 40) return null;
    if (pred(vm)) return vm.dataJson;
    const kids = vm.$children || [];
    for (let i = 0; i < kids.length; i++) {
      const found = walk(kids[i], depth + 1, pred);
      if (found) return found;
    }
    return null;
  }
  function rootVm() {
    const app = document.querySelector("#app");
    if (!app) return null;
    if (app.__vue__) return app.__vue__;
    const stack = [app];
    let steps = 0;
    while (stack.length && steps < 200) {
      const el = stack.pop();
      steps += 1;
      if (el && el.__vue__) return el.__vue__;
      const kids = el && el.children;
      if (!kids) continue;
      for (let i = 0; i < kids.length; i++) stack.push(kids[i]);
    }
    return null;
  }
  function plain(value, seen) {
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return value;
    if (!value || typeof value !== "object") return null;
    if (seen.has(value)) return null;
    seen.add(value);
    if (Array.isArray(value)) {
      const out = [];
      for (let i = 0; i < value.length; i++) out.push(plain(value[i], seen));
      return out;
    }
    const out = {};
    const keys = Object.keys(value);
    for (let i = 0; i < keys.length; i++) {
      const key = keys[i];
      if (key === "__ob__") continue;
      out[key] = plain(value[key], seen);
    }
    return out;
  }
  const top = rootVm();
  const named = walk(top, 0, (vm) => {
    const name = vm.$options && vm.$options.name;
    return name === "fsdoc" && ready(vm.dataJson);
  });
  const data = named || walk(top, 0, (vm) => ready(vm.dataJson) && Array.isArray(vm.dataJson.childrens));
  if (!data) return null;
  // waitForFunction only needs a boolean; the tree is copied on the evaluate below.
  if (flag === "ready") return true;
  return plain(data, new Set());
}

const wantAuthored = mode === "yitang" || (mode === "auto" && yitangDocUrl(pageUrl));
const wantGeneric = mode === "generic" || (mode === "auto" && !yitangDocUrl(pageUrl));
function readHtml() {
  const root = document.documentElement;
  return root ? root.outerHTML : "";
}
if (wantAuthored) {
  try {
    await page.waitForFunction(readAuthored, "ready", { timeout: 15000, polling: 200 });
  } catch (err) {
    // evaluate reports a missing model if the lesson never finished loading
  }
  const dataJson = await page.evaluate(readAuthored);
  if (!dataJson || !(dataJson.blockId || (dataJson.childrens && dataJson.childrens.length))) {
    throw new Error("yitang page has no authored dataJson");
  }
  console.log(JSON.stringify({ label: tab.label, title, url: pageUrl, dataJson }));
} else if (wantGeneric) {
  let html = "";
  try {
    const got = await page.evaluate(readHtml);
    html = typeof got === "string" ? got : "";
  } catch (err) {
    html = "";
  }
  const HTML_MAX = 2000000;
  if (html.length >= 32) {
    const truncated = html.length > HTML_MAX;
    if (truncated) html = html.slice(0, HTML_MAX);
    console.log(JSON.stringify({ label: tab.label, title, url: pageUrl, html, truncated }));
  } else {
    const snap = await page.snapshot({ scope: "full_page" });
    let text = typeof snap === "string" ? snap
      : (snap && (snap.content || snap.text)) || JSON.stringify(snap);
    const truncated = text.length > 20000;
    if (truncated) text = text.slice(0, 20000);
    console.log(JSON.stringify({
      label: tab.label, title, url: pageUrl, text, truncated, fallback: "snapshot",
    }));
  }
} else {
  const snap = await page.snapshot({ scope: "full_page" });
  let text = typeof snap === "string" ? snap
    : (snap && (snap.content || snap.text)) || JSON.stringify(snap);
  const truncated = text.length > 20000;
  if (truncated) text = text.slice(0, 20000);
  console.log(JSON.stringify({
    label: tab.label, title, url: pageUrl, text, truncated,
  }));
}
""".strip()


def iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg: str) -> None:
    line = f"{iso()} {msg}"
    print(line, file=sys.stderr, flush=True)
    if LOG_PATH is None:
        return
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(f"{iso()} log write failed: {exc}", file=sys.stderr, flush=True)


def config_path() -> Path:
    override = os.environ.get("HERMES_EGO_CONFIG")
    return Path(override) if override else Path.home() / ".config" / "hermes-ego" / "config.toml"


def load_config(path: Path) -> dict:
    if not path.is_file():
        sys.exit(f"missing config {path} — copy agent/config.example.toml and fill it in")
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    for key in ("relay_url", "token", "device"):
        if not isinstance(data.get(key), str) or not str(data.get(key)).strip():
            sys.exit(f"{path} needs a non-empty string '{key}'")
    url = data["relay_url"].strip()
    if not url.startswith(("ws://", "wss://")):
        sys.exit("relay_url must start with ws:// or wss://")
    device = "".join(c for c in data["device"] if c.isprintable()).strip()
    if not device or len(device) > 80:
        sys.exit("device must be 1-80 printable characters")
    data["relay_url"], data["token"], data["device"] = url, data["token"].strip(), device
    return data


def ego_bin(cfg: dict) -> str:
    custom = cfg.get("ego_browser")
    if isinstance(custom, str) and custom.strip():
        return custom.strip()
    return shutil.which("ego-browser") or str(Path.home() / ".local" / "bin" / "ego-browser")


def check_url(url: object) -> str:
    if not isinstance(url, str) or not url or len(url) > 2000 or any(ord(c) < 32 for c in url):
        raise RuntimeError("url must be an absolute http or https URL")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RuntimeError("url must be an absolute http or https URL")
    return url


def render(template: str, params: dict) -> str:
    script = template.replace(PARAMS, json.dumps(params, ensure_ascii=True), 1)
    if PARAMS in script:
        raise RuntimeError("failed to embed params")
    return script


def last_json_object(blob: str) -> dict | None:
    for line in reversed(blob.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def parse_result(stdout: str, stderr: str) -> dict:
    """Last JSON object in stdout followed by stderr.

    ego-browser nodejs writes console.log to stderr and leaves stdout empty.
    A non-JSON notice may follow the object, so this is not "the last line".
    """
    found = last_json_object(stdout + "\n" + stderr)
    if found is not None:
        return found
    tail = (stderr or stdout)[-400:].replace("\n", " ")
    raise RuntimeError(f"ego-browser produced no JSON object: {tail}")


def kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.pid:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def run_ego(
    binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None,
) -> tuple[str, str]:
    limit = EGO_TIMEOUT_S if timeout is None else timeout
    proc = await asyncio.create_subprocess_exec(
        binary, *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    async def killer() -> None:
        await asyncio.sleep(limit)
        kill_group(proc)

    task = asyncio.create_task(killer())
    try:
        out_b, err_b = await proc.communicate(stdin)
    except asyncio.CancelledError:
        # Drop the process group before the cancellation propagates. A tab
        # command cancelled because the socket closed must not keep running.
        kill_group(proc)
        raise
    finally:
        timed_out = task.done() and not task.cancelled()
        if not timed_out:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    out, err = out_b.decode(errors="replace"), err_b.decode(errors="replace")
    if timed_out:
        detail = " ".join(part.strip() for part in (err, out) if part.strip())
        extra = f": {detail.replace(chr(10), ' ')[-300:]}" if detail else ""
        raise RuntimeError(f"ego-browser timed out after {limit:g}s{extra}")
    if proc.returncode != 0:
        detail = " ".join(part.strip() for part in (err, out) if part.strip())
        raise RuntimeError(f"ego-browser exited {proc.returncode}: {detail.replace(chr(10), ' ')[-400:]}")
    return out, err


def clear_probe_cache() -> None:
    global _probe_cache
    _probe_cache = None


def reset_tab_command_lock() -> None:
    global _tab_command_lock
    _tab_command_lock = None


def tab_command_lock() -> asyncio.Lock:
    """The lock around list, navigate, and read. One tab, so one lock."""
    global _tab_command_lock
    if _tab_command_lock is None:
        _tab_command_lock = asyncio.Lock()
    return _tab_command_lock


def short_ego_error(exc: BaseException) -> str:
    """One line, capped. A probe that we killed ourselves gets a fixed reason."""
    text = " ".join(str(exc).split())
    low = text.lower()
    if any(marker in low for marker in _BOOTSTRAP_MARKERS):
        return text[:220]
    if "timed out" in low:
        return "ego lite did not answer within 5s; launch ego lite.app manually"
    return text[:220]


def _bootstrap_failure(exc: BaseException) -> bool:
    low = str(exc).lower()
    return any(marker in low for marker in _BOOTSTRAP_MARKERS)


def _now() -> float:
    return time.monotonic()


def _fresh_probe() -> dict | None:
    cached = _probe_cache
    if not cached:
        return None
    if _now() - float(cached["at"]) >= PROBE_CACHE_S:
        return None
    return cached


def _remember(reachable: bool, error: str | None, version: str | None = None, *, keep_version: bool = False) -> None:
    global _probe_cache
    if keep_version and version is None and _probe_cache is not None:
        version = _probe_cache.get("version")
    _probe_cache = {
        "at": _now(),
        "reachable": reachable,
        "error": error,
        "version": version,
    }


def _status_payload(version: str, reachable: bool, error: str | None) -> dict:
    return {
        "egoVersion": version,
        "egoReachable": reachable,
        "egoError": None if reachable else (error or "ego lite is not reachable"),
    }


def _log_probe(reachable: bool, error: str | None) -> None:
    detail = "" if reachable else f" {error or ''}".rstrip()
    log(f"ego probe reachable={'true' if reachable else 'false'}{detail}")


async def _ensure_reachable(binary: str) -> None:
    """Fail fast before a tab command.

    A fresh success is reused, so a read does not pay for a probe. A fresh
    failure is raised as stored. A missing or stale cache runs the 5s probe
    and raises its egoError instead of starting the 45s tool command.
    """
    cached = _fresh_probe()
    if cached is not None:
        if cached["reachable"]:
            return
        raise RuntimeError(cached.get("error") or "ego lite is not reachable")
    reachable, error = await _probe_ego(binary)
    _remember(reachable, error, keep_version=True)
    _log_probe(reachable, error)
    if not reachable:
        raise RuntimeError(error or "ego lite is not reachable")


async def _ego_version(binary: str) -> str:
    out, err = await run_ego(binary, ["--version"], None)
    blob = f"{out}\n{err}"
    match = re.search(r"\d+\.\d+\.\d+(?:\.\d+)?", blob)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    version = match.group(0) if match else (lines[-1] if lines else "")
    if not version:
        raise RuntimeError("ego-browser --version produced no version")
    return version


async def _probe_ego(binary: str) -> tuple[bool, str | None]:
    try:
        await run_ego(binary, ["nodejs"], render(PROBE_JS, {}).encode(), timeout=PROBE_TIMEOUT_S)
    except Exception as exc:
        return False, short_ego_error(exc)
    return True, None


async def collect_status(binary: str) -> dict:
    """ego version plus a cached bootstrap probe.

    `ego-browser --version` answers from the binary alone. The probe lists tabs
    in the hermes-ego space, which is the cheap call that actually reaches ego lite.
    """
    cached = _fresh_probe()
    if cached is not None:
        version = cached.get("version")
        if not isinstance(version, str) or not version:
            version = await _ego_version(binary)
            cached["version"] = version
        return _status_payload(version, bool(cached["reachable"]), cached.get("error"))
    version = await _ego_version(binary)
    reachable, error = await _probe_ego(binary)
    _remember(reachable, error, version)
    _log_probe(reachable, error)
    return _status_payload(version, reachable, error)


async def dispatch(binary: str, method: object, params: dict) -> dict:
    if method == "status":
        return await collect_status(binary)
    if method == "list_tabs":
        script, embedded = LIST_JS, {}
    elif method == "navigate":
        script, embedded = NAV_JS, {"url": check_url(params.get("url"))}
    elif method == "get_page_text":
        script, embedded = TEXT_JS, {}
        if params.get("label") is not None:
            label = params.get("label")
            if not isinstance(label, str) or not LABEL_RE.fullmatch(label):
                raise RuntimeError("label must be 1-64 characters of letters, digits, '_' or '-'")
            embedded["label"] = label
        if params.get("mode") is not None:
            mode = params.get("mode")
            if not isinstance(mode, str) or mode not in PAGE_TEXT_MODES:
                raise RuntimeError("mode must be auto, snapshot, yitang, or generic")
            embedded["mode"] = mode
    else:
        raise RuntimeError(f"unknown method {method!r}")
    # Fresh-false is immediate. Missing or stale runs the 5s probe first so a
    # down ego lite does not sit in this command's 45s timeout.
    await _ensure_reachable(binary)
    try:
        out, err = await run_ego(binary, ["nodejs"], render(script, embedded).encode())
    except Exception as exc:
        if _bootstrap_failure(exc):
            _remember(False, short_ego_error(exc), keep_version=True)
        raise
    _remember(True, None, keep_version=True)
    return page_text_from_ego(parse_result(out, err))


async def execute(binary: str, method: object, params: dict) -> dict:
    """Run one daemon command. Tab tools share a lock; status does not wait on it."""
    if method in TAB_METHODS:
        async with tab_command_lock():
            return await dispatch(binary, method, params)
    return await dispatch(binary, method, params)


def brief(params: dict) -> str:
    shown = {k: (v[:160] + "…") if isinstance(v, str) and len(v) > 160 else v for k, v in params.items()}
    return json.dumps(shown, ensure_ascii=False, default=str)[:300]


async def stream_fetch_images(
    ws, msg: dict, lock: asyncio.Lock, *, chunk_bytes: int | None = None, fetcher=None,
    ack_queue: asyncio.Queue | None = None,
) -> None:
    """Stream image bytes in small frames. Nothing is written to disk.

    At most IMAGE_FRAME_WINDOW frames are sent before the relay acks one, so
    the socket is never holding a multi-MB backlog and keepalive pings can
    be written between frames.
    """
    msg_id = msg.get("id")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    urls = params.get("urls")

    async def send(payload: dict) -> None:
        async with lock:
            await ws.send(json.dumps(payload))

    if not isinstance(urls, list):
        await send({"id": msg_id, "error": "urls must be a list"})
        return
    if len(urls) > MAX_UNIQUE_IMAGES:
        await send({"id": msg_id, "error": "too many image URLs"})
        return
    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not isinstance(url, str) or url in seen:
            continue
        seen.add(url)
        unique.append(url)
    acks = ack_queue if ack_queue is not None else asyncio.Queue()
    if isinstance(msg_id, int) and ack_queue is None:
        _ack_queues[msg_id] = acks
    window: set[tuple[str, int]] = set()
    window_cond = asyncio.Condition()
    stats = {"bytes": 0, "ok": 0, "failed": 0}
    started = time.perf_counter()

    async def ack_loop() -> None:
        while True:
            ack = await acks.get()
            if not isinstance(ack, dict):
                continue
            if ack.get("event") == "stop":
                return
            key = (ack.get("url"), ack.get("seq"))
            async with window_cond:
                if key in window:
                    window.remove(key)
                    window_cond.notify_all()

    async def send_frame(frame: dict) -> None:
        key = (frame.get("url"), frame.get("seq"))
        if not isinstance(key[0], str) or not isinstance(key[1], int):
            await send(frame)
            return
        async with window_cond:
            while len(window) >= IMAGE_FRAME_WINDOW:
                await window_cond.wait()
            window.add(key)
        await send(frame)
        try:
            await asyncio.wait_for(_wait_key(key), IMAGE_ACK_TIMEOUT_S)
        except TimeoutError:
            async with window_cond:
                window.discard(key)
                window_cond.notify_all()
            raise TimeoutError(f"image ack timed out for seq {key[1]}") from None

    async def _wait_key(key: tuple[str, int]) -> None:
        async with window_cond:
            while key in window:
                await window_cond.wait()

    sem = asyncio.Semaphore(IMAGE_CONCURRENCY)
    reader = asyncio.create_task(ack_loop())

    async def one(url: str) -> None:
        if normalize_image_url(url) is None:
            stats["failed"] += 1
            await send({"id": msg_id, "event": "image_fail", "url": url, "reason": "blocked"})
            return
        async with sem:
            try:
                if fetcher is None:
                    data, ctype = await fetch_url_with_retries(url)
                else:
                    data, ctype = await fetcher(url)
            except ImageFetchError as exc:
                stats["failed"] += 1
                await send({"id": msg_id, "event": "image_fail", "url": url, "reason": exc.reason})
                return
            except Exception:
                stats["failed"] += 1
                await send({"id": msg_id, "event": "image_fail", "url": url, "reason": "other"})
                return
        if len(data) > IMAGE_MAX_BYTES:
            stats["failed"] += 1
            await send({"id": msg_id, "event": "image_fail", "url": url, "reason": "too_large"})
            return
        frames = image_frames(url, data, ctype, **({} if chunk_bytes is None else {"chunk_bytes": chunk_bytes}))
        for frame in frames:
            frame["id"] = msg_id
            await send_frame(frame)
        stats["ok"] += 1
        stats["bytes"] += len(data)

    tasks = [asyncio.create_task(one(url)) for url in unique]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        raise
    except Exception:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        await acks.put({"event": "stop"})
        await reader
        if isinstance(msg_id, int):
            _ack_queues.pop(msg_id, None)
        elapsed = time.perf_counter() - started
        kib_s = (stats["bytes"] / 1024) / elapsed if elapsed > 0 else 0.0
        log(
            f"fetch_images bytes={stats['bytes']} images={len(unique)} "
            f"ok={stats['ok']} failed={stats['failed']} "
            f"elapsed_ms={elapsed * 1000:.0f} kib_s={kib_s:.0f}"
        )
    await send({"id": msg_id, "event": "done"})


async def handle(ws, binary: str, msg: dict, lock: asyncio.Lock) -> None:
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    method = str(msg.get("method") or "")
    if method == "fetch_images":
        started = time.perf_counter()
        ok = True
        try:
            await stream_fetch_images(ws, msg, lock)
        except Exception as exc:
            ok = False
            log(f"command fetch_images {exc}")
            async with lock:
                await ws.send(json.dumps({"id": msg.get("id"), "error": str(exc)}))
        log(
            f"command fetch_images {(time.perf_counter() - started) * 1000:.0f}ms "
            f"{'ok' if ok else 'err'}"
        )
        return
    started, ok = time.perf_counter(), True
    try:
        payload = {"id": msg.get("id"), "result": await execute(binary, method, params)}
    except Exception as exc:
        ok = False
        payload = {"id": msg.get("id"), "error": str(exc)}
    log(f"command {method} {brief(params)} {(time.perf_counter() - started) * 1000:.0f}ms {'ok' if ok else 'err'}")
    async with lock:
        await ws.send(json.dumps(payload))


async def serve(ws, binary: str) -> None:
    send_lock = asyncio.Lock()
    inflight: set[asyncio.Task] = set()

    async def beats() -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_S)
                async with send_lock:
                    await ws.send(json.dumps({"type": "ping"}))
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass

    def finished(task: asyncio.Task) -> None:
        inflight.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log(f"command task failed: {exc}")

    beat = asyncio.create_task(beats())
    try:
        async for raw in ws:
            msg = json.loads(raw)
            if isinstance(msg, dict) and msg.get("event") == "ack":
                ack_id = msg.get("id")
                queue = _ack_queues.get(ack_id) if isinstance(ack_id, int) else None
                if queue is not None:
                    queue.put_nowait(msg)
                continue
            if isinstance(msg, dict) and msg.get("type") != "ping" and "method" in msg:
                # Read the next frame while a tab command is in flight so
                # ego_status can bypass the tab lock. The lock inside execute
                # still runs list, navigate, and read one command at a time.
                task = asyncio.create_task(handle(ws, binary, msg, send_lock))
                inflight.add(task)
                task.add_done_callback(finished)
    finally:
        beat.cancel()
        running = list(inflight)
        for task in running:
            task.cancel()
        await asyncio.gather(beat, *running, return_exceptions=True)


async def run_once(cfg: dict, binary: str) -> bool:
    hello = {
        "type": "hello",
        "token": cfg["token"],
        "device": cfg["device"],
        "capabilities": [FETCH_CAPABILITY],
    }
    async with websockets.connect(
        cfg["relay_url"],
        open_timeout=10,
        ping_interval=WS_PING_INTERVAL_S,
        ping_timeout=WS_PING_TIMEOUT_S,
    ) as ws:
        await ws.send(json.dumps(hello))
        log(f"connected {cfg['relay_url']} device={cfg['device']}")
        try:
            await serve(ws, binary)
        except ConnectionClosed as exc:
            log(f"connection closed code={exc.code}")
            return exc.code != 1008
        return True


async def amain() -> None:
    global LOG_PATH
    cfg = load_config(config_path())
    binary = ego_bin(cfg)
    custom = cfg.get("log_path") or os.environ.get("HERMES_EGO_LOG")
    LOG_PATH = Path(str(custom)).expanduser() if custom else Path.home() / ".local" / "state" / "hermes-ego" / "daemon.log"
    log(f"hermes-ego daemon starting ego={binary}")
    delay = 1
    while True:
        connected = False
        try:
            connected = await run_once(cfg, binary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"connect failed: {exc}")
        if connected:
            delay = 1
        await asyncio.sleep(delay)
        if not connected:
            delay = min(delay * 2, BACKOFF_MAX_S)


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
