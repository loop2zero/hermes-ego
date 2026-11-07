"""Path jail, note names, image rewrite, and verbatim save. No ego lite."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import threading
import time
import unittest
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
for path in (str(ROOT), str(AGENT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from mcp.server.mcpserver.exceptions import ToolError

from relay import server
from relay.save_page import (
    FAILED_IMAGE_MARKER,
    DaemonCapabilityError,
    ImageFetchError,
    SaveError,
    apply_image_results,
    asset_filename,
    check_save_config,
    fetch_http,
    find_http_images,
    is_public_http_url,
    load_zone,
    note_names,
    parse_slug,
    parse_subdir,
    refresh_image_frontmatter,
    resolve_save_root,
    run_backfill,
    save_markdown,
    split_frontmatter,
)

RESULT_KEYS = {
    "path", "chars", "images_ok", "images_failed",
    "image_refs", "image_refs_ok", "image_refs_failed",
    "images_via_daemon", "images_via_relay", "images_pending",
    "failures", "bytes", "truncated",
}
TOOL_KEYS = RESULT_KEYS | {"elapsed_ms"}

WHEN = datetime(2026, 11, 7, 23, 30, tzinfo=timezone.utc)
PAGE_URL = "https://yitang.top/fs-doc/token/demo"
SAVE_URL = "https://yitang.top/fs-doc/token/save-me"


def _page(text: str, **extra) -> dict:
    page = {
        "label": "p1",
        "title": "页标题",
        "url": PAGE_URL,
        "text": text,
        "truncated": False,
    }
    page.update(extra)
    return page


class _ImageHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        host = self.server
        with host.lock:
            host.inflight += 1
            host.max_inflight = max(host.max_inflight, host.inflight)
            host.hits.append(self.path)
        try:
            route = host.routes.get(self.path)
            if route is None:
                status, body, ctype, delay = 404, b"missing", "text/plain", 0.0
            else:
                status, body, ctype, delay = route
            if delay:
                time.sleep(delay)
            self.send_response(status)
            if status in (301, 302, 303, 307, 308):
                self.send_header("Location", body.decode())
                body = b""
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return
        finally:
            with host.lock:
                host.inflight -= 1

    def log_message(self, fmt: str, *args) -> None:
        return


class _ImageServer:
    def __init__(self, routes: dict[str, tuple[int, bytes, str, float]]) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ImageHandler)
        self.httpd.routes = routes
        self.httpd.hits = []
        self.httpd.inflight = 0
        self.httpd.max_inflight = 0
        self.httpd.lock = threading.Lock()
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


class SaveCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(self._tmpdir())
        self.root = self.tmp / "notes"
        self.root.mkdir()
        self._env = mock.patch.dict(os.environ, {"EGO_SAVE_ROOT": str(self.root), "EGO_SAVE_TZ": "UTC"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._agent = server.agent
        server.agent = {"ws": None, "device": "unit-mac", "send_lock": None, "connectedAt": "x"}
        server._tab_gate = None
        self.addCleanup(self._restore_server)

    def _restore_server(self) -> None:
        server.agent = self._agent
        server._tab_gate = None

    def _tmpdir(self) -> str:
        import tempfile
        path = tempfile.mkdtemp(prefix="hermes-ego-save-")
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        return path

    async def _save(self, text: str = "hello", **kwargs):
        args = {
            "device": "unit-mac",
            "subdir": ".",
            "slug": "lesson",
            "title": None,
            "mode": "auto",
            "now": WHEN,
        }
        args.update(kwargs)
        return await save_markdown(_page(text), **args)

    def test_root_must_be_an_absolute_directory(self) -> None:
        os.environ.pop("EGO_SAVE_ROOT", None)
        with self.assertRaises(SaveError) as ctx:
            resolve_save_root()
        self.assertIn("disabled", str(ctx.exception))
        self.assertIn("EGO_SAVE_ROOT is not set", str(ctx.exception))

        os.environ["EGO_SAVE_ROOT"] = "notes"
        with self.assertRaises(SaveError) as missing_abs:
            resolve_save_root()
        self.assertIn("absolute", str(missing_abs.exception))

        os.environ["EGO_SAVE_ROOT"] = str(self.tmp / "missing")
        with self.assertRaises(SaveError) as missing:
            resolve_save_root()
        self.assertIn("does not exist", str(missing.exception))

        file_root = self.tmp / "not-a-dir"
        file_root.write_text("x", encoding="utf-8")
        os.environ["EGO_SAVE_ROOT"] = str(file_root)
        with self.assertRaises(SaveError) as not_dir:
            resolve_save_root()
        self.assertIn("not a directory", str(not_dir.exception))

    def test_subdir_and_slug_reject_escapes(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        for bad in ("/etc", "../etc", "foo/../../etc", "foo/../outside", "a\\b", "a/\x00b"):
            with self.assertRaises(SaveError):
                parse_subdir(bad)
        self.assertEqual(parse_subdir("."), Path())
        self.assertEqual(parse_subdir(""), Path())
        self.assertEqual(parse_subdir("lessons/2026"), Path("lessons/2026"))
        for bad in ("", "..", ".", "a/b", "a\\b", "-leading", "a" * 65, "slug.md"):
            with self.assertRaises(SaveError):
                parse_slug(bad)
        self.assertEqual(parse_slug("五步法"), "五步法")
        self.assertEqual(parse_slug("a" * 64), "a" * 64)
        self.assertFalse(any(outside.iterdir()))

    async def test_symlink_escape_is_refused(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        jump = self.root / "jump"
        jump.symlink_to(outside)
        with self.assertRaises(SaveError) as ctx:
            await self._save(subdir="jump")
        self.assertIn("symlink", str(ctx.exception))
        self.assertEqual(list(outside.iterdir()), [])

        nested = self.root / "real"
        nested.mkdir()
        (nested / "out").symlink_to(outside)
        with self.assertRaises(SaveError):
            await self._save(subdir="real/out/deeper")
        self.assertEqual(list(outside.iterdir()), [])

        inside = self.root / "inside"
        inside.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(inside)
        with self.assertRaises(SaveError):
            await self._save(subdir="alias")
        self.assertEqual(list(inside.iterdir()), [])

    async def test_save_root_symlink_is_the_jail(self) -> None:
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "link"
        link.symlink_to(real)
        os.environ["EGO_SAVE_ROOT"] = str(link)
        meta = await self._save(text="inside")
        path = Path(meta["path"])
        self.assertTrue(path.is_relative_to(real.resolve()))
        self.assertFalse(path.is_symlink())
        self.assertEqual(split_frontmatter(path.read_text(encoding="utf-8"))[1], "inside")

    async def test_filename_uses_the_configured_zone_and_never_overwrites(self) -> None:
        os.environ["EGO_SAVE_TZ"] = "Asia/Tokyo"
        first = await self._save(text="ONE", slug="五步法", title='say "hello"')
        second = await self._save(text="TWO", slug="五步法", title="second")
        self.assertTrue(first["path"].endswith("/2026-11-08-五步法.md"), first["path"])
        self.assertTrue(second["path"].endswith("/2026-11-08-五步法-2.md"), second["path"])
        first_body = split_frontmatter(Path(first["path"]).read_text(encoding="utf-8"))
        second_body = split_frontmatter(Path(second["path"]).read_text(encoding="utf-8"))
        self.assertEqual(first_body[1], "ONE")
        self.assertEqual(second_body[1], "TWO")
        self.assertEqual(json.loads(first_body[0]["captured_at"]), "2026-11-08T08:30:00+09:00")
        self.assertEqual(json.loads(first_body[0]["title"]), 'say "hello"')
        self.assertEqual(json.loads(first_body[0]["device"]), "unit-mac")
        self.assertEqual(json.loads(first_body[0]["source_url"]), PAGE_URL)
        self.assertEqual(json.loads(first_body[0]["mode"]), "auto")
        self.assertEqual(first_body[0]["chars"], "3")
        self.assertEqual(first_body[0]["images"], "0")
        self.assertEqual(first_body[0]["truncated"], "false")
        raw = Path(first["path"]).read_bytes()
        self.assertTrue(raw.endswith(b"ONE"))
        self.assertFalse(raw.endswith(b"ONE\n"))

    async def test_occupied_symlink_is_skipped_not_followed(self) -> None:
        os.environ["EGO_SAVE_TZ"] = "UTC"
        when = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        meta = await self._save(text="kept", now=when, slug="note")
        path = Path(meta["path"])
        outside = self.tmp / "outside"
        outside.mkdir()
        target = outside / "pwned.md"
        path.unlink()
        path.symlink_to(target)
        again = await self._save(text="next", now=when, slug="note")
        self.assertTrue(again["path"].endswith("-2.md"), again["path"])
        self.assertFalse(target.exists())
        self.assertEqual(Path(again["path"]).read_text(encoding="utf-8").endswith("next"), True)
        self.assertFalse(Path(again["path"]).is_symlink())

    async def test_name_limit_does_not_clobber(self) -> None:
        when = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        names = note_names("2026-11-07", "full")
        self.assertEqual(len(names), 100)
        self.assertEqual(names[0], "2026-11-07-full.md")
        self.assertEqual(names[-1], "2026-11-07-full-100.md")
        for name in names:
            (self.root / name).write_text("keep", encoding="utf-8")
        with self.assertRaises(SaveError) as ctx:
            await self._save(text="new", slug="full", now=when)
        self.assertIn("another slug", str(ctx.exception))
        self.assertEqual((self.root / "2026-11-07-full.md").read_text(encoding="utf-8"), "keep")
        self.assertEqual((self.root / "2026-11-07-full-100.md").read_text(encoding="utf-8"), "keep")
        self.assertFalse((self.root / "2026-11-07-full-101.md").exists())

    def test_bad_timezone(self) -> None:
        os.environ["EGO_SAVE_TZ"] = "Not/AZone"
        with self.assertRaises(SaveError) as ctx:
            load_zone()
        self.assertIn("EGO_SAVE_TZ", str(ctx.exception))

    def test_link_rewrite_touches_only_the_url(self) -> None:
        fenced = "```\n![image](https://cdn.example/hidden.png)\n```\n"
        text = (
            '前 ![image](<https://cdn.example/a.png> "cap") 中\n'
            + fenced
            + "![image](boxcnToken123)\n"
            + "![image](https://cdn.example/missing.png)"
        )
        hits = find_http_images(text)
        self.assertEqual([hit.url for hit in hits], [
            "https://cdn.example/a.png",
            "https://cdn.example/missing.png",
        ])
        rewritten, ok, failed = apply_image_results(text, hits, {
            "https://cdn.example/a.png": "assets/note/001-a.png",
        })
        self.assertEqual(ok, 1)
        self.assertEqual(failed, 1)
        self.assertEqual(
            rewritten,
            '前 ![image](assets/note/001-a.png "cap") 中\n'
            + fenced
            + "![image](boxcnToken123)\n"
            + "![image](https://cdn.example/missing.png)"
            + FAILED_IMAGE_MARKER,
        )
        self.assertEqual(asset_filename(1, "https://cdn.yitang.top/a/ok.png", "image/png"), "001-ok.png")
        self.assertEqual(asset_filename(2, "https://cdn.example/x.exe", "image/jpeg"), "002-x.jpg")

    def test_public_url_check_rejects_local_and_private_targets(self) -> None:
        self.assertFalse(is_public_http_url("http://127.0.0.1/a.png"))
        self.assertFalse(is_public_http_url("http://10.1.2.3/a.png"))
        self.assertFalse(is_public_http_url("http://192.168.1.9/a.png"))
        self.assertFalse(is_public_http_url("http://172.16.0.1/a.png"))
        self.assertFalse(is_public_http_url("http://169.254.169.254/latest"))
        self.assertFalse(is_public_http_url("http://[::1]/a.png"))
        self.assertFalse(is_public_http_url("http://user:pass@8.8.8.8/a.png"))
        self.assertFalse(is_public_http_url("file:///etc/passwd"))
        self.assertFalse(is_public_http_url("http://localhost/a.png"))
        self.assertTrue(is_public_http_url("http://8.8.8.8/a.png"))
        self.assertTrue(is_public_http_url("https://1.1.1.1/a.png"))
        self.assertFalse(is_public_http_url(urllib.parse.urljoin(
            "http://8.8.8.8/a.png", "http://127.0.0.1/secret",
        )))

    async def test_partial_image_failure_keeps_the_note(self) -> None:
        png = b"\x89PNG\r\n\x1a\nhello"
        server_http = _ImageServer({
            "/ok.png": (200, png, "image/png", 0.0),
            "/page.html": (200, b"<html>nope</html>", "text/html", 0.0),
        })
        self.addCleanup(server_http.close)
        base = server_http.base
        original = f"前 ![image]({base}/ok.png) 中 ![image]({base}/missing.png) 后 ![image]({base}/page.html) 尾"
        calls: list[str] = []

        def fetch(url: str) -> tuple[bytes, str]:
            calls.append(url)
            return fetch_http(url, connect_timeout=2, read_timeout=2, check_public=False)

        meta = await self._save(text=original, fetch=fetch, slug="partial", subdir="lessons")
        self.assertEqual(meta["images_ok"], 1)
        self.assertEqual(meta["images_failed"], 2)
        self.assertEqual(meta["image_refs"], 3)
        self.assertEqual(meta["failures"], {"http_404": 1, "not_image": 1})
        self.assertEqual(meta["chars"], len(original))
        self.assertNotIn("text", meta)
        note = Path(meta["path"])
        fields, body = split_frontmatter(note.read_text(encoding="utf-8"))
        rel = f"assets/{note.stem}/001-ok.png"
        expected = (
            f"前 ![image]({rel}) 中 ![image]({base}/missing.png){FAILED_IMAGE_MARKER} "
            f"后 ![image]({base}/page.html){FAILED_IMAGE_MARKER} 尾"
        )
        self.assertEqual(body, expected)
        raw = note.read_bytes()
        self.assertEqual(raw, raw[: len(raw) - len(expected.encode())] + expected.encode())
        self.assertEqual(fields["images"], "3")
        self.assertEqual(fields["images_ok"], "1")
        self.assertEqual(fields["images_failed"], "2")
        self.assertEqual(fields["image_refs"], "3")
        stored = note.parent / "assets" / note.stem / "001-ok.png"
        self.assertEqual(stored.read_bytes(), png)
        self.assertEqual(list((note.parent / "assets" / note.stem).iterdir()), [stored])
        self.assertEqual(meta["bytes"], note.stat().st_size + stored.stat().st_size)
        self.assertEqual(calls, [f"{base}/ok.png", f"{base}/missing.png", f"{base}/page.html"])
        self.assertIn("/ok.png", server_http.httpd.hits)
        self.assertIn("/missing.png", server_http.httpd.hits)
        self.assertNotIn("<html>", note.read_text(encoding="utf-8"))

    async def test_duplicate_urls_download_once(self) -> None:
        png = b"one-png"
        server_http = _ImageServer({"/a.png": (200, png, "image/png", 0.0)})
        self.addCleanup(server_http.close)
        url = f"{server_http.base}/a.png"
        original = f"![image]({url})\n![image]({url})"

        def fetch(image_url: str) -> tuple[bytes, str]:
            return fetch_http(image_url, connect_timeout=2, read_timeout=2, check_public=False)

        meta = await self._save(text=original, fetch=fetch, slug="dup")
        self.assertEqual(meta["images_ok"], 1)
        self.assertEqual(meta["images_failed"], 0)
        self.assertEqual(meta["image_refs_ok"], 2)
        self.assertEqual(meta["image_refs"], 2)
        self.assertEqual(meta["failures"], {})
        self.assertEqual(server_http.httpd.hits, ["/a.png"])
        note = Path(meta["path"])
        files = list((note.parent / "assets" / note.stem).iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), png)

    async def test_per_image_timeout_does_not_fail_the_save(self) -> None:
        png = b"fast"
        server_http = _ImageServer({
            "/fast.png": (200, png, "image/png", 0.0),
            "/slow.png": (200, b"slow", "image/png", 0.8),
        })
        self.addCleanup(server_http.close)
        base = server_http.base
        original = f"A ![image]({base}/fast.png) B ![image]({base}/slow.png) C"

        def fetch(image_url: str) -> tuple[bytes, str]:
            return fetch_http(image_url, connect_timeout=0.5, read_timeout=0.2, check_public=False)

        started = time.perf_counter()
        log = io.StringIO()
        with mock.patch("sys.stdout", log):
            meta = await self._save(
                text=original, fetch=fetch, slug="timed",
                connect_timeout=0.5, read_timeout=0.2, image_attempts=3, image_backoff=0,
            )
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.5)
        self.assertEqual(meta["images_ok"], 1)
        self.assertEqual(meta["images_failed"], 1)
        self.assertEqual(meta["failures"], {"timeout_read": 1})
        self.assertNotIn(base, json.dumps(meta["failures"]))
        logged = log.getvalue()
        self.assertIn("reason=timeout_read", logged)
        self.assertIn("save image failed", logged)
        self.assertEqual(logged.count("save image failed"), 1)
        self.assertIn("save image retry", logged)
        body = split_frontmatter(Path(meta["path"]).read_text(encoding="utf-8"))[1]
        self.assertIn(f"![image]({base}/slow.png){FAILED_IMAGE_MARKER}", body)
        self.assertTrue(body.startswith("A ![image](assets/"))
        self.assertTrue(body.endswith(" C"))

    async def test_image_downloads_run_concurrently(self) -> None:
        server_http = _ImageServer({
            "/one.png": (200, b"1", "image/png", 0.35),
            "/two.png": (200, b"2", "image/png", 0.35),
        })
        self.addCleanup(server_http.close)
        base = server_http.base
        original = f"![image]({base}/one.png) ![image]({base}/two.png)"

        def fetch(image_url: str) -> tuple[bytes, str]:
            return fetch_http(image_url, connect_timeout=2, read_timeout=2, check_public=False)

        started = time.perf_counter()
        meta = await self._save(
            text=original, fetch=fetch, slug="parallel", concurrency=2, read_timeout=2,
        )
        elapsed = time.perf_counter() - started
        self.assertEqual(meta["images_ok"], 2)
        self.assertGreaterEqual(server_http.httpd.max_inflight, 2)
        self.assertLess(elapsed, 0.6)

    async def test_private_image_is_not_fetched(self) -> None:
        server_http = _ImageServer({"/ok.png": (200, b"png", "image/png", 0.0)})
        self.addCleanup(server_http.close)
        original = f"前 ![image]({server_http.base}/ok.png) 后"
        meta = await self._save(text=original, slug="local")
        self.assertEqual(server_http.httpd.hits, [])
        self.assertEqual(meta["images_ok"], 0)
        self.assertEqual(meta["images_failed"], 1)
        self.assertEqual(meta["failures"], {"blocked": 1})
        body = split_frontmatter(Path(meta["path"]).read_text(encoding="utf-8"))[1]
        self.assertEqual(body, original.replace(
            f"![image]({server_http.base}/ok.png)",
            f"![image]({server_http.base}/ok.png){FAILED_IMAGE_MARKER}",
            1,
        ))

    async def test_image_cap_leaves_the_rest_marked(self) -> None:
        seen: list[str] = []

        def fetch(url: str) -> tuple[bytes, str]:
            seen.append(url)
            return b"png", "image/png"

        text = "![image](https://cdn.example/a.png) ![image](https://cdn.example/b.png)"
        meta = await self._save(text=text, fetch=fetch, slug="cap", max_images=1)
        self.assertEqual(seen, ["https://cdn.example/a.png"])
        self.assertEqual(meta["images_ok"], 1)
        self.assertEqual(meta["images_failed"], 1)
        self.assertEqual(meta["failures"], {"capped": 1})
        body = split_frontmatter(Path(meta["path"]).read_text(encoding="utf-8"))[1]
        self.assertIn("![image](https://cdn.example/b.png)" + FAILED_IMAGE_MARKER, body)

    def test_oversize_image_is_rejected_by_the_fetcher(self) -> None:
        server_http = _ImageServer({"/big.png": (200, b"0123456789", "image/png", 0.0)})
        self.addCleanup(server_http.close)
        with self.assertRaises(ImageFetchError) as ctx:
            fetch_http(
                f"{server_http.base}/big.png",
                connect_timeout=2, read_timeout=2, check_public=False, max_bytes=4,
            )
        self.assertIsInstance(ctx.exception, ImageFetchError)
        self.assertEqual(ctx.exception.reason, "too_large")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(server_http.httpd.hits, ["/big.png"])

    async def test_saved_body_is_get_page_text_plus_frontmatter_and_image_edits(self) -> None:
        original = (
            "课前\n\n"
            '![image](https://cdn.yitang.top/ok.png "封面")\n\n'
            "中文不变 ![image](https://cdn.yitang.top/missing.png) 后\n"
            "```\n"
            "![image](https://cdn.yitang.top/hidden.png)\n"
            "```\n"
            "![image](boxcnToken123)\n"
            "尾"
        )
        fetched: list[str] = []
        calls: list[tuple[str, dict]] = []

        def fake_fetch(url: str, **_kwargs: object) -> tuple[bytes, str]:
            fetched.append(url)
            if url.endswith("/ok.png"):
                return b"\x89PNG\r\nOK", "image/png"
            raise ImageFetchError("http_404", retryable=False)

        async def fake_call(method: str, params: dict | None = None, timeout: float = 60):
            calls.append((method, params or {}))
            if method == "navigate":
                return {"label": "p1", "title": "页", "url": params["url"], "spaceId": 7}
            if method == "get_page_text":
                return {
                    "label": "p1",
                    "title": "页标题",
                    "url": PAGE_URL,
                    "text": original,
                    "truncated": False,
                }
            raise AssertionError(method)

        with mock.patch("relay.save_page.fetch_http", fake_fetch), mock.patch("relay.server.call", fake_call):
            raw = await server.ego_save_page_markdown(
                subdir="lessons",
                slug="verbatim",
                url=SAVE_URL,
                title="笔记",
                mode="yitang",
            )
        self.assertNotIn("课前", raw)
        self.assertNotIn("中文不变", raw)
        self.assertNotIn("cdn.yitang.top", raw)
        self.assertNotIn("尾", raw)
        meta = json.loads(raw)
        self.assertEqual(set(meta), TOOL_KEYS)
        self.assertEqual(meta["failures"], {"http_404": 1})
        self.assertEqual(meta["image_refs"], 2)
        self.assertEqual(meta["image_refs_ok"], 1)
        self.assertEqual(meta["image_refs_failed"], 1)
        self.assertEqual(meta["chars"], len(original))
        self.assertEqual(meta["images_ok"], 1)
        self.assertEqual(meta["images_failed"], 1)
        self.assertIs(meta["truncated"], False)
        self.assertGreaterEqual(meta["elapsed_ms"], 0)
        self.assertEqual(calls, [
            ("navigate", {"url": SAVE_URL}),
            ("get_page_text", {"mode": "yitang"}),
        ])
        self.assertEqual(fetched, [
            "https://cdn.yitang.top/ok.png",
            "https://cdn.yitang.top/missing.png",
        ])
        note = Path(meta["path"])
        self.assertTrue(note.is_relative_to(self.root.resolve()))
        fields, body = split_frontmatter(note.read_text(encoding="utf-8"))
        rel = f"assets/{note.stem}/001-ok.png"
        expected = original.replace("https://cdn.yitang.top/ok.png", rel, 1).replace(
            "![image](https://cdn.yitang.top/missing.png)",
            "![image](https://cdn.yitang.top/missing.png)" + FAILED_IMAGE_MARKER,
            1,
        )
        self.assertEqual(body, expected)
        file_bytes = note.read_bytes()
        self.assertEqual(file_bytes[len(file_bytes) - len(expected.encode()):], expected.encode())
        self.assertEqual(json.loads(fields["title"]), "笔记")
        self.assertEqual(json.loads(fields["mode"]), "yitang")
        self.assertEqual(fields["chars"], str(len(original)))
        self.assertEqual(fields["images"], "2")
        asset = note.parent / rel
        self.assertEqual(asset.read_bytes(), b"\x89PNG\r\nOK")
        self.assertEqual(meta["bytes"], note.stat().st_size + asset.stat().st_size)
        self.assertIn("```\n![image](https://cdn.yitang.top/hidden.png)\n```", body)
        self.assertIn("![image](boxcnToken123)", body)

    async def test_no_image_body_is_byte_identical(self) -> None:
        original = "---\nno trailing newline"
        calls: list[str] = []

        async def fake_call(method: str, params: dict | None = None, timeout: float = 60):
            calls.append(method)
            if method == "get_page_text":
                return {"label": "p1", "title": "", "url": "https://example.com/", "text": original, "truncated": True}
            raise AssertionError(method)

        with mock.patch("relay.server.call", fake_call):
            raw = await server.ego_save_page_markdown(subdir=".", slug="plain", label="p1")
        self.assertNotIn("no trailing newline", raw)
        self.assertEqual(calls, ["get_page_text"])
        meta = json.loads(raw)
        note = Path(meta["path"])
        file_bytes = note.read_bytes()
        self.assertTrue(file_bytes.endswith(original.encode()))
        _fields, body = split_frontmatter(file_bytes.decode())
        self.assertEqual(body.encode(), original.encode())
        self.assertEqual(meta["chars"], len(original))
        self.assertEqual(meta["images_ok"], 0)
        self.assertEqual(meta["images_failed"], 0)
        self.assertEqual(meta["image_refs"], 0)
        self.assertEqual(meta["failures"], {})
        self.assertEqual(set(meta), TOOL_KEYS)
        self.assertIs(meta["truncated"], True)
        fields = split_frontmatter(file_bytes.decode())[0]
        self.assertEqual(json.loads(fields["title"]), "plain")
        self.assertEqual(json.loads(fields["mode"]), "auto")

    async def test_bad_arguments_do_not_touch_the_daemon(self) -> None:
        async def fake_call(method: str, params: dict | None = None, timeout: float = 60):
            raise AssertionError(f"unexpected {method}")

        with mock.patch("relay.server.call", fake_call):
            with self.assertRaises(ToolError) as both:
                await server.ego_save_page_markdown(
                    subdir=".", slug="x", url=SAVE_URL, label="p1",
                )
            self.assertIn("not both", str(both.exception))
            with self.assertRaises(ToolError) as bad_url:
                await server.ego_save_page_markdown(subdir=".", slug="x", url="javascript:alert(1)")
            self.assertIn("http", str(bad_url.exception).lower())
            with self.assertRaises(ToolError) as escape:
                await server.ego_save_page_markdown(subdir="../etc", slug="x")
            self.assertIn("EGO_SAVE_ROOT", str(escape.exception))
            os.environ.pop("EGO_SAVE_ROOT", None)
            with self.assertRaises(ToolError) as disabled:
                await server.ego_save_page_markdown(subdir=".", slug="x", url=SAVE_URL)
            self.assertIn("disabled", str(disabled.exception))
        self.assertEqual(list(self.root.rglob("*")), [])

    async def test_save_holds_the_gate_across_navigate_and_read(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        async def fake_call(method: str, params: dict | None = None, timeout: float = 60):
            order.append(method if params is None or params.get("url") != "https://evil.example/x" else "other")
            if method == "navigate" and (params or {}).get("url") == SAVE_URL:
                entered.set()
                await release.wait()
                return {"label": "p1", "title": "t", "url": SAVE_URL, "spaceId": 1}
            if method == "navigate":
                return {"label": "p1", "title": "t", "url": (params or {}).get("url"), "spaceId": 1}
            if method == "get_page_text":
                return {"label": "p1", "title": "t", "url": SAVE_URL, "text": "ONLY-BODY", "truncated": False}
            if method == "status":
                return {"egoVersion": "0.6.2.8", "egoReachable": True, "egoError": None}
            raise AssertionError(method)

        with mock.patch("relay.server.call", fake_call):
            save_task = asyncio.create_task(server.ego_save_page_markdown(
                subdir=".", slug="atomic", url=SAVE_URL,
            ))
            await asyncio.wait_for(entered.wait(), 1)
            other = asyncio.create_task(server.ego_navigate("https://evil.example/x"))
            await asyncio.sleep(0.05)
            self.assertEqual(order, ["navigate"])
            self.assertFalse(other.done())
            started = time.perf_counter()
            status = json.loads(await asyncio.wait_for(server.ego_status(), 0.5))
            self.assertLess(time.perf_counter() - started, 0.5)
            self.assertIs(status["online"], True)
            self.assertEqual(status["egoVersion"], "0.6.2.8")
            self.assertFalse(save_task.done())
            release.set()
            raw = await save_task
            await other
        self.assertEqual(order, ["navigate", "status", "get_page_text", "other"])
        self.assertNotIn("ONLY-BODY", raw)
        self.assertEqual(split_frontmatter(Path(json.loads(raw)["path"]).read_text(encoding="utf-8"))[1], "ONLY-BODY")

    async def test_get_page_text_without_url_only_reads(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def fake_call(method: str, params: dict | None = None, timeout: float = 60):
            calls.append((method, params or {}))
            return {
                "label": "p1", "title": "t", "url": "https://example.com/",
                "text": "BODY", "truncated": False,
            }

        with mock.patch("relay.server.call", fake_call):
            raw = await server.ego_get_page_text(label="p1", mode="generic")
        self.assertEqual(calls, [("get_page_text", {"label": "p1", "mode": "generic"})])
        self.assertEqual(json.loads(raw)["text"], "BODY")

    async def test_get_page_text_rejects_url_with_label_and_a_bad_url(self) -> None:
        async def fake_call(method: str, params: dict | None = None, timeout: float = 60):
            raise AssertionError(f"unexpected {method}")

        with mock.patch("relay.server.call", fake_call):
            with self.assertRaises(ToolError) as both:
                await server.ego_get_page_text(url=SAVE_URL, label="p1")
            self.assertIn("not both", str(both.exception))
            with self.assertRaises(ToolError) as bad_url:
                await server.ego_get_page_text(url="javascript:alert(1)")
            self.assertIn("http", str(bad_url.exception).lower())
            with self.assertRaises(ToolError) as bad_mode:
                await server.ego_get_page_text(mode="click")
            self.assertIn("mode", str(bad_mode.exception))

    async def test_separate_navigate_then_read_can_cross_pages(self) -> None:
        """Two MCP calls still release the gate between navigate and read."""
        example = "https://example.com/"
        hacker = "https://news.ycombinator.com/"
        page = {"url": None}
        started = asyncio.Event()

        async def fake_call(method: str, params: dict | None = None, timeout: float = 60):
            params = params or {}
            if method == "navigate":
                page["url"] = params["url"]
                if params["url"] == example and not started.is_set():
                    started.set()
                    await asyncio.sleep(0.05)
                return {"label": "p1", "title": "t", "url": params["url"], "spaceId": 1}
            if method == "get_page_text":
                return {
                    "label": "p1", "title": "t", "url": page["url"],
                    "text": f"BODY {page['url']}", "truncated": False,
                }
            raise AssertionError(method)

        async def pair(url: str) -> str:
            await server.ego_navigate(url)
            return json.loads(await server.ego_get_page_text())["url"]

        with mock.patch("relay.server.call", fake_call):
            first = asyncio.create_task(pair(example))
            await asyncio.wait_for(started.wait(), 1)
            second = asyncio.create_task(pair(hacker))
            await asyncio.sleep(0)
            urls = await asyncio.gather(first, second)
        self.assertEqual(urls, [hacker, hacker])

    async def test_concurrent_open_and_read_calls_do_not_cross_pages(self) -> None:
        """The two-call race above, closed by ego_get_page_text(url).

        example.com's navigate publishes its url and then waits, so the other
        caller is queued on the gate. Releasing the gate before the read would
        let the Hacker News open-and-read finish first and both reads would
        return that page.
        """
        example = "https://example.com/"
        hacker = "https://news.ycombinator.com/"
        evil = "https://evil.example/crossed"
        page = {"url": None}
        order: list[object] = []
        started = asyncio.Event()

        async def fake_call(method: str, params: dict | None = None, timeout: float = 60):
            params = params or {}
            if method == "status":
                order.append("status")
                return {"egoVersion": "0.6.2.8", "egoReachable": True, "egoError": None}
            if method == "navigate":
                url = params["url"]
                order.append(("navigate", url))
                page["url"] = url
                if url == example and not started.is_set():
                    started.set()
                    await asyncio.sleep(0.05)
                return {"label": "p1", "title": "t", "url": url, "spaceId": 1}
            if method == "get_page_text":
                url = page["url"]
                order.append(("read", url))
                return {
                    "label": "p1", "title": "t", "url": url,
                    "text": f"BODY {url}", "truncated": False,
                }
            raise AssertionError(method)

        with mock.patch("relay.server.call", fake_call):
            first = asyncio.create_task(server.ego_get_page_text(url=example))
            await asyncio.wait_for(started.wait(), 1)
            second = asyncio.create_task(server.ego_get_page_text(url=hacker))
            other = asyncio.create_task(server.ego_navigate(evil))
            await asyncio.sleep(0)
            self.assertEqual(order, [("navigate", example)])
            self.assertFalse(second.done())
            self.assertFalse(other.done())
            started_at = time.perf_counter()
            status = json.loads(await asyncio.wait_for(server.ego_status(), 0.5))
            self.assertLess(time.perf_counter() - started_at, 0.5)
            self.assertIs(status["online"], True)
            self.assertEqual(status["egoVersion"], "0.6.2.8")
            self.assertFalse(first.done())
            raw_first, raw_second = await asyncio.gather(first, second)
            raw_other = await other
        self.assertEqual(json.loads(raw_first)["url"], example)
        self.assertEqual(json.loads(raw_first)["text"], f"BODY {example}")
        self.assertEqual(json.loads(raw_second)["url"], hacker)
        self.assertEqual(json.loads(raw_second)["text"], f"BODY {hacker}")
        self.assertEqual(json.loads(raw_other)["url"], evil)
        self.assertEqual(order, [
            ("navigate", example),
            "status",
            ("read", example),
            ("navigate", hacker),
            ("read", hacker),
            ("navigate", evil),
        ])

    def test_check_save_config_creates_the_directory(self) -> None:
        path = check_save_config("lessons/2026", "slug")
        self.assertEqual(path, self.root / "lessons" / "2026")
        self.assertTrue(path.is_dir())
        self.assertFalse(path.is_symlink())
        root_stat = self.root.stat()
        for directory in (self.root / "lessons", path):
            st = directory.stat()
            self.assertEqual((st.st_uid, st.st_gid), (root_stat.st_uid, root_stat.st_gid))
            self.assertEqual(st.st_mode & 0o777, 0o755)

    async def test_retry_then_success_and_a_hard_failure_is_once(self) -> None:
        counts: dict[str, int] = {}

        def fetch(url: str) -> tuple[bytes, str]:
            counts[url] = counts.get(url, 0) + 1
            if url.endswith("/flaky.png") and counts[url] < 3:
                raise ImageFetchError("timeout_read", retryable=True)
            if url.endswith("/gone.png"):
                raise ImageFetchError("http_404", retryable=False)
            return b"png", "image/png"

        text = "![image](https://cdn.example/flaky.png) ![image](https://cdn.example/gone.png)"
        meta = await self._save(
            text=text, fetch=fetch, slug="retry", image_attempts=3, image_backoff=0,
        )
        self.assertEqual(counts["https://cdn.example/flaky.png"], 3)
        self.assertEqual(counts["https://cdn.example/gone.png"], 1)
        self.assertEqual(meta["images_ok"], 1)
        self.assertEqual(meta["images_failed"], 1)
        self.assertEqual(meta["failures"], {"http_404": 1})
        self.assertNotIn("cdn.example", json.dumps(meta["failures"]))

    async def test_duplicate_failure_counts_as_one_url(self) -> None:
        def fetch(url: str) -> tuple[bytes, str]:
            raise ImageFetchError("http_503", retryable=False)

        text = "![image](https://cdn.example/a.png) ![image](https://cdn.example/a.png)"
        meta = await self._save(text=text, fetch=fetch, slug="dupfail", image_attempts=3, image_backoff=0)
        self.assertEqual(meta["images_ok"], 0)
        self.assertEqual(meta["images_failed"], 1)
        self.assertEqual(meta["image_refs_failed"], 2)
        self.assertEqual(meta["failures"], {"http_503": 1})

    def test_connect_and_read_timeouts_are_distinct(self) -> None:
        class ConnectHang:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.sock = None

            def connect(self) -> None:
                raise TimeoutError("connect")

            def close(self) -> None:
                return None

        with mock.patch("image_fetch.http.client.HTTPConnection", ConnectHang):
            with self.assertRaises(ImageFetchError) as connect_ctx:
                fetch_http("http://8.8.8.8/a.png", connect_timeout=0.2, read_timeout=0.2)
        self.assertEqual(connect_ctx.exception.reason, "timeout_connect")
        self.assertTrue(connect_ctx.exception.retryable)

        class ReadHang:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.sock = mock.Mock()

            def connect(self) -> None:
                return None

            def request(self, *_args: object, **_kwargs: object) -> None:
                raise TimeoutError("read")

            def close(self) -> None:
                return None

        with mock.patch("image_fetch.http.client.HTTPConnection", ReadHang):
            with self.assertRaises(ImageFetchError) as read_ctx:
                fetch_http("http://1.1.1.1/a.png", connect_timeout=0.2, read_timeout=0.2)
        self.assertEqual(read_ctx.exception.reason, "timeout_read")

    async def test_redirect_stays_on_one_response_chain(self) -> None:
        png = b"redirected"
        server_http = _ImageServer({
            "/go": (302, b"/ok.png", "text/plain", 0.0),
            "/ok.png": (200, png, "image/png", 0.0),
            "/bad": (302, b"http://127.0.0.1/secret", "text/plain", 0.0),
        })
        self.addCleanup(server_http.close)
        body, ctype = fetch_http(
            f"{server_http.base}/go", connect_timeout=2, read_timeout=2, check_public=False,
        )
        self.assertEqual(body, png)
        self.assertIn("image/png", ctype)
        self.assertEqual(server_http.httpd.hits, ["/go", "/ok.png"])

    def test_redirect_to_a_private_address_is_not_followed(self) -> None:
        calls: list[str] = []

        def exchange(url: str, *_args: object, **_kwargs: object) -> tuple[int, str, str, bytes]:
            calls.append(url)
            return 302, "", "http://127.0.0.1/secret", b""

        with mock.patch("image_fetch._exchange", exchange):
            with self.assertRaises(ImageFetchError) as ctx:
                fetch_http("http://8.8.8.8/a.png", connect_timeout=1, read_timeout=1)
        self.assertEqual(ctx.exception.reason, "blocked")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(calls, ["http://8.8.8.8/a.png"])

    async def test_created_files_match_the_save_root_owner_and_mode(self) -> None:
        def fetch(url: str) -> tuple[bytes, str]:
            return b"png-bytes", "image/png"

        old = os.umask(0o077)
        try:
            meta = await self._save(
                text="note ![image](https://cdn.example/a.png)",
                fetch=fetch,
                subdir="lessons",
                slug="owned",
            )
        finally:
            os.umask(old)
        root_stat = self.root.stat()
        note = Path(meta["path"])
        asset_dir = note.parent / "assets" / note.stem
        asset = next(asset_dir.iterdir())
        for path, mode in (
            (note, 0o644),
            (asset, 0o644),
            (self.root / "lessons", 0o755),
            (asset_dir.parent, 0o755),
            (asset_dir, 0o755),
        ):
            st = path.stat()
            self.assertEqual(st.st_uid, root_stat.st_uid, path)
            self.assertEqual(st.st_gid, root_stat.st_gid, path)
            self.assertEqual(st.st_mode & 0o777, mode, path)
        self.assertFalse(note.is_symlink())
        self.assertTrue(note.resolve().is_relative_to(self.root.resolve()))

    async def test_existing_directory_is_not_rechowned(self) -> None:
        keep = self.root / "keep"
        keep.mkdir()
        os.chmod(keep, 0o700)
        seen: list[Path] = []
        real = os.lchown

        def spy(path: object, uid: int, gid: int) -> None:
            seen.append(Path(path))
            real(path, uid, gid)

        with mock.patch("relay.save_page.os.lchown", spy):
            await self._save(text="inside", subdir="keep", slug="stay")
        self.assertEqual(keep.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(keep.resolve(), [path.resolve() for path in seen])

    async def test_owner_override_is_requested_without_leaving_the_jail(self) -> None:
        recorded: list[tuple[int, int]] = []

        def fake_fchown(_fd: int, uid: int, gid: int) -> None:
            recorded.append((uid, gid))

        def fake_lchown(path: object, uid: int, gid: int) -> None:
            recorded.append((uid, gid))
            resolved = Path(path).resolve()
            self.assertTrue(resolved.is_relative_to(self.root.resolve()))
            self.assertNotEqual(resolved, self.root.resolve())

        os.environ["EGO_SAVE_UID"] = "10000"
        os.environ["EGO_SAVE_GID"] = "10000"
        with mock.patch("relay.save_page.os.fchown", fake_fchown), mock.patch("relay.save_page.os.lchown", fake_lchown):
            meta = await self._save(text="override", subdir="fresh", slug="uid")
        self.assertIn((10000, 10000), recorded)
        note = Path(meta["path"])
        self.assertTrue(note.is_relative_to(self.root.resolve()))
        self.assertEqual(note.stat().st_mode & 0o777, 0o644)
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.root / "jump").symlink_to(outside)
        with mock.patch("relay.save_page.os.fchown", fake_fchown), mock.patch("relay.save_page.os.lchown", fake_lchown):
            with self.assertRaises(SaveError):
                await self._save(text="nope", subdir="jump", slug="escape")
        self.assertEqual(list(outside.iterdir()), [])

    async def test_bad_owner_id_does_not_touch_the_daemon(self) -> None:
        os.environ["EGO_SAVE_UID"] = "-1"

        async def fake_call(method: str, params: dict | None = None, timeout: float = 60):
            raise AssertionError(method)

        with mock.patch("relay.server.call", fake_call):
            with self.assertRaises(ToolError) as ctx:
                await server.ego_save_page_markdown(subdir=".", slug="x", url=SAVE_URL)
        self.assertIn("EGO_SAVE_UID", str(ctx.exception))
        self.assertEqual(list(self.root.rglob("*.md")), [])

    async def test_auto_falls_back_per_url_and_daemon_mode_does_not(self) -> None:
        mac = "https://cdn.example/a.png"
        slow = "https://cdn.example/b.png"
        seen: list[str] = []

        async def daemon(urls: list[str]):
            self.assertEqual(urls, [mac, slow])
            return ({mac: (b"from-mac", "image/png")}, {slow: "timeout_connect"})

        def relay(url: str) -> tuple[bytes, str]:
            seen.append(url)
            return b"from-relay", "image/png"

        text = f"![image]({mac}) ![image]({slow})"
        meta = await self._save(
            text=text, fetch=relay, daemon_fetch=daemon, image_fetch_mode_name="auto", slug="mix",
        )
        self.assertEqual(seen, [slow])
        self.assertEqual(meta["images_ok"], 2)
        self.assertEqual(meta["images_via_daemon"], 1)
        self.assertEqual(meta["images_via_relay"], 1)
        self.assertEqual(meta["failures"], {})
        note = Path(meta["path"])
        files = sorted((note.parent / "assets" / note.stem).iterdir())
        self.assertEqual([path.read_bytes() for path in files], [b"from-mac", b"from-relay"])

        seen.clear()

        async def unsupported(_urls: list[str]):
            raise DaemonCapabilityError("unknown method 'fetch_images'")

        meta = await self._save(
            text=text, fetch=relay, daemon_fetch=unsupported,
            image_fetch_mode_name="auto", slug="old",
        )
        self.assertEqual(sorted(seen), sorted([mac, slow]))
        self.assertEqual(meta["images_via_daemon"], 0)
        self.assertEqual(meta["images_via_relay"], 2)

        seen.clear()

        async def died(_urls: list[str]):
            raise ConnectionError("websocket closed")

        meta = await self._save(
            text=f"![image]({mac})", fetch=relay, daemon_fetch=died,
            image_fetch_mode_name="auto", slug="dropped",
        )
        self.assertEqual(meta["images_ok"], 1)
        self.assertEqual(meta["images_via_relay"], 1)
        self.assertEqual(meta["images_via_daemon"], 0)
        self.assertTrue(Path(meta["path"]).is_file())

        seen.clear()
        meta = await self._save(
            text=text, fetch=relay, daemon_fetch=None,
            image_fetch_mode_name="daemon", slug="nodeamon",
        )
        self.assertEqual(seen, [])
        self.assertEqual(meta["images_ok"], 0)
        self.assertEqual(meta["images_via_daemon"], 0)
        self.assertEqual(meta["images_via_relay"], 0)
        self.assertEqual(meta["failures"], {"no_daemon": 2})
        body = split_frontmatter(Path(meta["path"]).read_text(encoding="utf-8"))[1]
        self.assertIn(FAILED_IMAGE_MARKER, body)

    async def test_daemon_bytes_cannot_escape_the_save_root(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        url = "https://cdn.example/%2e%2e/%2e%2e/etc/cron.png"

        async def daemon(urls: list[str]):
            self.assertEqual(urls, [url])
            return ({url: (b"PNGDATA", 'image/png; filename="../../evil.png"')}, {})

        meta = await self._save(
            text=f"before ![image]({url}) after",
            daemon_fetch=daemon,
            image_fetch_mode_name="daemon",
            slug="jail",
        )
        note = Path(meta["path"])
        assets = list((note.parent / "assets" / note.stem).rglob("*"))
        files = [path for path in assets if path.is_file()]
        self.assertEqual(len(files), 1)
        saved = files[0]
        self.assertTrue(saved.resolve().is_relative_to(self.root.resolve()))
        self.assertNotIn("..", saved.parts)
        self.assertEqual(saved.read_bytes(), b"PNGDATA")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(meta["images_via_daemon"], 1)
        self.assertNotIn("PNGDATA", json.dumps({k: v for k, v in meta.items() if k != "bytes"}))

        html = "https://cdn.example/page.png"

        async def html_daemon(urls: list[str]):
            return ({html: (b"<html>secret</html>", "text/html")}, {})

        meta = await self._save(
            text=f"![image]({html})",
            daemon_fetch=html_daemon,
            image_fetch_mode_name="daemon",
            slug="html",
        )
        self.assertEqual(meta["failures"], {"not_image": 1})
        self.assertEqual(meta["images_ok"], 0)
        note = Path(meta["path"])
        self.assertFalse((note.parent / "assets" / note.stem).exists())
        self.assertNotIn("<html>", note.read_text(encoding="utf-8"))

    async def test_save_logs_phase_timings(self) -> None:
        async def fake_call(method: str, params: dict | None = None, timeout: float = 60):
            if method == "get_page_text":
                return {
                    "label": "p1", "title": "t", "url": "https://example.com/",
                    "text": "phase-body", "truncated": False,
                }
            raise AssertionError(method)

        with mock.patch("relay.server.call", fake_call):
            log = io.StringIO()
            with mock.patch("sys.stdout", log):
                raw = await server.ego_save_page_markdown(subdir=".", slug="phases")
        self.assertNotIn("phase-body", raw)
        text = log.getvalue()
        self.assertIn("save phases", text)
        for key in ("navigate_ms=", "read_ms=", "images_ms=", "write_ms=", "total_ms="):
            self.assertIn(key, text)
        self.assertNotIn("token", text)

    async def test_image_budget_stops_new_downloads_and_writes_the_note(self) -> None:
        started = {"n": 0}

        def fetch(url: str) -> tuple[bytes, str]:
            started["n"] += 1
            time.sleep(0.25)
            return b"png", "image/png"

        text = " ".join(f"![image](https://cdn.example/{i}.png)" for i in range(6))
        t0 = time.perf_counter()
        meta = await self._save(
            text=text,
            fetch=fetch,
            image_fetch_mode_name="relay",
            image_budget_s=0.3,
            concurrency=1,
            image_attempts=1,
            allow_backfill=True,
            slug="budget",
        )
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 1.2)
        self.assertGreaterEqual(meta["images_ok"], 1)
        self.assertLess(started["n"], 6)
        self.assertGreaterEqual(meta["failures"].get("budget", 0), 1)
        self.assertEqual(meta["images_pending"], meta["images_failed"])
        self.assertEqual(meta["images_via_relay"], meta["images_ok"])
        self.assertEqual(meta["images_via_daemon"], 0)
        note = Path(meta["path"])
        self.assertTrue(note.is_file())
        self.assertIn(FAILED_IMAGE_MARKER, note.read_text(encoding="utf-8"))
        self.assertIsInstance(meta["_backfill"], dict)

    async def test_backfill_rewrites_markers_and_is_idempotent(self) -> None:
        url = "https://cdn.example/later.png"

        def fetch(_url: str) -> tuple[bytes, str]:
            raise ImageFetchError("timeout_connect", retryable=False)

        meta = await self._save(
            text=f"keep ![image]({url}) tail",
            fetch=fetch,
            image_fetch_mode_name="relay",
            image_attempts=1,
            allow_backfill=True,
            slug="later",
        )
        self.assertEqual(meta["images_pending"], 1)
        job = meta["_backfill"]
        calls = {"n": 0}

        async def daemon(urls: list[str]):
            calls["n"] += 1
            self.assertEqual(urls, [url])
            return ({url: (b"MACBYTES", "image/png")}, {})

        await run_backfill(job, daemon)
        note = Path(meta["path"])
        body = note.read_text(encoding="utf-8")
        self.assertNotIn(FAILED_IMAGE_MARKER, body)
        self.assertNotIn(url, body)
        fields, _rest = split_frontmatter(body)
        self.assertEqual(fields["images"], "1")
        self.assertEqual(fields["images_ok"], "1")
        self.assertEqual(fields["images_failed"], "0")
        self.assertIn("assets/", body)
        self.assertIn("keep ", body)
        self.assertIn(" tail", body)
        stored = list((note.parent / "assets" / note.stem).iterdir())
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].read_bytes(), b"MACBYTES")
        self.assertTrue(stored[0].resolve().is_relative_to(self.root.resolve()))
        self.assertFalse(note.with_name(note.name + ".pending.json").exists())
        await run_backfill(job, daemon)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(stored[0].read_bytes(), b"MACBYTES")
        rel = f"assets/{stored[0].parent.name}/{stored[0].name}"
        restored = note.read_text(encoding="utf-8").replace(
            f"]({rel})",
            f"]({url}){FAILED_IMAGE_MARKER}",
            1,
        )
        self.assertIn(FAILED_IMAGE_MARKER, restored)
        note.write_text(restored, encoding="utf-8")
        await run_backfill(job, daemon)
        self.assertEqual(calls["n"], 1)
        self.assertNotIn(FAILED_IMAGE_MARKER, note.read_text(encoding="utf-8"))
        self.assertEqual(stored[0].read_bytes(), b"MACBYTES")

    async def test_backfill_updates_frontmatter_to_match_the_body(self) -> None:
        ok_url = "https://cdn.example/kept.png"
        left_url = "https://cdn.example/later.png"

        def fetch(url: str) -> tuple[bytes, str]:
            if url == ok_url:
                return b"png", "image/png"
            raise ImageFetchError("timeout_connect", retryable=False)

        fenced = "```\n![image](https://cdn.example/fenced.png) **[image download failed]**\n```"
        meta = await self._save(
            text=f"![image]({ok_url})\n![image]({left_url})\n{fenced}\n![image](boxcnToken)",
            fetch=fetch,
            image_fetch_mode_name="relay",
            image_attempts=1,
            allow_backfill=True,
            slug="counts",
        )
        note = Path(meta["path"])
        before, _body = split_frontmatter(note.read_text(encoding="utf-8"))
        self.assertEqual(before["images"], "2")
        self.assertEqual(before["images_ok"], "1")
        self.assertEqual(before["images_failed"], "1")

        async def daemon(urls: list[str]):
            self.assertEqual(urls, [left_url])
            return ({left_url: (b"MAC", "image/png")}, {})

        await run_backfill(meta["_backfill"], daemon)
        fields, body = split_frontmatter(note.read_text(encoding="utf-8"))
        self.assertEqual(fields["images"], "2")
        self.assertEqual(fields["images_ok"], "2")
        self.assertEqual(fields["images_failed"], "0")
        self.assertNotIn(FAILED_IMAGE_MARKER, body.split("```")[0])
        self.assertIn("**[image download failed]**", body)
        self.assertIn("boxcnToken", body)
        self.assertEqual(fields["title"], before["title"])
        self.assertEqual(fields["source_url"], before["source_url"])

    def test_refresh_image_frontmatter_leaves_unrelated_fields(self) -> None:
        text = "\n".join([
            "---",
            'title: "keep me"',
            "images: 1",
            "images_ok: 0",
            "images_failed: 1",
            "truncated: false",
            "---",
            "![image](assets/note/001-a.png)",
            "```",
            "![image](https://cdn.example/nope.png) **[image download failed]**",
            "```",
            "",
        ])
        fields, body = split_frontmatter(refresh_image_frontmatter(text))
        self.assertEqual(fields["title"], '"keep me"')
        self.assertEqual(fields["truncated"], "false")
        self.assertEqual(fields["images"], "1")
        self.assertEqual(fields["images_ok"], "1")
        self.assertEqual(fields["images_failed"], "0")
        self.assertIn("nope.png", body)

    async def test_backfill_rejects_paths_outside_the_jail(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        url = "https://cdn.example/%2e%2e/%2e%2e/etc/cron.png"
        meta = await self._save(
            text=f"![image]({url})",
            fetch=lambda _url: (_ for _ in ()).throw(ImageFetchError("timeout_connect", retryable=False)),
            image_fetch_mode_name="relay",
            image_attempts=1,
            allow_backfill=True,
            slug="bfjail",
        )
        job = meta["_backfill"]

        async def daemon(urls: list[str]):
            return ({urls[0]: (b"PNGDATA", 'image/png; filename="../../evil.png"')}, {})

        await run_backfill(job, daemon)
        note = Path(meta["path"])
        files = [path for path in (note.parent / "assets").rglob("*") if path.is_file()]
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].resolve().is_relative_to(self.root.resolve()))
        self.assertNotIn("..", files[0].parts)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertNotIn(FAILED_IMAGE_MARKER, note.read_text(encoding="utf-8"))

        html = "https://cdn.example/page.png"
        meta = await self._save(
            text=f"![image]({html})",
            fetch=lambda _url: (_ for _ in ()).throw(ImageFetchError("timeout_connect", retryable=False)),
            image_fetch_mode_name="relay",
            image_attempts=1,
            allow_backfill=True,
            slug="bfhtml",
        )

        async def html_daemon(urls: list[str]):
            return ({urls[0]: (b"<html>secret</html>", "text/html")}, {})

        await run_backfill(meta["_backfill"], html_daemon)
        note = Path(meta["path"])
        self.assertIn(FAILED_IMAGE_MARKER, note.read_text(encoding="utf-8"))
        self.assertFalse(any("<html>" in path.read_text(encoding="utf-8", errors="ignore")
                             for path in self.root.rglob("*") if path.is_file() and path.suffix != ".md"))

    async def test_backfill_without_a_daemon_leaves_the_markers(self) -> None:
        url = "https://cdn.example/stay.png"
        meta = await self._save(
            text=f"![image]({url})",
            fetch=lambda _url: (_ for _ in ()).throw(ImageFetchError("timeout_connect", retryable=False)),
            image_fetch_mode_name="relay",
            image_attempts=1,
            allow_backfill=True,
            slug="nodaemon",
        )
        before = Path(meta["path"]).read_bytes()
        await run_backfill(meta["_backfill"], None)
        self.assertEqual(Path(meta["path"]).read_bytes(), before)
        self.assertIn(FAILED_IMAGE_MARKER, before.decode())

    async def test_chown_failure_removes_the_note(self) -> None:
        def boom(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("nope")

        with mock.patch("relay.save_page.os.fchown", boom):
            with self.assertRaises(SaveError) as ctx:
                await self._save(text="x", slug="chown")
        self.assertIn("ownership", str(ctx.exception))
        self.assertEqual(list(self.root.rglob("*.md")), [])


if __name__ == "__main__":
    unittest.main()
