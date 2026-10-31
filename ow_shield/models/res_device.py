"""OW Shield on top of Odoo 19's native session/device tracking.

Core already maintains ``res.device.log`` rows for every authenticated
session (browser, platform, IP, GeoIP country, last activity), garbage-
collects them and marks rows revoked once their session file is gone.
This layer adds:

* proxy-aware IP capture (core logs the socket peer, i.e. 127.0.0.1
  behind nginx), using the trusted-proxy rules the WAF middleware applies;
* an admin force-logout (core's ``revoke`` is the end-user self-service
  flow and additionally demands a password identity check).

The idle-session timeout cron lives on ``waf.config``.
"""
import logging

from odoo import _, models
from odoo.exceptions import AccessError

from ..core import iputil

_logger = logging.getLogger(__name__)


class ResDeviceLog(models.Model):
    _inherit = "res.device.log"

    def _update_device(self, request):
        """Record the *real* client IP, not the local nginx socket.

        Core reads ``remote_addr``, which behind a same-host proxy is
        always 127.0.0.1, making the device log and its GeoIP attribution
        useless. Resolve the client IP with the same trust rules as the
        WAF and patch it only during core's trace update, then restore.
        """
        environ = request.httprequest.environ
        peer = environ.get("REMOTE_ADDR")
        try:
            cfg = self.env["waf.config"].sudo().get_singleton()
            real_ip = iputil.resolve_client_ip(
                peer, environ.get("HTTP_X_REAL_IP"),
                environ.get("HTTP_X_FORWARDED_FOR"),
                iputil.parse_cidrs(cfg.trusted_proxies))
        except Exception:  # fail-open: never break request dispatch
            return super()._update_device(request)
        if not real_ip or real_ip == peer:
            return super()._update_device(request)
        # Odoo's HTTPRequest proxies remote_addr through a property on the
        # wrapped werkzeug request, so neither the WSGI environ nor the
        # proxy's own __dict__ matter: patch the wrapped request's
        # attribute, then restore it.
        httprequest = request.httprequest
        wrapped = httprequest.__dict__.get("_HTTPRequest__wrapped", httprequest)
        cache = wrapped.__dict__
        had = "remote_addr" in cache
        original = cache.get("remote_addr")
        cache["remote_addr"] = real_ip
        try:
            return super()._update_device(request)
        finally:
            if had:
                cache["remote_addr"] = original
            else:
                cache.pop("remote_addr", None)


class ResDevice(models.Model):
    _inherit = "res.device"

    def action_revoke_session(self):
        """Force-logout the sessions behind the selected device rows.

        Deletes the session files (all rotated sids of the logical
        session) and marks the device logs revoked. OW Shield managers
        only: this ends *other users'* sessions; core's ``revoke`` stays
        the self-service path for everyone else.
        """
        if not self.env.user.has_group("ow_shield.group_waf_manager"):
            raise AccessError(_(
                "Only OW Shield managers can end other users' sessions."))
        if not self:
            return True
        self._revoke()
        return True
