# -*- coding: utf-8 -*-
import io

from markupsafe import Markup
from odoo.addons.mail.tools.discuss import Store
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user


def _png_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (4, 4), color=(10, 20, 30)).save(buf, format='PNG')
    return buf.getvalue()


class TestDiscussChannelCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(cls.env, login='ow_ai_chat_user', groups='base.group_user')
        cls.other_user = new_test_user(cls.env, login='ow_ai_chat_other', groups='base.group_user')
        cls.agent = cls.env['ow.ai.agent'].create({'name': "Chatty"})

    def _create_channel(self, **kw):
        return self.agent._create_chat_channel(self.user, **kw)


class TestCreateChatChannel(TestDiscussChannelCase):

    def test_exactly_two_members(self):
        channel = self._create_channel()
        self.assertEqual(len(channel.channel_member_ids), 2)
        self.assertEqual(
            channel.channel_member_ids.partner_id, self.user.partner_id | self.agent.partner_id)

    def test_channel_type_and_agent(self):
        channel = self._create_channel()
        self.assertEqual(channel.channel_type, 'ow_ai_chat')
        self.assertEqual(channel.sudo().ow_ai_agent_id, self.agent)

    def test_display_name_defaults_to_agent_name(self):
        # `name` is a required Char: the "no name" branch is an empty
        # string, not a missing/NULL value.
        channel = self.env['discuss.channel'].sudo().with_context(
            ow_ai_channel_setup=True).create({
                'channel_type': 'ow_ai_chat',
                'name': '',
                'ow_ai_agent_id': self.agent.id,
                'channel_member_ids': [(0, 0, {'partner_id': self.user.partner_id.id})],
            })
        self.assertEqual(channel.with_user(self.user).display_name, self.agent.name)

    def test_avatar_is_agent_avatar(self):
        image_b64 = (
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
            '+A8AAQUBAScY42YAAAAASUVORK5CYII='
        )
        self.agent.write({'image_128': image_b64})
        channel = self._create_channel()
        channel_avatar = channel.with_user(self.user).read(['avatar_128'])[0]['avatar_128']
        agent_avatar = self.agent.read(['avatar_128'])[0]['avatar_128']
        self.assertEqual(channel_avatar, agent_avatar)

    def test_root_session_created(self):
        channel = self._create_channel(res_model='res.partner', res_id=1)
        self.assertEqual(len(channel.sudo().ow_ai_session_ids), 1)
        session = channel.sudo().ow_ai_session_ids
        self.assertEqual(session.agent_id, self.agent)
        self.assertEqual(session.res_model, 'res.partner')
        self.assertEqual(session.res_id, 1)


class TestMemberRestriction(TestDiscussChannelCase):

    def test_add_members_raises(self):
        channel = self._create_channel()
        with self.assertRaises(UserError):
            channel.sudo()._add_members(users=self.other_user)

    def test_direct_member_create_raises(self):
        channel = self._create_channel()
        with self.assertRaises(UserError):
            self.env['discuss.channel.member'].sudo().create({
                'channel_id': channel.id,
                'partner_id': self.other_user.partner_id.id,
            })

    def test_setup_context_allows_create(self):
        channel = self.env['discuss.channel'].sudo().with_context(ow_ai_channel_setup=True).create({
            'channel_type': 'ow_ai_chat',
            'name': "x",
            'ow_ai_agent_id': self.agent.id,
        })
        member = self.env['discuss.channel.member'].sudo().with_context(ow_ai_channel_setup=True).create({
            'channel_id': channel.id,
            'partner_id': self.other_user.partner_id.id,
        })
        self.assertTrue(member)


