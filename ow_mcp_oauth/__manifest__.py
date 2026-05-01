{
    'name': 'OW MCP OAuth 2.1',
    'version': '18.0.1.0.1',
    'summary': 'OAuth 2.1 + Dynamic Client Registration for OW MCP Server '
               '— direct Claude/ChatGPT remote-MCP connect, no bridge.',
    'description': """
OAuth 2.1 + Dynamic Client Registration for OW MCP Server.
==========================================================

Bolts the OAuth 2.1 Authorization Server endpoints required by the MCP
Authorization spec (rev 2025-06-18) onto `ow_mcp_server`, so remote-MCP clients
(Claude.ai Connectors, Claude Desktop Custom Connectors, ChatGPT Developer
Mode) can connect to a self-hosted Odoo MCP server directly — no stdio→HTTP
bridge, no static API key.

Endpoints (8): /.well-known/oauth-protected-resource (RFC 9728),
/.well-known/oauth-authorization-server (RFC 8414), /ow_mcp/oauth/jwks.json
(RFC 7517), /ow_mcp/oauth/register (RFC 7591 DCR), /ow_mcp/oauth/authorize
(RFC 6749 §4.1 + RFC 7636 PKCE S256 + RFC 8707 resource), /ow_mcp/oauth/token
(authorization_code + refresh_token with rotation), /ow_mcp/oauth/revoke
(RFC 7009). Tokens: RS256 JWT access tokens (RFC 9068, audience-bound) and
opaque refresh tokens with reuse-detection (OAuth 2.1 §4.3.1). Scopes:
mcp:read, mcp:write — gated per tool inside the MCP dispatcher. The existing
User API Keys (scope: mcp) keep working unchanged; both authentication
modes coexist.

Requires: pip install 'pyjwt[crypto]'.
""",
    'category': 'Technical',
    'license': 'LGPL-3',
    'author': 'Openworx',
    'website': 'https://www.openworx.nl',
    'depends': ['ow_mcp_server'],
    'external_dependencies': {
        'python': ['jwt', 'cryptography'],
    },
    'data': [
        'security/oauth_security.xml',
        'security/ir.model.access.csv',
        'data/oauth_config_data.xml',
        'data/ir_cron.xml',
        'views/oauth_consent_template.xml',
        'views/oauth_client_views.xml',
        'views/oauth_event_views.xml',
        'views/res_config_settings_views.xml',
        'views/oauth_menus.xml',
    ],
    'installable': True,
    'application': False,
}
