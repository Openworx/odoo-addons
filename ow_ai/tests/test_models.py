import importlib.util
import pathlib

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.modules.module import load_script
from odoo.tests import TransactionCase, new_test_user
from odoo.tools import mute_logger
from psycopg2 import IntegrityError


class TestAgent(TransactionCase):

    def test_create_creates_inactive_partner_and_syncs_name(self):
        agent = self.env['ow.ai.agent'].create({'name': "Test Agent"})
        self.assertTrue(agent.partner_id)
        self.assertFalse(agent.partner_id.active)
        self.assertEqual(agent.partner_id.name, "Test Agent")
        self.assertEqual(agent.name, "Test Agent")

    def test_create_session_ignores_default_keys_of_the_context(self):
        agent = self.env.ref('ow_ai.agent_default').with_context(
            default_active=False, default_loop_state='waiting_model', default_request_round=7)
        session = agent._create_session(self.env['discuss.channel'], res_model='res.partner', res_id=1)
        self.assertEqual((session.active, session.loop_state, session.request_round), (True, 'ready', 0))
        self.assertEqual((session.res_model, session.res_id), ('res.partner', 1))

    def test_get_model_falls_back_to_default_setting(self):
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.default_model', 'openai/gpt-4.1-mini')
        agent = self.env['ow.ai.agent'].create({'name': "Test Agent"})
        self.assertEqual(agent._get_model(), 'openai/gpt-4.1-mini')
        agent.model = 'mistralai/mistral-large-2411'
        self.assertEqual(agent._get_model(), 'mistralai/mistral-large-2411')

    def test_write_partner_id_raises(self):
        agent = self.env['ow.ai.agent'].create({'name': "Test Agent"})
        other_partner = self.env['res.partner'].create({'name': "Other"})
        with self.assertRaises(UserError):
            agent.write({'partner_id': other_partner.id})

    def test_unlink_system_agent_raises(self):
        agent = self.env['ow.ai.agent'].create({'name': "Test Agent", 'is_system_agent': True})
        with self.assertRaises(UserError):
            agent.unlink()

    def test_unlink_normal_agent_removes_partner(self):
        agent = self.env['ow.ai.agent'].create({'name': "Test Agent"})
        partner = agent.partner_id
        self.assertTrue(partner.ow_ai_agent_partner)
        agent.unlink()
        self.assertFalse(partner.exists())

    def test_unlink_agent_on_a_shared_contact_keeps_the_contact(self):
        # Only the superuser (data files, migrations) binds an agent to a
        # contact of its own; deleting the agent leaves that contact alone.
        contact = self.env['res.partner'].create({'name': "Shared contact"})
        agent = self.env['ow.ai.agent'].create({'partner_id': contact.id})
        self.assertFalse(contact.ow_ai_agent_partner)
        manager = new_test_user(self.env, login='ow_ai_shared_contact_manager', groups='ow_ai.group_ai_manager')
        agent.with_user(manager).unlink()
        self.assertFalse(agent.exists())
        self.assertTrue(contact.exists())

    def test_unlink_keeps_a_contact_an_archived_agent_still_uses(self):
        agent = self.env['ow.ai.agent'].create({'name': "Test Agent"})
        archived = self.env['ow.ai.agent'].create({'partner_id': agent.partner_id.id, 'active': False})
        agent.unlink()
        self.assertTrue(archived.exists())
        self.assertTrue(archived.partner_id.exists())

    def test_copy_gets_its_own_contact(self):
        manager = new_test_user(self.env, login='ow_ai_copy_agent_manager', groups='ow_ai.group_ai_manager')
        agent = self.env['ow.ai.agent'].with_user(manager).create({'name': "Original"})
        duplicate = agent.copy()
        self.assertNotEqual(duplicate.partner_id, agent.partner_id)
        self.assertEqual(duplicate.name, "Original (copy)")
        self.assertTrue(duplicate.sudo().partner_id.ow_ai_agent_partner)
        duplicate.unlink()
        self.assertTrue(agent.partner_id.exists())
        self.assertEqual(agent.name, "Original")

    def test_copy_carries_the_avatar_to_its_new_contact(self):
        # 1x1 transparent PNG.
        image_b64 = (
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
            '+A8AAQUBAScY42YAAAAASUVORK5CYII='
        )
        agent = self.env['ow.ai.agent'].create({'name': "With Avatar"})
        agent.write({'image_128': image_b64})

        duplicate = agent.copy()

        self.assertNotEqual(duplicate.partner_id, agent.partner_id)
        self.assertTrue(duplicate.partner_id.image_1920)
        # Odoo 20 binary values are `BinaryValue` objects without `__eq__`
        self.assertEqual(duplicate.partner_id.image_1920.content, agent.partner_id.image_1920.content)

    def test_upgrade_flags_the_contacts_of_existing_agents(self):
        """``migrations/20.0.1.2.0``: agents created through ``create()``
        before the flag existed get their contact flagged."""
        agent = self.env['ow.ai.agent'].create({'name': "Legacy"})
        archived = self.env['ow.ai.agent'].create({'name': "Legacy archived", 'active': False})
        (agent | archived).partner_id.ow_ai_agent_partner = False
        other = self.env['res.partner'].create({'name': "Not an agent"})
        script = load_script('ow_ai/migrations/20.0.1.2.0/post-flag_agent_partners.py',
                             'odoo.upgrade.ow_ai.test_flag_agent_partners')
        script.migrate(self.env.cr, '20.0.1.1.0')
        self.assertTrue(agent.partner_id.ow_ai_agent_partner)
        self.assertTrue(archived.partner_id.ow_ai_agent_partner)
        self.assertFalse(other.ow_ai_agent_partner)

    def _bound_contacts(self):
        """Agents bound (as superuser, as data files and older versions could)
        to contacts that differ from the one ``create()`` makes -- archived,
        a plain contact, nothing else -- in one respect each."""
        partners = self.env['res.partner'].with_context(active_test=False)
        plain = {'active': False, 'type': 'contact'}
        contacts = {
            'active': partners.create({'name': "Active", 'active': True}),
            'invoice address': partners.create({**plain, 'name': "Invoice address", 'type': 'invoice'}),
            'company': partners.create({**plain, 'name': "Company"}),
            'email': partners.create({**plain, 'name': "Email", 'email': 'someone@example.com'}),
            'phone': partners.create({**plain, 'name': "Phone", 'phone': '+31 20 123 4567'}),
            'tax id': partners.create({**plain, 'name': "Tax id", 'vat': 'NL123456789B01'}),
            'parent': partners.create({
                **plain, 'name': "Employee", 'parent_id': partners.create({'name': "Employer"}).id}),
            'child': partners.create({**plain, 'name': "Parent"}),
            'user': new_test_user(self.env, login='ow_ai_bound_contact_user').partner_id,
            'external id': partners.create({**plain, 'name': "Imported"}),
        }
        # `is_company` is a stored compute in 20.0 (`commercial_partner_id == self and has_vat`);
        # it cannot be set through `create()`. A row where it is still `True` without a `vat`
        # only happens for data the compute has not revisited since (an import, an old branch
        # of an upgrade): reproduce that stale state directly.
        self.env.cr.execute("UPDATE res_partner SET is_company = true WHERE id = %s", (contacts['company'].id,))
        contacts['company'].invalidate_recordset(['is_company'])
        partners.create({'name': "Archived child", 'active': False, 'parent_id': contacts['child'].id})
        contacts['user'].user_ids.active = False
        contacts['user'].active = False
        self.env['ir.model.data'].create({
            'module': '__import__', 'name': 'ow_ai_bound_contact', 'model': 'res.partner',
            'res_id': contacts['external id'].id})
        for contact in contacts.values():
            self.env['ow.ai.agent'].create({'partner_id': contact.id})
        return contacts

    def test_upgrade_flags_only_contacts_shaped_like_its_own(self):
        """``20.0.1.2.0`` cannot know which contact an older version created:
        any contact that is not exactly what ``create()`` makes stays unflagged."""
        contacts = self._bound_contacts()
        own = self.env['ow.ai.agent'].create({'name': "Own", 'active': False}).partner_id
        own.ow_ai_agent_partner = False

        self.env['ow.ai.agent']._ow_ai_flag_agent_partners()

        self.assertTrue(own.ow_ai_agent_partner)
        for reason, contact in contacts.items():
            with self.subTest(reason=reason):
                self.assertFalse(contact.ow_ai_agent_partner)

    def test_upgrade_repair_unflags_only_contacts_not_shaped_like_its_own(self):
        contacts = self._bound_contacts()
        for contact in contacts.values():
            contact.ow_ai_agent_partner = True   # what 20.0.1.2.0 did
        own = self.env['ow.ai.agent'].create({'name': "Own"}).partner_id
        own_with_avatar = self.env['ow.ai.agent'].create({
            'name': "Avatar",
            'image_128': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
        }).partner_id
        not_an_agent = self.env['res.partner'].create({'name': "Not an agent", 'email': 'x@example.com'})

        script = load_script('ow_ai/migrations/20.0.1.2.1/post-repair_agent_partner_flags.py',
                             'odoo.upgrade.ow_ai.test_repair_agent_partner_flags')
        script.migrate(self.env.cr, '20.0.1.2.0')

        self.assertTrue(own.ow_ai_agent_partner)
        self.assertTrue(own_with_avatar.ow_ai_agent_partner)
        self.assertFalse(not_an_agent.ow_ai_agent_partner)
        for reason, contact in contacts.items():
            with self.subTest(reason=reason):
                self.assertFalse(contact.ow_ai_agent_partner)

    def test_model_options_validation(self):
        agent = self.env['ow.ai.agent'].create({'name': "Test Agent"})
        with self.assertRaises(ValidationError):
            agent.model_options = {'unknown_key': 1}
        agent.model_options = {'temperature': 0.5}
        self.assertEqual(agent._get_model_options(), {'temperature': 0.5})

    def test_get_default_tools(self):
        agent = self.env['ow.ai.agent'].create({'name': "Test Agent"})
        tools = agent._get_default_tools()
        self.assertEqual(set(tools.mapped('builtin_key')), {'interaction.load_skills', 'interaction.ask_user_question'})

    def test_write_image_128_updates_partner_image(self):
        # 1x1 transparent PNG.
        image_b64 = (
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
            '+A8AAQUBAScY42YAAAAASUVORK5CYII='
        )
        agent = self.env['ow.ai.agent'].create({'name': "Test Agent"})
        self.assertFalse(agent.partner_id.image_1920)
        agent.write({'image_128': image_b64})
        # image_128 relates to partner_id.image_1920 (the writable source
        # field), not partner_id.image_128 (itself a readonly related field).
        self.assertTrue(agent.partner_id.image_1920)
        self.assertTrue(agent.image_128)
        self.assertTrue(agent.partner_id.image_128)


