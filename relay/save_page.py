"""Write a captured page under EGO_SAVE_ROOT without handing the body to the model.

The note body is the get_page_text string with two edits: http(s) image links may
be rewritten to files under assets/, and a failed download keeps the original URL
plus a visible marker. YAML frontmatter is prepended. Nothing else is inserted,
including a trailing newline. After a background backfill rewrites a marker,
images_ok, images_failed, and images are set from the body.

Environment:
  EGO_SAVE_ROOT   absolute directory the tool may write under. Unset disables it.
  EGO_SAVE_TZ     IANA timezone for the date prefix and captured_at. Default UTC.
  EGO_SAVE_UID    optional numeric uid for created files and directories.
                  Default: the uid of EGO_SAVE_ROOT.
  EGO_SAVE_GID    optional numeric gid. Default: the gid of EGO_SAVE_ROOT.
  EGO_IMAGE_FETCH relay (default), auto, or daemon. relay downloads on the
                  VPS during the save, within EGO_IMAGE_BUDGET_MS, then asks the
                  Mac once in the background for anything still missing.
                  auto asks the Mac during the save, then the relay.
                  daemon uses only the Mac during the save.
  EGO_IMAGE_BUDGET_MS
                  hard cap for the synchronous relay image phase. Default 90000.
                  When it hits, new downloads stop, the note is written, and
                  the leftovers are marked budget.

Created files are mode 0644 and created directories are mode 0755. Ownership is
applied to paths this tool creates, not to EGO_SAVE_ROOT itself and not to
directories that already existed. Symlinks are still never followed.

Collision: the note is YYYY-MM-DD-<slug>.md in EGO_SAVE_TZ. An existing name is
never overwritten and a symlink is never followed. The next free suffix is -2,
-3, … through -100. If every one of those names is taken, the save errors.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import re
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Imported by the relay and by unit tests. The daemon package is not installed.
_AGENT = Path(__file__).resolve().parent.parent / "agent"
if str(_AGENT) not in sys.path:
    sys.path.insert(0, str(_AGENT))
from image_fetch import (
    IMAGE_ATTEMPTS,
    IMAGE_CONCURRENCY,
    IMAGE_CONNECT_TIMEOUT_S,
    IMAGE_MAX_BYTES,
    IMAGE_READ_TIMEOUT_S,
    IMAGE_RETRY_BACKOFF_S,
    MAX_UNIQUE_IMAGES,
    DaemonCapabilityError,
    ImageFetchError,
    acceptable_image_type,
    content_main,
    fetch_http,
    is_public_http_url,
    normalize_image_url,
)
from yitang_text import PAGE_TEXT_MODES

NOTE_NAME_LIMIT = 100
FILE_MODE = 0o644
DIR_MODE = 0o755
FAILED_IMAGE_MARKER = " **[image download failed]**"
_SLUG_RE = re.compile(r"^[\w][\w-]{0,63}$")
_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\(\s*(<[^>\n]+>|[^)\s]+)([^)\n]*)\)")
_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_OPEN_NEW = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
    "image/tiff": ".tif",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
}
_ALLOWED_SUFFIXES = (
    ".jpeg", ".webp", ".avif", ".tiff", ".png", ".jpg", ".gif", ".svg", ".bmp", ".tif", ".ico",
)
Fetch = Callable[[str], tuple[bytes, str]]


class SaveError(Exception):
    """A save that must not write, or must be reported as a tool error."""


@dataclass(frozen=True)
class ImageHit:
    """One http(s) image destination outside a fenced code block."""

    url: str
    url_start: int
    url_end: int
    match_end: int


@dataclass
class _Downloads:
    saved: dict[str, str]
    files: list[Path]
    asset_bytes: int
    reasons: dict[str, str]


def resolve_save_root() -> Path:
    """The real directory behind EGO_SAVE_ROOT. Raises SaveError when unusable."""
    raw = os.environ.get("EGO_SAVE_ROOT", "").strip()
    if not raw:
        raise SaveError("ego_save_page_markdown is disabled: EGO_SAVE_ROOT is not set")
    root = Path(raw)
    if not root.is_absolute():
        raise SaveError("ego_save_page_markdown is disabled: EGO_SAVE_ROOT must be an absolute path")
    if not root.exists():
        raise SaveError(f"ego_save_page_markdown is disabled: EGO_SAVE_ROOT does not exist: {root}")
    if root.is_symlink():
        # The configured path may be a symlink. The jail is the directory it names.
        pass
    real = root.resolve()
    if not real.is_dir() or real.is_symlink():
        raise SaveError("ego_save_page_markdown is disabled: EGO_SAVE_ROOT is not a directory")
    return real


def load_zone() -> ZoneInfo:
    name = os.environ.get("EGO_SAVE_TZ", "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise SaveError(f"EGO_SAVE_TZ {name!r} is not a valid timezone") from exc


def parse_subdir(value: str) -> Path:
    """A relative directory under the save root. `.` and `""` are the root itself."""
    if not isinstance(value, str):
        raise SaveError("subdir must be a relative path under EGO_SAVE_ROOT")
    if "\x00" in value or "\\" in value:
        raise SaveError("subdir must be a relative path under EGO_SAVE_ROOT")
    if value in ("", "."):
        return Path()
    path = Path(value)
    if path.is_absolute() or value.startswith("/"):
        raise SaveError("subdir must be a relative path under EGO_SAVE_ROOT")
    parts = path.parts
    if not parts:
        return Path()
    for part in parts:
        if part in ("", ".", "..") or "/" in part or "\\" in part or "\x00" in part:
            raise SaveError("subdir must be a relative path under EGO_SAVE_ROOT without '..'")
        if len(part) > 120 or any(ord(ch) < 32 for ch in part):
            raise SaveError("subdir must be a relative path under EGO_SAVE_ROOT")
    return Path(*parts)


def parse_slug(value: str) -> str:
    if not isinstance(value, str) or not _SLUG_RE.fullmatch(value):
        raise SaveError(
            "slug must be 1-64 characters of letters, digits, '_' or '-', without a path"
        )
    return value


def note_names(day: str, slug: str) -> list[str]:
    """YYYY-MM-DD-slug.md, then -2 … -100. Never the bare name twice."""
    base = f"{day}-{slug}"
    names = [f"{base}.md"]
    for number in range(2, NOTE_NAME_LIMIT + 1):
        names.append(f"{base}-{number}.md")
    return names


def local_now(now: datetime | None, zone: ZoneInfo) -> datetime:
    if now is None:
        return datetime.now(zone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(zone)


def render_frontmatter(fields: list[tuple[str, object]]) -> str:
    lines = ["---"]
    for key, value in fields:
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        elif isinstance(value, str):
            rendered = json.dumps(value, ensure_ascii=False)
        else:
            raise SaveError(f"cannot encode frontmatter field {key}")
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a note this module wrote. String fields stay JSON-encoded."""
    if not text.startswith("---\n"):
        raise SaveError("saved note is missing frontmatter")
    close = text.find("\n---\n", 4)
    if close < 0:
        raise SaveError("saved note has unterminated frontmatter")
    fields: dict[str, str] = {}
    for line in text[4:close].split("\n"):
        if not line:
            continue
        key, value = line.split(": ", 1)
        fields[key] = value
    return fields, text[close + len("\n---\n"):]


