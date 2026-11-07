"""Daemon image streaming. No vault writes, no ego lite."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
for path in (str(ROOT), str(AGENT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from hermes_ego_daemon import stream_fetch_images
from image_fetch import (
    CHUNK_RAW_BYTES,
    DaemonCapabilityError,
    ImageAssembler,
    ImageFetchError,
    fetch_http,
    image_frames,
)


class _Handler(BaseHTTPRequestHandler):
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


class _Server:
    def __init__(self, routes: dict) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
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


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._lock = asyncio.Lock()

    async def send(self, data: str) -> None:
        import json
        async with self._lock:
            self.sent.append(json.loads(data))


def _local_fetcher(check_public: bool = False):
    async def fetch(url: str):
        return await asyncio.to_thread(
            lambda: fetch_http(url, connect_timeout=2, read_timeout=2, check_public=check_public),
        )
    return fetch


async def _ack_as_sent(ws: _FakeSocket, ack_q: asyncio.Queue, delay: float = 0.0) -> None:
    seen = 0
    while True:
        await asyncio.sleep(delay)
        while seen < len(ws.sent):
            msg = ws.sent[seen]
            seen += 1
            if msg.get("event") == "image":
                await ack_q.put({"event": "ack", "url": msg["url"], "seq": msg["seq"]})
            if msg.get("event") == "done" or msg.get("error"):
                return


class DaemonFetchTests(unittest.IsolatedAsyncioTestCase):
    def test_frames_round_trip_and_reject_html(self) -> None:
        raw = bytes(range(256)) * 3
        frames = image_frames("https://cdn.example/a.png", raw, "image/png", chunk_bytes=100)
        self.assertGreater(len(frames), 1)
        for frame in frames:
            self.assertLess(len(frame["data"]), 200)
            self.assertNotIn("..", frame["url"])
        asm = ImageAssembler({"https://cdn.example/a.png", "https://cdn.example/bad.png"})
        for frame in frames:
            self.assertFalse(asm.feed(frame))
        html = image_frames("https://cdn.example/bad.png", b"<html>nope</html>", "text/html", chunk_bytes=8)
        for frame in html:
            asm.feed(frame)
        self.assertTrue(asm.feed({"event": "done"}))
        self.assertEqual(asm.ok["https://cdn.example/a.png"], (raw, "image/png"))
        self.assertEqual(asm.failed["https://cdn.example/bad.png"], "not_image")
        self.assertLessEqual(CHUNK_RAW_BYTES, 256 * 1024)

    def test_unknown_method_is_a_missing_capability(self) -> None:
        asm = ImageAssembler({"https://cdn.example/a.png"})
        with self.assertRaises(DaemonCapabilityError):
            asm.feed({"error": "unknown method 'fetch_images'"})

    async def test_daemon_streams_without_writing_files(self) -> None:
        png = b"P" * 180
        server = _Server({
            "/ok.png": (200, png, "image/png", 0.0),
            "/nope.png": (404, b"missing", "text/plain", 0.0),
        })
        self.addCleanup(server.close)
        ok = f"{server.base}/ok.png"
        missing = f"{server.base}/nope.png"
        ws = _FakeSocket()
        ack_q: asyncio.Queue = asyncio.Queue()
        root = Path(self._tmpdir())
        before = {path.relative_to(root) for path in root.rglob("*")}
        pumper = asyncio.create_task(_ack_as_sent(ws, ack_q))
        await stream_fetch_images(
            ws,
            {"id": 9, "params": {"urls": [ok, missing, "not a url", ok]}},
            asyncio.Lock(),
            chunk_bytes=64,
            fetcher=_local_fetcher(),
            ack_queue=ack_q,
        )
        await pumper
        after = {path.relative_to(root) for path in root.rglob("*")}
        self.assertEqual(before, after)
        asm = ImageAssembler({ok, missing, "not a url"})
        done = False
        for msg in ws.sent:
            self.assertNotIn("token", msg)
            done = asm.feed(msg) or done
        self.assertTrue(done)
        self.assertEqual(asm.ok[ok][0], png)
        self.assertEqual(asm.failed[missing], "http_404")
        self.assertEqual(asm.failed["not a url"], "blocked")
        ids = {msg.get("id") for msg in ws.sent}
        self.assertEqual(ids, {9})
        self.assertLess(max(len(msg.get("data", "")) for msg in ws.sent), 120)

    async def test_downloads_run_with_bounded_concurrency(self) -> None:
        inflight = {"now": 0, "max": 0}
        gate = asyncio.Lock()

        async def fetcher(url: str):
            async with gate:
                inflight["now"] += 1
                inflight["max"] = max(inflight["max"], inflight["now"])
            await asyncio.sleep(0.08)
            async with gate:
                inflight["now"] -= 1
            return b"x", "image/png"

        urls = [f"https://cdn.example/{i}.png" for i in range(8)]
        ws = _FakeSocket()
        ack_q: asyncio.Queue = asyncio.Queue()
        pumper = asyncio.create_task(_ack_as_sent(ws, ack_q))
        started = time.perf_counter()
        await stream_fetch_images(
            ws, {"id": 1, "params": {"urls": urls}}, asyncio.Lock(),
            fetcher=fetcher, ack_queue=ack_q,
        )
        await pumper
        elapsed = time.perf_counter() - started
        self.assertEqual(inflight["max"], 4)
        self.assertLess(elapsed, 0.35)
        asm = ImageAssembler(set(urls))
        for msg in ws.sent:
            asm.feed(msg)
        self.assertEqual(len(asm.ok), 8)

    def test_size_cap_is_enforced_across_chunks(self) -> None:
        url = "https://cdn.example/big.png"
        asm = ImageAssembler({url}, max_bytes=100)
        for seq, last in ((0, False), (1, False), (2, True)):
            frame = {
                "event": "image",
                "url": url,
                "seq": seq,
                "last": last,
                "data": base64.b64encode(b"a" * 40).decode("ascii"),
            }
            if seq == 0:
                frame["content_type"] = "image/png"
            self.assertFalse(asm.feed(frame))
        self.assertTrue(asm.feed({"event": "done"}))
        self.assertNotIn(url, asm.ok)
        self.assertEqual(asm.failed[url], "too_large")

    def test_disconnect_keeps_complete_images_only(self) -> None:
        done_url = "https://cdn.example/done.png"
        partial = "https://cdn.example/partial.png"
        asm = ImageAssembler({done_url, partial})
        for frame in image_frames(done_url, b"hello-image", "image/png", chunk_bytes=4):
            self.assertFalse(asm.feed(frame))
        first = image_frames(partial, b"partial-image", "image/png", chunk_bytes=4)[0]
        self.assertFalse(asm.feed(first))
        self.assertTrue(asm.feed({"error": "Connection to the ego daemon aborted"}))
        self.assertEqual(asm.ok[done_url][0], b"hello-image")
        self.assertNotIn(partial, asm.ok)
        self.assertEqual(asm.failed[partial], "protocol")

    async def test_slow_link_does_not_queue_a_multi_megabyte_backlog(self) -> None:
        payload = b"Z" * (64 * 1024)
        url = "https://cdn.example/slow.png"

        async def fetcher(_url: str):
            return payload, "image/png"

        class SlowSocket(_FakeSocket):
            def __init__(self) -> None:
                super().__init__()
                self.unacked = 0
                self.max_unacked = 0
                self.events: list[str] = []

            async def send(self, data: str) -> None:
                msg = json.loads(data)
                async with self._lock:
                    self.sent.append(msg)
                    if msg.get("event") == "image":
                        self.unacked += 1
                        self.max_unacked = max(self.max_unacked, self.unacked)
                        self.events.append("image")
                    elif msg.get("type") == "ping":
                        self.events.append("ping")

            def note_ack(self) -> None:
                self.unacked -= 1

        ws = SlowSocket()
        ack_q: asyncio.Queue = asyncio.Queue()
        lock = asyncio.Lock()

        async def slow_acks() -> None:
            seen = 0
            while True:
                await asyncio.sleep(0.01)
                while seen < len(ws.sent):
                    msg = ws.sent[seen]
                    seen += 1
                    if msg.get("event") == "image":
                        await asyncio.sleep(0.02)
                        ws.note_ack()
                        await ack_q.put({"event": "ack", "url": msg["url"], "seq": msg["seq"]})
                    if msg.get("event") == "done":
                        return

        async def pings() -> None:
            for _ in range(12):
                await asyncio.sleep(0.01)
                async with lock:
                    await ws.send('{"type":"ping"}')

        pumper = asyncio.create_task(slow_acks())
        ping_task = asyncio.create_task(pings())
        await stream_fetch_images(
            ws, {"id": 4, "params": {"urls": [url]}}, lock,
            chunk_bytes=8 * 1024, fetcher=fetcher, ack_queue=ack_q,
        )
        await pumper
        ping_task.cancel()
        self.assertLessEqual(ws.max_unacked, 2)
        self.assertGreaterEqual(ws.max_unacked, 1)
        self.assertTrue(any(
            kind == "ping" and "image" in ws.events[:index] and "image" in ws.events[index + 1:]
            for index, kind in enumerate(ws.events)
        ), ws.events)
        asm = ImageAssembler({url})
        for msg in ws.sent:
            asm.feed(msg)
        self.assertEqual(asm.ok[url][0], payload)

    def _tmpdir(self) -> str:
        import tempfile
        path = tempfile.mkdtemp(prefix="hermes-ego-fetch-")
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        return path


if __name__ == "__main__":
    unittest.main()
