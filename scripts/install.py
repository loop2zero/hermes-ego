#!/usr/bin/env python3
"""Install hermes-ego config, a virtualenv, and the service unit for this OS.

Mode A (same machine): relay and daemon together. On a Mac that is the default.
Mode B (split): Linux installs the relay, a Mac installs the daemon. Pass
--ssh-target user@vps on the Mac to add an SSH local forward.

An existing token that is not a placeholder is never replaced and never printed.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

PLACEHOLDERS = {
    "",
    "replace-with-a-long-random-string",
    "replace-with-the-relay-token",
}
SSH_TARGET_RE = re.compile(r"^[A-Za-z0-9._+-]+@[A-Za-z0-9._-]+$")
RELAY_URL_RE = re.compile(r"^wss?://[^\s]+$")


def token_is_real(value: str) -> bool:
    return value.strip() not in PLACEHOLDERS and len(value.strip()) >= 16


def default_role() -> str:
    return "both" if platform.system() == "Darwin" else "relay"


def quote_env(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_env(path: Path) -> list[str]:
    if not path.exists():
        return []
    if path.is_symlink():
        raise SystemExit(f"refusing to follow symlink {path}")
    return path.read_text(encoding="utf-8").splitlines()


def env_value(lines: list[str], key: str) -> str:
    prefix = key + "="
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def upsert_env(lines: list[str], key: str, value: str) -> list[str]:
    prefix = key + "="
    commented = "# " + prefix
    out: list[str] = []
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(prefix) or stripped.startswith(commented) or stripped.startswith("#" + prefix):
            if not found:
                out.append(f"{key}={quote_env(value)}")
                found = True
            continue
        out.append(line)
    if not found:
        out.append(f"{key}={quote_env(value)}")
    return out


def write_secret(path: Path, text: str) -> None:
    if path.is_symlink():
        raise SystemExit(f"refusing to follow symlink {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def read_toml_token(path: Path) -> str:
    if not path.exists():
        return ""
    if path.is_symlink():
        raise SystemExit(f"refusing to follow symlink {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("token") and "=" in stripped:
            raw = stripped.split("=", 1)[1].strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                raw = raw[1:-1]
            return raw
    return ""


def upsert_toml_line(lines: list[str], key: str, rendered: str) -> list[str]:
    out: list[str] = []
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(key) and "=" in stripped and stripped.split("=", 1)[0].strip() == key:
            if not found:
                out.append(f"{key} = {rendered}")
                found = True
            continue
        out.append(line)
    if not found:
        out.append(f"{key} = {rendered}")
    return out


def xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fill_plist(template: str, mapping: dict[str, str]) -> str:
    text = template
    for key, value in mapping.items():
        text = text.replace(key, xml_escape(value))
    if "__" in text:
        raise SystemExit("plist template still has an unfilled placeholder")
    return text


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "uv"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def sync_venv(repo: Path) -> Path:
    if os.environ.get("HERMES_EGO_SKIP_SYNC") == "1":
        python = repo / ".venv" / "bin" / "python"
        return python if python.is_file() else Path(sys.executable)
    uv = find_uv()
    if uv is None:
        python = repo / ".venv" / "bin" / "python"
        if python.is_file():
            print(f"uv is not installed; using {python}")
            return python
        raise SystemExit("uv is not installed and .venv is missing. Install uv, then re-run scripts/install.sh.")
    subprocess.run([uv, "sync"], cwd=repo, check=True)
    python = repo / ".venv" / "bin" / "python"
    if not python.is_file():
        raise SystemExit("uv sync did not create .venv/bin/python")
    return python


def choose_token(existing: str, supplied: str, *, generate: bool) -> tuple[str, str]:
    """Return (token, note). Never invent a second token when one is already real."""
    if token_is_real(existing):
        if supplied and supplied.strip() != existing.strip():
            return existing, "kept existing token; the new value was ignored"
        return existing, "kept existing token"
    if supplied.strip():
        if not token_is_real(supplied):
            raise SystemExit("the supplied token is empty or too short")
        return supplied.strip(), "wrote the supplied token"
    if generate:
        return secrets.token_urlsafe(32), "wrote a new token"
    if sys.stdin.isatty():
        import getpass
        entered = getpass.getpass("EGO_RELAY_TOKEN (input hidden): ")
        if not token_is_real(entered):
            raise SystemExit("that token is empty or too short")
        return entered.strip(), "wrote the token from the prompt"
    raise SystemExit(
        "no token is configured. Re-run with a tty, or set EGO_RELAY_TOKEN in the environment "
        "for this command only. The installer does not print the value."
    )


def zone_ok(name: str) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(f"EGO_SAVE_TZ is not an IANA timezone: {name}") from exc


def systemd_text(repo: Path, env_file: Path, python: Path, *, system: bool) -> str:
    wanted = "multi-user.target" if system else "default.target"
    return "\n".join([
        "[Unit]",
        "Description=hermes-ego relay (Mac daemon WebSocket <-> Hermes MCP)",
        "After=network-online.target",
        "",
        "[Service]",
        f"WorkingDirectory={repo}",
        f"EnvironmentFile={env_file}",
        f"ExecStart={python} -m relay",
        "Restart=on-failure",
        "RestartSec=2",
        "",
        "[Install]",
        f"WantedBy={wanted}",
        "",
    ])


def install_systemd(repo: Path, env_file: Path, python: Path, unit_dir: Path, *, start: bool) -> None:
    system = os.geteuid() == 0 and unit_dir == Path("/etc/systemd/system")
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "hermes-ego-relay.service"
    unit.write_text(systemd_text(repo, env_file, python, system=system), encoding="utf-8")
    os.chmod(unit, 0o644)
    print(f"wrote {unit}")
    if not start:
        print("did not start the relay")
        return
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        print("systemctl is not on PATH; start the unit after installing systemd")
        return
    scope: list[str] = [] if system else ["--user"]
    subprocess.run([systemctl, *scope, "daemon-reload"], check=False)
    result = subprocess.run([systemctl, *scope, "enable", "--now", "hermes-ego-relay"], check=False)
    if result.returncode != 0:
        raise SystemExit("systemctl enable --now hermes-ego-relay failed")
    if not system:
        print('If the relay should survive logout: loginctl enable-linger "$USER"')


def launchctl_install(label: str, plist_path: Path, *, start: bool) -> None:
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"wrote {plist_path}")
    if not start:
        print(f"did not bootstrap {label} (--no-start)")
        return
    if shutil.which("launchctl") is None:
        print("launchctl is not on PATH; bootstrap the plist on the Mac")
        return
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{label}"
    if subprocess.run(["launchctl", "print", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        subprocess.run(["launchctl", "bootout", target], check=False)
    result = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=False)
    if result.returncode != 0:
        raise SystemExit(f"launchctl bootstrap failed for {label}")


def write_plist(src: Path, dest: Path, mapping: dict[str, str]) -> None:
    text = fill_plist(src.read_text(encoding="utf-8"), mapping)
    if dest.is_symlink():
        raise SystemExit(f"refusing to follow symlink {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install hermes-ego for mode A (same machine) or mode B (split).")
    parser.add_argument("--repo", default="")
    parser.add_argument("--role", choices=["auto", "relay", "daemon", "both"], default="auto")
    parser.add_argument("--token", default="", help="EGO_RELAY_TOKEN. Ignored when a real token is already configured. Visible in the process list; a prompt is quieter.")
    parser.add_argument("--save-root", default="")
    parser.add_argument("--save-tz", default="UTC")
    parser.add_argument("--image-fetch", choices=["relay", "auto", "daemon"], default="relay")
    parser.add_argument("--image-budget-ms", default="90000")
    parser.add_argument("--ssh-target", default="", help="Mac split mode: user@vps for an SSH local forward of port 8767.")
    parser.add_argument("--relay-url", default="", help="Daemon WebSocket URL. Default ws://127.0.0.1:8767/agent (same machine or the SSH forward).")
    parser.add_argument("--device", default="")
    parser.add_argument("--config-dir", default="")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--unit-dir", default="")
    parser.add_argument("--no-start", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parents[1]
    role = default_role() if args.role == "auto" else args.role
    system = platform.system()
    config_dir = Path(args.config_dir).expanduser() if args.config_dir else Path.home() / ".config" / "hermes-ego"
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else Path.home() / ".local" / "state" / "hermes-ego"
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.mkdir(parents=True, exist_ok=True)
    zone_ok(args.save_tz)
    try:
        budget = int(args.image_budget_ms, 10)
    except ValueError as exc:
        raise SystemExit("EGO_IMAGE_BUDGET_MS must be a positive integer") from exc
    if budget <= 0:
        raise SystemExit("EGO_IMAGE_BUDGET_MS must be a positive integer")
    relay_url = args.relay_url.strip() or "ws://127.0.0.1:8767/agent"
    if not RELAY_URL_RE.match(relay_url):
        raise SystemExit("--relay-url must be a ws:// or wss:// URL")
    if args.ssh_target and not SSH_TARGET_RE.match(args.ssh_target):
        raise SystemExit("--ssh-target must look like user@host")
    if role in ("relay", "both"):
        save_root = Path(args.save_root).expanduser() if args.save_root else (
            Path("/root/hermes-notes") if os.geteuid() == 0 else Path.home() / "hermes-notes"
        )
        if not save_root.is_absolute():
            raise SystemExit("EGO_SAVE_ROOT must be absolute")
        save_root.mkdir(parents=True, exist_ok=True)
    else:
        save_root = Path(args.save_root).expanduser() if args.save_root else None

    python = sync_venv(repo)
    supplied = args.token.strip() or os.environ.get("EGO_RELAY_TOKEN", "").strip()
    notes: list[str] = []
    relay_token = ""

    if role in ("relay", "both"):
        env_path = config_dir / "relay.env"
        lines = read_env(env_path)
        relay_token, note = choose_token(env_value(lines, "EGO_RELAY_TOKEN"), supplied, generate=True)
        notes.append(f"relay env: {note} ({env_path})")
        lines = upsert_env(lines, "EGO_RELAY_TOKEN", relay_token)
        lines = upsert_env(lines, "EGO_RELAY_HOST", "127.0.0.1")
        lines = upsert_env(lines, "EGO_RELAY_PORT", "8767")
        lines = upsert_env(lines, "EGO_SAVE_ROOT", str(save_root))
        lines = upsert_env(lines, "EGO_SAVE_TZ", args.save_tz)
        lines = upsert_env(lines, "EGO_IMAGE_FETCH", args.image_fetch)
        lines = upsert_env(lines, "EGO_IMAGE_BUDGET_MS", str(budget))
        if not any(line.startswith("# hermes-ego relay") for line in lines):
            lines.insert(0, "# hermes-ego relay. Mode 0600. Do not commit this file.")
        write_secret(env_path, "\n".join(lines) + "\n")
        print(f"wrote {env_path} mode 0600")

    if role in ("daemon", "both"):
        toml_path = config_dir / "config.toml"
        existing = read_toml_token(toml_path)
        daemon_source = relay_token or supplied
        daemon_token, note = choose_token(existing, daemon_source, generate=role == "both" and not daemon_source)
        notes.append(f"daemon config: {note} ({toml_path})")
        lines = []
        if toml_path.exists() and not toml_path.is_symlink():
            lines = toml_path.read_text(encoding="utf-8").splitlines()
        device = args.device.strip() or re.sub(r"[^A-Za-z0-9._-]+", "-", platform.node()).strip("-")[:80] or "mac"
        lines = upsert_toml_line(lines, "relay_url", toml_string(relay_url))
        lines = upsert_toml_line(lines, "token", toml_string(daemon_token))
        lines = upsert_toml_line(lines, "device", toml_string(device))
        if not any(line.startswith("# hermes-ego daemon") for line in lines):
            lines.insert(0, "# hermes-ego daemon. Mode 0600. The token matches EGO_RELAY_TOKEN. Do not commit this file.")
        write_secret(toml_path, "\n".join(lines) + "\n")
        print(f"wrote {toml_path} mode 0600")

    start = not args.no_start
    if role in ("relay", "both") and system == "Linux":
        if args.unit_dir:
            unit_dir = Path(args.unit_dir)
        elif os.geteuid() == 0:
            unit_dir = Path("/etc/systemd/system")
        else:
            unit_dir = Path.home() / ".config" / "systemd" / "user"
        install_systemd(repo, config_dir / "relay.env", python, unit_dir, start=start and not args.unit_dir)
        if args.unit_dir and start:
            print(f"unit dir is {unit_dir}; not calling systemctl")
    if role in ("relay", "both") and system == "Darwin":
        agents = Path.home() / "Library" / "LaunchAgents"
        write_plist(
            repo / "deploy" / "com.hermes.ego.relay.plist",
            agents / "com.hermes.ego.relay.plist",
            {"__HOME__": str(Path.home()), "__REPO__": str(repo), "__PYTHON__": str(python)},
        )
        launchctl_install("com.hermes.ego.relay", agents / "com.hermes.ego.relay.plist", start=start)
    if role in ("daemon", "both") and system == "Darwin":
        agents = Path.home() / "Library" / "LaunchAgents"
        daemon_plist = agents / "com.hermes.ego.daemon.plist"
        template = (repo / "deploy" / "com.hermes.ego.daemon.plist").read_text(encoding="utf-8")
        template = template.replace("/Users/YOU/hermes-ego", xml_escape(str(repo))).replace("/Users/YOU", xml_escape(str(Path.home())))
        if daemon_plist.is_symlink():
            raise SystemExit(f"refusing to follow symlink {daemon_plist}")
        daemon_plist.parent.mkdir(parents=True, exist_ok=True)
        daemon_plist.write_text(template, encoding="utf-8")
        launchctl_install("com.hermes.ego.daemon", daemon_plist, start=start)
        if args.ssh_target:
            write_plist(
                repo / "deploy" / "com.hermes.ego.tunnel.plist",
                agents / "com.hermes.ego.tunnel.plist",
                {"__HOME__": str(Path.home()), "__SSH_TARGET__": args.ssh_target},
            )
            launchctl_install("com.hermes.ego.tunnel", agents / "com.hermes.ego.tunnel.plist", start=start)
    if role in ("daemon", "both") and system != "Darwin":
        print("launchd is Mac-only. The daemon config is written; install the plist on the Mac with scripts/install.sh --role daemon.")

    print("")
    print("Next steps")
    for line in notes:
        print(f"- {line}")
    if role in ("relay", "both"):
        print(f"- Notes go under {save_root}. The relay listens on 127.0.0.1:8767.")
        print("- Hermes on this host uses http://127.0.0.1:8767/mcp with the bearer token from the relay env.")
        print("- Channel prompt: call ego_save_page_markdown. Do not transcribe the page with write_file or apply_patch.")
    if role == "relay":
        print("- On the Mac, run scripts/install.sh --role daemon. Use the same token. This script does not print it.")
        print("- Mac split access is an SSH local forward (ssh -N -L 127.0.0.1:8767:127.0.0.1:8767 user@vps) or Tailscale. Pass --ssh-target user@vps to install the tunnel agent.")
        print("- If SSH upload is slow because Clash sends the VPS public IP through a proxy, add a Clash DIRECT rule for that IP. The relay still binds 127.0.0.1.")
    if role in ("daemon", "both"):
        print("- Leave the ego lite GUI running. The daemon does not start it.")
        print(f"- Daemon relay URL is {relay_url}.")
    if system == "Linux" and role in ("relay", "both") and os.geteuid() != 0:
        print("- The live VPS copy is sometimes a system unit at /root/hermes-ego, not this user unit. Use that unit if it is the one already in production.")
    print("- Run scripts/doctor.sh. It does not print the token.")


if __name__ == "__main__":
    main()
