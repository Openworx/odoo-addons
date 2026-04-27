from unittest.mock import MagicMock, patch
import time

from odoo.tests import TransactionCase
from odoo.tests.common import tagged

from ..protocol import cache as cache_mod


def _make_env(uid=1, no_cache=False, cache_enabled=True, ttl=60,
              groups=(), companies=(1,)):
    cfg = MagicMock()
    cfg.cache_enabled = cache_enabled
    cfg.cache_ttl_seconds = ttl
    env = MagicMock()
    env.uid = uid
    env.context = {'ow_mcp_no_cache': True} if no_cache else {}
    # _user_fp(env) reads env.user.{groups_id,company_ids}.ids — make both
    # deterministic so the cache key is stable across calls.
    env.user.groups_id.ids = list(groups)
    env.user.company_ids.ids = list(companies)
    env.__getitem__ = MagicMock(return_value=MagicMock(
        sudo=MagicMock(return_value=MagicMock(
            get_singleton=MagicMock(return_value=cfg)
        ))
    ))
    return env


@tagged('post_install', '-at_install')
class TestInMemoryCache(TransactionCase):

    def setUp(self):
        super().setUp()
        cache_mod._store.clear()

    def test_miss_returns_none(self):
        env = _make_env()
        self.assertIsNone(cache_mod.lookup(env, 'search_records', {'model': 'res.partner'}))

    def test_store_and_hit(self):
        env = _make_env()
        result = {'records': [1, 2, 3]}
        cache_mod.store(env, 'search_records', {'model': 'res.partner'}, result)
        hit = cache_mod.lookup(env, 'search_records', {'model': 'res.partner'})
        self.assertEqual(hit, result)

    def test_write_tool_not_cached(self):
        env = _make_env()
        cache_mod.store(env, 'create_record', {'model': 'res.partner'}, {'id': 1})
        self.assertEqual(len(cache_mod._store), 0)

    def test_no_cache_header_skips_lookup(self):
        env_store = _make_env()
        result = {'records': [1]}
        cache_mod.store(env_store, 'search_records', {'model': 'res.partner'}, result)
        env_skip = _make_env(no_cache=True)
        self.assertIsNone(cache_mod.lookup(env_skip, 'search_records', {'model': 'res.partner'}))

    def test_expired_entry_returns_none(self):
        env = _make_env(ttl=1)
        cache_mod.store(env, 'get_record', {'model': 'res.partner'}, {'id': 1})
        with patch('time.time', return_value=time.time() + 10):
            self.assertIsNone(cache_mod.lookup(env, 'get_record', {'model': 'res.partner'}))

    def test_invalidate_for_write_clears_model(self):
        env = _make_env()
        cache_mod.store(env, 'search_records', {'model': 'res.partner'}, {'x': 1})
        cache_mod.store(env, 'search_records', {'model': 'account.move'}, {'y': 2})
        cache_mod.invalidate_for_write(env, 'delete_record', {'model': 'res.partner'})
        self.assertIsNone(cache_mod.lookup(env, 'search_records', {'model': 'res.partner'}))
        self.assertIsNotNone(cache_mod.lookup(env, 'search_records', {'model': 'account.move'}))

    def test_cache_disabled_skips_store(self):
        env = _make_env(cache_enabled=False)
        cache_mod.store(env, 'search_records', {'model': 'res.partner'}, {'x': 1})
        self.assertEqual(len(cache_mod._store), 0)

    def test_cache_disabled_skips_lookup(self):
        env_on = _make_env(cache_enabled=True)
        cache_mod.store(env_on, 'search_records', {'model': 'res.partner'}, {'x': 1})
        env_off = _make_env(cache_enabled=False)
        self.assertIsNone(cache_mod.lookup(env_off, 'search_records', {'model': 'res.partner'}))

    def test_different_users_separate_entries(self):
        env1 = _make_env(uid=1)
        env2 = _make_env(uid=2)
        cache_mod.store(env1, 'search_records', {'model': 'res.partner'}, {'user': 1})
        self.assertIsNone(cache_mod.lookup(env2, 'search_records', {'model': 'res.partner'}))

    def test_list_models_cached(self):
        env = _make_env()
        result = {'models': ['res.partner', 'account.move']}
        cache_mod.store(env, 'list_models', {}, result)
        self.assertEqual(cache_mod.lookup(env, 'list_models', {}), result)

    def test_invalidate_non_write_tool_noop(self):
        env = _make_env()
        cache_mod.store(env, 'search_records', {'model': 'res.partner'}, {'x': 1})
        cache_mod.invalidate_for_write(env, 'search_records', {'model': 'res.partner'})
        self.assertIsNotNone(cache_mod.lookup(env, 'search_records', {'model': 'res.partner'}))

    # ---- M5: group fingerprint is part of the cache key ----

    def test_group_change_invalidates_cache_entry(self):
        # Same user, same args, but group membership changed → different key,
        # so the previously cached result must no longer be returned.
        env_before = _make_env(uid=42, groups=(1, 2, 3))
        cache_mod.store(
            env_before, 'search_records',
            {'model': 'res.partner'}, {'rows': 'old'},
        )
        env_after = _make_env(uid=42, groups=(1, 2))  # group 3 revoked
        self.assertIsNone(
            cache_mod.lookup(env_after, 'search_records', {'model': 'res.partner'})
        )

    def test_same_groups_different_order_is_same_key(self):
        # `_user_fp` sorts the list, so order on the caller side must not
        # split otherwise-identical cache entries.
        env1 = _make_env(uid=7, groups=(3, 1, 2))
        env2 = _make_env(uid=7, groups=(1, 2, 3))
        cache_mod.store(env1, 'list_models', {}, {'models': []})
        self.assertEqual(
            cache_mod.lookup(env2, 'list_models', {}), {'models': []},
        )
