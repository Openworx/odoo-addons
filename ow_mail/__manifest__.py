{
    "name": "OW Mail",
    "version": "18.0.1.8.0",
    "summary": "Mail client for Odoo backend (IMAP/SMTP, multi-account, tags, safe mode)",
    "description": """
OW Mail — personal mail client inside Odoo.

Features:
* Multi-account IMAP/SMTP (SSL & STARTTLS)
* Plain text and sanitized HTML rendering
* Inline image and PDF viewer
* HTML signatures
* Tags (with optional IMAP keyword round-trip)
* Safe Mode: remote-content blocking with per-sender trust
* Floating compose window
* Add a thread to a record in Odoo (like contact, task, etc.)
""",
    "author": "Openworx",
    "website": "https://www.openworx.nl",
    "license": "LGPL-3",
    "category": "Productivity/Mail",
    "depends": ["base", "mail", "web", "calendar"],
    "external_dependencies": {
        "python": ["cryptography", "lxml"],
    },
    "data": [
        "security/ow_mail_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/ow_mail_account_views.xml",
        "views/ow_mail_tag_views.xml",
        "views/ow_mail_contact_views.xml",
        "views/res_partner_views.xml",
        "views/ow_mail_attach_record_views.xml",
        "views/ow_mail_create_record_views.xml",
        "views/ow_mail_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "ow_mail/static/src/**/*",
        ],
    },
    "images": ["static/description/screenshot.png"],
    "installable": True,
    "application": True,
}