class TestDeleteChat(TestDiscussChannelCase):

    def test_member_can_delete(self):
        channel = self._create_channel()
        channel.with_user(self.user).ow_ai_delete_chat()
        self.assertFalse(channel.exists())

    def test_non_member_cannot_delete(self):
        channel = self._create_channel()
        with self.assertRaises(AccessError):
            channel.with_user(self.other_user).ow_ai_delete_chat()
        self.assertTrue(channel.exists())

    def test_member_of_a_non_ai_channel_cannot_delete_it(self):
        # `ow_ai_delete_chat` is a public RPC method ending in a sudo()
        # unlink: it must refuse any channel that is not an AI chat, even
        # one the caller is a member of.
        for channel_type in ('group', 'channel'):
            with self.subTest(channel_type=channel_type):
                channel = self.env['discuss.channel'].create({
                    'name': f"Team {channel_type}",
                    'channel_type': channel_type,
                })
                channel._add_members(users=self.user | self.other_user)
                self.assertIn(self.user.partner_id, channel.channel_member_ids.partner_id)
                with self.assertRaises(AccessError):
                    channel.with_user(self.user).ow_ai_delete_chat()
                self.assertTrue(channel.exists())

    def test_member_can_unlink_via_orm(self):
        # Exercises the ir.access record-rule row directly (not
        # ow_ai_delete_chat()'s own membership check): a member who is an
        # ordinary AI user (not admin) must be able to unlink their own
        # ow_ai_chat channel through plain ORM access control.
        channel = self._create_channel()
        channel.with_user(self.user).unlink()
        self.assertFalse(channel.exists())

    def test_non_member_cannot_unlink_via_orm(self):
        channel = self._create_channel()
        with self.assertRaises(AccessError):
            channel.with_user(self.other_user).unlink()
        self.assertTrue(channel.exists())

    def test_non_member_cannot_read_messages(self):
        channel = self._create_channel()
        channel.sudo().message_post(body="hello", message_type='comment', subtype_xmlid='mail.mt_comment')
        messages = self.env['mail.message'].with_user(self.other_user).search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', channel.id)])
        self.assertFalse(messages)


class TestGarbageCollection(TestDiscussChannelCase):

    def _age_channel(self, channel, days):
        self.env.cr.execute(
            "UPDATE discuss_channel SET last_interest_dt = now() - interval %s WHERE id = %s",
            (f'{days} days', channel.id))
        channel.invalidate_recordset(['last_interest_dt'])

    def test_gc_removes_stale_and_empty_channels(self):
        stale = self._create_channel()
        self._age_channel(stale, 31)

        empty = self._create_channel()
        self._age_channel(empty, 2)

        active = self._create_channel()
        active.sudo().message_post(body="hi", message_type='comment', subtype_xmlid='mail.mt_comment')
        self._age_channel(active, 2)

        self.env['discuss.channel']._gc_ow_ai_chats()

        self.assertFalse(stale.exists())
        self.assertFalse(empty.exists())
        self.assertTrue(active.exists())


class TestMailMessageToParts(TestDiscussChannelCase):

    def test_text_and_attachment_parts(self):
        message = self.env['mail.message'].create({
            'model': 'res.partner',
            'res_id': self.agent.partner_id.id,
            'body': '<p>Please check this file</p>',
            'message_type': 'comment',
        })
        attachment = self.env['ir.attachment'].create({
            'name': 'x.png',
            'raw': _png_bytes(),
            'mimetype': 'image/png',
            'res_model': 'mail.message',
            'res_id': message.id,
        })
        message.attachment_ids = [(6, 0, attachment.ids)]

        parts = message._ow_ai_to_parts()
        types = [part['type'] for part in parts]
        self.assertIn('text', types)
        self.assertIn('inline_data', types)
        self.assertTrue(any('Please check this file' in part['text'] for part in parts if part['type'] == 'text'))
        self.assertTrue(any('x.png' in part['text'] for part in parts if part['type'] == 'text'))

    def test_no_text_when_body_empty(self):
        message = self.env['mail.message'].create({
            'model': 'res.partner',
            'res_id': self.agent.partner_id.id,
            'body': '',
            'message_type': 'comment',
        })
        self.assertEqual(message._ow_ai_to_parts(), [])


def _store_entry(data, model, record_id):
    """Look up `record_id` in a Store.as_dict() result for `model` (list or singleton dict form)."""
    entries = data.get(model) or []
    if isinstance(entries, dict):
        return entries
    return next(item for item in entries if item.get('id') == record_id)


