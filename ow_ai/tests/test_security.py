# -*- coding: utf-8 -*-
"""Security-focused tests spanning several Task 1.13 surfaces:
``ow.ai.agent.action_launch_chat``'s access checks, the AI-manager-only ACL
on ``ow.ai.agent``, the API key's server-side-only visibility,
and a job whose user is the superuser failing closed (in addition to the
dedicated coverage in ``test_jobs.py``, since the brief calls this out
explicitly as a security property).
"""
from lxml import etree
from odoo import SUPERUSER_ID
from odoo.addons.mail.tools.discuss import Store
from odoo.exceptions import AccessError, UserError
from odoo.modules.module import load_script
from odoo.tests import TransactionCase, new_test_user

from ..utils.access import ModelAccessError
from .common import OwAiCase
from .test_discuss_channel import _store_entry


class TestActionLaunchChatSecurity(OwAiCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent = cls.env.ref('ow_ai.agent_default')
        cls.user = new_test_user(cls.env, login='ow_ai_sec_launch_user', groups='base.group_user')
        cls.other_user = new_test_user(cls.env, login='ow_ai_sec_launch_other', groups='base.group_user')

    def test_record_the_user_cannot_read_raises_access_error(self):
        other_channel = self.agent._create_chat_channel(self.other_user, title="Other user's chat")
        with self.assertRaises(AccessError):
            self.agent.with_user(self.user).action_launch_chat(
                interface_key='record_chat', res_model='discuss.channel', res_id=other_channel.id)

    def test_blocklisted_model_raises_model_access_error(self):
        with self.assertRaises(ModelAccessError):
            self.agent.with_user(self.user).action_launch_chat(
                interface_key='record_chat', res_model='res.groups', res_id=1)

    def test_launch_chat_without_ai_group_raises_access_error(self):
        portal = new_test_user(self.env, login='ow_ai_sec_launch_portal', groups='base.group_portal')
        with self.assertRaises(AccessError):
            self.agent.with_user(portal).action_launch_chat()


class TestAgentAcl(TransactionCase):

    def test_ai_user_without_manager_cannot_create_agents(self):
        user = new_test_user(self.env, login='ow_ai_sec_agent_user', groups='base.group_user')
        with self.assertRaises(AccessError):
            self.env['ow.ai.agent'].with_user(user).create({'name': "Rogue Agent"})

    def test_ai_manager_can_create_agents(self):
        manager = new_test_user(
            self.env, login='ow_ai_sec_agent_manager', groups='ow_ai.group_ai_manager,base.group_user')
        agent = self.env['ow.ai.agent'].with_user(manager).create({'name': "Legit Agent"})
        self.assertTrue(agent)


class TestAgentContactOwnership(TransactionCase):
    """An AI manager manages agents, not contacts: an agent is bound to the
    contact the assistant created for it (``res.partner.ow_ai_agent_partner``),
    and deleting the agent deletes that contact only."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(
            cls.env, login='ow_ai_sec_contact_manager', groups='base.group_user,ow_ai.group_ai_manager')
        cls.contact = cls.env['res.partner'].create({'name': "Existing customer"})

    def test_manager_cannot_bind_an_agent_to_an_existing_contact(self):
        self.assertFalse(self.contact.with_user(self.manager).has_access('unlink'))
        with self.assertRaisesRegex(UserError, "An AI agent's contact is created by the assistant itself."):
            self.env['ow.ai.agent'].with_user(self.manager).create({'partner_id': self.contact.id})
        self.assertFalse(self.env['ow.ai.agent'].with_context(active_test=False).search(
            [('partner_id', '=', self.contact.id)]))
        self.assertTrue(self.contact.exists())

    def test_superuser_binding_an_existing_contact_cannot_rename_or_reimage_it(self):
        """`name` is `ow.ai.agent.name = related='partner_id.name'`: an
        explicit `name` (or `image_128`) alongside a superuser-bound
        `partner_id` would otherwise rename/re-image the bound contact
        itself, exactly the harm finding 1 already refuses for a manager."""
        original_name = self.contact.name
        with self.assertRaisesRegex(
                UserError, "its name and image belong to the contact"):
            self.env['ow.ai.agent'].sudo().create({'partner_id': self.contact.id, 'name': "Renamed"})
        self.assertEqual(self.contact.name, original_name)
        self.assertFalse(self.env['ow.ai.agent'].sudo().with_context(active_test=False).search(
            [('partner_id', '=', self.contact.id)]))

        # binding alone (no name/image) still works, unchanged
        agent = self.env['ow.ai.agent'].sudo().create({'partner_id': self.contact.id})
        self.assertEqual(agent.partner_id, self.contact)
        self.assertEqual(self.contact.name, original_name)

    def test_manager_deletes_an_agent_with_its_own_contact(self):
        agent = self.env['ow.ai.agent'].with_user(self.manager).create({'name': "Own contact"})
        partner = agent.sudo().partner_id
        self.assertTrue(partner.ow_ai_agent_partner)
        agent.unlink()
        self.assertFalse(partner.exists())

    def test_upgrade_leaves_a_bound_business_contact_to_its_owner(self):
        """Before 20.0.1.2.0 an existing contact could be bound to an agent:
        the upgrade must not flag it (so deleting the agent never deletes it)."""
        customer = self.env['res.partner'].create({'name': "Bound customer", 'email': 'customer@example.com'})
        agent = self.env['ow.ai.agent'].sudo().create({'partner_id': customer.id})
        self.assertFalse(customer.with_user(self.manager).has_access('unlink'))

        script = load_script('ow_ai/migrations/20.0.1.2.0/post-flag_agent_partners.py',
                             'odoo.upgrade.ow_ai.test_flag_bound_customer')
        script.migrate(self.env.cr, '20.0.1.1.0')

        self.assertFalse(customer.ow_ai_agent_partner)
        agent.with_user(self.manager).unlink()
        self.assertFalse(agent.exists())
        self.assertTrue(customer.exists())

    def test_upgrade_repair_unflags_a_bound_business_contact(self):
        """20.0.1.2.1 repairs a database 20.0.1.2.0 already ran on: a business
        contact it flagged loses the flag, the assistant's own contact keeps it."""
        customer = self.env['res.partner'].create({'name': "Bound customer", 'email': 'customer@example.com'})
        bound = self.env['ow.ai.agent'].sudo().create({'partner_id': customer.id})
        customer.ow_ai_agent_partner = True   # what 20.0.1.2.0 did
        own = self.env['ow.ai.agent'].create({'name': "Own contact"})

        script = load_script('ow_ai/migrations/20.0.1.2.1/post-repair_agent_partner_flags.py',
                             'odoo.upgrade.ow_ai.test_repair_bound_customer')
        script.migrate(self.env.cr, '20.0.1.2.0')

        self.assertFalse(customer.ow_ai_agent_partner)
        self.assertTrue(own.partner_id.ow_ai_agent_partner)
        bound.with_user(self.manager).unlink()
        self.assertTrue(customer.exists())
        own_partner = own.partner_id
        own.with_user(self.manager).unlink()
        self.assertFalse(own_partner.exists())

    def test_only_administrators_see_or_set_the_flag(self):
        # even a manager allowed to edit contacts
        editor = new_test_user(
            self.env, login='ow_ai_sec_contact_editor',
            groups='base.group_user,base.group_partner_manager,ow_ai.group_ai_manager')
        contact = self.contact.with_user(editor)
        self.assertTrue(contact.has_access('write'))
        with self.assertRaises(AccessError):
            contact.write({'ow_ai_agent_partner': True})
        with self.assertRaises(AccessError):
            contact.read(['ow_ai_agent_partner'])
        self.assertFalse(self.contact.ow_ai_agent_partner)


class TestApiKeyVisibility(TransactionCase):

    def test_api_key_param_unreadable_by_non_system_user(self):
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', 'sk-or-secret')
        user = new_test_user(self.env, login='ow_ai_sec_apikey_user', groups='base.group_user')
        with self.assertRaises(AccessError):
            self.env['ir.config_parameter'].with_user(user).search([('key', '=', 'ow_ai.api_key')])

    def test_api_key_absent_from_settings_get_values(self):
        self.env['ir.config_parameter'].sudo().set_str('ow_ai.api_key', 'sk-or-secret')
        settings = self.env['res.config.settings'].create({})
        values = settings.get_values()
        self.assertTrue('ow_ai_api_key' not in values or not values['ow_ai_api_key'])


class TestJobRootUserFailsClosed(OwAiCase):

    def test_job_with_root_user_fails_closed(self):
        channel = self.open_chat(self.env.ref('base.user_admin'))
        session = self.chat_session(channel)
        job = self.env['ow.ai.job'].sudo().create({
            'session_id': session.id,
            'kind': 'agent_round',
            'user_id': SUPERUSER_ID,
            'context': {},
        })
        session.write({
            'loop_state': 'waiting_model', 'request_round': 1, 'request_round_limit': 5,
            'request_user_id': SUPERUSER_ID,
        })

        job._run_one()

        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.transport.requests, [])


