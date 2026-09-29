# Openworx Odoo addons

| Module | Purpose |
|---|---|
| ow_account_invoice_partner | Email invoices to the customer's invoice contact |
| ow_ai | AI assistant in Discuss that reads Odoo data and changes it after confirmation, on any OpenAI-compatible provider |
| ow_mail | Mail client in the Odoo backend (IMAP/SMTP, multiple accounts, tags, safe mode) |
| ow_mail_oauth | OAuth2 (Gmail / Microsoft 365) for ow_mail accounts |
| ow_mcp_server | MCP server: exposes models and records safely to AI clients |
| ow_mcp_oauth | OAuth 2.1 authorization server with Dynamic Client Registration for ow_mcp_server |
| ow_security_txt | `/.well-known/security.txt` (RFC 9116) |

One branch per Odoo version (`18.0`, `19.0`, `20.0`). The 19.0 and 20.0 code of `ow_ai`, `ow_mcp_server` and
`ow_security_txt` is mirrored from the internal Openworx repository where the production version is
maintained; changes there land here as one commit per release.
