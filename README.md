# Openworx Odoo addons

| Module | Doel |
|---|---|
| ow_account_invoice_partner | Facturen mailen naar het factuurcontact van de klant |
| ow_mail | Mailclient in de Odoo-backend (IMAP/SMTP, meerdere accounts, tags, safe mode) |
| ow_mail_oauth | OAuth2 (Gmail / Microsoft 365) voor ow_mail-accounts |
| ow_mcp_server | MCP-server: modellen en records veilig beschikbaar voor AI-clients |
| ow_mcp_oauth | OAuth 2.1-autorisatieserver met Dynamic Client Registration voor ow_mcp_server |
| ow_security_txt | `/.well-known/security.txt` (RFC 9116) |

Per Odoo-versie een branch (`18.0`, `19.0`, `20.0`). De 19.0- en 20.0-code van `ow_mcp_server` en
`ow_security_txt` wordt gespiegeld uit de interne Openworx-repo waarin de productieversie wordt
onderhouden; wijzigingen daar landen hier als één commit per release.