class TestStoreAndPosting(TestDiscussChannelCase):

    def test_store_init_publishes_ai_access(self):
        """`has_access_ow_ai` drives the AI tab of the messaging menu."""
        portal = new_test_user(self.env, login='ow_ai_chat_portal', groups='base.group_portal')
        for user, expected in ((self.user, True), (portal, False)):
            store = Store()
            store.add_global_values(user.with_user(user)._store_init_global_fields)
            self.assertIs(store._build_result()['Store']['has_access_ow_ai'], expected)

    def test_store_channel_contains_agent_and_sessions(self):
        channel = self._create_channel()
        store = Store().add(channel.with_user(self.user), '_store_channel_fields')
        data = store.as_dict()
        channel_data = _store_entry(data, 'discuss.channel', channel.id)
        self.assertIn('ow_ai_agent_id', channel_data)
        agent_id = channel_data['ow_ai_agent_id']
        self.assertTrue(agent_id)
        agent_data = _store_entry(data, 'ow.ai.agent', agent_id)
        self.assertEqual(agent_data['name'], self.agent.name)
        self.assertIn('ow_ai_session_ids', channel_data)

    def test_resume_token_only_published_during_interaction_states(self):
        channel = self._create_channel()
        session = channel.sudo().ow_ai_session_ids

        # `waiting_model` is not an interaction state: a resume_token value
        # sitting on the record (e.g. stale from a previous interaction)
        # must not be exposed through the Store.
        session.write({
            'loop_state': 'waiting_model',
            'request_round': 1,
            'request_round_limit': 1,
            'request_user_id': self.user.id,
            'resume_token': 'stale-token',
        })
        data = Store().add(session, '_store_session_fields').as_dict()
        session_data = _store_entry(data, 'ow.ai.session', session.id)
        self.assertFalse(session_data.get('resume_token'))

        # `waiting_confirmation` is an interaction state: resume_token must
        # be published.
        session.write({
            'loop_state': 'waiting_confirmation',
            'pending_tool_call': {'user_input_request': {'type': 'confirm'}},
            'resume_token': 'live-token',
        })
        data = Store().add(session, '_store_session_fields').as_dict()
        session_data = _store_entry(data, 'ow.ai.session', session.id)
        self.assertEqual(session_data.get('resume_token'), 'live-token')

    def test_user_input_request_body_is_published_as_markup(self):
        """The card body is nested in the ``userInputRequest`` attr, where the
        Store does not tag ``Markup`` values itself: it must be sent as
        ``['markup', html]`` for the client's ``fields.Html`` to keep it HTML."""
        channel = self._create_channel()
        session = channel.sudo().ow_ai_session_ids
        session.write({
            'loop_state': 'waiting_confirmation',
            'request_round': 1,
            'request_round_limit': 1,
            'request_user_id': self.user.id,
            'resume_token': 'live-token',
            'pending_tool_call': {'user_input_request': {
                'type': 'confirmation', 'body': '<p>Create <b>Acme</b>?</p>'}},
        })
        data = Store().add(session, '_store_session_fields').as_dict()
        request = _store_entry(data, 'ow.ai.session', session.id)['userInputRequest']
        self.assertEqual(request['body'], ['markup', '<p>Create <b>Acme</b>?</p>'])
        self.assertEqual(request['resumeToken'], 'live-token')

    def test_posting_helpers(self):
        channel = self._create_channel()
        session = channel.sudo().ow_ai_session_ids

        answer_msg = session._post_answer(Markup("<p>hi</p>"))
        self.assertEqual(answer_msg.subtype_id, self.env.ref('mail.mt_comment'))
        self.assertEqual(answer_msg.author_id, self.agent.partner_id)

        step_msg = session._post_agent_step(Markup("<p>looking</p>"), 1)
        self.assertIn('o_ow_ai_agent_step', step_msg.body)
        self.assertTrue(step_msg.is_internal)

        tool_msg = session._post_tool_summary('search', "Searching", 'call_1')
        self.assertIn('o_ow_ai_tool_summary', tool_msg.body)
        self.assertEqual(tool_msg.message_type, 'notification')

        notif_msg = session._post_notification('note', Markup("<p>note</p>"))
        self.assertIn('data-oe-type="ow_ai_note"', notif_msg.body)

        session.request_user_id = self.user.id
        choice_msg = session._post_user_choice("My choice")
        self.assertEqual(choice_msg.author_id, self.user.partner_id)

    def test_notify_typing_no_error(self):
        channel = self._create_channel()
        session = channel.sudo().ow_ai_session_ids
        agent_member = channel.sudo().channel_member_ids._ow_ai_agent_member()
        self.assertTrue(agent_member)
        # `is_typing_dt` is a Store-only synthetic attribute (see
        # discuss.channel.member._notify_typing), not a persisted field:
        # this only asserts the call does not raise.
        session._notify_typing(True)

    def test_publish_state_no_error(self):
        channel = self._create_channel()
        session = channel.sudo().ow_ai_session_ids
        session._publish_state()
