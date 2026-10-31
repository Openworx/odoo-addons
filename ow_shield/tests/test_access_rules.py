from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase


class TestCounterModelAccess(TransactionCase):
    """The four automatic counter tables (rule-hit lockout, login
    brute-force, endpoint flood, master-password brute-force) are
    raw-SQL-only stores: every production read/write goes through the
    counter layer's own cursors, never through the ORM.

    Since 19.0.2.9.2 they carry EXPLICIT deny-all access rules instead
    of simply having none. That keeps the product behaviour identical
    (no group, WAF managers included, may touch them via the ORM; only
    superuser/raw SQL does) while turning the implicit default-deny into
    a reviewable, enforced statement -- and it silences the module
    loader's "models ... have no access rules" warning, so a genuine
    missing-ACL regression stays loud in the logs.
    """

    COUNTERS = ("waf.lockout.hit", "waf.login.hit", "waf.rate.hit",
                "waf.master.hit")

    def _mk(self, login, groups):
        return self.env["res.users"].create({
            "name": login,
            "login": login,
            "password": "Waf-Acl-Probe-2026!",
            "group_ids": [(6, 0, [self.env.ref(g).id for g in groups])],
        })

    def test_01_counter_models_carry_deny_all_rules(self):
        for name in self.COUNTERS:
            rules = self.env["ir.model.access"].search(
                [("model_id.model", "=", name)])
            self.assertTrue(
                rules, f"{name} must carry explicit access rules")
            granted = [r for r in rules if r.perm_read or r.perm_write
                       or r.perm_create or r.perm_unlink]
            self.assertFalse(
                granted, f"{name} must stay ORM-inaccessible")

    def test_02_internal_user_denied_every_counter(self):
        user = self._mk("waf_acl_internal", ["base.group_user"])
        for name in self.COUNTERS:
            with self.assertRaises(AccessError,
                                   msg=f"{name} readable by internal user"):
                self.env[name].with_user(user).search([])

    def test_03_waf_manager_denied_every_counter(self):
        mgr = self._mk("waf_acl_manager",
                       ["base.group_user", "ow_shield.group_waf_manager"])
        for name in self.COUNTERS:
            model = self.env[name].with_user(mgr)
            with self.assertRaises(AccessError,
                                   msg=f"{name} readable by WAF manager"):
                model.search([])
            with self.assertRaises(AccessError,
                                   msg=f"{name} writable by WAF manager"):
                model.create({"network": "192.0.2.53/32"})
