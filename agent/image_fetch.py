"""Download http(s) images with the save tool's retry policy.

The Mac daemon and the relay share this module. It returns bytes. It does not
write files, and it does not know about EGO_SAVE_ROOT.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import http.client
import ipaddress
import socket
import ssl
import urllib.parse

IMAGE_CONCURRENCY = 4
IMAGE_CONNECT_TIMEOUT_S = 5.0
IMAGE_READ_TIMEOUT_S = 10.0
IMAGE_ATTEMPTS = 3
IMAGE_RETRY_BACKOFF_S = 0.4
IMAGE_MAX_BYTES = 8 * 1024 * 1024
MAX_UNIQUE_IMAGES = 100
MAX_REDIRECTS = 5
# One frame stays well under the 1 MiB WebSocket default. 256 KiB raw is
# about 350 KiB of base64 plus the JSON envelope.
CHUNK_RAW_BYTES = 256 * 1024
FETCH_CAPABILITY = "fetch_images"
_RETRY_HTTP = frozenset({408, 429, 500, 502, 503, 504})
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_UNSPECIFIED_V4 = ipaddress.ip_network("0.0.0.0/8")


class ImageFetchError(Exception):
    """One image attempt failed. `reason` is a short token, not a URL."""

    def __init__(self, reason: str, *, retryable: bool, detail: str = "") -> None:
        self.reason = reason
        self.retryable = retryable
        text = reason if not detail else f"{reason}: {detail}"
        super().__init__(text)


class DaemonCapabilityError(Exception):
    """The connected daemon cannot fetch images."""


def normalize_image_url(raw: str) -> str | None:
    """Fetchable http(s) URL, or None when the destination is not one."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == "<" and text[-1] == ">":
        text = text[1:-1].strip()
    if not text or len(text) > 2000 or any(ord(ch) < 33 or ch.isspace() for ch in text):
        return None
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    return text


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    if isinstance(ip, ipaddress.IPv4Address) and (ip in _CGNAT or ip in _UNSPECIFIED_V4):
        return True
    return False