def fence_ranges(text: str) -> list[tuple[int, int]]:
    """Byte-offset spans of fenced code blocks, including an unclosed fence."""
    ranges: list[tuple[int, int]] = []
    offset = 0
    marker = ""
    length = 0
    open_at = 0
    for line in text.splitlines(keepends=True):
        body = line[:-1] if line.endswith("\n") else line
        if body.endswith("\r"):
            body = body[:-1]
        matched = _FENCE_RE.match(body)
        if matched:
            tick = matched.group(2)
            info = matched.group(3)
            if not marker:
                if not (tick[0] == "`" and "`" in info):
                    marker = tick[0]
                    length = len(tick)
                    open_at = offset
            elif tick[0] == marker and len(tick) >= length and info.strip() == "":
                ranges.append((open_at, offset + len(line)))
                marker = ""
        offset += len(line)
    if marker:
        ranges.append((open_at, len(text)))
    return ranges


def _overlaps(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start < stop and end > begin for begin, stop in ranges)


def find_http_images(text: str) -> list[ImageHit]:
    """Inline images with an http(s) destination, skipping fenced code."""
    masked = fence_ranges(text)
    hits: list[ImageHit] = []
    for match in _IMAGE_RE.finditer(text):
        if _overlaps(match.start(), match.end(), masked):
            continue
        url = normalize_image_url(match.group(2))
        if url is None:
            continue
        hits.append(ImageHit(url, match.start(2), match.end(2), match.end()))
    return hits


def apply_image_results(text: str, hits: list[ImageHit], saved: Mapping[str, str]) -> tuple[str, int, int]:
    """Rewrite successful URLs in place. Failures keep the URL and gain a marker.

    Every other byte of `text` is copied unchanged. `saved` maps a fetch URL to
    a relative posix path.
    """
    parts: list[str] = []
    cursor = 0
    ok = 0
    failed = 0
    for hit in hits:
        local = saved.get(hit.url)
        if local:
            parts.append(text[cursor:hit.url_start])
            parts.append(local)
            cursor = hit.url_end
            ok += 1
        else:
            parts.append(text[cursor:hit.match_end])
            parts.append(FAILED_IMAGE_MARKER)
            cursor = hit.match_end
            failed += 1
    parts.append(text[cursor:])
    return "".join(parts), ok, failed


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def save_ids(root: Path) -> tuple[int, int]:
    """Uid and gid for paths this save creates. Does not change `root`."""
    st = root.stat()
    return _parse_id("EGO_SAVE_UID", st.st_uid), _parse_id("EGO_SAVE_GID", st.st_gid)


