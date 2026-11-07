# hermes-ego

Hermes drives [ego lite](https://lite.ego.app/) through an MCP relay and a Mac daemon. The daemon dials out. The Mac does not accept a connection. This is a link-proving prototype for the Hermes Agent Plugin Hackathon. The catalog package is the portable Agent Plugins v1 directory `plugins/hermes-ego` (`plugin.json`, not a native `plugin.yaml` Python plugin). The relay and the Mac daemon stay separately installed programs, the same split as the catalog entry `local-mcp`.

Two ways to run it:

**A. Same machine.** Hermes, the relay, the daemon, and ego lite are on one Mac. The relay binds `127.0.0.1:8767`. The daemon uses `ws://127.0.0.1:8767/agent`. Linux can run the relay half of this; the daemon still needs a Mac with ego lite.

**B. Split.** The relay and Hermes run on a Linux VPS. `EGO_RELAY_HOST=127.0.0.1`, so the relay is not on a public port. The Mac daemon reaches it through an SSH local forward, or through Tailscale when the tailnet ACL allows that address. Shawn's production link is this shape.

```
Mode A, one Mac                         Mode B, split
┌──────────────────────────┐            Mac                              VPS
│ Hermes ──MCP──▶ relay    │            ┌─────────────────────┐         ┌─────────────────────┐
│ ego lite ◀── daemon ─────┘            │ ego lite            │  SSH -L │ Hermes              │
│              ws://127.0.0.1:8767      │ daemon ─────────────┼────────▶│ relay 127.0.0.1:8767│
└──────────────────────────┘            └─────────────────────┘         └─────────────────────┘
```

The relay's socket-and-MCP shape is derived from [hermes-tabs](https://github.com/ai-ag2026/hermes-tabs) (MIT). That copyright is in `LICENSE` and `NOTICE`.

## Quickstart

Two layers. **Layer A** is the relay and the Mac daemon, below. **Layer B** is the portable plugin Hermes loads, in [Hermes plugin (layer B)](#hermes-plugin-layer-b). The plugin does not start the relay or the daemon. Layer A has to be up before the plugin's tools can answer.

Install [uv](https://docs.astral.sh/uv/). On a Mac, install ego lite and leave its GUI running. This repository does not ship ego lite, and this packaging change did not verify a checksum-pinned download URL. The product page linked at the top is the reference already in this tree. Install ego lite from a source you trust. `scripts/doctor.sh` looks for `ego-browser` on a daemon role.

`scripts/install.sh` creates `~/.config/hermes-ego` (mode `0600`), writes config, runs `uv sync`, and installs a service unit. It does not print tokens. A token that is already set, and is not the example placeholder, is left alone.

Mode A on a Mac (relay + daemon):

```bash
git clone <this repo> hermes-ego && cd hermes-ego
scripts/install.sh
scripts/doctor.sh
```

The default role on macOS is `both`. The relay unit is `com.hermes.ego.relay`. The daemon unit is `com.hermes.ego.daemon`. Notes default to `~/hermes-notes`.

Mode B, relay on the VPS. Root gets a system unit. Any other user gets a systemd user unit:

```bash
git clone <this repo> hermes-ego && cd hermes-ego
scripts/install.sh --role relay --save-root /root/hermes-notes --save-tz Europe/Berlin
scripts/doctor.sh --role relay
```

Mode B, daemon on the Mac. `--ssh-target` installs `com.hermes.ego.tunnel`, which runs `ssh -N -L 127.0.0.1:8767:127.0.0.1:8767 user@vps`. The daemon then uses `ws://127.0.0.1:8767/agent`. For Tailscale instead, omit `--ssh-target` and pass `--relay-url wss://<vps>.<tailnet>.ts.net/agent`.

```bash
scripts/install.sh --role daemon --ssh-target user@vps
scripts/doctor.sh --role daemon
```

Use the same token on both sides. The installer will not print it. On the relay it lives in `~/.config/hermes-ego/relay.env` as `EGO_RELAY_TOKEN`. On the Mac it is `token` in `~/.config/hermes-ego/config.toml`. `--token` is accepted and then ignored when a real token is already stored. It is visible in the process list; the hidden prompt (or `EGO_RELAY_TOKEN` in the environment for that one command) is quieter.

`scripts/doctor.sh` prints `PASS`, `FAIL`, and `SKIP` lines and exits non-zero when any check fails. It checks Python 3.11+, uv or the repo venv, `ego-browser` on a daemon role, that a token is set, that `EGO_SAVE_ROOT` exists and its owner can write it, that the relay answers on `/mcp`, that `ego_save_page_markdown` is in the MCP tool list, that the daemon is connected with `capabilities=fetch_images`, that ego lite answered, and that the systemd unit is not failed and its recent journal has no traceback.

## Environment

| Variable | Where | Default |
| --- | --- | --- |
| `EGO_RELAY_TOKEN` | relay env | required. Bearer for `/mcp` and the daemon hello. |
| `EGO_RELAY_HOST` | relay env | `127.0.0.1`. Leave it there in both modes. |
| `EGO_RELAY_PORT` | relay env | `8767` |
| `EGO_SAVE_ROOT` | relay env | unset disables `ego_save_page_markdown` only |
| `EGO_SAVE_TZ` | relay env | `UTC`. An IANA name, for example `Europe/Berlin`. |
| `EGO_SAVE_UID`, `EGO_SAVE_GID` | relay env | uid/gid of `EGO_SAVE_ROOT` |
| `EGO_IMAGE_FETCH` | relay env | `relay` |
| `EGO_IMAGE_BUDGET_MS` | relay env | `90000` |
| `HERMES_EGO_ENV` | Mac relay launchd | path of `relay.env`. systemd uses `EnvironmentFile` instead. |
| `relay_url`, `token`, `device` | daemon `config.toml` | `ws://127.0.0.1:8767/agent` after install |

`/mcp` requires `Authorization: Bearer`. A request with an `Origin` header is rejected with 403. `GET /mcp` is 405. The daemon sends the token in the first WebSocket message, not in the URL. A bad token is closed with 1008.

## Hermes plugin (layer B)

`plugins/hermes-ego` is what a catalog `subdir` points at. Hermes connects to the relay that layer A already bound at `http://127.0.0.1:8767/mcp`. The bearer is `${EGO_RELAY_TOKEN}` in `mcp.json`. The token is not in the file. Put the same value the relay uses into the Hermes profile environment, as `EGO_RELAY_TOKEN` (the plugin settings field `relay_token` stores that name). Do not commit it.

Check the package with the command catalog CI runs:

```bash
hermes plugins validate plugins/hermes-ego --install-deps
```

Use an absolute path when the shell is not at the repo root. This directory has no Python dependencies. `--install-deps` is still the admission flag.

Until the public repository exists, copy `plugins/hermes-ego` to `~/.hermes/plugins/hermes-ego` so `plugin.json` is directly inside that folder, then:

```bash
hermes plugins enable hermes-ego
```

After https://github.com/loop2zero/hermes-ego exists, the current plugins guide installs a package that is not at the repo root as `owner/repo/subdir` (the iris-chat catalog page uses that shape):

```bash
hermes plugins install loop2zero/hermes-ego/plugins/hermes-ego --no-enable
hermes plugins enable hermes-ego
```

If this Hermes rejects the extra path segment, use the copy into `~/.hermes/plugins/hermes-ego` above. That layout is what the developer guide discovers.

Portable packages stay disabled until enable. Start a new session so the MCP server connects. Details, including why this is Streamable HTTP rather than a stdio launcher, are in `plugins/hermes-ego/README.md`.

The server name is `ego`. A `mcp_servers.ego` block already in `~/.hermes/config.yaml` replaces the package entry, including `trust: untrusted`. Remove that block, or set `trust: untrusted` on it.

## Hermes

The hand-written server below is the same connection the plugin declares. Prefer layer B so the catalog pin and the untrusted gate travel with the package. Use this block only when you are not enabling the plugin. If both exist, the block wins and drops the package's trust unless you add `trust: untrusted`.

In `~/.hermes/.env`:

```bash
EGO_RELAY_TOKEN=the-same-token-as-the-relay
```

In `~/.hermes/config.yaml`, Hermes on the same host as the relay (mode A, or mode B on the VPS):

```yaml
mcp_servers:
  ego:
    url: http://127.0.0.1:8767/mcp
    headers:
      Authorization: "Bearer ${EGO_RELAY_TOKEN}"
```

Checked against the Hermes docs on 2026-11-06. `url`, `headers`, and `${EGO_RELAY_TOKEN}` from `~/.hermes/.env` are documented. `transport: http` is what [hermes-tabs](https://github.com/ai-ag2026/hermes-tabs) ships; the Hermes config reference treats streamable HTTP as the default when `url` is set and says `transport: sse` selects SSE. If a build rejects `transport: http`, delete that line. This repo was not run against a live Hermes process.

Discord or any other Hermes channel, as the channel prompt:

```text
When reading a page, call ego_get_page_text with the url so the open and the read are one call. Do not call ego_navigate and then ego_get_page_text as two calls; another caller can change the shared tab in between. When saving a page, call ego_save_page_markdown with the url (or omit it for the current tab), a relative subdir, and a short slug. Do not transcribe the page with write_file or apply_patch. The tool stores the markdown verbatim and returns only the path and counts.
```

## Security

The bearer token is the only authentication on `/mcp` and `/agent`. Keep `relay.env` and `config.toml` mode `0600`, and do not commit them. The relay binds localhost. Do not publish port 8767. In mode B the Mac connects out (SSH forward or Tailscale); the VPS does not open a port for the daemon.

`ego_save_page_markdown` writes only under `EGO_SAVE_ROOT`. Absolute paths, `..`, and symlinks under that root are rejected. The root itself may be a symlink; the real directory is the jail. The tool does not chown the root or directories that already existed. Files it creates are mode `0644`, directories `0755`, owned by the root's uid and gid unless `EGO_SAVE_UID` / `EGO_SAVE_GID` override that. Notes are never overwritten: the name is `YYYY-MM-DD-<slug>.md`, then `-2` through `-100`.

There is no click or type tool. The daemon uses one task space, `hermes-ego`, and only pages it created (`openedBy === "agent"`).

## Security / disclosures (catalog rule 13)

What a user should know before enabling `plugins/hermes-ego`. The same text is in that directory's README, which is the file a catalog page renders when `subdir` is set. Nothing here is a real address, hostname, channel, or token.

- **Long-running relay.** Layer A leaves a relay process bound to `127.0.0.1:8767`. The plugin does not supervise it. Do not publish that port.
- **Mac daemon runs the ego CLI.** A separate process on the Mac talks to ego lite and dials out to the relay (loopback, or an SSH local forward when Hermes is on another host). The Mac does not accept a connection from the Hermes host.
- **Logged-in browser.** Tools use the user's ego lite session, including sites that session is logged into. There is no click or type tool.
- **Filesystem write.** `ego_save_page_markdown` writes markdown under the configured save root, for example an Obsidian vault. Paths outside that root are rejected.
- **Image download.** A save downloads third-party image URLs from the page. Leftovers can finish in a background pass after the tool returns.
- **Optional SSH tunnel.** When Hermes is not on the relay's machine, an SSH local forward makes `127.0.0.1:8767` on the Hermes host the relay. The plugin still calls loopback. The token is not put in the SSH command by this package.
- **Token.** `EGO_RELAY_TOKEN` comes through the Hermes profile environment (the plugin secret or `~/.hermes/.env`). It is not committed. The package does not read `relay.env`, `auth.json`, or a browser profile.
- **Approvals.** The package marks server `ego` untrusted, so Hermes asks before tools that are not annotated `readOnlyHint: true`. The relay does not set that hint, so reads ask too, and cron or any other unattended run fails closed.
- **No telemetry and no self-update** in the plugin directory.

ego lite itself is not part of this repository. Install it on the Mac before layer A. A download URL with a checksum was not verified for this change.

## Tools

| Tool | Behavior |
| --- | --- |
| `ego_status` | Daemon socket (`online`), device, ego version, `egoReachable`, and `capabilities` from the hello. `online` stays true when the daemon is up but ego lite is down. |
| `ego_list_tabs` | Agent tabs: `label`, `title`, `url`, `active`. |
| `ego_navigate(url)` | `http` and `https` only. One shared tab. Does not read. |
| `ego_get_page_text(url?, label?, mode?)` | Pass `url` to open that page and read it under one tab lock. Omit `url` to read the current agent page. `auto`: yitang `/fs-doc/` authored markdown up to 1,000,000 characters; every other page is a generic article extract. |
| `ego_save_page_markdown(subdir, slug, url?, label?, title?, mode?)` | Writes that page on the relay. Returns path and counts, not the body. Same `mode` values as `ego_get_page_text`. Pass `url` to open and save under one tab lock. |

`mode` is `auto`, `generic`, `snapshot`, or `yitang`.

| Mode | What is saved |
| --- | --- |
| `auto` | `yitang` when the URL host is `yitang.top` (or a subdomain) and the path contains `/fs-doc/`. Otherwise `generic`. |
| `yitang` | Authored lesson markdown from the page's `dataJson`. Unchanged. |
| `generic` | Article markdown from the page HTML the daemon reads in ego: title, headings, paragraphs, lists, links, and images. |
| `snapshot` | The accessibility tree, cut at 20,000 characters. |

`generic` does not download the URL again, so a logged-in page still extracts. Nav, footer, header, aside, and cookie or consent blocks are removed before [trafilatura](https://trafilatura.readthedocs.io/) writes the markdown. Image destinations stay `![alt](https://...)`, so the relay budget and the Mac backfill run as they do for any other note. If ego cannot return HTML, the daemon sends the accessibility snapshot and the relay drops obvious chrome lines (`Accept all`, `Privacy Policy`, and the same kind of landmark). That fallback is still the accessibility tree, only shorter. An older daemon that does not send HTML keeps returning the raw snapshot for non-yitang pages.

`ego_get_page_text` with `url`, and `ego_save_page_markdown` with `url`, hold the relay tab lock across navigate and read. Two of those calls at once cannot return each other's page. `ego_navigate` followed by `ego_get_page_text` without `url` drops the lock between the calls, so another client can navigate in the gap. `ego_status` does not take the lock and still returns while a read is in progress. Pass `url` or `label`, not both.

The lock that makes an open-and-read atomic is the relay's. The daemon still runs `list_tabs`, `navigate`, and `get_page_text` one command at a time, and `status` still bypasses that daemon lock. The daemon lock does not cover the gap between two MCP calls. There is still one shared agent tab; this does not add tabs.

## Saving a page and images

The note body is the `get_page_text` string for the `mode` above. The edits are http(s) image links rewritten to files under `assets/<note-stem>/`, a failure marker on images that did not download, and YAML frontmatter. Fenced code and non-http tokens such as `![image](boxcnToken)` are left alone. Generic article images use the same rewrite, the same 90s relay budget, and the same Mac backfill.

Frontmatter includes `title`, `source_url`, `captured_at`, `device`, `mode`, `chars`, `images`, `images_ok`, `images_failed`, `image_refs`, and `truncated`.

`EGO_IMAGE_FETCH` defaults to `relay`. The synchronous image phase runs on the relay and stops starting new downloads after `EGO_IMAGE_BUDGET_MS` (default 90s). Downloads already in flight finish the current attempt. Successes are kept. The rest are marked `budget`, or with the real error if an attempt already failed. The note is written and the tool returns. `images_pending` is how many distinct URLs are left for one background pass. `images_via_daemon` and `images_via_relay` count only that synchronous phase.

After the tool returns, if the daemon hello listed `fetch_images`, one background pass asks the Mac for the leftovers. The Mac fetches them (China CDN is reliable from the Mac) and streams the bytes. The relay writes them into the same asset directory, with the same path jail and owner, and replaces each ` **[image download failed]**` marker with a relative link. It then sets `images_ok`, `images_failed`, and `images` from the final body. `image_refs` stays the count from the original capture. A file already on disk is not downloaded again. A second pass for the same note does not start while the first is running. `<note>.pending.json` lists what is still open and is removed when the pass finishes. There is no MCP response for the backfill. If the daemon lacks `fetch_images` or is disconnected, the pass logs `backfill skipped` and the markers stay.

`auto` asks the Mac during the save, then the relay, and does not schedule the background pass. `daemon` uses only the Mac during the save.

Either side: 4 at a time, at most 100 distinct URLs, 8 MiB each, 3 tries, backoff 0.4s then 0.8s, connect timeout 5s, body timeout 10s. A 404, a non-image, a private address, or a file over the cap is not retried. One bad image does not fail the save.

## Layout

| Path | What |
| --- | --- |
| `plugins/hermes-ego/` | Portable Agent Plugins v1 package (catalog `subdir`). |
| `docs/catalog/` | Draft catalog entry and the NousResearch PR description. Not opened against Nous from here. |
| `relay/` | MCP server and daemon WebSocket. |
| `agent/hermes_ego_daemon.py` | Mac daemon. |
| `deploy/` | systemd unit and launchd plists. `install.sh` fills in paths. |
| `scripts/install.sh` | Config, venv, and service install. |
| `scripts/doctor.sh` | Self-check. |
| `smoke_test.py` | Relay, real daemon, fake `ego-browser`. |

```bash
uv sync
uv run python smoke_test.py
```

## Updating a running link

An old daemon still returns a snapshot for pages that are not yitang lessons. An old relay still cuts every page at 20,000 characters and does not know `mode=generic`. `fetch_images` needs the daemon from the keepalive change. Without that capability the synchronous relay save still finishes and the background pass logs `backfill skipped`. Generic extraction needs this relay (it runs trafilatura) and a daemon that reads page HTML.

User unit:

```bash
cd ~/hermes-ego
git pull
scripts/install.sh --no-start
systemctl --user restart hermes-ego-relay
```

The live VPS relay is often a non-git tree at `/root/hermes-ego` and a system unit, not the user unit in `deploy/`. The Mac tree is often `~/code/hermes-ego` or `~/hermes-ego`, also not a clone. Copy the changed files and restart that unit.

An open-and-read fix lives in `relay/server.py` only. Copy that file to the VPS and restart the relay. The running daemon does not need that file and does not need a restart. Copy `README.md` if you keep the operator notes beside the code. Copy `agent/test_save_page.py`, `agent/test_ego_status.py`, `agent/hermes_ego_daemon.py`, and `smoke_test.py` when you want the race tests on that machine; the daemon copy is a comment about the per-command lock, not a behavior change.

```bash
install -m 644 relay/server.py /root/hermes-ego/relay/
systemctl restart hermes-ego-relay
```

The broader file set, when the change is not just that lock:

```bash
install -m 644 relay/server.py relay/save_page.py /root/hermes-ego/relay/
install -m 644 agent/article_text.py agent/yitang_text.py agent/image_fetch.py /root/hermes-ego/agent/
uv pip install --python /root/hermes-ego/.venv/bin/python 'trafilatura>=2.0'
systemctl restart hermes-ego-relay
```

On the Mac, generic mode needs the daemon that reads page HTML. Copy that file and the mode list, then kickstart. The Mac does not need trafilatura; the relay extracts.

```bash
install -m 644 agent/hermes_ego_daemon.py agent/yitang_text.py ~/hermes-ego/agent/
launchctl kickstart -k "gui/$UID/com.hermes.ego.daemon"
```

The running relay does not import `hermes_ego_daemon.py`. The test suite on the VPS does, because `agent/test_image_fetch.py` imports it. Copy `agent/hermes_ego_daemon.py`, `agent/yitang_text.py`, `agent/article_text.py`, `agent/test_image_fetch.py`, `agent/test_save_page.py`, `agent/test_article_text.py`, `agent/test_install_doctor.py`, and `smoke_test.py` as well when you want to run tests there.

A finished save logs `save phases` with `via_relay` and `pending`. Leftovers log `backfill start` and `backfill done`. The daemon logs `fetch_images` with `kib_s`.

## Troubleshooting

`scripts/doctor.sh` is the first check. `FAIL daemon is not connected` means the WebSocket hello has not stuck: in mode B that is usually the SSH forward or Tailscale, then the token, then the daemon process. `FAIL capabilities do not include fetch_images` means the connected daemon is too old for backfill. `FAIL ego lite is not reachable` means the GUI or the ego-browser bootstrap server is down; `ego-browser --version` can still succeed in that state.

Relay journal: `journalctl --user -u hermes-ego-relay -n 200` or, for the system unit, `journalctl -u hermes-ego-relay -n 200`. A failed unit or a traceback is a doctor failure. Daemon log: `~/.local/state/hermes-ego/daemon.log`.

SSH upload through a proxy is slow. If the Mac uses Clash and image backfill or `scp` to the VPS sits at tens or a few hundred KiB/s, the VPS public IP is probably leaving through a proxy node. Add a Clash DIRECT rule for that IP and measure again. The relay still binds `127.0.0.1`. The DIRECT rule only changes the path SSH takes to the VPS. The synchronous save does not depend on that upload: it downloads on the relay for at most 90s and returns. The background pass is what uploads the images the relay did not get.

Tailscale is the other split path. `tailscale serve` on the VPS, or `EGO_RELAY_HOST` on the tailnet address, keeps the bearer token on the tailnet. Prefer `wss://` when the path is not the SSH forward. Without TLS, the token and every page cross the network in the clear.

Caddy, if you do publish a name, should proxy `/agent` only and answer 404 for everything else. Hermes on the VPS still uses `http://127.0.0.1:8767/mcp` and does not need that name.

## Notes that are still assumptions

The daemon reuses a page only when `tabs()` reports `openedBy === "agent"` and a label. It does not call `finish()` or `release()`. One daemon per relay: a second hello replaces the first socket. Image frames are 256 KiB with at most two unacked, so a keepalive ping is not stuck behind a multi-megabyte SSH backlog. The relay waits 60 seconds for a tab command and does not retry it after a disconnect.

## License

MIT. See `LICENSE` and `NOTICE`.