class TestSettingsButtonsAdminOnly(OwAiCase):
    """The settings buttons are public methods of ``res.config.settings``."""

    def setUp(self):
        super().setUp()
        self.settings = self.env['res.config.settings'].create({})

    def test_non_admins_cannot_clear_the_key(self):
        for login, groups in (('ow_ai_sec_cfg_user', 'base.group_user'), ('ow_ai_sec_cfg_portal', 'base.group_portal')):
            user = new_test_user(self.env, login=login, groups=groups)
            with self.subTest(groups=groups), self.assertRaises(AccessError):
                self.settings.with_user(user).action_ow_ai_clear_api_key()
            self.assertEqual(self.env['ir.config_parameter'].sudo().get_str('ow_ai.api_key'), 'sk-or-test')

    def test_non_admins_cannot_test_the_connection(self):
        user = new_test_user(self.env, login='ow_ai_sec_cfg_tester', groups='base.group_user')
        with self.assertRaises(AccessError):
            self.settings.with_user(user).action_ow_ai_test_connection()
        self.assertEqual(self.transport.requests, [])

    def test_admin_can_clear_the_key(self):
        self.settings.with_user(self.env.ref('base.user_admin')).action_ow_ai_clear_api_key()
        self.assertEqual(self.env['ir.config_parameter'].sudo().get_str('ow_ai.api_key'), '')