def _parse_id(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise SaveError(f"{name} must be a non-negative numeric id") from exc
    if value < 0:
        raise SaveError(f"{name} must be a non-negative numeric id")
    return value


def _own_dir(path: Path, uid: int, gid: int) -> None:
    """chmod and chown a real directory. lchown does not follow a symlink."""
    if path.is_symlink() or not path.is_dir():
        raise SaveError("refusing to follow a symlink under EGO_SAVE_ROOT")
    os.chmod(path, DIR_MODE)
    try:
        os.lchown(path, uid, gid)
    except PermissionError as exc:
        raise SaveError(f"could not set ownership to {uid}:{gid}") from exc


def _own_fd(fd: int, uid: int, gid: int) -> None:
    """Force mode and owner on an fd opened with O_NOFOLLOW."""
    os.fchmod(fd, FILE_MODE)
    try:
        os.fchown(fd, uid, gid)
    except PermissionError as exc:
        raise SaveError(f"could not set ownership to {uid}:{gid}") from exc


def place_directory(root: Path, relative: Path) -> Path:
    """Create `relative` under `root` without following a symlink."""
    uid, gid = save_ids(root)
    current = root
    for part in relative.parts:
        if part in ("", ".", "..") or "/" in part or "\\" in part:
            raise SaveError("path escapes EGO_SAVE_ROOT")
        nxt = current / part
        if nxt.is_symlink():
            raise SaveError("refusing to follow a symlink under EGO_SAVE_ROOT")
        created = False
        if nxt.exists():
            if not nxt.is_dir():
                raise SaveError("save path is not a directory")
        else:
            try:
                nxt.mkdir(mode=DIR_MODE)
                created = True
            except FileExistsError:
                if nxt.is_symlink() or not nxt.is_dir():
                    raise SaveError("refusing to follow a symlink under EGO_SAVE_ROOT") from None
        if not _is_inside(nxt, root):
            if created:
                with contextlib.suppress(OSError):
                    nxt.rmdir()
            raise SaveError("path escapes EGO_SAVE_ROOT")
        if created:
            try:
                _own_dir(nxt, uid, gid)
            except BaseException:
                with contextlib.suppress(OSError):
                    nxt.rmdir()
                raise
        current = nxt
    return current


def _single_component(name: str) -> None:
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise SaveError("refusing an unsafe filename")


def reserve_note(directory: Path, names: list[str], root: Path) -> tuple[Path, int]:
    """Create an empty note with O_EXCL | O_NOFOLLOW. Symlinks count as taken."""
    for name in names:
        _single_component(name)
        path = directory / name
        if path.is_symlink():
            continue
        try:
            fd = os.open(path, _OPEN_NEW, 0o644)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ELOOP):
                continue
            raise SaveError(f"could not create the note: {exc.strerror}") from exc
        if not _is_inside(path, root):
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise SaveError("path escapes EGO_SAVE_ROOT")
        try:
            uid, gid = save_ids(root)
            _own_fd(fd, uid, gid)
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(path)
            raise
        return path, fd
    raise SaveError("too many notes for this date and slug; pick another slug")