class TestSkill(TransactionCase):

    def test_native_lock_write_locked_field_raises(self):
        skill = self.env['ow.ai.skill'].create({
            'name': "Native Skill", 'description': "desc", 'is_native': True,
        })
        with self.assertRaises(UserError):
            skill.write({'name': "New name"})

    def test_native_lock_allows_description(self):
        skill = self.env['ow.ai.skill'].create({
            'name': "Native Skill", 'description': "desc", 'is_native': True,
        })
        skill.write({'description': "new desc", 'active': False, 'sequence': 20})
        self.assertEqual(skill.description, "new desc")

    def test_native_unlink_raises(self):
        skill = self.env['ow.ai.skill'].create({
            'name': "Native Skill", 'description': "desc", 'is_native': True,
        })
        with self.assertRaises(UserError):
            skill.unlink()

    def test_skill_type_computed(self):
        skill = self.env['ow.ai.skill'].create({'name': "Skill", 'description': "desc"})
        self.assertEqual(skill.skill_type, 'guidance')
        tool = self.env['ow.ai.tool'].create({
            'name': "Tool", 'tool_name': 'my_tool', 'description': "desc",
            'builtin_key': 'interaction.load_skills',
        })
        skill.tool_ids = [(4, tool.id)]
        self.assertEqual(skill.skill_type, 'executable')


