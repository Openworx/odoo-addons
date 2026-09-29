# -*- coding: utf-8 -*-
"""``utils/params.py``: typed ``ir.config_parameter`` access on Odoo 19.

Odoo 19 only has ``get_param``/``set_param`` (strings); the helpers coerce,
fall back to the default on empty or junk values (a junk value logs a
warning instead of crashing a chat round) and read with ``sudo()``.
"""
from odoo.tests import TransactionCase, new_test_user

from ..utils import params

_LOGGER = 'odoo.addons.ow_ai.utils.params'
_KEY = 'ow_ai.test_param'


class TestParams(TransactionCase):

    def _store(self, value):
        self.env['ir.config_parameter'].sudo().set_param(_KEY, value)

    def test_missing_returns_the_default(self):
        self.assertEqual(params.get_str(self.env, _KEY), '')
        self.assertEqual(params.get_str(self.env, _KEY, 'x'), 'x')
        self.assertEqual(params.get_int(self.env, _KEY), 0)
        self.assertEqual(params.get_int(self.env, _KEY, 7), 7)
        self.assertEqual(params.get_float(self.env, _KEY), 0.0)
        self.assertEqual(params.get_float(self.env, _KEY, 2.5), 2.5)
        self.assertIs(params.get_bool(self.env, _KEY), False)
        self.assertIs(params.get_bool(self.env, _KEY, True), True)

    def test_empty_value_returns_the_default(self):
        self.env['ir.config_parameter'].sudo().create({'key': _KEY, 'value': ''})
        self.assertEqual(params.get_str(self.env, _KEY, 'x'), 'x')
        self.assertEqual(params.get_int(self.env, _KEY, 7), 7)
        self.assertEqual(params.get_float(self.env, _KEY, 2.5), 2.5)
        self.assertIs(params.get_bool(self.env, _KEY, True), True)

    def test_get_str(self):
        self._store('openai/gpt-6-luna')
        self.assertEqual(params.get_str(self.env, _KEY, 'x'), 'openai/gpt-6-luna')

    def test_get_int(self):
        self._store('12')
        self.assertEqual(params.get_int(self.env, _KEY, 7), 12)
        self._store('0')
        self.assertEqual(params.get_int(self.env, _KEY, 7), 0)

    def test_get_int_junk_returns_the_default_and_warns(self):
        self._store('abc')
        with self.assertLogs(_LOGGER, 'WARNING') as logs:
            self.assertEqual(params.get_int(self.env, _KEY, 7), 7)
        self.assertIn(_KEY, logs.output[0])
        self._store('1.5')
        with self.assertLogs(_LOGGER, 'WARNING'):
            self.assertEqual(params.get_int(self.env, _KEY, 7), 7)

    def test_get_float(self):
        self._store('1.5')
        self.assertEqual(params.get_float(self.env, _KEY), 1.5)
        self._store('12')
        self.assertEqual(params.get_float(self.env, _KEY), 12.0)

    def test_get_float_junk_returns_the_default_and_warns(self):
        self._store('abc')
        with self.assertLogs(_LOGGER, 'WARNING'):
            self.assertEqual(params.get_float(self.env, _KEY, 2.5), 2.5)

    def test_get_bool(self):
        for value in ('True', 'true', 'TRUE', '1', 'yes', 'Yes', 'on', 'ON'):
            with self.subTest(value=value):
                self._store(value)
                self.assertIs(params.get_bool(self.env, _KEY, False), True)
        for value in ('False', 'false', 'FALSE', '0', 'no', 'No', 'off', 'OFF'):
            with self.subTest(value=value):
                self._store(value)
                self.assertIs(params.get_bool(self.env, _KEY, True), False)

    def test_get_bool_junk_returns_the_default_and_warns(self):
        self._store('maybe')
        with self.assertLogs(_LOGGER, 'WARNING') as logs:
            self.assertIs(params.get_bool(self.env, _KEY, True), True)
        self.assertIn(_KEY, logs.output[0])
        with self.assertLogs(_LOGGER, 'WARNING'):
            self.assertIs(params.get_bool(self.env, _KEY, False), False)

    def test_set_bool(self):
        params.set_bool(self.env, _KEY, True)
        self.assertEqual(self.env['ir.config_parameter'].sudo().get_param(_KEY), 'True')
        self.assertIs(params.get_bool(self.env, _KEY), True)
        # False is stored, not unset: a default of True must not win over it.
        params.set_bool(self.env, _KEY, False)
        self.assertEqual(self.env['ir.config_parameter'].sudo().get_param(_KEY), 'False')
        self.assertIs(params.get_bool(self.env, _KEY, True), False)

    def test_set_int_and_float(self):
        params.set_int(self.env, _KEY, 12)
        self.assertEqual(self.env['ir.config_parameter'].sudo().get_param(_KEY), '12')
        self.assertEqual(params.get_int(self.env, _KEY), 12)
        params.set_int(self.env, _KEY, 0)
        self.assertEqual(params.get_int(self.env, _KEY, 7), 0)
        params.set_float(self.env, _KEY, 1.5)
        self.assertEqual(self.env['ir.config_parameter'].sudo().get_param(_KEY), '1.5')
        self.assertEqual(params.get_float(self.env, _KEY), 1.5)

    def test_set_str(self):
        params.set_str(self.env, _KEY, 'sk-or-test')
        self.assertEqual(params.get_str(self.env, _KEY), 'sk-or-test')
        params.set_str(self.env, _KEY, '')
        self.assertIs(self.env['ir.config_parameter'].sudo().get_param(_KEY), False)
        self.assertEqual(params.get_str(self.env, _KEY, 'x'), 'x')

    def test_reads_and_writes_as_superuser(self):
        # ir.config_parameter is readable by administrators only; the
        # engine reads its limits in the requesting user's environment.
        user = new_test_user(self.env, login='ow_ai_params_user', groups='base.group_user')
        user_env = self.env(user=user)
        params.set_int(user_env, _KEY, 12)
        self.assertEqual(params.get_int(user_env, _KEY), 12)
