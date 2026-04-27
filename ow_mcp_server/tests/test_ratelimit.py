from odoo.tests.common import BaseCase

from odoo.addons.ow_mcp_server.protocol import ratelimit


class TestRateLimit(BaseCase):
    def setUp(self):
        super().setUp()
        ratelimit.reset()

    def test_disabled_always_allows(self):
        for _ in range(1000):
            self.assertTrue(ratelimit.check_and_record(1, 0))

    def test_allows_up_to_limit(self):
        for _ in range(5):
            self.assertTrue(ratelimit.check_and_record(42, 5))

    def test_blocks_over_limit(self):
        for _ in range(5):
            self.assertTrue(ratelimit.check_and_record(42, 5))
        self.assertFalse(ratelimit.check_and_record(42, 5))
        self.assertFalse(ratelimit.check_and_record(42, 5))

    def test_buckets_are_per_uid(self):
        for _ in range(5):
            self.assertTrue(ratelimit.check_and_record(1, 5))
        # Different uid has its own bucket.
        self.assertTrue(ratelimit.check_and_record(2, 5))