class TestTranscriptFieldsSystemOnly(OwAiCase):
    """Ruling 13: AI managers get the session/job lists, not the transcript
    (``ow.ai.session.event.metadata``) or the job data (``context``,
    ``payload``, ``error``)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(cls.env, login='ow_ai_sec_transcript_user', groups='base.group_user')
        cls.manager = new_test_user(
            cls.env, login='ow_ai_sec_transcript_manager', groups='base.group_user,ow_ai.group_ai_manager')

    def setUp(self):
        super().setUp()
        self.channel = self.open_chat(self.user)
        self.session = self.chat_session(self.channel)
        self.queue_tool_calls([('search', {'model_name': 'res.partner', 'tool_status': 'Looking'}, 'call_1')])
        self.queue_text('Done.')
        state = dict(self.session.state or {})
        state['available_tools'] = [self.env.ref('ow_ai.tool_search').id]
        self.session.state = state
        self.job = self.say(self.channel, 'Find partners', self.user)
        self.events = self.session.event_ids

    def test_manager_lists_sessions_and_jobs(self):
        sessions = self.env['ow.ai.session'].with_user(self.manager).search_read(
            [('id', '=', self.session.id)], ['loop_state', 'agent_id', 'request_round', 'request_user_id'])
        self.assertEqual(len(sessions), 1)
        events = self.env['ow.ai.session.event'].with_user(self.manager).search_read(
            [('session_id', '=', self.session.id)], ['sequence', 'role', 'usage_id'])
        self.assertEqual(len(events), len(self.events))
        jobs = self.env['ow.ai.job'].with_user(self.manager).search_read(
            [('session_id', '=', self.session.id)], ['kind', 'state', 'attempt', 'can_retry'])
        self.assertTrue(jobs)

    def test_manager_cannot_read_transcript_or_job_data(self):
        for fname in ('metadata', 'summary'):
            with self.subTest(field=fname), self.assertRaises(AccessError):
                self.events.with_user(self.manager).read([fname])
        for fname in ('context', 'payload', 'error'):
            with self.subTest(field=fname), self.assertRaises(AccessError):
                self.job.with_user(self.manager).read([fname])

    def test_manager_views_hide_transcript_and_job_data(self):
        session_views = self.env['ow.ai.session'].with_user(self.manager).get_views([(False, 'form')])
        self.assertNotIn('metadata', session_views['models']['ow.ai.session.event']['fields'])
        self.assertNotIn('summary', session_views['models']['ow.ai.session.event']['fields'])
        job_views = self.env['ow.ai.job'].with_user(self.manager).get_views([(False, 'list'), (False, 'form')])
        for fname in ('context', 'payload', 'error'):
            self.assertNotIn(fname, job_views['models']['ow.ai.job']['fields'])

    def test_system_user_reads_transcript(self):
        admin = self.env.ref('base.user_admin')
        self.assertTrue(self.events.with_user(admin).read(['metadata'])[0]['metadata'])
        self.assertIn('payload', self.job.with_user(admin).read(['payload'])[0])

    def test_chat_owner_still_gets_tool_params(self):
        event = self.events.filtered(lambda event: event.role == 'assistant')[:1]
        params = event.with_user(self.user).get_tool_params('call_1')
        self.assertEqual(params, {'model_name': 'res.partner'})

    def test_manager_without_system_cannot_get_tool_params_of_a_channelless_session(self):
        session = self.env['ow.ai.session'].sudo().create({'agent_id': self.env.ref('ow_ai.agent_default').id})
        event = session._append_event('assistant', {
            'role': 'assistant', 'content': [{'type': 'tool_call', 'name': 'search', 'call_id': 'c1', 'args': {}}],
        })
        with self.assertRaises(AccessError):
            event.with_user(self.manager).get_tool_params('c1')


# The session fields holding conversation content or what resumes it: tool
# arguments and results (`pending_tool_call`), the request context, the
# resume token, the notification buffers, the tool state and the last error.
_SESSION_CONTENT_FIELDS = (
    'state', 'request_context', 'pending_tool_call', 'resume_token',
    'turn_notifications', 'turn_client_notifications', 'last_error',
)


class TestSessionContentFieldsSystemOnly(OwAiCase):
    """AI managers see a session's operational fields (Reports > Sessions),
    not its content: that is for administrators, and for the chat's own
    member through the Store (sudo), never through ``read()``."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(cls.env, login='ow_ai_sec_content_user', groups='base.group_user')
        cls.manager = new_test_user(
            cls.env, login='ow_ai_sec_content_manager', groups='base.group_user,ow_ai.group_ai_manager')

    def setUp(self):
        super().setUp()
        self.channel = self.open_chat(self.user)
        self.session = self.chat_session(self.channel)
        # a batch paused on a confirmation card, with a result already fetched
        self.session.write({
            'loop_state': 'waiting_confirmation',
            'request_round': 1,
            'request_round_limit': 5,
            'request_user_id': self.user.id,
            'request_context': {'uid': self.user.id, 'secret': 'SECRET_CONTEXT'},
            'pending_tool_call': {
                'calls': [{'args': {'secret': 'SECRET_ARGS'}}],
                'results': [{'response': 'SECRET_RESULT'}],
                'user_input_request': {'type': 'confirmation', 'body': '<p>Create SECRET?</p>'},
            },
            'resume_token': 'live-token',
            'state': {'secret': 'SECRET_STATE'},
            'turn_notifications': [{'html': 'SECRET_NOTE'}],
            'turn_client_notifications': [{'name': 'SECRET_CLIENT'}],
            'last_error': 'SECRET_ERROR',
        })

    def test_non_participant_manager_cannot_read_the_content(self):
        session = self.session.with_user(self.manager)
        with self.assertRaises(AccessError):
            self.channel.with_user(self.manager).check_access('read')
        for fname in _SESSION_CONTENT_FIELDS:
            with self.subTest(field=fname), self.assertRaises(AccessError):
                session.read([fname])
        with self.assertRaises(AccessError):
            session.search_read([('id', '=', self.session.id)], ['pending_tool_call', 'request_context'])
        with self.assertRaises(AccessError):
            session.search([('resume_token', '=', 'live-token')])

    def test_manager_still_reads_the_operational_fields(self):
        data = self.session.with_user(self.manager).read(
            ['loop_state', 'request_round', 'request_user_id', 'agent_id', 'channel_id', 'auto_confirm'])[0]
        self.assertEqual(data['loop_state'], 'waiting_confirmation')
        self.assertEqual(data['request_user_id'][0], self.user.id)

    def test_manager_views_hide_the_content(self):
        views = self.env['ow.ai.session'].with_user(self.manager).get_views(
            [(False, 'list'), (False, 'form'), (False, 'search')])
        fields = views['models']['ow.ai.session']['fields']
        self.assertIn('loop_state', fields)
        for fname in _SESSION_CONTENT_FIELDS:
            self.assertNotIn(fname, fields)
        for view in views['views'].values():
            for node in etree.fromstring(view['arch']).iter(etree.Element):
                for value in node.attrib.values():
                    self.assertNotIn('last_error', value)

    def test_administrator_reads_the_content(self):
        admin = self.env.ref('base.user_admin')
        data = self.session.with_user(admin).read(list(_SESSION_CONTENT_FIELDS))[0]
        self.assertEqual(data['resume_token'], 'live-token')
        self.assertEqual(data['last_error'], 'SECRET_ERROR')
        fields = self.env['ow.ai.session'].with_user(admin).get_views(
            [(False, 'list'), (False, 'form')])['models']['ow.ai.session']['fields']
        self.assertIn('last_error', fields)

    def test_participant_gets_the_resume_token_through_the_store(self):
        data = Store().add(self.channel.with_user(self.user), '_store_channel_fields').as_dict()
        session_data = _store_entry(data, 'ow.ai.session', self.session.id)
        self.assertEqual(session_data['resume_token'], 'live-token')
        self.assertEqual(session_data['userInputRequest']['resumeToken'], 'live-token')
        self.assertNotIn('pending_tool_call', session_data)
        self.assertNotIn('request_context', session_data)


