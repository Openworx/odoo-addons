{
    'name': 'Security.txt',
    'version': '18.0.1.0.0',
    'summary': 'Serve /.well-known/security.txt (RFC 9116)',
    'description': """
Security.txt
============

Serves a security.txt file at /.well-known/security.txt as specified by
RFC 9116, so security researchers know how to report vulnerabilities.

All values are configured in Settings > Security.txt. The required Expires
field is computed automatically as a rolling "now + N months" timestamp.
If no Contact is configured, the endpoint returns 404 instead of serving
an RFC-invalid file. The legacy /security.txt path redirects to the
well-known location.
""",
    'author': 'Openworx',
    'category': 'Website',
    'license': 'LGPL-3',
    'depends': ['base_setup'],
    'data': [
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'application': False,
}
