# OW Mail OAuth (Gmail / Microsoft 365)

Brugmodule die OAuth2-authenticatie (XOAUTH2) toevoegt aan OW Mail-accounts,
bovenop Odoo's standaard `google_gmail`- en `microsoft_outlook`-modules.
Token-opslag, -verversing en de SASL-string komen uit die mixins; deze module
levert het per-gebruiker koppel-proces (de standaardflow van Odoo is
admin-only) en de XOAUTH2-tak in de IMAP/SMTP-verbindingen van ow_mail.

Bestaande wachtwoord-accounts blijven ongewijzigd werken (`auth_type`
standaard "Password").

## Vereisten

- Odoo bereikbaar via **HTTPS** (de OAuth-callbacks vereisen dit).
- `web.base.url` correct ingesteld (bepaalt de redirect-URI's).

### Microsoft 365 (Azure / Entra ID)

1. Azure Portal → App registrations → New registration.
2. Redirect URI (type *Web*): `https://<jouw-odoo>/ow_mail_oauth/outlook/confirm`
3. API permissions → Microsoft Graph is niet nodig; voeg **delegated**
   permissies toe onder *APIs my organization uses → Office 365 Exchange
   Online*: `IMAP.AccessAsUser.All`, `SMTP.Send`, plus `offline_access`.
4. Certificates & secrets → nieuw client secret.
5. In Odoo: Settings → General Settings → sectie *Outlook Credentials*
   (van de standaardmodule `microsoft_outlook`): vul Client ID en Client
   Secret in. Optioneel `microsoft_outlook_tenant_id` als systeemparameter
   voor single-tenant-apps (default `common`).
6. Zorg dat **SMTP AUTH** aanstaat op de mailbox (Microsoft 365 admin
   center → gebruiker → Mail → Manage email apps → Authenticated SMTP).

### Gmail (Google Cloud)

1. Google Cloud Console → project → *APIs & Services* → OAuth consent
   screen. Scope: `https://mail.google.com/` (restricted — voor publieke
   productie is Google-verificatie nodig; voor een eigen Workspace-domein
   kies "Internal", of gebruik testgebruikers).
2. Credentials → OAuth client ID (type *Web application*), redirect URI:
   `https://<jouw-odoo>/ow_mail_oauth/gmail/confirm`
3. In Odoo: Settings → General Settings → sectie *Gmail Credentials*
   (van de standaardmodule `google_gmail`): Client ID + Client Secret.

## Gebruik

- **Nieuwe mailbox**: OW Mail → Configuration → Connect Mailbox → kies
  provider *Gmail* of *Microsoft 365* → naam + e-mailadres → Connect.
  De browser gaat naar het toestemmingsscherm van de provider; na akkoord
  wordt de verbinding getest, de mappenboom geladen en land je in de client.
- **Bestaand account omzetten**: open het account, zet *Authentication* op
  Gmail of Microsoft 365 en klik *Connect with Google/Microsoft*.
- *Reconnect* verschijnt zodra een account verbonden is (bijv. na een
  ingetrokken token of gewijzigde scopes).

## Testen

- Unit-tests (geen netwerk): `--test-tags ow_mail_oauth`.
- De XOAUTH2-handshake zelf is alleen end-to-end te verifiëren met echte
  accounts (GreenMail ondersteunt geen XOAUTH2). Handmatige checklist:
  1. Wizard-preset → consent-scherm → callback → account `confirmed`.
  2. Mail lezen én verzenden via de client.
  3. Cron-tick (`OW Mail: refresh folder counts`) na >60 min: log toont
     "fetch new access token" — verversing werkt onder OdooBot.
  4. Token intrekken bij de provider → volgende sync zet het account op
     *Error* met een duidelijke melding; *Reconnect* herstelt het.