def _url_leaf(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    leaf = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    leaf = re.sub(r"[^A-Za-z0-9._-]+", "-", leaf.replace("\\", "")).strip(".-")
    return leaf[:80]


def _allowed_suffix(leaf: str) -> str:
    lower = leaf.lower()
    for suffix in _ALLOWED_SUFFIXES:
        if lower.endswith(suffix):
            return suffix
    return ""


def asset_filename(index: int, url: str, content_type: str) -> str:
    ctype = content_main(content_type)
    ext = _EXT_BY_TYPE.get(ctype, "")
    leaf = _url_leaf(url)
    suffix = _allowed_suffix(leaf)
    if not ext:
        ext = suffix or ".bin"
    if suffix:
        stem = leaf[: -len(suffix)]
    elif "." in leaf:
        stem = leaf.rsplit(".", 1)[0]
    else:
        stem = leaf
    stem = stem.strip(".-")[:60] or "image"
    return f"{index:03d}-{stem}{ext}"


def write_asset(
    directory: Path, stem: str, index: int, url: str, data: bytes, content_type: str, root: Path,
) -> tuple[Path, str]:
    filename = asset_filename(index, url, content_type)
    _single_component(filename)
    _single_component(stem)
    path = directory / filename
    if path.is_symlink() or not _is_inside(path, root):
        raise SaveError("refusing to follow a symlink under EGO_SAVE_ROOT")
    try:
        fd = os.open(path, _OPEN_NEW, FILE_MODE)
    except OSError as exc:
        raise SaveError(f"could not store an image: {exc.strerror}") from exc
    try:
        if path.is_symlink() or not _is_inside(path, root):
            raise SaveError("path escapes EGO_SAVE_ROOT")
        _own_fd(fd, *save_ids(root))
        _write_all(fd, data)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    os.close(fd)
    return path, f"assets/{stem}/{filename}"


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise SaveError("short write")
        view = view[written:]


def _remove_if_empty(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.rmdir()


def _cleanup(path: Path, fd: int | None, files: list[Path], asset_dir: Path | None) -> None:
    if fd is not None:
        with contextlib.suppress(OSError):
            os.close(fd)
    with contextlib.suppress(OSError):
        os.unlink(path)
    for item in files:
        with contextlib.suppress(OSError):
            os.unlink(item)
    if asset_dir is not None:
        _remove_if_empty(asset_dir)
        _remove_if_empty(asset_dir.parent)


def image_fetch_mode() -> str:
    raw = os.environ.get("EGO_IMAGE_FETCH", "relay").strip().lower() or "relay"
    if raw not in ("auto", "daemon", "relay"):
        raise SaveError("EGO_IMAGE_FETCH must be relay, auto, or daemon")
    return raw


def image_budget_seconds() -> float:
    raw = os.environ.get("EGO_IMAGE_BUDGET_MS", "").strip()
    if not raw:
        return 90.0
    try:
        millis = int(raw, 10)
    except ValueError as exc:
        raise SaveError("EGO_IMAGE_BUDGET_MS must be a positive number of milliseconds") from exc
    if millis <= 0:
        raise SaveError("EGO_IMAGE_BUDGET_MS must be a positive number of milliseconds")
    return millis / 1000.0


def check_save_config(subdir: str, slug: str) -> Path:
    """Create the note directory, or raise before the browser is touched."""
    root = resolve_save_root()
    relative = parse_subdir(subdir)
    parse_slug(slug)
    load_zone()
    image_fetch_mode()
    image_budget_seconds()
    return place_directory(root, relative)


def _clean_title(title: str | None, page_title: str, slug: str) -> str:
    raw = title if isinstance(title, str) and title.strip() else page_title
    text = " ".join(raw.split())[:300] if isinstance(raw, str) else ""
    return text or slug


def _unique_urls(hits: list[ImageHit], limit: int) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.url in seen:
            continue
        seen.add(hit.url)
        if len(unique) >= limit:
            continue
        unique.append(hit.url)
    return unique


def _classify(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, ImageFetchError):
        return exc.reason, exc.retryable
    if isinstance(exc, TimeoutError):
        return "timeout_read", True
    if isinstance(exc, OSError):
        return "connect", True
    return "other", False


def _log_image(url: str, reason: str, attempt: int, *, final: bool) -> None:
    shown = url if len(url) <= 300 else url[:300] + "…"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    kind = "failed" if final else "retry"
    print(
        f"{stamp} save image {kind} reason={reason} attempt={attempt} url={shown}",
        flush=True,
    )


def _attempt_budget(connect_timeout: float, read_timeout: float) -> float:
    return connect_timeout + read_timeout + 1.0


async def _download_all(
    indexed: list[tuple[int, str]],
    directory: Path,
    stem: str,
    root: Path,
    fetch: Fetch,
    *,
    connect_timeout: float,
    read_timeout: float,
    concurrency: int,
    attempts: int,
    backoff: float,
    deadline: float | None = None,
) -> _Downloads:
    sem = asyncio.Semaphore(max(1, concurrency))
    files: list[Path] = []
    attempt_cap = _attempt_budget(connect_timeout, read_timeout)

    async def one(index: int, url: str) -> tuple[str, str | None, int, str | None]:
        reason = "other"
        started = False
        for attempt in range(1, attempts + 1):
            if deadline is not None and time.monotonic() >= deadline:
                if not started:
                    return url, None, 0, "budget"
                _log_image(url, reason, attempt - 1, final=True)
                return url, None, 0, reason
            acquired = False
            try:
                if deadline is None:
                    await sem.acquire()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if not started:
                            return url, None, 0, "budget"
                        _log_image(url, reason, attempt - 1, final=True)
                        return url, None, 0, reason
                    await asyncio.wait_for(sem.acquire(), remaining)
                acquired = True
                if deadline is not None and time.monotonic() >= deadline and not started:
                    return url, None, 0, "budget"
                started = True
                try:
                    data, ctype = await asyncio.wait_for(asyncio.to_thread(fetch, url), attempt_cap)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    reason, retryable = _classify(exc)
                    failed = True
                else:
                    failed = False
                    retryable = False
                if not failed:
                    path, rel = write_asset(directory, stem, index, url, data, ctype, root)
                    files.append(path)
                    return url, rel, len(data), None
            except TimeoutError:
                if not started:
                    return url, None, 0, "budget"
                _log_image(url, reason, attempt, final=True)
                return url, None, 0, reason
            finally:
                if acquired:
                    sem.release()
            if not retryable or attempt == attempts:
                _log_image(url, reason, attempt, final=True)
                return url, None, 0, reason
            _log_image(url, reason, attempt, final=False)
            await asyncio.sleep(backoff * (2 ** (attempt - 1)))
        _log_image(url, reason, attempts, final=True)
        return url, None, 0, reason

    tasks = [asyncio.create_task(one(index, url)) for index, url in indexed]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for item in files:
            with contextlib.suppress(OSError):
                os.unlink(item)
        files.clear()
        raise
    saved: dict[str, str] = {}
    reasons: dict[str, str] = {}
    total = 0
    for url, rel, size, reason in results:
        if rel is None:
            reasons[url] = reason or "other"
            continue
        saved[url] = rel
        total += size
    return _Downloads(saved, files, total, reasons)


_backfill_guard = asyncio.Lock()
_backfill_running: set[str] = set()


def _has_failed_marker(text: str, url: str) -> bool:
    for hit in find_http_images(text):
        if hit.url == url and text.startswith(FAILED_IMAGE_MARKER, hit.match_end):
            return True
    return False


def replace_failed_image(text: str, url: str, rel: str) -> str:
    """Turn a failed image marker for `url` into a local link. Other bytes stay."""
    while True:
        changed = False
        for hit in find_http_images(text):
            if hit.url != url or not text.startswith(FAILED_IMAGE_MARKER, hit.match_end):
                continue
            text = (
                text[:hit.url_start]
                + rel
                + text[hit.url_end:hit.match_end]
                + text[hit.match_end + len(FAILED_IMAGE_MARKER):]
            )
            changed = True
            break
        if not changed:
            return text


def body_image_counts(body: str) -> tuple[int, int, int]:
    """Distinct image totals from a note body: (images, images_ok, images_failed).

    A destination with the failure marker counts as failed. A relative `assets/`
    path counts as saved. Other destinations, including non-http tokens and
    fenced copies, are ignored.
    """
    masked = fence_ranges(body)
    ok: set[str] = set()
    failed: set[str] = set()
    for match in _IMAGE_RE.finditer(body):
        if _overlaps(match.start(), match.end(), masked):
            continue
        dest = match.group(2).strip()
        if dest.startswith("<") and dest.endswith(">"):
            dest = dest[1:-1].strip()
        if body.startswith(FAILED_IMAGE_MARKER, match.end()):
            failed.add(normalize_image_url(dest) or dest)
            continue
        if dest.startswith("assets/") and ".." not in dest.split("/") and "://" not in dest:
            ok.add(dest)
    return len(ok) + len(failed), len(ok), len(failed)


def refresh_image_frontmatter(text: str) -> str:
    """Set images_ok, images_failed, and images (when present) from the body."""
    if not text.startswith("---\n"):
        return text
    close = text.find("\n---\n", 4)
    if close < 0:
        return text
    header = text[4:close]
    body = text[close + len("\n---\n"):]
    images, ok, failed = body_image_counts(body)
    wanted = {"images_ok": str(ok), "images_failed": str(failed)}
    if any(line.startswith("images: ") for line in header.split("\n")):
        wanted["images"] = str(images)
    lines: list[str] = []
    seen: set[str] = set()
    for line in header.split("\n"):
        key, sep, _rest = line.partition(": ")
        if sep and key in wanted and key not in seen:
            lines.append(f"{key}: {wanted[key]}")
            seen.add(key)
        else:
            lines.append(line)
    for key in ("images_ok", "images_failed"):
        if key not in seen:
            lines.append(f"{key}: {wanted[key]}")
    return "---\n" + "\n".join(lines) + "\n---\n" + body


def _rewrite_note(note: Path, root: Path, url: str, rel: str) -> bool:
    if note.is_symlink() or not note.is_file() or not _is_inside(note, root):
        raise SaveError("refusing to rewrite a note outside EGO_SAVE_ROOT")
    text = note.read_text(encoding="utf-8")
    updated = replace_failed_image(text, url, rel)
    if updated == text:
        return False
    updated = refresh_image_frontmatter(updated)
    tmp = note.with_name(f".{note.name}.{os.getpid()}.tmp")
    if tmp.is_symlink() or not _is_inside(tmp, root):
        raise SaveError("refusing to rewrite a note outside EGO_SAVE_ROOT")
    fd = os.open(tmp, _OPEN_NEW, FILE_MODE)
    try:
        _own_fd(fd, *save_ids(root))
        _write_all(fd, updated.encode("utf-8"))
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    os.close(fd)
    os.replace(tmp, note)
    return True


def _sidecar(note: Path) -> Path:
    return note.with_name(note.name + ".pending.json")


def _write_sidecar(note: Path, root: Path, items: list[dict]) -> None:
    path = _sidecar(note)
    if path.is_symlink() or not _is_inside(path, root):
        raise SaveError("refusing to write a sidecar outside EGO_SAVE_ROOT")
    payload = json.dumps({"urls": items}, ensure_ascii=False).encode("utf-8")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), FILE_MODE)
    try:
        if path.is_symlink() or not _is_inside(path, root):
            raise SaveError("refusing to write a sidecar outside EGO_SAVE_ROOT")
        _own_fd(fd, *save_ids(root))
        _write_all(fd, payload)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    os.close(fd)