class TestAiChatSetupContextIsServerSide(OwAiCase):
    """An AI chat has exactly two members. The ``ow_ai_channel_setup``
    context key lets members be created only during the assistant's own
    (sudo) setup; a member who puts it in their RPC context is still refused."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent = cls.env.ref('ow_ai.agent_default')
        cls.user = new_test_user(cls.env, login='ow_ai_sec_setup_user', groups='base.group_user')
        cls.other_user = new_test_user(cls.env, login='ow_ai_sec_setup_other', groups='base.group_user')

    def setUp(self):
        super().setUp()
        self.channel = self.open_chat(self.user)

    def _member_count(self):
        return self.env['discuss.channel.member'].search_count([('channel_id', '=', self.channel.id)])

    def test_forged_context_cannot_create_a_third_member(self):
        members = self.env['discuss.channel.member'].with_user(self.user).with_context(ow_ai_channel_setup=True)
        with self.assertRaisesRegex(UserError, "AI chats cannot have additional members."):
            members.create({'channel_id': self.channel.id, 'partner_id': self.other_user.partner_id.id})
        self.assertEqual(self._member_count(), 2)
        self.assertFalse(self.channel.with_user(self.other_user).has_access('read'))

    def test_forged_context_cannot_add_members(self):
        channel = self.channel.with_user(self.user).with_context(ow_ai_channel_setup=True)
        with self.assertRaisesRegex(UserError, "AI chats cannot have additional members."):
            channel._add_members(users=self.other_user)
        # Odoo 20 has no public `add_members` method: the
        # `/discuss/channel/add_members` route calls `_add_members(partners=...)`
        # in the caller's environment, context included.
        with self.assertRaisesRegex(UserError, "AI chats cannot have additional members."):
            channel._add_members(partners=self.other_user.partner_id)
        self.assertEqual(self._member_count(), 2)

    def test_chat_start_still_sets_up_both_members(self):
        result = self.agent.with_user(self.user).action_launch_chat()
        channel = self.env['discuss.channel'].browse(result['channel_id'])
        self.assertEqual(channel.channel_member_ids.partner_id, self.user.partner_id | self.agent.partner_id)

    def test_forged_default_context_does_not_add_a_member(self):
        """A forged ``default_channel_partner_ids`` in the caller's context
        must never reach the sudo channel-member setup: ``_create_chat_channel``
        now wraps the context in ``clean_context`` like ``_create_session``
        already did."""
        result = self.agent.with_user(self.user).with_context(
            default_channel_partner_ids=[self.other_user.partner_id.id]).action_launch_chat()
        channel = self.env['discuss.channel'].browse(result['channel_id'])
        self.assertEqual(channel.channel_member_ids.partner_id, self.user.partner_id | self.agent.partner_id)
