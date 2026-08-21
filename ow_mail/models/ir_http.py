"""Custom route authentication: internal users only.

``auth="user"`` is weaker than it reads — :meth:`ir.http._auth_method_user`
only rejects the *public* user, so a logged-in portal or website customer
passes it. Every ``/ow_mail`` route then relies on the model ACL (all
``ow.mail.*`` models are granted to ``base.group_user`` alone) to stop them,
which works but fails as an AccessError traceback rather than a clean
refusal — and would silently stop working the day a route only touches
models outside that ACL, or uses ``sudo()``.

``auth="ow_mail_user"`` closes that gap at the door: authenticate as usual,
then require an internal user. Portal and public callers get a 403 without
ever reaching a controller body.
"""
from odoo import models
from odoo.exceptions import AccessDenied
from odoo.http import request


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _auth_method_ow_mail_user(cls):
        """Authenticated internal user, or AccessDenied.

        Registered automatically: ``_authenticate_explicit`` resolves an
        endpoint's ``auth`` string to ``_auth_method_<auth>``.
        """
        cls._auth_method_user()
        if not request.env.user._is_internal():
            raise AccessDenied()
