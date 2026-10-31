"""Active-session features: device tracking fixups, force logout, idle timeout.

Sessions are core 19 machinery (``res.device.log`` + file session store);
OW Shield adds the proxy-aware IP capture, the manager-only force-logout and
the idle-timeout cron. The fake sessions below are crafted directly in the
live session store so they behave exactly like real browser sessions.
"""
import os
import time
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.http import STORED_SESSION_BYTES, root
from odoo.tests import HttpCase, tagged

PW = "owl-sess-test-pw"


@tagged("post_install", "-at_install", "ow_shield")
class TestSessions(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Re-running the suite hours later on the same database leaves
        # *committed* device logs from earlier runs' authenticated
        # requests; those rows age in real time and the idle cron counts
        # them, breaking the exact-count asserts below. They are invisible
        # to the test cursor until committed by another connection, so
        # purge everything clearly not from this run (older than 30 min:
        # rows from the current suite are seconds-to-minutes old, stale
        # pollution is hours old) for the duration of this transaction.
        cls.env["res.device.log"].sudo().search([
            ("last_activity", "<", fields.Datetime.now() - timedelta(minutes=30)),
        ]).unlink()
        cls.sess_user = cls.env["res.users"].create({
            "name": "OWL Session Victim", "login": "owl_sess_user",
            "password": PW,
        })
        cls.plain_user = cls.env["res.users"].create({
            "name": "OWL Plain Manager-less", "login": "owl_plain_user",
            "password": PW,
        })
        cls.created_sessions = []

    @classmethod
    def tearDownClass(cls):
        for session in cls.created_sessions:
            root.session_store.delete(session)
        cls.env["res.device.log"].sudo().search([
            ("user_id", "in", [cls.sess_user.id, cls.plain_user.id]),
        ]).unlink()
        cls.env["waf.config"].get_singleton().write({"session_idle_minutes": 0})
        (cls.sess_user | cls.plain_user).active = False
        super().tearDownClass()

    def _make_session(self, user):
        """A real, saved session file with a valid token for *user*."""
        session = root.session_store.new()
        session.uid = user.id
        session.login = user.login
        session.db = self.env.cr.dbname
        session.context = {}
        session["_trace"] = []
        session.session_token = user._compute_session_token(session.sid)
        root.session_store.save(session)
        self.created_sessions.append(session)
        return session

    # ------------------------------------------------------------- device log

    def test_01_device_log_on_authenticated_request(self):
        # authenticate() normally flags the test session with _trace_disable
        # to keep device logs out of unrelated tests; re-enable it here.
        self.authenticate("owl_sess_user", PW,
                          session_extra={"_trace_disable": False})
        self.url_open("/web")
        logs = self.env["res.device.log"].sudo().search(
            [("user_id", "=", self.sess_user.id)])
        self.assertTrue(logs, "authenticated request must log the device")
        self.assertEqual(set(logs.mapped("ip_address")), {"127.0.0.1"})

    def test_02_proxy_aware_ip_capture(self):
        # core records the socket peer (127.0.0.1 behind nginx); the OW
        # Shield fixup swaps in the trust-aware client IP when the peer is
        # a configured trusted proxy (localhost by default) -- and refuses
        # to when it is not.
        cfg = self.env["waf.config"].get_singleton()
        self.authenticate("owl_sess_user", PW,
                          session_extra={"_trace_disable": False})
        identifier = self.session.sid[:STORED_SESSION_BYTES]

        self.url_open("/web", headers={"X-Forwarded-For": "203.0.113.77"})
        logs = self.env["res.device.log"].sudo().search(
            [("user_id", "=", self.sess_user.id),
             ("session_identifier", "=", identifier)])
        self.assertIn("203.0.113.77", logs.mapped("ip_address"),
                      "X-Forwarded-For from a trusted proxy must be logged")

        old = cfg.trusted_proxies
        try:
            cfg.write({"trusted_proxies": "10.0.0.0/8"})
            self.url_open("/web", headers={"X-Forwarded-For": "198.51.100.66"})
            logs = self.env["res.device.log"].sudo().search(
                [("user_id", "=", self.sess_user.id),
                 ("session_identifier", "=", identifier)])
            self.assertNotIn("198.51.100.66", logs.mapped("ip_address"),
                             "spoofed XFF from an untrusted peer must be ignored")
            self.assertIn("127.0.0.1", logs.mapped("ip_address"),
                          "the socket peer must be recorded instead")
        finally:
            cfg.write({"trusted_proxies": old})

    def test_03_admin_force_logout(self):
        session = self._make_session(self.sess_user)
        identifier = session.sid[:STORED_SESSION_BYTES]
        now = fields.Datetime.now()
        self.env["res.device.log"].sudo().create({
            "session_identifier": identifier, "user_id": self.sess_user.id,
            "ip_address": "198.51.100.9", "platform": "linux",
            "browser": "firefox", "device_type": "computer",
            "first_activity": now - timedelta(minutes=30),
            "last_activity": now,
        })
        # the session works until revoked: token check passes, uid is served
        self.assertTrue(root.session_store.get(session.sid).get("uid"))
        devices = self.env["res.device"].search(
            [("user_id", "=", self.sess_user.id),
             ("session_identifier", "=", identifier)])
        self.assertTrue(devices)
        devices.action_revoke_session()
        # session file gone -> the browser is anonymous again on next request
        missing = root.session_store.get_missing_session_identifiers({identifier})
        self.assertEqual(missing, {identifier})
        logs = self.env["res.device.log"].sudo().search(
            [("session_identifier", "=", identifier)])
        self.assertTrue(all(logs.mapped("revoked")),
                        "revoked sessions must show as revoked")

    def test_04_force_logout_requires_manager(self):
        session = self._make_session(self.sess_user)
        identifier = session.sid[:STORED_SESSION_BYTES]
        self.env["res.device.log"].sudo().create({
            "session_identifier": identifier, "user_id": self.sess_user.id,
            "ip_address": "198.51.100.10", "last_activity": fields.Datetime.now(),
        })
        devices = self.env["res.device"].search(
            [("session_identifier", "=", identifier)])
        with self.assertRaises(AccessError):
            devices.with_user(self.plain_user.id).action_revoke_session()
        # and the session must still be alive (the attempt was refused)
        self.assertTrue(root.session_store.get(session.sid).get("uid"))

    # ------------------------------------------------------------- idle cron

    def test_05_idle_timeout_cron(self):
        cfg = self.env["waf.config"].get_singleton()
        # the timeout must NOT be mirrored onto the server-global core
        # parameter: that one drives cookie max-age and the session-store
        # vacuum for *every* database on the server (cross-database leak
        # fixed in review round 3). Whatever the parameter held before must
        # still hold afterwards -- including nothing at all on a clean
        # database, and including an administrator's own value.
        icp = self.env["ir.config_parameter"].sudo()
        param = "sessions.max_inactivity_seconds"
        before = icp.get_param(param)
        cfg.write({"session_idle_minutes": 60})
        self.assertEqual(icp.get_param(param), before,
                         "idle timeout must stay per-database")

        session = self._make_session(self.sess_user)
        identifier = session.sid[:STORED_SESSION_BYTES]
        now = fields.Datetime.now()
        self.env["res.device.log"].sudo().create({
            "session_identifier": identifier, "user_id": self.sess_user.id,
            "ip_address": "198.51.100.11", "last_activity": now - timedelta(hours=3),
        })
        # backdate the session file: 3h of inactivity > 60min + slack cutoff
        past = time.time() - 3 * 3600
        os.utime(root.session_store.get_session_filename(session.sid),
                 (past, past))

        cfg._cron_session_timeout()
        missing = root.session_store.get_missing_session_identifiers({identifier})
        self.assertEqual(missing, {identifier},
                         "idle session file must be force-deleted")
        log = self.env["res.device.log"].sudo().search(
            [("session_identifier", "=", identifier)])
        self.assertTrue(log.revoked, "device log must be marked revoked")

        # a *fresh* session survives the same pass
        keeper = self._make_session(self.sess_user)
        cfg._cron_session_timeout()
        self.assertTrue(root.session_store.get(keeper.sid).get("uid"),
                        "active sessions must not be killed")

        # disabling leaves the core parameter untouched as well
        cfg.write({"session_idle_minutes": 0})
        self.assertEqual(icp.get_param(param), before,
                         "turning the timeout off must not write the "
                         "server-global parameter either")
        self.assertEqual(cfg._cron_session_timeout(), {"disabled": True})

    def test_06_idle_cron_evicts_per_session_not_per_device_log(self):
        # review round-2: one session keeps a device log per IP/browser it
        # used, and last_activity refreshes only hourly (core's
        # Session.update_trace). An old log from a departed network must
        # not evict the session that is live on the user's current one.
        cfg = self.env["waf.config"].get_singleton()
        cfg.write({"session_idle_minutes": 60})
        session = self._make_session(self.sess_user)
        identifier = session.sid[:STORED_SESSION_BYTES]
        now = fields.Datetime.now()
        self.env["res.device.log"].sudo().create([
            {"session_identifier": identifier, "user_id": self.sess_user.id,
             "ip_address": "198.51.100.20",   # office Wi-Fi: gone 3h ago
             "last_activity": now - timedelta(hours=3)},
            {"session_identifier": identifier, "user_id": self.sess_user.id,
             "ip_address": "198.51.100.21",   # hotspot: still working
             "last_activity": now - timedelta(minutes=5)},
        ])
        self.assertEqual(cfg._cron_session_timeout(),
                         {"revoked_sessions": 0},
                         "session active on one device must survive")
        self.assertTrue(root.session_store.get(session.sid).get("uid"),
                        "session file of an active session must stay")
        logs = self.env["res.device.log"].sudo().search(
            [("session_identifier", "=", identifier)])
        self.assertFalse(any(logs.mapped("revoked")),
                         "no device log may be marked revoked")

    def test_07_idle_timeout_floor_constraint(self):
        # review round-2: sub-hour timeouts would log out continuously
        # active users (activity is recorded at most hourly) -> refused.
        cfg = self.env["waf.config"].get_singleton()
        with self.assertRaises(ValidationError):
            cfg.write({"session_idle_minutes": 30})
        cfg.write({"session_idle_minutes": 0})    # disabled stays valid
        cfg.write({"session_idle_minutes": 60})   # the floor itself is valid

        # legacy value set *before* the floor existed (raw SQL, bypassing
        # the constraint): the cron clamps it instead of evicting a
        # 30-minutes-ago-but-possibly-still-active session
        self.env.cr.execute(
            "UPDATE waf_config SET session_idle_minutes = 10 WHERE id = %s",
            (cfg.id,))
        # flush=False: the *default* would flush the pending cached write
        # of 60 over the raw update and the fixture would self-defeat
        cfg.invalidate_recordset(["session_idle_minutes"], flush=False)
        self.assertEqual(self.env["waf.config"].get_singleton()
                         .session_idle_minutes, 10,
                         "fixture must really carry a sub-floor value")
        session = self._make_session(self.sess_user)
        self.env["res.device.log"].sudo().create({
            "session_identifier": session.sid[:STORED_SESSION_BYTES],
            "user_id": self.sess_user.id, "ip_address": "198.51.100.22",
            "last_activity": fields.Datetime.now() - timedelta(minutes=30),
        })
        self.assertEqual(cfg._cron_session_timeout(),
                         {"revoked_sessions": 0},
                         "cron must enforce the 60-minute floor")
        cfg.write({"session_idle_minutes": 0})

    def test_08_idle_cron_adds_hour_of_recording_slack(self):
        # review round-3: core's Session.update_trace only rewrites
        # last_activity when the stored value is >= 3600s old, so an
        # *active* user's stored activity can be up to an hour behind their
        # real one (last click 10:59, stored 10:00). Evicting at exactly
        # T=60 logged such users out ~1 minute after a real click. The
        # cutoff must wait T + SESSION_ACTIVITY_SLACK: worst case logout
        # lands at T+60 idle minutes, never earlier than T.
        cfg = self.env["waf.config"].get_singleton()
        cfg.write({"session_idle_minutes": 60})
        now = fields.Datetime.now()
        active = self._make_session(self.sess_user)
        self.env["res.device.log"].sudo().create({
            "session_identifier": active.sid[:STORED_SESSION_BYTES],
            "user_id": self.sess_user.id, "ip_address": "198.51.100.23",
            # recorded 61 min ago; user could have been active until a
            # minute ago (the hourly refresh gap)
            "last_activity": now - timedelta(minutes=61),
        })
        self.assertEqual(cfg._cron_session_timeout(),
                         {"revoked_sessions": 0},
                         "inside the T+60 recording-slack window: keep")
        self.assertTrue(root.session_store.get(active.sid).get("uid"),
                        "possibly-still-active session must survive")

        # beyond the window (60 + 60 slack + 1) the session is evicted
        gone = self._make_session(self.sess_user)
        self.env["res.device.log"].sudo().create({
            "session_identifier": gone.sid[:STORED_SESSION_BYTES],
            "user_id": self.sess_user.id, "ip_address": "198.51.100.24",
            "last_activity": now - timedelta(minutes=121),
        })
        self.assertEqual(cfg._cron_session_timeout(),
                         {"revoked_sessions": 1},
                         "idle past T+slack must be evicted")
        self.assertFalse(root.session_store.get(gone.sid).get("uid"),
                         "evicted session must be gone")
        cfg.write({"session_idle_minutes": 0})

    def test_10_runtime_never_touches_global_param(self):
        # the runtime path must stay inert in both directions: OW Shield
        # never writes the server-global parameter, and never removes it
        # either. Otherwise an admin who independently set 3600 while
        # running a 60-minute idle timeout lost their own core setting on
        # the next config save.
        cfg = self.env["waf.config"].get_singleton()
        icp = self.env["ir.config_parameter"].sudo()
        param = "sessions.max_inactivity_seconds"
        try:
            icp.set_param(param, "3600")
            cfg.write({"session_idle_minutes": 60})
            self.assertEqual(icp.get_param(param), "3600",
                             "saving the timeout must not remove an "
                             "administrator's own core setting")
            cfg.write({"session_idle_minutes": 0})
            self.assertEqual(icp.get_param(param), "3600",
                             "disabling it must not remove it either")
        finally:
            icp.set_param(param, False)