class TestTool(TransactionCase):

    def test_tool_name_regex(self):
        with self.assertRaises(ValidationError):
            self.env['ow.ai.tool'].create({
                'name': "Bad", 'tool_name': 'bad name!', 'description': "desc",
            })

    def test_unique_active_tool_name_archived_duplicate_allowed(self):
        self.env['ow.ai.tool'].create({
            'name': "Tool 1", 'tool_name': 'dup_tool', 'description': "desc", 'active': False,
            'builtin_key': 'interaction.load_skills',
        })
        tool = self.env['ow.ai.tool'].create({
            'name': "Tool 2", 'tool_name': 'dup_tool', 'description': "desc",
            'builtin_key': 'interaction.load_skills',
        })
        self.assertTrue(tool)

    def test_unique_active_tool_name_raises_when_both_active(self):
        self.env['ow.ai.tool'].create({
            'name': "Tool 1", 'tool_name': 'dup_tool_active', 'description': "desc",
            'builtin_key': 'interaction.load_skills',
        })
        with mute_logger('odoo.sql_db'), self.assertRaises(IntegrityError):
            self.env['ow.ai.tool'].create({
                'name': "Tool 2", 'tool_name': 'dup_tool_active', 'description': "desc",
                'builtin_key': 'interaction.load_skills',
            })
            self.env.flush_all()

    def test_schema_validation_error_surfaces(self):
        with self.assertRaises(ValidationError):
            self.env['ow.ai.tool'].create({
                'name': "Tool", 'tool_name': 'bad_schema', 'description': "desc",
                'builtin_key': 'interaction.load_skills',
                'schema': '{"type": "string"}',
            })

    def test_builtin_key_unknown_raises(self):
        with self.assertRaises(ValidationError):
            self.env['ow.ai.tool'].create({
                'name': "Tool", 'tool_name': 'unknown_builtin', 'description': "desc",
                'kind': 'builtin', 'builtin_key': 'does.not.exist',
            })

    def test_server_action_kind_is_refused(self):
        # Server-action tools are deferred to a later version: the kind stays
        # selectable in the schema, but no such tool can be created.
        server_action = self.env['ir.actions.server'].create({
            'name': "Do nothing",
            'model_id': self.env['ir.model']._get_id('res.partner'),
            'state': 'code',
            'code': 'pass',
        })
        with self.assertRaisesRegex(ValidationError, "Server-action tools are not available in this version"):
            self.env['ow.ai.tool'].create({
                'name': "Tool", 'tool_name': 'server_tool', 'description': "desc",
                'kind': 'server_action', 'server_action_id': server_action.id,
            })

    def test_builtin_tool_cannot_be_switched_to_server_action(self):
        tool = self.env['ow.ai.tool'].create({
            'name': "Tool", 'tool_name': 'switch_tool', 'description': "desc",
            'builtin_key': 'interaction.load_skills',
        })
        with self.assertRaises(ValidationError):
            tool.write({'kind': 'server_action'})

    def test_native_lock_write_is_write_raises(self):
        tool = self.env['ow.ai.tool'].create({
            'name': "Native Tool", 'tool_name': 'native_tool', 'description': "desc",
            'builtin_key': 'interaction.load_skills', 'is_native': True,
        })
        with self.assertRaises(UserError):
            tool.write({'is_write': True})

    def test_native_lock_write_model_name_raises(self):
        tool = self.env['ow.ai.tool'].create({
            'name': "Native Tool", 'tool_name': 'native_tool_2', 'description': "desc",
            'builtin_key': 'interaction.load_skills', 'is_native': True,
        })
        with self.assertRaises(UserError):
            tool.write({'model_name': 'res.partner'})

    def test_to_neutral_tool_shape(self):
        tool = self.env['ow.ai.tool'].create({
            'name': "Tool", 'tool_name': 'neutral_tool', 'description': "Does things",
            'builtin_key': 'interaction.load_skills',
            'schema': '{"type": "object", "properties": {"x": {"type": "integer"}}}',
        })
        self.assertEqual(tool._to_neutral_tool(), {
            'name': 'neutral_tool',
            'instructions': "Does things",
            'schema': {"type": "object", "properties": {"x": {"type": "integer"}}},
        })

    def test_requires_confirmation_defaults_to_is_write(self):
        tool = self.env['ow.ai.tool'].create({
            'name': "Tool", 'tool_name': 'write_tool', 'description': "desc", 'is_write': True,
            'builtin_key': 'interaction.load_skills',
        })
        self.assertTrue(tool.requires_confirmation)
        tool2 = self.env['ow.ai.tool'].create({
            'name': "Tool2", 'tool_name': 'read_tool', 'description': "desc", 'is_write': False,
            'builtin_key': 'interaction.load_skills',
        })
        self.assertFalse(tool2.requires_confirmation)


