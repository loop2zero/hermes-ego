# Hermes Ego

Portable Agent Plugins v1 package. Hermes reads and saves pages in the user's [ego lite](https://lite.ego.app/) browser by calling a loopback MCP relay. The relay and the Mac daemon are separate programs. This directory does not start them, does not embed them, and does not download them. That is the same split as the catalog entry `local-mcp`: the plugin is a thin client, and the sidecar is installed on its own.

Public repository (not created by this packaging change): https://github.com/loop2zero/hermes-ego

Topology, with no hostnames or addresses beyond loopback:

```
Hermes host  →  loopback MCP relay  →  SSH local forward  →  Mac daemon  →  ego(lite) browser
```

On one machine the SSH forward is absent and the daemon uses the same loopback relay. The Hermes host can be Linux. The daemon and ego(lite) run on a Mac.

## What Hermes loads

| File | Role |
| --- | --- |
| `plugin.json` | Manifest. Server `ego` is `trust: untrusted`. Secret `relay_token` is stored as `EGO_RELAY_TOKEN`. |
| `mcp.json` | Streamable HTTP at `http://127.0.0.1:8767/mcp`. The bearer is `${EGO_RELAY_TOKEN}`, not a literal token. |
| `skills/hermes-ego/SKILL.md` | When to call `ego_get_page_text`, `ego_save_page_markdown`, and `ego_status`. |

`plugin.json` has no `title` field in Agent Plugins v1. The catalog display title is `Hermes Ego`.

## MCP wiring

Streamable HTTP, not a stdio launcher.

The plugin developer guide routes a portable `streamable-http` entry through Hermes's native URL MCP client, and it allows plain HTTP only for localhost or a loopback address. `http://127.0.0.1:8767/mcp` is that case. The header name `headers` is the portable remote field (`type`, `url`, `headers`).

On Hermes Agent v0.23.0, `tools/mcp_tool_config.py` `_portable_mcp_servers` runs `_interpolate_env_vars` on that entry. A `${VAR}` left in `url` or `headers` is refused with "not set in this profile's .env". That is the same expansion native `mcp_servers` headers use, and it is why the placeholder survives a reconnect (NousResearch/hermes-agent#148879, in the v0.23.0 tag). The token value is not written in this file.

The developer guide's portable section tells you not to put a credential in `mcp.json`, and it spells `"NAME": "${NAME}"` env passthrough for stdio servers (current guide; the v0.23.0 copy of that page does not yet contain that sentence). A stdio launcher would be the path if a header could not carry `${EGO_RELAY_TOKEN}`. On v0.23.0 the header can, so this package does not add one.

## Install

Two layers.

**A. Relay and Mac daemon.** From the repository root, `scripts/install.sh` and `scripts/doctor.sh`. See the root README. This package does not replace that step. ego(lite) has to be installed on the Mac already. This repo does not ship it, and this packaging change did not verify a checksum-pinned download URL. The product page linked above is the reference already used by the repo. Leave the ego(lite) GUI running.

**B. This plugin.** Put `EGO_RELAY_TOKEN` in the Hermes profile `.env` (the same value the relay uses). The plugin settings field `relay_token` stores that env name through Hermes. Do not commit the token. Do not copy `relay.env`.

Check the package, then enable it. Catalog CI runs the same validate command:

```bash
hermes plugins validate plugins/hermes-ego --install-deps
```

Pass an absolute path if the shell is not at the repo root. There are no Python dependencies in this directory; `--install-deps` is still the admission command.

Until https://github.com/loop2zero/hermes-ego exists, copy this directory to `~/.hermes/plugins/hermes-ego` (root `plugin.json` in that folder; anything nested deeper is ignored) and enable it:

```bash
hermes plugins enable hermes-ego
```

After that public repository exists, the current plugins guide installs a package that is not at the repo root as `owner/repo/subdir` (the iris-chat catalog page uses that shape):

```bash
hermes plugins install loop2zero/hermes-ego/plugins/hermes-ego --no-enable
hermes plugins enable hermes-ego
```

If this Hermes rejects the extra path segment, copy this directory to `~/.hermes/plugins/hermes-ego` as above. That is the layout the developer guide discovers.

Once the catalog entry is merged, `hermes plugins install hermes-ego` installs the reviewed pin. Portable packages stay disabled until enable. Start a new Hermes session so the MCP server connects.

If `~/.hermes/config.yaml` already has `mcp_servers.ego`, that entry replaces this package's server, including `trust`. Delete it, or set `trust: untrusted` on it, or the approval gate is gone. The hand-written block in the root README is that same server.

## Tools

The relay registers these MCP tools. This package does not call `ctx.register_tool`.

| Tool | Use |
| --- | --- |
| `ego_get_page_text` | Pass `url` so open and read are one call. |
| `ego_save_page_markdown` | Save. Do not retype the page with `write_file`. |
| `ego_status` | Daemon socket and whether ego(lite) answered. |
| `ego_navigate` | Open only. Not atomic with a later read. |
| `ego_list_tabs` | Agent tabs in the `hermes-ego` task space. |

## Security / disclosures (catalog rule 13)

- **Long-running relay.** Something else must keep the relay listening on `127.0.0.1:8767`. This package only connects to it.
- **Mac daemon.** A separate process on the Mac runs the ego CLI against ego(lite). It dials out to the relay. The Mac does not accept an inbound connection from Hermes.
- **Logged-in browser.** Tools act in the user's ego(lite) session, so pages that user is logged into are readable. There is no click or type tool. The daemon uses one task space, `hermes-ego`, and pages that space opened.
- **Filesystem write.** `ego_save_page_markdown` writes markdown under the relay's configured save root (for example an Obsidian vault). It does not write outside that root.
- **Image download.** Saving a page downloads third-party image URLs referenced by the page. Some of those downloads can finish in a background pass after the tool returns.
- **Network.** Hermes calls loopback only. A remote Hermes host reaches that loopback through an SSH local forward. The relay is not a public port. Page fetches and image downloads leave the machine as those tools run.
- **Credential.** The bearer token is `EGO_RELAY_TOKEN` in the Hermes profile environment (settings secret or `.env`). It is not committed. This package does not read `relay.env`, `auth.json`, or a browser profile.
- **Approvals.** `trust: untrusted` asks before every tool that lacks `readOnlyHint: true`. The relay does not set that hint, so reads ask too, and unattended runs fail closed.
- **No telemetry and no self-update** in this package.

## License

MIT. Copyright (c) 2026 Shawn Yuan. The relay implementation elsewhere in the repo is derived from hermes-tabs; that notice stays in the repository `LICENSE` and `NOTICE`.