def _backfill_log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{stamp} {message}", flush=True)


def _local_asset(asset_dir: Path, index: int, root: Path) -> Path | None:
    """A regular file already stored for this image index, or None."""
    if index < 1 or asset_dir.is_symlink() or not asset_dir.is_dir() or not _is_inside(asset_dir, root):
        return None
    prefix = f"{index:03d}-"
    found: Path | None = None
    try:
        children = list(asset_dir.iterdir())
    except OSError:
        return None
    for child in children:
        if not child.name.startswith(prefix):
            continue
        if child.is_symlink() or not child.is_file() or not _is_inside(child, root):
            continue
        if found is not None:
            return None
        found = child
    return found


async def run_backfill(job: Mapping[str, object], daemon_fetch) -> None:
    """Fill failed images for one note. A second call for the same path does not start.

    `daemon_fetch` is `fetch_images_from_daemon` or None. This never raises into
    the save tool: the caller schedules it after the tool has returned.
    """
    raw_path = job.get("path")
    items = job.get("items")
    if not isinstance(raw_path, str) or not isinstance(items, list):
        _backfill_log("backfill skipped reason=bad-job")
        return
    try:
        root = resolve_save_root()
    except SaveError as exc:
        _backfill_log(f"backfill skipped reason={exc}")
        return
    note = Path(raw_path)
    key = str(note)
    async with _backfill_guard:
        if key in _backfill_running:
            _backfill_log(f"backfill skipped path={note} reason=in-progress")
            return
        _backfill_running.add(key)
    try:
        if note.is_symlink() or not note.is_file() or not _is_inside(note, root):
            _backfill_log(f"backfill skipped path={note} reason=outside-root")
            return
        if daemon_fetch is None:
            _backfill_log(f"backfill skipped path={note} reason=no-daemon")
            return
        try:
            text = note.read_text(encoding="utf-8")
        except OSError as exc:
            _backfill_log(f"backfill skipped path={note} reason={exc.strerror}")
            return
        pending: list[tuple[int, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            index = item.get("index")
            if not isinstance(url, str) or isinstance(index, bool) or not isinstance(index, int):
                continue
            if _has_failed_marker(text, url):
                pending.append((index, url))
        if not pending:
            with contextlib.suppress(OSError):
                side = _sidecar(note)
                if side.is_file() and not side.is_symlink() and _is_inside(side, root):
                    os.unlink(side)
            _backfill_log(f"backfill done path={note} ok=0 failed=0")
            return
        asset_dir = note.parent / "assets" / note.stem
        local = [(index, url, found) for index, url in pending if (found := _local_asset(asset_dir, index, root))]
        need = [(index, url) for index, url in pending if _local_asset(asset_dir, index, root) is None]
        ok = 0
        missed = 0
        for index, url, found in local:
            rel = f"assets/{note.stem}/{found.name}"
            try:
                if _rewrite_note(note, root, url, rel):
                    ok += 1
                    _backfill_log(f"backfill wrote path={note} index={index} bytes={found.stat().st_size}")
            except (SaveError, OSError) as exc:
                missed += 1
                _backfill_log(f"backfill reject path={note} index={index} reason={exc}")
        if not need:
            side = _sidecar(note)
            if side.is_file() and not side.is_symlink() and _is_inside(side, root):
                with contextlib.suppress(OSError):
                    os.unlink(side)
            _backfill_log(f"backfill done path={note} ok={ok} failed={missed}")
            return
        try:
            _write_sidecar(note, root, [{"url": url, "index": index} for index, url in need])
        except SaveError as exc:
            _backfill_log(f"backfill skipped path={note} reason={exc}")
            return
        _backfill_log(f"backfill start path={note} pending={len(need)}")
        try:
            got, _failed = await daemon_fetch([url for _index, url in need])
        except Exception as exc:
            _backfill_log(f"backfill skipped path={note} reason={exc}")
            return
        if not isinstance(got, dict):
            got = {}
        try:
            asset_dir = place_directory(root, asset_dir.relative_to(root))
        except (SaveError, ValueError) as exc:
            _backfill_log(f"backfill skipped path={note} reason={exc}")
            return
        for index, url in need:
            payload = got.get(url)
            if not isinstance(payload, tuple) or len(payload) != 2:
                missed += 1
                continue
            data, ctype = payload
            if (
                not isinstance(data, (bytes, bytearray))
                or not isinstance(ctype, str)
                or not acceptable_image_type(ctype)
                or len(data) > IMAGE_MAX_BYTES
            ):
                missed += 1
                _backfill_log(f"backfill reject path={note} index={index} reason=not-image")
                continue
            filename = asset_filename(index, url, ctype)
            dest = asset_dir / filename
            rel = f"assets/{note.stem}/{filename}"
            try:
                if dest.is_symlink() or not _is_inside(dest, root):
                    missed += 1
                    _backfill_log(f"backfill reject path={note} index={index} reason=escape")
                    continue
                if not (dest.is_file() and not dest.is_symlink()):
                    _written, rel = write_asset(asset_dir, note.stem, index, url, bytes(data), ctype, root)
                if _rewrite_note(note, root, url, rel):
                    ok += 1
                    _backfill_log(f"backfill wrote path={note} index={index} bytes={len(data)}")
            except SaveError as exc:
                missed += 1
                _backfill_log(f"backfill reject path={note} index={index} reason={exc}")
        left = [
            {"url": url, "index": index}
            for index, url in pending
            if _has_failed_marker(note.read_text(encoding="utf-8"), url)
        ]
        side = _sidecar(note)
        if left:
            with contextlib.suppress(SaveError):
                _write_sidecar(note, root, left)
        elif side.is_file() and not side.is_symlink() and _is_inside(side, root):
            with contextlib.suppress(OSError):
                os.unlink(side)
        _backfill_log(f"backfill done path={note} ok={ok} failed={missed}")
    finally:
        async with _backfill_guard:
            _backfill_running.discard(key)


async def save_markdown(
    page: Mapping[str, object],
    *,
    device: str,
    subdir: str,
    slug: str,
    title: str | None,
    mode: str,
    now: datetime | None = None,
    fetch: Fetch | None = None,
    daemon_fetch=None,
    image_fetch_mode_name: str | None = None,
    connect_timeout: float = IMAGE_CONNECT_TIMEOUT_S,
    read_timeout: float = IMAGE_READ_TIMEOUT_S,
    concurrency: int = IMAGE_CONCURRENCY,
    image_attempts: int = IMAGE_ATTEMPTS,
    image_backoff: float = IMAGE_RETRY_BACKOFF_S,
    max_images: int = MAX_UNIQUE_IMAGES,
    image_budget_s: float | None = None,
    allow_backfill: bool = False,
) -> dict[str, object]:
    """Write `page["text"]` to disk. The return value has no body.

    `page` is a get_page_text result: text, url, title, truncated.
    """
    text = page.get("text")
    if not isinstance(text, str):
        raise SaveError("ego daemon returned no page text")
    if mode not in PAGE_TEXT_MODES:
        raise SaveError("mode must be auto, snapshot, yitang, or generic")
    source = page.get("url") if isinstance(page.get("url"), str) else ""
    page_title = page.get("title") if isinstance(page.get("title"), str) else ""
    truncated = page.get("truncated") is True
    root = resolve_save_root()
    relative = parse_subdir(subdir)
    slug = parse_slug(slug)
    note_dir = place_directory(root, relative)
    zone = load_zone()
    local = local_now(now, zone)
    hits = find_http_images(text)
    unique = _unique_urls(hits, max_images)
    path, fd = reserve_note(note_dir, note_names(local.strftime("%Y-%m-%d"), slug), root)
    stem = path.stem
    files: list[Path] = []
    asset_dir: Path | None = None
    open_fd: int | None = fd
    images_started = time.perf_counter()
    try:
        saved: dict[str, str] = {}
        reasons: dict[str, str] = {}
        sources: dict[str, str] = {}
        if unique:
            asset_dir = place_directory(root, relative / "assets" / stem)
            indexed = list(enumerate(unique, start=1))
            chosen = image_fetch_mode_name or image_fetch_mode()
            daemon_ok: dict[str, tuple[bytes, str]] = {}
            daemon_fail: dict[str, str] = {}
            if chosen == "daemon" and daemon_fetch is None:
                daemon_fail = {url: "no_daemon" for url in unique}
            elif daemon_fetch is not None and chosen in ("auto", "daemon"):
                try:
                    got, failed = await daemon_fetch(list(unique))
                except DaemonCapabilityError:
                    if chosen == "daemon":
                        daemon_fail = {url: "no_daemon" for url in unique}
                    got, failed = {}, {}
                except Exception:
                    # The socket died mid-batch. Keep any bytes already returned
                    # and let auto mode fetch the rest here. The note still saves.
                    if chosen == "daemon":
                        daemon_fail = {url: "disconnect" for url in unique}
                    got, failed = {}, {}
                if isinstance(got, dict):
                    for url, payload in got.items():
                        if url not in set(unique) or not isinstance(payload, tuple) or len(payload) != 2:
                            continue
                        data, ctype = payload
                        if not isinstance(data, (bytes, bytearray)) or not isinstance(ctype, str):
                            daemon_fail[url] = "protocol"
                            continue
                        if len(data) > IMAGE_MAX_BYTES:
                            daemon_fail[url] = "too_large"
                            continue
                        if not acceptable_image_type(ctype):
                            daemon_fail[url] = "not_image"
                            continue
                        daemon_ok[url] = (bytes(data), ctype)
                if isinstance(failed, dict):
                    for url, reason in failed.items():
                        if url in daemon_ok or url not in set(unique):
                            continue
                        daemon_fail[url] = reason if isinstance(reason, str) and reason else "other"
            for index, url in indexed:
                if url not in daemon_ok:
                    continue
                data, ctype = daemon_ok[url]
                written, rel = write_asset(asset_dir, stem, index, url, data, ctype, root)
                files.append(written)
                saved[url] = rel
                sources[url] = "daemon"
            relay_items = [(index, url) for index, url in indexed if url not in saved]
            if chosen == "daemon":
                for _index, url in relay_items:
                    reasons[url] = daemon_fail.get(url, "no_daemon")
            elif relay_items:

                def default_fetch(image_url: str) -> tuple[bytes, str]:
                    return fetch_http(
                        image_url,
                        connect_timeout=connect_timeout,
                        read_timeout=read_timeout,
                    )

                deadline = None
                if chosen == "relay":
                    seconds = image_budget_seconds() if image_budget_s is None else image_budget_s
                    deadline = time.monotonic() + seconds
                batch = await _download_all(
                    relay_items,
                    asset_dir,
                    stem,
                    root,
                    fetch if fetch is not None else default_fetch,
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    concurrency=concurrency,
                    attempts=max(1, image_attempts),
                    backoff=image_backoff,
                    deadline=deadline,
                )
                saved.update(batch.saved)
                files.extend(batch.files)
                reasons.update(batch.reasons)
                for url in batch.saved:
                    sources[url] = "relay"
            if not files:
                _remove_if_empty(asset_dir)
                _remove_if_empty(asset_dir.parent)
                asset_dir = None
        images_ms = int(round((time.perf_counter() - images_started) * 1000))
        write_started = time.perf_counter()
        body, refs_ok, refs_failed = apply_image_results(text, hits, saved)
        failed_urls: list[str] = []
        seen_urls: set[str] = set()
        for hit in hits:
            if hit.url in seen_urls:
                continue
            seen_urls.add(hit.url)
            if hit.url not in saved:
                failed_urls.append(hit.url)
                reasons.setdefault(hit.url, "capped")
        failure_counts: dict[str, int] = {}
        for failed_url in failed_urls:
            reason = reasons.get(failed_url, "other")
            failure_counts[reason] = failure_counts.get(reason, 0) + 1
        unique_ok = len(saved)
        unique_failed = len(failed_urls)
        if failure_counts.get("budget"):
            print(
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                f"save image budget skipped={failure_counts['budget']}",
                flush=True,
            )
        pending_items = [
            {"url": url, "index": index}
            for index, url in enumerate(unique, start=1)
            if allow_backfill and url not in saved
        ]
        device_text = " ".join(device.split())[:80] if isinstance(device, str) else ""
        header = render_frontmatter([
            ("title", _clean_title(title, page_title, slug)),
            ("source_url", source),
            ("captured_at", local.isoformat(timespec="seconds")),
            ("device", device_text),
            ("mode", mode),
            ("chars", len(text)),
            ("images", unique_ok + unique_failed),
            ("images_ok", unique_ok),
            ("images_failed", unique_failed),
            ("image_refs", refs_ok + refs_failed),
            ("truncated", truncated),
        ])
        try:
            payload = (header + body).encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SaveError("page text is not valid Unicode") from exc
        _write_all(fd, payload)
        os.fsync(fd)
        os.close(fd)
        open_fd = None
        asset_bytes = sum(item.stat().st_size for item in files)
        write_ms = int(round((time.perf_counter() - write_started) * 1000))
        return {
            "path": str(path),
            "chars": len(text),
            "images_ok": unique_ok,
            "images_failed": unique_failed,
            "image_refs": refs_ok + refs_failed,
            "image_refs_ok": refs_ok,
            "image_refs_failed": refs_failed,
            "images_via_daemon": sum(1 for url in saved if sources.get(url) == "daemon"),
            "images_via_relay": sum(1 for url in saved if sources.get(url) == "relay"),
            "images_pending": len(pending_items),
            "failures": dict(sorted(failure_counts.items())),
            "bytes": path.stat().st_size + asset_bytes,
            "truncated": truncated,
            "_images_ms": images_ms,
            "_write_ms": write_ms,
            "_backfill": {"path": str(path), "items": pending_items} if pending_items else None,
        }
    except BaseException:
        _cleanup(path, open_fd, files, asset_dir)
        raise