class TestComposer(TransactionCase):

    def _agent(self):
        return self.env['ow.ai.agent'].create({'name': "Composer Agent"})

    def test_get_for_specificity(self):
        # The module data already provides a generic (no model) 'record_chat'
        # composer (ow_ai.composer_record_chat); a model-specific one must win.
        agent = self._agent()
        generic = self.env.ref('ow_ai.composer_record_chat')
        model_id = self.env['ir.model']._get_id('res.partner')
        specific = self.env['ow.ai.composer'].create({
            'name': "Specific", 'interface_key': 'record_chat', 'agent_id': agent.id,
            'model_id': model_id,
        })
        found = self.env['ow.ai.composer']._get_for('record_chat', res_model='res.partner')
        self.assertEqual(found, specific)
        found_generic = self.env['ow.ai.composer']._get_for('record_chat', res_model='res.users')
        self.assertEqual(found_generic, generic)

    def test_get_for_missing_returns_empty(self):
        found = self.env['ow.ai.composer']._get_for('systray', res_model='nonexistent.model')
        # falls back to generic systray composer if one exists via data, else empty
        self.assertEqual(found._name, 'ow.ai.composer')

    def test_unique_index_duplicate_active_composer_raises(self):
        agent = self._agent()
        model_id = self.env['ir.model']._get_id('res.users')
        self.env['ow.ai.composer'].create({
            'name': "First", 'interface_key': 'record_chat', 'agent_id': agent.id,
            'model_id': model_id,
        })
        with mute_logger('odoo.sql_db'), self.assertRaises(IntegrityError):
            self.env['ow.ai.composer'].create({
                'name': "Second", 'interface_key': 'record_chat', 'agent_id': agent.id,
                'model_id': model_id,
            })
            self.env.flush_all()


