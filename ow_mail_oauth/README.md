# OW Mail OAuth (Gmail / Microsoft 365) — beta

> **Beta status.** The module is fully unit-tested, but the end-to-end
> OAuth flow depends on your provider app registration (Google Cloud /
> Azure). Feedback from real-world tenants is welcome.

Bridge module that adds OAuth2 authentication (XOAUTH2) to OW Mail
accounts, on top of Odoo's standard `google_gmail` and `microsoft_outlook`
modules. Token storage, refresh and the SASL string come from those
mixins; this module provides the per-user connect flow (Odoo's standard
flow is admin-only) and the XOAUTH2 branch in ow_mail's IMAP/SMTP
connections.

Existing password accounts keep working unchanged (`auth_type` defaults
to "Password").

## Requirements

- Odoo reachable over **HTTPS** (the OAuth callbacks require it).
- `web.base.url` set correctly (it determines the redirect URIs).

### Microsoft 365 (Azure / Entra ID)

1. Azure Portal → App registrations → New registration.
2. Redirect URI (type *Web*): `https://<your-odoo>/ow_mail_oauth/outlook/confirm`
3. API permissions → Microsoft Graph is not needed; add **delegated**
   permissions under *APIs my organization uses → Office 365 Exchange
   Online*: `IMAP.AccessAsUser.All`, `SMTP.Send`, plus `offline_access`.
4. Certificates & secrets → create a client secret.
5. In Odoo: Settings → General Settings → *Outlook Credentials* section
   (added by the standard `microsoft_outlook` module): fill in the
   Client ID and Client Secret. Optionally set the
   `microsoft_outlook_tenant_id` system parameter for single-tenant
   apps (defaults to `common`).
6. Make sure **SMTP AUTH** is enabled on the mailbox (Microsoft 365
   admin center → user → Mail → Manage email apps → Authenticated SMTP).

### Gmail (Google Cloud)

1. Google Cloud Console → project → *APIs & Services* → OAuth consent
   screen. Scope: `https://mail.google.com/` (restricted — public
   production use requires Google verification; for your own Workspace
   domain choose "Internal", or use test users).
2. Credentials → OAuth client ID (type *Web application*), redirect URI:
   `https://<your-odoo>/ow_mail_oauth/gmail/confirm`
3. In Odoo: Settings → General Settings → *Gmail Credentials* section
   (added by the standard `google_gmail` module): Client ID + Client
   Secret.

## Usage

- **New mailbox**: OW Mail → Configuration → Connect Mailbox → pick
  provider *Gmail* or *Microsoft 365* → name + email address → Connect.
  The browser goes to the provider's consent screen; after approval the
  connection is tested, the folder tree is loaded and you land in the
  client.
- **Converting an existing account**: open the account, set
  *Authentication* to Gmail or Microsoft 365 and click *Connect with
  Google/Microsoft*.
- *Reconnect* appears once an account is connected (e.g. after a revoked
  token or changed scopes).

## Testing

- Unit tests (no network): `--test-tags ow_mail_oauth`.
- The XOAUTH2 handshake itself can only be verified end-to-end with real
  accounts (GreenMail does not support XOAUTH2). Manual checklist:
  1. Wizard preset → consent screen → callback → account `confirmed`.
  2. Read and send mail through the client.
  3. Cron tick (`OW Mail: refresh folder counts`) after >60 min: the log
     shows "fetch new access token" — refresh works under OdooBot.
  4. Revoke the token at the provider → the next sync puts the account
     in *Error* with a clear message; *Reconnect* restores it.
