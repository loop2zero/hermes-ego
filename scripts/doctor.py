#!/usr/bin/env python3
"""Self-check for a hermes-ego install.

Prints one PASS, FAIL, or SKIP line per check. Exits 1 when any check fails.
Does not print tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install import env_value, read_env, read_toml_token, token_is_real  # noqa: E402

CHECKS: list[tuple[str, str]] = []


def add(level: str, message: str) -> None:
    CHECKS.append((level, message))
    print(f"{level} {message}", flush=True)


def python_version() -> tuple[int, int]:
    return sys.version_info[0], sys.version_info[1]


def check_python() -> None:
    major, minor = python_version()
    if (major, minor) >= (3, 11):
        add("PASS", f"python {major}.{minor}.{sys.version_info[2]}")
    else:
        add("FAIL", f"python {major}.{minor} is older than 3.11")


def check_uv(repo: Path) -> None:
    uv = shutil.which("uv")
    venv = repo / ".venv" / "bin" / "python"
    if uv:
        add("PASS", f"uv {uv}")
    elif venv.is_file():
        add("PASS", f"uv not on PATH; venv python {venv}")
    else:
        add("FAIL", "uv is not on PATH and .venv is missing")


def check_ego_browser(role: str) -> None:
    if role == "relay":
        add("SKIP", "ego-browser (relay role; the binary is on the Mac)")
        return
    found = shutil.which("ego-browser")
    if not found:
        candidate = Path.home() / ".local" / "bin" / "ego-browser"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            found = str(candidate)
    if not found:
        add("FAIL", "ego-browser is not on PATH or ~/.local/bin")
        return
    try:
        result = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        add("FAIL", f"ego-browser --version failed: {exc}")
        return
    version = (result.stdout or result.stderr).strip().splitlines()
    shown = version[0] if version else "no version text"
    if result.returncode != 0:
        add("FAIL", f"ego-browser --version exited {result.returncode}")
        return
    add("PASS", f"ego-browser {shown}")


def load_env_map(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        return {}
    lines = read_env(path)
    keys = (
        "EGO_RELAY_TOKEN", "EGO_RELAY_HOST", "EGO_RELAY_PORT",
        "EGO_SAVE_ROOT", "EGO_SAVE_TZ", "EGO_SAVE_UID", "EGO_IMAGE_FETCH",
    )
    return {key: env_value(lines, key) for key in keys}


def check_tokens(role: str, env_path: Path, toml_path: Path) -> tuple[str, dict[str, str]]:
    env = load_env_map(env_path)
    relay_token = env.get("EGO_RELAY_TOKEN", "")
    daemon_token = read_toml_token(toml_path) if toml_path.is_file() else ""
    if role in ("relay", "both"):
        if token_is_real(relay_token):
            add("PASS", f"relay token is set ({env_path})")
        else:
            add("FAIL", f"relay token is missing or still a placeholder ({env_path})")
    else:
        add("SKIP", "relay token (daemon role)")
    if role in ("daemon", "both"):
        if token_is_real(daemon_token):
            add("PASS", f"daemon token is set ({toml_path})")
        else:
            add("FAIL", f"daemon token is missing or still a placeholder ({toml_path})")
    else:
        add("SKIP", "daemon token (relay role)")
    if role == "both" and token_is_real(relay_token) and token_is_real(daemon_token):
        if relay_token != daemon_token:
            add("FAIL", "relay token and daemon token differ")
        else:
            add("PASS", "relay token and daemon token match")
    return relay_token if token_is_real(relay_token) else daemon_token, env


def check_save_root(role: str, env: dict[str, str]) -> None:
    if role == "daemon":
        add("SKIP", "EGO_SAVE_ROOT (daemon role; the vault is on the relay host)")
        return
    raw = env.get("EGO_SAVE_ROOT", "").strip()
    if not raw:
        add("FAIL", "EGO_SAVE_ROOT is unset; ego_save_page_markdown is disabled")
        return
    path = Path(raw)
    if not path.is_absolute():
        add("FAIL", "EGO_SAVE_ROOT is not absolute")
        return
    if not path.exists():
        add("FAIL", f"EGO_SAVE_ROOT does not exist: {path}")
        return
    if path.is_symlink():
        target = path.resolve()
        if not target.is_dir():
            add("FAIL", "EGO_SAVE_ROOT symlink does not point at a directory")
            return
    elif not path.is_dir():
        add("FAIL", "EGO_SAVE_ROOT is not a directory")
        return
    st = path.stat()
    if not stat.S_ISDIR(st.st_mode):
        add("FAIL", "EGO_SAVE_ROOT is not a directory")
        return
    if not (st.st_mode & stat.S_IWUSR):
        add("FAIL", f"EGO_SAVE_ROOT is not writable by its owner ({st.st_uid})")
        return
    uid_raw = env.get("EGO_SAVE_UID", "").strip()
    if uid_raw:
        try:
            wanted = int(uid_raw, 10)
        except ValueError:
            add("FAIL", "EGO_SAVE_UID is not an integer")
            return
        if wanted != st.st_uid:
            add("FAIL", f"EGO_SAVE_UID {wanted} does not match directory owner {st.st_uid}")
            return
    if os.access(path, os.W_OK):
        add("PASS", f"EGO_SAVE_ROOT writable {path} owner={st.st_uid}")
    else:
        add("PASS", f"EGO_SAVE_ROOT owner {st.st_uid} can write {path}; this user cannot")


def check_relay_port(host: str, port: int) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except OSError as exc:
        add("FAIL", f"relay is not reachable at {host}:{port} ({exc.errno or exc})")
        return False
    request = urllib.request.Request(
        f"http://{host}:{port}/mcp",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except urllib.error.URLError as exc:
        add("FAIL", f"relay port is open but /mcp failed ({exc.reason})")
        return False
    if status == 401:
        add("PASS", f"relay reachable {host}:{port} (unauthorized /mcp, as expected)")
        return True
    add("FAIL", f"relay /mcp returned HTTP {status}, expected 401 without a token")
    return False


def journal_verdict(unit_states: list[tuple[str, str]], log_text: str) -> tuple[str, str]:
    failed = [name for name, state in unit_states if state == "failed"]
    if failed:
        return "FAIL", "hermes-ego-relay systemd unit is failed"
    if "Traceback (most recent call last)" in log_text:
        return "FAIL", "relay journal contains a traceback"
    active = [name for name, state in unit_states if state == "active"]
    if active:
        return "PASS", "relay journal healthy (" + ", ".join(active) + " unit active)"
    return "SKIP", "no active hermes-ego-relay systemd unit"


def collect_journal() -> tuple[list[tuple[str, str]], str]:
    systemctl = shutil.which("systemctl")
    journalctl = shutil.which("journalctl")
    states: list[tuple[str, str]] = []
    logs: list[str] = []
    if systemctl is None or journalctl is None:
        return states, ""
    scopes = [("user", ["--user"]), ("system", [])]
    for name, scope in scopes:
        active = subprocess.run(
            [systemctl, *scope, "is-active", "hermes-ego-relay"],
            capture_output=True, text=True, timeout=5,
        )
        state = (active.stdout or active.stderr or "").strip().splitlines()
        states.append((name, state[0] if state else "unknown"))
        logged = subprocess.run(
            [journalctl, *scope, "-u", "hermes-ego-relay", "-n", "200", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=5,
        )
        if logged.returncode == 0 and logged.stdout.strip():
            logs.append(logged.stdout)
    return states, "\n".join(logs)


async def probe_mcp(host: str, port: int, token: str) -> None:
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:
        add("FAIL", "mcp package is not installed; run uv sync in the repo")
        return
    try:
        import httpx2 as httpx
    except ImportError:
        try:
            import httpx
        except ImportError:
            add("FAIL", "httpx is not installed; run uv sync in the repo")
            return
    url = f"http://{host}:{port}/mcp"
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(20.0, read=25.0),
        ) as client:
            async with streamable_http_client(url, http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    names = sorted(tool.name for tool in listed.tools)
                    if "ego_save_page_markdown" in names:
                        add("PASS", "ego_save_page_markdown is registered")
                    else:
                        add("FAIL", "ego_save_page_markdown is not in the MCP tool list")
                    result = await session.call_tool("ego_status", {})
                    text = "".join(getattr(block, "text", str(block)) for block in (result.content or []))
                    status = json.loads(text)
    except Exception as exc:
        add("FAIL", f"MCP probe failed ({type(exc).__name__})")
        return
    if not isinstance(status, dict):
        add("FAIL", "ego_status did not return an object")
        return
    if status.get("online") is True:
        device = status.get("device") or "?"
        add("PASS", f"daemon connected device={device}")
        caps = status.get("capabilities")
        if isinstance(caps, list) and "fetch_images" in caps:
            add("PASS", "capabilities=fetch_images")
        else:
            add("FAIL", "daemon is connected but capabilities do not include fetch_images")
        if status.get("egoReachable") is True:
            add("PASS", f"ego lite reachable version={status.get('egoVersion')}")
        else:
            reason = status.get("egoError") or "ego lite is not reachable"
            add("FAIL", f"ego lite is not reachable ({reason})")
    else:
        add("FAIL", "daemon is not connected")
        add("FAIL", "capabilities=fetch_images not negotiated")


def infer_role(env_path: Path, toml_path: Path, explicit: str) -> str:
    if explicit != "auto":
        return explicit
    has_env = env_path.is_file()
    has_toml = toml_path.is_file()
    if has_env and has_toml:
        return "both"
    if has_toml and not has_env:
        return "daemon"
    if has_env:
        return "relay"
    return "both" if platform.system() == "Darwin" else "relay"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check hermes-ego and exit non-zero on FAIL.")
    parser.add_argument("--repo", default="")
    parser.add_argument("--role", choices=["auto", "relay", "daemon", "both"], default="auto")
    parser.add_argument("--config-dir", default="")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    CHECKS.clear()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parents[1]
    config_dir = Path(args.config_dir).expanduser() if args.config_dir else Path.home() / ".config" / "hermes-ego"
    env_path = config_dir / "relay.env"
    toml_path = config_dir / "config.toml"
    role = infer_role(env_path, toml_path, args.role)
    add("PASS", f"role={role}")
    check_python()
    check_uv(repo)
    check_ego_browser(role)
    token, env = check_tokens(role, env_path, toml_path)
    check_save_root(role, env)
    host = args.host.strip() or env.get("EGO_RELAY_HOST", "").strip() or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port_raw = args.port.strip() or env.get("EGO_RELAY_PORT", "").strip() or "8767"
    try:
        port = int(port_raw, 10)
    except ValueError:
        add("FAIL", "EGO_RELAY_PORT is not an integer")
        port = 0
    reachable = False
    if port:
        reachable = check_relay_port(host, port)
    if reachable and token_is_real(token):
        try:
            asyncio.run(probe_mcp(host, port, token))
        except Exception as exc:
            add("FAIL", f"MCP probe failed ({type(exc).__name__})")
    elif not token_is_real(token):
        add("FAIL", "MCP tool list skipped because the token is not set")
    else:
        add("FAIL", "MCP tool list skipped because the relay is not reachable")
    try:
        states, log_text = collect_journal()
    except (OSError, subprocess.TimeoutExpired):
        states, log_text = [], ""
    level, message = journal_verdict(states, log_text)
    add(level, message)
    failed = [line for level, line in CHECKS if level == "FAIL"]
    print(f"{len(CHECKS) - len(failed)} ok, {len(failed)} failed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
