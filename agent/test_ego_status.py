"""ego_status reachability and the shared-tab lock, with ego-browser mocked.

No relay process, no GUI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
for path in (str(ROOT), str(AGENT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import hermes_ego_daemon as daemon
from relay.server import status_body

BOOTSTRAP = (
    "ego-browser exited 5: Failed to connect to ego_cli bootstrap server, "
    "launch ego lite.app manually."
)


class StatusProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        daemon.clear_probe_cache()
        daemon.reset_tab_command_lock()
        self.clock = {"t": 1_000.0}
        self._time = mock.patch("hermes_ego_daemon._now", lambda: self.clock["t"])
        self._time.start()

    def tearDown(self) -> None:
        self._time.stop()
        daemon.clear_probe_cache()
        daemon.reset_tab_command_lock()

    def test_probe_is_a_real_tabs_call_with_a_short_timeout(self) -> None:
        self.assertLessEqual(daemon.PROBE_TIMEOUT_S, 5)
        script = daemon.render(daemon.PROBE_JS, {})
        self.assertIn('taskSpace("hermes-ego")', script)
        self.assertIn("task.tabs()", script)
        self.assertNotIn("finish(", script)
        self.assertNotIn("__HERMES_EGO_PARAMS__", script)
        node = shutil.which("node")
        if not node:
            return
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.mjs"
            path.write_text(script, encoding="utf-8")
            proc = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    async def test_version_ok_but_bootstrap_down(self) -> None:
        calls: list[tuple[tuple[str, ...], float | None, bytes | None]] = []

        async def fake_run(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            calls.append((tuple(args), timeout, stdin))
            if args == ["--version"]:
                return "ego-browser 0.6.2.8\n", ""
            raise RuntimeError(BOOTSTRAP)

        with mock.patch.object(daemon, "run_ego", fake_run):
            result = await daemon.dispatch("/bin/ego-browser", "status", {})
        self.assertEqual(result["egoVersion"], "0.6.2.8")
        self.assertIs(result["egoReachable"], False)
        self.assertIn("bootstrap", result["egoError"])
        self.assertNotIn("online", result)
        version_calls = [item for item in calls if item[0] == ("--version",)]
        probe_calls = [item for item in calls if item[0] == ("nodejs",)]
        self.assertEqual(len(version_calls), 1)
        self.assertEqual(len(probe_calls), 1)
        self.assertEqual(probe_calls[0][1], daemon.PROBE_TIMEOUT_S)
        self.assertIn(b"task.tabs()", probe_calls[0][2] or b"")

        calls.clear()
        with mock.patch.object(daemon, "run_ego", fake_run):
            again = await daemon.dispatch("/bin/ego-browser", "status", {})
        self.assertEqual(calls, [])
        self.assertEqual(again, result)

        with mock.patch.object(daemon, "run_ego", fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                await daemon.dispatch("/bin/ego-browser", "list_tabs", {})
        self.assertIn("bootstrap", str(ctx.exception))
        self.assertEqual(calls, [])

        with mock.patch.object(daemon, "run_ego", fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                await daemon.dispatch("/bin/ego-browser", "navigate", {"url": "javascript:alert(1)"})
        self.assertIn("http", str(ctx.exception).lower())
        self.assertEqual(calls, [])

        self.clock["t"] += daemon.PROBE_CACHE_S + 0.1
        with mock.patch.object(daemon, "run_ego", fake_run):
            refreshed = await daemon.dispatch("/bin/ego-browser", "status", {})
        self.assertIs(refreshed["egoReachable"], False)
        self.assertEqual(len([item for item in calls if item[0] == ("nodejs",)]), 1)

    async def test_probe_timeout_is_a_short_error(self) -> None:
        async def fake_run(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            if args == ["--version"]:
                return "ego-browser 0.6.2.8\n", ""
            raise RuntimeError("ego-browser timed out after 5s")

        with mock.patch.object(daemon, "run_ego", fake_run):
            result = await daemon.dispatch("/bin/ego-browser", "status", {})
        self.assertEqual(result["egoVersion"], "0.6.2.8")
        self.assertIs(result["egoReachable"], False)
        self.assertEqual(
            result["egoError"],
            "ego lite did not answer within 5s; launch ego lite.app manually",
        )
        self.assertNotIn("/api/", result["egoError"])

    async def test_reachable_probe_is_cached(self) -> None:
        calls: list[tuple[str, ...]] = []

        async def fake_run(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            calls.append(tuple(args))
            if args == ["--version"]:
                return "ego-browser 0.6.2.8\n", ""
            return "", '{"ok": true}\n'

        with mock.patch.object(daemon, "run_ego", fake_run):
            result = await daemon.dispatch("/bin/ego-browser", "status", {})
        self.assertIs(result["egoReachable"], True)
        self.assertIsNone(result["egoError"])
        self.assertEqual(result["egoVersion"], "0.6.2.8")
        calls.clear()
        with mock.patch.object(daemon, "run_ego", fake_run):
            again = await daemon.dispatch("/bin/ego-browser", "status", {})
        self.assertEqual(calls, [])
        self.assertEqual(again["egoReachable"], True)

    async def test_bootstrap_failure_on_a_tool_fails_the_next_call_fast(self) -> None:
        calls: list[tuple[str, ...]] = []

        async def fake_run(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            calls.append(tuple(args))
            raise RuntimeError(BOOTSTRAP)

        with mock.patch.object(daemon, "run_ego", fake_run):
            with self.assertRaises(RuntimeError):
                await daemon.dispatch("/bin/ego-browser", "list_tabs", {})
            with self.assertRaises(RuntimeError) as ctx:
                await daemon.dispatch("/bin/ego-browser", "get_page_text", {})
        self.assertIn("bootstrap", str(ctx.exception))
        self.assertEqual(calls, [("nodejs",)])

        self.clock["t"] += daemon.PROBE_CACHE_S + 0.1

        async def down_again(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            calls.append(tuple(args))
            if args == ["--version"]:
                return "ego-browser 0.6.2.8\n", ""
            raise RuntimeError(BOOTSTRAP)

        calls.clear()
        with mock.patch.object(daemon, "run_ego", down_again):
            result = await daemon.dispatch("/bin/ego-browser", "status", {})
        self.assertEqual(result["egoVersion"], "0.6.2.8")
        self.assertIs(result["egoReachable"], False)
        self.assertIn(("nodejs",), calls)

    async def _fail_fast_without_the_hanging_tool(self) -> None:
        calls: list[float | None] = []

        async def fake_run(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            calls.append(timeout)
            if timeout == daemon.PROBE_TIMEOUT_S:
                raise RuntimeError("ego-browser timed out after 5s")
            await asyncio.Event().wait()

        started = time.perf_counter()
        with mock.patch.object(daemon, "run_ego", fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                await asyncio.wait_for(
                    daemon.dispatch("/bin/ego-browser", "get_page_text", {}),
                    timeout=1,
                )
        self.assertLess(time.perf_counter() - started, 1)
        self.assertEqual(
            str(ctx.exception),
            "ego lite did not answer within 5s; launch ego lite.app manually",
        )
        self.assertEqual(calls, [daemon.PROBE_TIMEOUT_S])

        calls.clear()
        with mock.patch.object(daemon, "run_ego", fake_run):
            with self.assertRaises(RuntimeError) as again:
                await daemon.dispatch("/bin/ego-browser", "get_page_text", {})
        self.assertIn("5s", str(again.exception))
        self.assertEqual(calls, [])

    async def test_cold_cache_fails_fast_when_the_tool_would_hang(self) -> None:
        await self._fail_fast_without_the_hanging_tool()

    async def test_stale_reachable_cache_probes_again_and_fails_fast(self) -> None:
        daemon._remember(True, None, "0.6.2.8")
        self.clock["t"] += daemon.PROBE_CACHE_S + 0.1
        await self._fail_fast_without_the_hanging_tool()

    async def test_fresh_success_does_not_probe_before_a_tool(self) -> None:
        calls: list[float | None] = []

        async def fake_run(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            calls.append(timeout)
            if timeout == daemon.PROBE_TIMEOUT_S:
                raise AssertionError("probed despite a fresh success")
            return "", '{"tabs": [], "spaceId": 7}\n'

        daemon._remember(True, None, "0.6.2.8")
        with mock.patch.object(daemon, "run_ego", fake_run):
            first = await daemon.dispatch("/bin/ego-browser", "list_tabs", {})
            second = await daemon.dispatch("/bin/ego-browser", "list_tabs", {})
        self.assertEqual(first["tabs"], [])
        self.assertEqual(second["spaceId"], 7)
        self.assertEqual(calls, [None, None])

    async def test_cold_success_probes_once_then_reuses_it(self) -> None:
        calls: list[float | None] = []

        async def fake_run(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            calls.append(timeout)
            if timeout == daemon.PROBE_TIMEOUT_S:
                return "", '{"ok": true}\n'
            return "", '{"tabs": [], "spaceId": 7}\n'

        with mock.patch.object(daemon, "run_ego", fake_run):
            listed = await daemon.dispatch("/bin/ego-browser", "list_tabs", {})
            again = await daemon.dispatch("/bin/ego-browser", "list_tabs", {})
        self.assertEqual(listed["tabs"], [])
        self.assertEqual(again["tabs"], [])
        self.assertEqual(calls, [daemon.PROBE_TIMEOUT_S, None, None])

    async def test_bad_url_does_not_probe_on_a_cold_cache(self) -> None:
        calls: list[float | None] = []

        async def fake_run(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            calls.append(timeout)
            await asyncio.Event().wait()

        with mock.patch.object(daemon, "run_ego", fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                await daemon.execute("/bin/ego-browser", "navigate", {"url": "javascript:alert(1)"})
        self.assertIn("http", str(ctx.exception).lower())
        self.assertEqual(calls, [])


class RelayStatusTests(unittest.TestCase):
    def test_online_stays_true_when_ego_lite_is_down(self) -> None:
        body = status_body(
            online=True,
            device="mac",
            result={
                "egoVersion": "0.6.2.8",
                "egoReachable": False,
                "egoError": "Failed to connect to ego_cli bootstrap",
            },
            error=None,
        )
        self.assertIs(body["online"], True)
        self.assertEqual(body["device"], "mac")
        self.assertEqual(body["egoVersion"], "0.6.2.8")
        self.assertIs(body["egoReachable"], False)
        self.assertIn("bootstrap", body["egoError"])
        self.assertNotIn("error", body)

    def test_reachable_clears_ego_error(self) -> None:
        body = status_body(
            online=True,
            device="mac",
            result={"egoVersion": "0.6.2.8", "egoReachable": True, "egoError": "stale"},
            error=None,
        )
        self.assertIs(body["egoReachable"], True)
        self.assertIsNone(body["egoError"])

    def test_daemon_offline_does_not_invent_an_ego_error(self) -> None:
        body = status_body(online=False, device=None, result=None, error=None)
        self.assertEqual(
            body,
            {
                "online": False,
                "device": None,
                "egoVersion": None,
                "egoReachable": False,
                "egoError": None,
            },
        )

    def test_status_call_failure_is_unreachable(self) -> None:
        body = status_body(online=True, device="mac", result=None, error="socket dropped " + ("x" * 400))
        self.assertIs(body["online"], True)
        self.assertIs(body["egoReachable"], False)
        self.assertLessEqual(len(body["egoError"]), 220)
        self.assertEqual(body["error"], body["egoError"])


def _script_op(stdin: bytes | None) -> tuple[str, dict]:
    script = (stdin or b"").decode()
    op = ""
    for line in script.splitlines():
        if line.startswith("// hermes-ego-op: "):
            op = line.split(": ", 1)[1].strip()
            break
    marker = "const params = "
    at = script.find(marker)
    if at < 0:
        return op, {}
    params, _end = json.JSONDecoder().raw_decode(script[at + len(marker):])
    return op, params


def _sleep_60_pids() -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if b"sleep\x0060" in cmd:
            found.append(int(entry.name))
    return found


class TabLockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        daemon.clear_probe_cache()
        daemon.reset_tab_command_lock()
        self.clock = {"t": 5_000.0}
        self._time = mock.patch("hermes_ego_daemon._now", lambda: self.clock["t"])
        self._time.start()
        daemon._remember(True, None, "0.6.2.8")

    def tearDown(self) -> None:
        self._time.stop()
        daemon.clear_probe_cache()
        daemon.reset_tab_command_lock()

    async def test_navigate_then_read_in_sequence_sees_its_own_page(self) -> None:
        page = {"url": None}

        async def fake_run(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            op, params = _script_op(stdin)
            if op == "probe" or timeout == daemon.PROBE_TIMEOUT_S:
                raise AssertionError("fresh cache should not probe")
            if op == "navigate":
                page["url"] = params["url"]
                return "", json.dumps({"label": "p1", "title": "t", "url": params["url"], "spaceId": 7})
            if op == "get_page_text":
                url = page["url"]
                return "", json.dumps({
                    "label": "p1", "title": "t", "url": url, "text": url, "truncated": False,
                })
            raise AssertionError(op)

        example = "https://example.com/"
        hacker = "https://news.ycombinator.com/"
        with mock.patch.object(daemon, "run_ego", fake_run):
            await daemon.execute("/bin/ego-browser", "navigate", {"url": example})
            first = await daemon.execute("/bin/ego-browser", "get_page_text", {})
            await daemon.execute("/bin/ego-browser", "navigate", {"url": hacker})
            second = await daemon.execute("/bin/ego-browser", "get_page_text", {})
        self.assertEqual(first["url"], example)
        self.assertEqual(first["text"], example)
        self.assertEqual(second["url"], hacker)

    async def test_concurrent_navigate_read_pairs_are_serialized(self) -> None:
        example = "https://example.com/"
        hacker = "https://news.ycombinator.com/"
        page = {"url": None}
        events: list[tuple[str, str, str | None]] = []
        overlap = {"now": 0, "max": 0}
        first_nav = asyncio.Event()

        async def fake_run(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            op, params = _script_op(stdin)
            if op == "probe" or timeout == daemon.PROBE_TIMEOUT_S:
                raise AssertionError("fresh cache should not probe")
            overlap["now"] += 1
            overlap["max"] = max(overlap["max"], overlap["now"])
            events.append(("start", op, params.get("url")))
            try:
                if op == "navigate" and params.get("url") == example and not first_nav.is_set():
                    first_nav.set()
                await asyncio.sleep(0.05)
                if op == "navigate":
                    page["url"] = params["url"]
                    events.append(("end", op, params["url"]))
                    return "", json.dumps({
                        "label": "p1", "title": "t", "url": params["url"], "spaceId": 7,
                    })
                if op == "get_page_text":
                    url = page["url"]
                    events.append(("end", op, url))
                    return "", json.dumps({
                        "label": "p1", "title": "t", "url": url,
                        "text": f"SNAPSHOT {url}", "truncated": False,
                    })
                raise AssertionError(op)
            finally:
                overlap["now"] -= 1

        async def pair(url: str) -> str:
            await daemon.execute("/bin/ego-browser", "navigate", {"url": url})
            read = await daemon.execute("/bin/ego-browser", "get_page_text", {})
            return read["url"]

        with mock.patch.object(daemon, "run_ego", fake_run):
            first = asyncio.create_task(pair(example))
            await asyncio.wait_for(first_nav.wait(), 1)
            second = asyncio.create_task(pair(hacker))
            await asyncio.sleep(0)
            urls = await asyncio.gather(first, second)

        self.assertEqual(overlap["max"], 1)
        self.assertEqual(
            [(kind, op) for kind, op, _url in events],
            [
                ("start", "navigate"), ("end", "navigate"),
                ("start", "navigate"), ("end", "navigate"),
                ("start", "get_page_text"), ("end", "get_page_text"),
                ("start", "get_page_text"), ("end", "get_page_text"),
            ],
        )
        self.assertEqual(
            [url for kind, op, url in events if kind == "end" and op == "navigate"],
            [example, hacker],
        )
        # Per command only. The second navigate was already queued, so both
        # reads see that page. ego_get_page_text(url) on the relay is the
        # transaction; see SaveCase.test_concurrent_open_and_read_calls_do_not_cross_pages.
        self.assertEqual(
            [url for kind, op, url in events if kind == "end" and op == "get_page_text"],
            [hacker, hacker],
        )
        self.assertEqual(urls, [hacker, hacker])

    async def test_status_does_not_wait_on_the_tab_lock(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            op, params = _script_op(stdin)
            if op != "navigate":
                raise AssertionError(op)
            started.set()
            await release.wait()
            return "", json.dumps({"label": "p1", "title": "t", "url": params["url"], "spaceId": 7})

        with mock.patch.object(daemon, "run_ego", fake_run):
            nav = asyncio.create_task(daemon.execute(
                "/bin/ego-browser", "navigate", {"url": "https://example.com/"},
            ))
            await asyncio.wait_for(started.wait(), 1)
            started_at = time.perf_counter()
            status = await asyncio.wait_for(daemon.execute("/bin/ego-browser", "status", {}), timeout=0.5)
            self.assertLess(time.perf_counter() - started_at, 0.5)
            self.assertIs(status["egoReachable"], True)
            self.assertEqual(status["egoVersion"], "0.6.2.8")
            self.assertFalse(nav.done())
            release.set()
            opened = await nav
        self.assertEqual(opened["url"], "https://example.com/")

    async def test_list_tabs_waits_for_an_in_flight_navigate(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        listed = asyncio.Event()

        async def fake_run(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            op, params = _script_op(stdin)
            if op == "navigate":
                started.set()
                await release.wait()
                return "", json.dumps({"label": "p1", "title": "t", "url": params["url"], "spaceId": 7})
            if op == "list_tabs":
                listed.set()
                return "", '{"tabs": [], "spaceId": 7}\n'
            raise AssertionError(op)

        with mock.patch.object(daemon, "run_ego", fake_run):
            nav = asyncio.create_task(daemon.execute(
                "/bin/ego-browser", "navigate", {"url": "https://example.com/"},
            ))
            await asyncio.wait_for(started.wait(), 1)
            listing = asyncio.create_task(daemon.execute("/bin/ego-browser", "list_tabs", {}))
            await asyncio.sleep(0.05)
            self.assertFalse(listed.is_set())
            self.assertFalse(listing.done())
            release.set()
            await nav
            await listing
        self.assertTrue(listed.is_set())

    async def test_concurrent_tools_share_one_probe_when_the_cache_is_cold(self) -> None:
        daemon.clear_probe_cache()
        calls: list[float | None] = []

        async def fake_run(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            calls.append(timeout)
            await asyncio.sleep(0.05)
            if timeout == daemon.PROBE_TIMEOUT_S:
                return "", '{"ok": true}\n'
            return "", '{"tabs": [], "spaceId": 7}\n'

        with mock.patch.object(daemon, "run_ego", fake_run):
            first, second = await asyncio.gather(
                daemon.execute("/bin/ego-browser", "list_tabs", {}),
                daemon.execute("/bin/ego-browser", "list_tabs", {}),
            )
        self.assertEqual(first["tabs"], [])
        self.assertEqual(second["tabs"], [])
        self.assertEqual(calls.count(daemon.PROBE_TIMEOUT_S), 1)
        self.assertEqual(calls.count(None), 2)

    async def test_status_on_the_socket_is_not_stuck_behind_navigate(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run(binary: str, args: list[str], stdin: bytes | None, timeout: float | None = None):
            op, params = _script_op(stdin)
            if op != "navigate":
                raise AssertionError(op)
            started.set()
            await release.wait()
            return "", json.dumps({"label": "p1", "title": "t", "url": params["url"], "spaceId": 7})

        class QueueSocket:
            def __init__(self) -> None:
                self.queue: asyncio.Queue[str | None] = asyncio.Queue()
                self.sent: list[dict] = []

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await self.queue.get()
                if item is None:
                    raise StopAsyncIteration
                return item

            async def send(self, data: str) -> None:
                self.sent.append(json.loads(data))

            async def close(self) -> None:
                return None

        socket = QueueSocket()
        serve_task = asyncio.create_task(daemon.serve(socket, "/bin/ego-browser"))
        try:
            with mock.patch.object(daemon, "run_ego", fake_run):
                await socket.queue.put(json.dumps({
                    "id": 1, "method": "navigate", "params": {"url": "https://example.com/"},
                }))
                await asyncio.wait_for(started.wait(), 1)
                await socket.queue.put(json.dumps({"id": 2, "method": "status", "params": {}}))

                async def status_sent() -> dict:
                    while True:
                        for message in socket.sent:
                            if message.get("id") == 2:
                                return message
                        await asyncio.sleep(0.01)

                status = await asyncio.wait_for(status_sent(), 1)
                self.assertIs(status["result"]["egoReachable"], True)
                self.assertFalse(any(message.get("id") == 1 for message in socket.sent))
                release.set()

                async def nav_sent() -> None:
                    while not any(message.get("id") == 1 for message in socket.sent):
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(nav_sent(), 1)
                await socket.queue.put(None)
                await asyncio.wait_for(serve_task, 1)
        finally:
            release.set()
            if not serve_task.done():
                await socket.queue.put(None)
                serve_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await serve_task

    async def test_cancelled_run_ego_kills_the_process(self) -> None:
        # IsolatedAsyncioTestCase runs the loop in debug mode, so asyncio logs
        # the killed transport at INFO. The relay import leaves the root logger
        # there. Raise this logger before the process starts and leave it: the
        # close lines are emitted as the runner stops the loop.
        logging.getLogger("asyncio").setLevel(logging.WARNING)
        task = asyncio.create_task(daemon.run_ego("/bin/sleep", ["60"], None, timeout=30))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not _sleep_60_pids():
            await asyncio.sleep(0.02)
        self.assertTrue(_sleep_60_pids(), "sleep did not start")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and _sleep_60_pids():
            await asyncio.sleep(0.02)
        self.assertFalse(_sleep_60_pids())
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