def _addresses(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    literal = hostname.strip("[]")
    try:
        return [ipaddress.ip_address(literal)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ImageFetchError("dns", retryable=True, detail=hostname) from exc
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        found.append(ipaddress.ip_address(info[4][0]))
    if not found:
        raise ImageFetchError("dns", retryable=True, detail=hostname)
    return found


def is_public_http_url(url: str) -> bool:
    """True when every resolved address is a public http(s) destination."""
    normalized = normalize_image_url(url)
    if normalized is None:
        return False
    host = urllib.parse.urlsplit(normalized).hostname or ""
    try:
        addresses = _addresses(host)
    except ImageFetchError:
        return False
    return bool(addresses) and all(not _blocked_ip(ip) for ip in addresses)


def content_main(header: str) -> str:
    return header.split(";", 1)[0].strip().lower()


def acceptable_image_type(header: str) -> bool:
    main = content_main(header)
    return not main or main.startswith("image/") or main == "application/octet-stream"


def _read_limited(resp: http.client.HTTPResponse, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            block = resp.read(65536)
        except TimeoutError as exc:
            raise ImageFetchError("timeout_read", retryable=True) from exc
        except http.client.IncompleteRead as exc:
            raise ImageFetchError("truncated", retryable=True) from exc
        if not block:
            break
        total += len(block)
        if total > limit:
            raise ImageFetchError("too_large", retryable=False)
        chunks.append(block)
    if total == 0:
        raise ImageFetchError("empty", retryable=True)
    return b"".join(chunks)


def _exchange(
    url: str, connect_timeout: float, read_timeout: float, max_bytes: int,
) -> tuple[int, str, str, bytes]:
    """One HTTP request. Connect and read use different socket timeouts."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            host, port, timeout=connect_timeout, context=ssl.create_default_context(),
        )
    else:
        conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
    try:
        try:
            conn.connect()
        except TimeoutError as exc:
            raise ImageFetchError("timeout_connect", retryable=True) from exc
        except socket.gaierror as exc:
            raise ImageFetchError("dns", retryable=True) from exc
        except ssl.SSLError as exc:
            raise ImageFetchError("connect", retryable=True) from exc
        except OSError as exc:
            raise ImageFetchError("connect", retryable=True) from exc
        if conn.sock is not None:
            conn.sock.settimeout(read_timeout)
        try:
            conn.request(
                "GET",
                path,
                headers={
                    "User-Agent": "hermes-ego-save/0.1",
                    "Accept": "image/*,*/*;q=0.8",
                    "Connection": "close",
                },
            )
            resp = conn.getresponse()
        except TimeoutError as exc:
            raise ImageFetchError("timeout_read", retryable=True) from exc
        except http.client.HTTPException as exc:
            raise ImageFetchError("connect", retryable=True) from exc
        except ssl.SSLError as exc:
            raise ImageFetchError("connect", retryable=True) from exc
        except OSError as exc:
            raise ImageFetchError("connect", retryable=True) from exc
        status = resp.status
        content_type = resp.getheader("Content-Type", "") or ""
        location = resp.getheader("Location", "") or ""
        body = b""
        if status == 200:
            if not acceptable_image_type(content_type):
                raise ImageFetchError("not_image", retryable=False)
            body = _read_limited(resp, max_bytes)
        return status, content_type, location, body
    finally:
        conn.close()


def fetch_http(
    url: str,
    *,
    connect_timeout: float = IMAGE_CONNECT_TIMEOUT_S,
    read_timeout: float = IMAGE_READ_TIMEOUT_S,
    check_public: bool = True,
    max_bytes: int = IMAGE_MAX_BYTES,
) -> tuple[bytes, str]:
    """GET one image. Returns `(body, content-type header)`.

    `check_public` refuses loopback, private, link-local, and similar addresses
    before the connection, and again on each redirect. Tests that serve images
    on 127.0.0.1 pass False.
    """
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        if normalize_image_url(current) is None:
            raise ImageFetchError("blocked", retryable=False)
        if check_public and not is_public_http_url(current):
            raise ImageFetchError("blocked", retryable=False)
        status, content_type, location, body = _exchange(
            current, connect_timeout, read_timeout, max_bytes,
        )
        if status in (301, 302, 303, 307, 308):
            if not location:
                raise ImageFetchError(f"http_{status}", retryable=False)
            current = urllib.parse.urljoin(current, location)
            continue
        if status != 200:
            raise ImageFetchError(f"http_{status}", retryable=status in _RETRY_HTTP)
        return body, content_type
    raise ImageFetchError("redirect", retryable=False)


async def fetch_url_with_retries(
    url: str,
    *,
    fetch=fetch_http,
    attempts: int = IMAGE_ATTEMPTS,
    backoff: float = IMAGE_RETRY_BACKOFF_S,
    connect_timeout: float = IMAGE_CONNECT_TIMEOUT_S,
    read_timeout: float = IMAGE_READ_TIMEOUT_S,
) -> tuple[bytes, str]:
    """Try one URL up to `attempts` times. The last error is raised."""
    last: ImageFetchError | None = None
    tries = max(1, attempts)
    for attempt in range(1, tries + 1):
        try:
            return await asyncio.to_thread(
                fetch, url, connect_timeout=connect_timeout, read_timeout=read_timeout,
            )
        except ImageFetchError as exc:
            last = exc
            if not exc.retryable or attempt == tries:
                raise
        await asyncio.sleep(backoff * (2 ** (attempt - 1)))
    assert last is not None
    raise last


def image_frames(
    url: str, data: bytes, content_type: str, *, chunk_bytes: int = CHUNK_RAW_BYTES,
) -> list[dict]:
    """Chunk one image into WebSocket-sized JSON events. No id field."""
    step = max(1, chunk_bytes)
    if not data:
        return [{
            "event": "image",
            "url": url,
            "seq": 0,
            "last": True,
            "content_type": content_type,
            "data": "",
        }]
    frames: list[dict] = []
    seq = 0
    for start in range(0, len(data), step):
        piece = data[start:start + step]
        last = start + step >= len(data)
        frame = {
            "event": "image",
            "url": url,
            "seq": seq,
            "last": last,
            "data": base64.b64encode(piece).decode("ascii"),
        }
        if seq == 0:
            frame["content_type"] = content_type
        frames.append(frame)
        seq += 1
    return frames


class ImageAssembler:
    """Reassemble interleaved per-image frames. Ignores URLs it was not given."""

    def __init__(self, allowed: set[str], *, max_bytes: int = IMAGE_MAX_BYTES) -> None:
        self.allowed = allowed
        self.max_bytes = max_bytes
        self.ok: dict[str, tuple[bytes, str]] = {}
        self.failed: dict[str, str] = {}
        self._open: dict[str, dict] = {}

    def feed(self, msg: dict) -> bool:
        """Consume one daemon message. True when the batch is finished.

        A top-level `error` whose text says the method is unknown raises
        DaemonCapabilityError. Any other top-level error ends the stream.
        """
        if not isinstance(msg, dict):
            return False
        if "error" in msg and "event" not in msg:
            text = str(msg.get("error") or "")
            if "unknown method" in text:
                raise DaemonCapabilityError(text)
            self._fail_unfinished("protocol")
            return True
        event = msg.get("event")
        if event == "done":
            self._fail_unfinished("protocol")
            return True
        url = msg.get("url")
        if not isinstance(url, str) or url not in self.allowed or url in self.ok or url in self.failed:
            return False
        if event == "image_fail":
            reason = msg.get("reason")
            self.failed[url] = reason if isinstance(reason, str) and reason else "other"
            self._open.pop(url, None)
            return False
        if event != "image":
            return False
        self._chunk(url, msg)
        return False

    def _chunk(self, url: str, msg: dict) -> None:
        seq = msg.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            self.failed[url] = "protocol"
            self._open.pop(url, None)
            return
        state = self._open.get(url)
        if state is None:
            if seq != 0:
                self.failed[url] = "protocol"
                return
            state = {"next": 0, "parts": [], "ctype": "", "size": 0}
            self._open[url] = state
        if seq != state["next"]:
            self.failed[url] = "protocol"
            self._open.pop(url, None)
            return
        raw_b64 = msg.get("data")
        if not isinstance(raw_b64, str):
            self.failed[url] = "protocol"
            self._open.pop(url, None)
            return
        try:
            piece = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError):
            self.failed[url] = "protocol"
            self._open.pop(url, None)
            return
        if seq == 0 and isinstance(msg.get("content_type"), str):
            state["ctype"] = msg["content_type"]
        state["size"] += len(piece)
        if state["size"] > self.max_bytes:
            self.failed[url] = "too_large"
            self._open.pop(url, None)
            return
        state["parts"].append(piece)
        state["next"] = seq + 1
        if msg.get("last") is True:
            data = b"".join(state["parts"])
            ctype = state["ctype"]
            self._open.pop(url, None)
            if not data:
                self.failed[url] = "empty"
                return
            if not acceptable_image_type(ctype):
                self.failed[url] = "not_image"
                return
            self.ok[url] = (data, ctype)

    def _fail_unfinished(self, reason: str) -> None:
        for url in list(self._open):
            if url not in self.failed and url not in self.ok:
                self.failed[url] = reason
        self._open.clear()
        for url in self.allowed:
            if url not in self.ok and url not in self.failed:
                self.failed[url] = reason
