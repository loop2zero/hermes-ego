# Catalog PR description (NousResearch/hermes-agent)

Shawn opens this pull request himself, against `NousResearch/hermes-agent`, adding one file: `plugin-catalog/hermes-ego.yaml`. Do not open it from the hermes-ego repository. The public GitHub repository https://github.com/loop2zero/hermes-ego does not exist yet. Replace `sha` with the full 40-character commit after that push. `requires_hermes` is `">=0.23.0"` (quoted, same style as the other entries). That floor is Hermes Agent v0.23.0 (2026.9.24), the install that will run `hermes plugins validate`. Do not set a newer floor.

Paste the sections below into the PR body.

## What this PR does

Adds `plugin-catalog/hermes-ego.yaml` for Hermes Ego, a community plugin.

- Repo: https://github.com/loop2zero/hermes-ego (maintainer Shawn Yuan). `subdir: plugins/hermes-ego`. `version: "0.1.0"` matches `plugin.json`.
- Portable Agent Plugins v1 package: `plugin.json`, `mcp.json`, and `skills/hermes-ego/SKILL.md`. No `plugin.yaml`, no Python entrypoint, no hooks, no middleware.
- The MCP server is Streamable HTTP at `http://127.0.0.1:8767/mcp`. The `Authorization` header is `Bearer ${EGO_RELAY_TOKEN}`. The token is not in the tree.
- The relay and the Mac daemon are installed separately, the same split as `local-mcp`. This package does not start them, download them, or update them.
- `extensions.com.nousresearch.hermes.servers.ego.trust` is `untrusted`.
- `config_schema.relay_token` is a secret stored as `EGO_RELAY_TOKEN`.

## Hermes surfaces used

- Portable skills, loaded through `skills_list` and `skill_view`. On v0.23.0 the qualified name is `hermes-ego:hermes-ego`.
- One `streamable-http` server from `mcp.json`, named `ego`, routed through the native URL MCP client. Loopback HTTP is the documented exception to the https rule.
- The Hermes `trust` extension on that server. `requires_hermes: ">=0.23.0"` is the catalog floor. `plugin.json` has no `requires_hermes` field, so this entry is the only floor (catalog rule 14).
- A `config_schema` secret so the bearer is set through Hermes and not committed.

No Desktop surface, no `ctx.register_*` tools, no core override, no self-update, no bundled binary.

`capabilities.provides_tools` lists the relay's MCP tools: `ego_status`, `ego_list_tabs`, `ego_navigate`, `ego_get_page_text`, `ego_save_page_markdown`. They are not `ctx.register_tool` registrations. `aave-mcp` leaves `provides_tools` empty for that reason. If validate or catalog CI reports them declared but not registered, empty the list and keep the names in the description.

`requires_env: [EGO_RELAY_TOKEN]`.

## Disclosure (catalog rule 13)

- Long-running relay service on `127.0.0.1:8767`, installed and supervised outside this package.
- Mac daemon runs the ego CLI. It dials out to the relay. The Mac opens no inbound port.
- Uses the user's logged-in ego(lite) browser session. ego(lite) is a separate Mac install. This package does not ship it and does not name a checksum-pinned download URL.
- `ego_save_page_markdown` writes markdown under a configured save root (for example an Obsidian vault) and downloads third-party page images. Images may finish in a background pass after the tool returns.
- Optional SSH local forward when Hermes is not on the same host as the relay. Hermes still calls loopback. The relay is not published.
- Credential: `EGO_RELAY_TOKEN` via the Hermes profile environment (the settings secret). Not committed. The package does not read `relay.env`, `auth.json`, or a browser profile.
- Approvals: `trust: untrusted`. The relay tools do not set `readOnlyHint`, so Hermes asks before every tool on this server, including reads, and fails closed in cron and other unattended runs.
- No telemetry and no self-update in the package.

The package README (`plugins/hermes-ego/README.md`) repeats this. With `subdir` set, that README is the one the catalog page renders.

## Known issues called out on the entry

- Remote topology needs an SSH local forward to loopback.
- Images may backfill asynchronously.
- Untrusted gating applies to every tool until the relay sets `readOnlyHint` on the read-only ones.
- A same-named `mcp_servers.ego` block in `config.yaml` replaces the package entry, including trust.

## Validation

On the Hermes that will review the pin (v0.23.0 or newer, not older):

```bash
git clone https://github.com/loop2zero/hermes-ego.git
cd hermes-ego
git checkout --detach <40-character sha>
hermes plugins validate plugins/hermes-ego --install-deps
```
