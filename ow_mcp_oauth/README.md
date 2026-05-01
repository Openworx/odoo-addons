# OW MCP OAuth 2.1

OAuth 2.1 + Dynamic Client Registration extension for [`ow_mcp_server`](../ow_mcp_server).

Adds the OAuth 2.1 Authorization Server endpoints required by the MCP
Authorization spec (rev `2025-06-18`) so MCP clients that speak remote-MCP
(Claude.ai Connectors, Claude Desktop Custom Connectors, ChatGPT Developer
Mode) can connect directly to a self-hosted Odoo MCP server **without a
stdio→HTTP bridge** and without provisioning a static API key.

## What it does

- Serves the discovery documents:
  - `GET /.well-known/oauth-protected-resource` (RFC 9728)
  - `GET /.well-known/oauth-authorization-server` (RFC 8414)
  - `GET /ow_mcp/oauth/jwks.json` (RFC 7517)
- Implements the Authorization Server endpoints:
  - `POST /ow_mcp/oauth/register` — Dynamic Client Registration (RFC 7591)
  - `GET/POST /ow_mcp/oauth/authorize` — auth-code grant + consent UI, with mandatory PKCE S256 (RFC 7636) and resource indicator (RFC 8707)
  - `POST /ow_mcp/oauth/token` — `authorization_code` and `refresh_token` grants, with refresh-token rotation + reuse-detection (OAuth 2.1 §4.3.1)
  - `POST /ow_mcp/oauth/revoke` — token revocation (RFC 7009)
- Issues **RS256 JWT** access tokens with the RFC 9068 claim layout (`iss`, `sub`, `aud`, `client_id`, `iat`, `exp`, `jti`, `scope`).
- Wraps the existing `/mcp` controller so an OAuth bearer token is tried first; if the token is missing or invalid, the request falls through to the existing `res.users.apikeys` check. Both auth methods coexist.
- Returns RFC 9728-compliant `WWW-Authenticate: Bearer ... resource_metadata="..."` on 401 so MCP clients can bootstrap discovery.
- Enforces scopes `mcp:read` and `mcp:write` per tool category (read-only vs. mutating).

## Install

```bash
pip install 'pyjwt[crypto]'
```

Then in Odoo: install **OW MCP Server** first, then **OW MCP OAuth 2.1**.

## Settings

Open *Settings → MCP Server* and scroll to the **OAuth 2.1** sections:

- *Enable OAuth 2.1* — master toggle.
- *Enable Dynamic Client Registration* — admin kill-switch for `/register`.
- *Canonical resource URI* — defaults to `<web.base.url>/mcp`. Override only when the MCP server is reverse-proxied at a different URL.
- *TTLs* — access token (default 15 min), refresh token (default 30 d), auth code (default 60 s), consent memory (default 30 d).
- *DCR rate limit per IP/hour* — default 20.
- *Signing keys* — current and previous `kid`, plus a "Rotate now" button that demotes the active key to "previous" and generates a new keypair.

## Connecting Claude

Claude needs to reach the server over **HTTPS** with a real public hostname (Claude.ai will not accept `http://localhost`). Easiest path:

```bash
docker compose -f /Users/mario/odoo/odoo18/docker-compose.yml up -d
ngrok http 8069   # gives e.g. https://abcd.ngrok-free.app
```

Then in Odoo:

- *Settings → Technical → Parameters → System Parameters*: set `web.base.url` = `https://abcd.ngrok-free.app` and `web.base.url.freeze` = `True`.
- In `odoo.conf` (mounted at `./config/odoo.conf` in the standard compose), set `proxy_mode = True` so the `https` scheme propagates.

In Claude.ai → *Settings → Connectors → Add custom connector* paste:

```
https://abcd.ngrok-free.app/mcp
```

Claude will:
1. POST to `/mcp` → receive 401 with `WWW-Authenticate: Bearer ... resource_metadata=...`.
2. Fetch the protected-resource and AS metadata.
3. Register itself via `/ow_mcp/oauth/register` (DCR).
4. Open a browser to `/ow_mcp/oauth/authorize` — you log into Odoo and approve the consent screen.
5. Exchange the code at `/ow_mcp/oauth/token` (with PKCE).
6. Retry `/mcp` with the resulting JWT.

## Development with Docker Compose

The standard layout assumed below is `/Users/mario/odoo/odoo18/docker-compose.yml` with `./addons:/mnt/extra-addons`:

```bash
cd /Users/mario/odoo/odoo18/addons
ln -sf /Users/mario/project/odoo-addons/ow_mcp_server .
ln -sf /Users/mario/project/odoo-addons/ow_mcp_oauth .

# Install pyjwt inside the container
docker compose -f /Users/mario/odoo/odoo18/docker-compose.yml exec -u root web \
    pip install 'pyjwt[crypto]'

# Install / upgrade the addons
docker compose -f /Users/mario/odoo/odoo18/docker-compose.yml exec web \
    odoo -d odoo --stop-after-init -i ow_mcp_oauth --without-demo=all
docker compose -f /Users/mario/odoo/odoo18/docker-compose.yml restart web
```

## Tests

```bash
docker compose -f /Users/mario/odoo/odoo18/docker-compose.yml exec web \
    odoo -d odoo_test --stop-after-init --test-enable \
        --test-tags ow_mcp_oauth -i ow_mcp_oauth --without-demo=all
```

The suite covers: well-known discovery, DCR, PKCE enforcement, token exchange,
refresh-token rotation + reuse detection, audience binding, end-to-end
register→authorize→token→/mcp, and a regression check that existing
`res.users.apikeys` clients still work.

## Spec citations

| RFC / Spec | Topic |
|---|---|
| [MCP Authorization 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization) | Required OAuth profile for MCP |
| [draft-ietf-oauth-v2-1](https://datatracker.ietf.org/doc/html/draft-ietf-oauth-v2-1-13) | OAuth 2.1 |
| RFC 6749 | OAuth 2.0 core |
| RFC 7009 | Token Revocation |
| RFC 7517 | JSON Web Key |
| RFC 7591 | Dynamic Client Registration |
| RFC 7636 | PKCE |
| RFC 8252 | OAuth 2.0 for Native Apps (loopback redirect) |
| RFC 8414 | Authorization Server Metadata |
| RFC 8707 | Resource Indicators (audience binding) |
| RFC 9068 | JWT Profile for OAuth Access Tokens |
| RFC 9728 | Protected Resource Metadata |

## License

LGPL-3.
