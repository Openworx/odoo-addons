# OW MCP Server — native Odoo 18 MCP addon

Serves the **Model Context Protocol** (MCP, JSON-RPC 2.0) directly from Odoo
on `/mcp`. Claude Code, Cursor, Copilot, and Claude Desktop talk to Odoo
without any external Python library or translation bridge.

Eleven tools are exposed: `list_models`, `search_records`, `get_record`,
`create_record`, `update_record`, `delete_record`, `search_count`,
`get_model_schema`, `read_group`, `get_user_context`, `list_modules`.
Output is LLM-friendly (many2one as `{id, name}`, dates ISO 8601, pagination
envelopes, binaries as `{size, truncated}`). Authentication is **Odoo's
built-in User API keys only** — no passwords accepted on `/mcp`.

## Contents

- [Install](#install)
- [Create an API key](#create-an-api-key-required)
- [Configure exposed models](#configure-exposed-models)
- [Client configuration](#client-configuration) — Claude Code · Cursor · Claude Desktop · multi-db
- [Tools reference](#tools-reference)
- [YOLO mode](#yolo-mode-dev-only--never-in-production)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

---

## Install

1. Drop the addon into your `addons_path` (or use the bundled docker-compose
   stack at the repo root — see `../README.md`).
2. Install into your target database:
   ```bash
   odoo -c odoo.conf -d <db> -i ow_mcp_server --stop-after-init
   ```
3. Assign users who should have MCP access to the group *MCP User*. The
   admin automatically gets *MCP Administrator* and thus both groups.

## Create an API key (required)

The `/mcp` endpoint accepts **only** Bearer API keys with scope `mcp`.

1. Log in as the user who will issue MCP calls.
2. *My Profile → Account Security → New API Key*.
3. Set **Scope** to `mcp`, pick an expiration date, and copy the key
   (shown only once).
4. Revoking the key immediately cuts off that client — there is no other
   authentication surface to worry about.

## Configure exposed models

*MCP Server → Model Access* (top-level app menu).

For each model you want to expose, create a row with:
- Model (e.g. `res.partner`).
- CRUD flags (read/write/create/delete).
- Optional `allowed_fields_json`, e.g. `["name", "email", "country_id"]` —
  restricts which fields clients may read or write. Leave empty for all.
- Optional Required Groups — further narrows who may use this model.

Sensitive internal models are permanently blocked and can never be exposed,
even in YOLO mode. The blocklist covers most of `ir.*` (`rule`,
`config_parameter`, `logging`, `attachment`, `actions.server`,
`actions.act_url`, `cron`, `ui.view`, `mail_server`, `model.access`,
`model.data`, `filters`, `module.module`, `sequence`, `autovacuum`, `http`),
all of `res.users.*` (including `apikeys`), `auth_*`, `base_automation*`,
`base_import.*`, `fetchmail.server`, `mail.mail`, and `bus.*`. Models that
are not blocklisted (e.g. `mail.message`, `mail.followers`, `mail.template`)
are still **not exposed by default** — they only become reachable if you
add a row in *Model Access* for them.

## Client configuration

### Claude Code (native HTTP transport)

```bash
claude mcp add --transport http odoo https://odoo.example.com/mcp \
  --header "Authorization: Bearer $ODOO_MCP_KEY"
```

### Cursor / recent Claude Desktop

Same HTTP URL, same Bearer header — both support streamable-http natively.

### Claude Desktop (older / stdio-only)

Use the bundled `tools/ow_mcp_stdio.py` bridge. Add to
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "odoo": {
      "command": "python3",
      "args": [
        "/absolute/path/to/ow_mcp_server/tools/ow_mcp_stdio.py",
        "--url", "https://odoo.example.com/mcp"
      ],
      "env": { "ODOO_MCP_KEY": "paste-your-key-here" }
    }
  }
}
```

The bridge uses only the Python standard library — no pip install needed.

### Multi-database Odoo servers

If the Odoo instance hosts multiple databases, select which one to talk to:

```json
{
  "mcpServers": {
    "odoo": {
      "command": "python3",
      "args": [
        "/absolute/path/to/ow_mcp_server/tools/ow_mcp_stdio.py",
        "--url", "https://odoo.example.com/mcp",
        "--database", "my_db"
      ],
      "env": { "ODOO_MCP_KEY": "paste-your-key-here" }
    }
  }
}
```

(Or set `"ODOO_DB": "my_db"` in `env` instead of the `--database` arg —
same effect.) The bridge sends three routing hints with every request:

- `?db=my_db` in the query string — for clients that inspect URLs.
- `X-Odoo-Database: my_db` header — read by this addon's controller to
  verify the request landed on the expected database (otherwise HTTP 404).
- `X-Odoo-Dbfilter: ^my_db$` header — honored by OCA's
  [`dbfilter_from_header`](https://github.com/OCA/server-tools/tree/18.0/dbfilter_from_header)
  module to pick the target DB per request (regex, anchored exact match).

**Recommended multi-db setup:** install `dbfilter_from_header` from OCA
alongside this addon. Then the bridge's per-request header is enough —
no `odoo.conf` hostname gymnastics, no reverse proxy rewriting.

Without `dbfilter_from_header`, Odoo picks a DB before this addon's
controller runs (based on `dbfilter` in `odoo.conf`, the session cookie,
or the single-db rule). Two alternatives that keep working:

1. Configure Odoo's `dbfilter` (in `odoo.conf`) so that the hostname used
   by the client resolves to the target database, **or**
2. Run the Odoo instance in single-db mode (only the target DB exists).

Claude Code equivalent:
```bash
claude mcp add --transport http odoo "https://odoo.example.com/mcp?db=my_db" \
  --header "Authorization: Bearer $ODOO_MCP_KEY" \
  --header "X-Odoo-Database: my_db"
```

---

## Tools reference

Each tool takes a JSON object as argument and returns a JSON object as result.
Input schemas are published via `tools/list` so LLMs fill arguments correctly.

### `list_models` — list exposed models
Input: `{}`
Output: `{ "models": [ { "model", "label", "can_read", "can_write", "can_create", "can_delete", "allowed_fields" }, … ] }`

### `search_records` — search with domain + pagination
Input:
```json
{
  "model": "res.partner",
  "domain": [["is_company", "=", true]],
  "fields": ["name", "country_id", "email"],
  "limit": 50,
  "offset": 0,
  "order": "name asc"
}
```
Output:
```json
{
  "model": "res.partner",
  "records": [ { "id": 7, "name": "ACME", "country_id": {"id": 38, "name": "Belgium"}, "email": "info@acme.com" } ],
  "total": 142,
  "offset": 0,
  "limit": 50,
  "has_more": true,
  "next_offset": 50
}
```

### `get_record` — fetch one or many by id(s)
Input: `{"model": "res.partner", "ids": [7, 8, 9], "fields": ["name"]}` or `{"model": "res.partner", "id": 7}`
Output: `{"model": …, "records": […], "missing": []}` — `missing` lists ids that were not found or not accessible.

### `create_record` — create a record
Input: `{"model": "res.partner", "values": {"name": "New Customer", "email": "new@example.com"}}`
Output: `{"model": …, "id": 42, "record": {…}}`

### `update_record` — update one or many
Input: `{"model": "res.partner", "ids": [7, 8], "values": {"comment": "updated via MCP"}}`
Output: `{"model": …, "updated": [7, 8], "missing": []}`

### `delete_record` — delete one or many
Input: `{"model": "res.partner", "id": 42}` or `{"model": "res.partner", "ids": [42, 43]}`
Output: `{"model": …, "deleted": [42, 43], "missing": []}`

### `search_count` — count without fetching
Input: `{"model": "res.partner", "domain": [["is_company", "=", true]]}`
Output: `{"model": …, "count": 142}`

### `get_model_schema` — field definitions for a model
Input: `{"model": "res.partner"}`
Output: `{"model": …, "fields": [{"name": "name", "type": "char", "label": "Name", "required": true}, …]}`
Returns name, type, label, required, readonly, relation, selection, help. Honors the field allow-list when set.

### `read_group` — group + aggregate
Input: `{"model": "sale.order", "groupby": ["state"], "fields": ["amount_total:sum"], "domain": [["date_order", ">=", "2025-01-01"]]}`
Output: `{"model": …, "groups": [{"state": "sale", "amount_total": 12345.0, "count": 7}, …], "total_groups": 4}`
Date truncation supported (e.g. `"date_invoice:month"`).

### `get_user_context` — caller identity + companies
Input: `{}` or `{"company_id": 2}`
Output: `{"id", "name", "login", "company": {…}, "allowed_companies": […], "default_company_id", "lang", "tz"}`
MCP administrators additionally receive `groups` (full group names); other callers receive only `is_mcp_admin: false` to avoid leaking the group topology.

### `list_modules` — installed modules (admin only)
Input: `{}`
Output: `{"modules": [{"name": "sale", "label": "Sales", "version": "18.0.1.0"}, …], "count": 42}`
Restricted to *MCP Administrator*; raises `AccessError` for ordinary MCP users.

### Multi-company switching

Every tool above (except `list_models` and `list_modules`) accepts an optional
`company_id` argument. The id must be one of the user's allowed companies; an
out-of-scope id returns an `isError` result. The active company is set for the
duration of the call only — the user's persistent default company is unchanged.

```json
{"model": "res.partner", "domain": [["customer_rank", ">", 0]], "company_id": 2}
```

---

## YOLO mode (dev only — NEVER in production)

*MCP Server → Configuration → YOLO mode*:
- `off` — default. Full MCP access config enforced.
- `read` — skip the per-model MCP layer for read operations only. Odoo's
  native ACL is still enforced.
- `full` — skip the MCP layer for every op. Dangerous; useful only on a
  throwaway dev database. A red banner appears in the admin UI whenever
  YOLO is active.

Even with YOLO `full`, the hardcoded blocklist still blocks `ir.rule`,
`ir.config_parameter`, `res.users.apikeys`, and the auth/import internals.

## Security notes

- **HTTPS only** in production. Toggle `require_https` in *MCP Server →
  Configuration* to reject plain HTTP. When Odoo runs behind a reverse
  proxy, set `proxy_mode = True` in `odoo.conf` so `X-Forwarded-Proto`
  and `X-Forwarded-For` are trusted.
- **One key per client** (Claude Code, Cursor, Desktop…) so revocation is
  surgical.
- **Expiration**: set reasonable expiries, rotate periodically.
- **YOLO off** in production — a visible red banner warns if it's on.
  YOLO mode is silently downgraded to `off` unless the server runs with
  `--dev=*` or `--test-enable` *and* the env var `OW_MCP_ALLOW_YOLO=1` is
  set, so a flipped config field alone never opens up access.
- MCP calls run **as the API key's owning user**, so record rules, group
  memberships, and ACLs behave exactly as they would in the web client.
- **Per-call savepoint isolation.** A failure inside one JSON-RPC batch
  message cannot poison the rest of the batch.
- **Domain field allow-list.** Search domains are walked hop-by-hop and
  rejected when they reference fields outside the per-model allow-list,
  traverse into a blocklisted model, or traverse into a model that is not
  exposed via MCP. Closes count-oracle pivots through related models.
- **Nested-write guard.** Create/update payloads cannot embed x2many
  `(0,…)` / `(1,…)` / `(2,…)` opcodes; only safe link-only opcodes
  (3/4/5/6) pass. Each related model must be mutated through its own tool
  call so its allow-list applies.
- **Output scrubbing.** HTML is stripped to plain text and truncated;
  binaries return `{size, truncated}` only — no payloads, no chatter HTML.
- **Sanitized errors.** `AccessError`, `UserError`, and `ValidationError`
  messages are returned to the client. Any other exception is logged
  server-side with a UUID trace id; the client sees only
  `Internal server error (trace id: <uuid>)` so SQL fragments, column
  names, and offending row values do not leak through MCP.
- **Audit log.** When *Audit log* is enabled, every call is recorded in
  `ow.mcp.audit.log` with user, login, company, tool, model, success,
  duration, IP, and a redacted payload/response summary. Sensitive keys
  (`password`, `api_key`, `token`, `secret`, `iban`, `cvv`, `ssn`, …) are
  replaced with `***REDACTED***` before persistence. A daily cron prunes
  rows older than `audit_retention_days` (set to 0 to keep forever).
- **Per-API-key rate limit.** Sliding 60-second window per uid; default
  120 req/min, configurable. The counter is per Odoo worker, so the
  effective ceiling is `N_workers × configured`.
- **Auth-failure throttle.** After 10 failed Bearer-key checks per IP in
  60 seconds, further requests from that IP are rejected with HTTP 429
  before any credential check runs. Per-worker, like the rate limit.
- **IP allow-list.** Optional CIDR/IP list (IPv4 + IPv6) gated at the
  controller before authentication. Behind a reverse proxy this trusts
  `X-Forwarded-For` only when `proxy_mode = True` is set in `odoo.conf`.
- **CORS.** Disabled by default. When enabled, only origins listed in
  `cors_origins` (one per line, exact match) receive an
  `Access-Control-Allow-Origin` header. An empty list while CORS is on
  falls back to `*` — keep CORS off unless you actually need browser
  clients.
- **Body size limit.** `max_body_bytes` (default 1 MB) is enforced both
  pre-read (Content-Length) and post-read (actual UTF-8 bytes).
- **Multi-company isolation.** The optional `company_id` argument is
  validated against `env.user.company_ids` before the call runs, and the
  cache fingerprint includes both the caller's groups and their company
  set, so a just-revoked group or company access cannot replay a cached
  result.
- **Prompt-injection awareness.** Models containing free-text from
  external sources (chatter via `mail.message`, support tickets, mail
  aliases) can carry adversarial instructions aimed at the LLM. These
  models are *not* blocklisted but are *not exposed by default* either.
  Before adding a *Model Access* row for them, narrow the field
  allow-list and add a record rule that excludes external subtypes
  (e.g. `subtype_id.internal = True`).

## Troubleshooting

**Admin user doesn't see the MCP menu.**
Log out and back in — Odoo caches group membership in the session until the
next login. After that, the *MCP Server* tile appears in Odoo's main Apps
grid (the user must be in the *MCP Administrator* group).

**`401 Authentication required` from `/mcp`.**
- API key missing → add `Authorization: Bearer <key>` header.
- Key scope is not `mcp` → re-create the key with Scope = `mcp`.
- Key expired → Odoo 18 requires an expiration date; create a new one.

**`-32002 Permission denied: Model 'xxx' is not exposed via MCP`.**
The model has no row in *Model Access*, or the requested operation flag
(read/write/create/delete) is off. Add/adjust it.

**`-32002 Fields not allowed via MCP: ['yyy']`.**
The model has an `allowed_fields_json` allow-list and `yyy` isn't on it.
Either add the field to the JSON list, or clear the list to allow all.

**`404 Request reached database 'A' but client asked for 'B'`.**
Client sent `?db=B` / `X-Odoo-Database: B`, but Odoo routed to database `A`.
Install OCA's `dbfilter_from_header`, or configure `dbfilter` in
`odoo.conf` so the host used by the client resolves to database `B`.

**Tools list empty in the client.**
`ow.mcp.model.access` has no active rows. Add at least one model
(e.g. `res.partner` with `allow_read=True`) from
*MCP Server → Model Access* (top-level app menu).

**MCP client configuration rejected ("not a valid MCP server entry").**
Usually your client only supports stdio, not HTTP MCP. Use the bundled
`tools/ow_mcp_stdio.py` bridge with `command: python3` + `args: […, --url, …]`.

## Development

From the repo root:

```bash
make up          # start Odoo + Postgres (port 8070)
make install     # install into testdb
make test        # run the full suite
make test-one T=TestMcpController
make shell       # shell inside the odoo container
make logs        # tail odoo logs
make down        # stop
```
