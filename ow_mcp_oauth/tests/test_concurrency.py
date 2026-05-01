"""Regression tests for the production blockers fixed in this branch:

  1. Atomic single-use of authorization codes (no TOCTOU).
  2. Atomic single-use of refresh tokens + atomic chain revoke.
  3. DCR rate-limit persisted across workers (DB-backed).
"""
from datetime import datetime, timedelta

from odoo.tests import HttpCase, tagged

from . import _helpers as h
from ..controllers import _utils as _u


@tagged('post_install', '-at_install')
class TestAtomicCodeRedemption(HttpCase):

    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'login': 'race_code_user',
            'name': 'Race Code',
            'password': 'pass',
            'groups_id': [
                (4, self.env.ref('ow_mcp_server.group_mcp_user').id),
                (4, self.env.ref('base.group_user').id),
            ],
        })
        self.dcr = h.register_client(self)
        self.client = self.env['ow.mcp.oauth.client'].sudo().search(
            [('client_id', '=', self.dcr['client_id'])], limit=1,
        )
        self.cfg = self.env['ow.mcp.oauth.config'].sudo().get_singleton()

    def test_atomic_update_burns_code(self):
        """Direct SQL test: the UPDATE...RETURNING used at /token must
        match exactly once for the same code_hash. A second UPDATE
        cannot consume the same row."""
        verifier, challenge = h.make_pkce_pair()
        code = h.issue_code_directly(
            self, client=self.client, user=self.user,
            redirect_uri='http://127.0.0.1:9999/callback',
            code_challenge=challenge,
            resource=self.cfg.effective_canonical_resource_uri(),
        )
        import hashlib
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        sql = (
            "UPDATE ow_mcp_oauth_authorization_code "
            "   SET consumed = TRUE "
            " WHERE code_hash = %s AND consumed = FALSE "
            "RETURNING id"
        )
        self.env.cr.execute(sql, (code_hash,))
        first = self.env.cr.fetchall()
        self.env.cr.execute(sql, (code_hash,))
        second = self.env.cr.fetchall()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)


@tagged('post_install', '-at_install')
class TestAtomicRefreshRotation(HttpCase):

    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'login': 'race_refresh_user',
            'name': 'Race Refresh',
            'password': 'pass',
            'groups_id': [
                (4, self.env.ref('ow_mcp_server.group_mcp_user').id),
                (4, self.env.ref('base.group_user').id),
            ],
        })
        self.dcr = h.register_client(self)
        self.client = self.env['ow.mcp.oauth.client'].sudo().search(
            [('client_id', '=', self.dcr['client_id'])], limit=1,
        )

    def _seed_token(self, parent=None):
        return self.env['ow.mcp.oauth.refresh_token'].sudo().create({
            'token_hash': 'h-' + (parent.token_hash if parent else 'root'),
            'client_id': self.client.id,
            'user_id': self.user.id,
            'audience': 'http://x/mcp',
            'parent_id': parent.id if parent else False,
            'issued_at': 0,
            'expires_at': datetime.utcnow() + timedelta(days=1),
        })

    def test_atomic_consume(self):
        """The UPDATE used by /token must claim a refresh row exactly once."""
        rec = self.env['ow.mcp.oauth.refresh_token'].sudo().create({
            'token_hash': 'h-atomic',
            'client_id': self.client.id,
            'user_id': self.user.id,
            'audience': 'http://x/mcp',
            'issued_at': 0,
            'expires_at': datetime.utcnow() + timedelta(days=1),
        })
        sql = (
            "UPDATE ow_mcp_oauth_refresh_token "
            "   SET consumed_at = (NOW() AT TIME ZONE 'UTC') "
            " WHERE token_hash = %s AND consumed_at IS NULL "
            "   AND revoked = FALSE "
            "RETURNING id"
        )
        self.env.cr.execute(sql, ('h-atomic',))
        first = self.env.cr.fetchall()
        self.env.cr.execute(sql, ('h-atomic',))
        second = self.env.cr.fetchall()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)
        rec.invalidate_recordset(['consumed_at'])
        self.assertTrue(rec.consumed_at)

    def test_revoke_chain_walks_full_graph(self):
        """`revoke_chain` must reach every node reachable via parent_id
        or replaced_by_id, in a single SQL pass."""
        a = self._seed_token()
        b = self._seed_token(parent=a)
        c = self._seed_token(parent=b)
        d = self._seed_token(parent=c)
        a.replaced_by_id = b.id
        b.replaced_by_id = c.id
        c.replaced_by_id = d.id
        # Revoke from a middle node — should still revoke all four.
        b.revoke_chain()
        for tok in (a, b, c, d):
            tok.invalidate_recordset(['revoked'])
            self.assertTrue(
                tok.revoked, f'token {tok.token_hash} not revoked',
            )


@tagged('post_install', '-at_install')
class TestDCRThrottlePersisted(HttpCase):

    def setUp(self):
        super().setUp()
        _u.dcr_reset(self.env)

    def test_throttle_counts_persist_across_calls(self):
        """The DCR ledger lives in the DB so two callers (same Python
        process here, different workers in production) share the same
        sliding-window counter."""
        ip = '203.0.113.7'
        for _ in range(3):
            self.assertTrue(_u.dcr_check_throttle(self.env, ip, 3))
        self.assertFalse(_u.dcr_check_throttle(self.env, ip, 3))
        # And the persisted ledger reflects the count.
        n = self.env['ow.mcp.oauth.dcr_attempt'].sudo().search_count(
            [('ip', '=', ip)],
        )
        self.assertEqual(n, 3)

    def test_throttle_independent_per_ip(self):
        for _ in range(2):
            self.assertTrue(_u.dcr_check_throttle(self.env, '198.51.100.1', 2))
        self.assertFalse(_u.dcr_check_throttle(self.env, '198.51.100.1', 2))
        self.assertTrue(_u.dcr_check_throttle(self.env, '198.51.100.2', 2))

    def test_dcr_prune_drops_old_rows(self):
        """The hourly cron must drop rows outside the sliding window."""
        env = self.env
        env['ow.mcp.oauth.dcr_attempt'].sudo().create({'ip': '10.0.0.1'})
        env.cr.execute(
            "UPDATE ow_mcp_oauth_dcr_attempt "
            "   SET create_date = (NOW() AT TIME ZONE 'UTC') "
            "                     - INTERVAL '2 hours' "
            " WHERE ip = '10.0.0.1'"
        )
        _u.dcr_prune(env)
        n = env['ow.mcp.oauth.dcr_attempt'].sudo().search_count(
            [('ip', '=', '10.0.0.1')],
        )
        self.assertEqual(n, 0)