class TestUsage(TransactionCase):

    def test_log_creates_row(self):
        record = self.env['ow.ai.usage']._log(
            self.env, kind='chat', model='openai/gpt-4.1-mini',
            usage={'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15, 'cost': 0.01},
        )
        self.assertEqual(record.kind, 'chat')
        self.assertEqual(record.total_tokens, 15)
        self.assertEqual(record.status, 'ok')


class TestData(TransactionCase):

    def test_default_agent_exists_with_inactive_partner(self):
        agent = self.env.ref('ow_ai.agent_default')
        self.assertTrue(agent.is_system_agent)
        self.assertFalse(agent.partner_id.active)

    def test_composers_resolve_via_get_for(self):
        composer = self.env['ow.ai.composer']._get_for('systray')
        self.assertEqual(composer, self.env.ref('ow_ai.composer_systray'))

    def test_interaction_tools_exist_and_valid(self):
        load_skills = self.env.ref('ow_ai.tool_load_skills')
        ask_question = self.env.ref('ow_ai.tool_ask_user_question')
        self.assertEqual(load_skills.tool_name, 'load_skills')
        self.assertEqual(ask_question.tool_name, 'ask_user_question')
        self.assertTrue(load_skills.is_native)
        self.assertTrue(ask_question.is_native)

    def test_native_tools_and_skills_are_updatable_data(self):
        # A module update must be able to refresh native tools/skills; the
        # default agent and composers stay `noupdate` (user-tunable).
        imd = self.env['ir.model.data']
        for name, noupdate in (('tool_search', False), ('skill_search_database', False),
                               ('agent_default', True), ('composer_systray', True)):
            row = imd.search([('module', '=', 'ow_ai'), ('name', '=', name)])
            self.assertEqual(row.noupdate, noupdate, name)

    def test_migration_makes_existing_native_data_updatable(self):
        # Databases installed while tools/skills were `noupdate` data keep
        # that flag (Odoo never clears it): the 20.0.1.0.1 pre-migration does.
        path = pathlib.Path(__file__).parent.parent / 'migrations' / '20.0.1.0.1' / 'pre-migrate.py'
        spec = importlib.util.spec_from_file_location('ow_ai_pre_migrate_20_0_1_0_1', path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        imd = self.env['ir.model.data']
        names = ('tool_search', 'skill_search_database', 'agent_default')
        rows = imd.search([('module', '=', 'ow_ai'), ('name', 'in', names)])
        rows.write({'noupdate': True})
        self.env.flush_all()

        migration.migrate(self.env.cr, '20.0.1.0.0')

        rows.invalidate_recordset(['noupdate'])
        self.assertEqual({row.name: row.noupdate for row in rows},
                         {'tool_search': False, 'skill_search_database': False, 'agent_default': True})

    def test_sync_relinks_native_skills_to_the_default_agent(self):
        agent = self.env.ref('ow_ai.agent_default')
        skill = self.env.ref('ow_ai.skill_update_records')
        custom = self.env['ow.ai.skill'].create({'name': "Custom", 'description': "Not native"})
        agent.write({'skill_ids': [Command.unlink(skill.id)]})
        self.assertNotIn(skill, agent.skill_ids)

        self.env['ow.ai.skill']._sync_default_agent_skills()

        self.assertIn(skill, agent.skill_ids)
        self.assertNotIn(custom, agent.skill_ids)
        native = self.env['ow.ai.skill'].search([('is_native', '=', True)])
        self.assertEqual(agent.skill_ids & native, native)


class TestAcl(TransactionCase):

    def test_ai_user_can_read_but_not_create(self):
        user = new_test_user(self.env, login='ai_user_test', groups='ow_ai.group_ai_user')
        agent = self.env['ow.ai.agent'].create({'name': "ACL Agent"})
        agent.with_user(user).read(['name'])
        with self.assertRaises(AccessError):
            self.env['ow.ai.agent'].with_user(user).create({'name': "Should Fail"})

    def test_ai_manager_can_create(self):
        user = new_test_user(self.env, login='ai_manager_test', groups='ow_ai.group_ai_manager')
        agent = self.env['ow.ai.agent'].with_user(user).create({'name': "Manager Agent"})
        self.assertTrue(agent)
