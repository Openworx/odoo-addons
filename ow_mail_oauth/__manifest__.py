{
    "name": "OW Mail OAuth (Gmail / Microsoft 365)",
    "version": "18.0.1.0.1",
    "summary": "OAuth2 (XOAUTH2) authentication for OW Mail accounts: "
               "Gmail and Microsoft 365",
    "description": """
OW Mail OAuth — connect Gmail and Microsoft 365 mailboxes to OW Mail.

Bridge module: adds an OAuth2 authentication type to ``ow.mail.account``
on top of Odoo's standard ``google_gmail`` and ``microsoft_outlook``
modules. Token storage, refresh and the XOAUTH2 SASL string come from
those mixins; this module adds:

* An ``auth_type`` selection on the mail account (password / Gmail /
  Microsoft 365) — existing accounts keep password authentication.
* XOAUTH2 authentication in the IMAP and SMTP connections.
* A per-user connect flow (authorize URL + callback) so regular users
  can link their own mailbox — the standard Odoo flow is admin-only.
* Provider presets in the Connect Mailbox wizard.

Prerequisites (see README.md): a Google Cloud project and/or an
Azure/Entra app registration, with the client id/secret configured in
Settings → General Settings (the fields added by the standard modules),
and the Odoo instance reachable over HTTPS for the OAuth callbacks:
``/ow_mail_oauth/gmail/confirm`` and ``/ow_mail_oauth/outlook/confirm``.
""",
    "author": "Openworx",
    "website": "https://www.openworx.nl",
    "license": "LGPL-3",
    "category": "Productivity/Mail",
    "depends": ["ow_mail", "google_gmail", "microsoft_outlook"],
    "data": [
        "views/ow_mail_account_views.xml",
        "views/ow_mail_connect_wizard_views.xml",
    ],
    "installable": True,
    "application": False,
}
