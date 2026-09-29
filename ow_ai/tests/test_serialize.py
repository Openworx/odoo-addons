# -*- coding: utf-8 -*-
import base64
import io
import json

from odoo.exceptions import AccessError
from odoo.fields import Domain
from odoo.tests import TransactionCase, new_test_user

from ..utils.access import ModelAccessError, check_model_access, parse_domain
from ..utils.serialize import compact_json, describe_fields, serialize_record, serialize_records, truncate_text


def _png_bytes(width, height):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (width, height), color=(120, 60, 200)).save(buf, format='PNG')
    return buf.getvalue()


class TestCheckModelAccess(TransactionCase):

    def setUp(self):
        super().setUp()
        user = new_test_user(self.env, login='ow_ai_internal', groups='base.group_user')
        self.env = self.env(user=user)

    def test_unknown_model_raises(self):
        with self.assertRaises(ModelAccessError):
            check_model_access(self.env, 'does.not.exist')

    def test_blocklisted_exact_model_raises(self):
        with self.assertRaises(ModelAccessError):
            check_model_access(self.env, 'res.users.apikeys')

    def test_blocklisted_prefix_model_raises(self):
        with self.assertRaises(ModelAccessError):
            check_model_access(self.env, 'ir.cron')

    def test_ir_attachment_read_allowed_write_blocked(self):
        model = check_model_access(self.env, 'ir.attachment', 'read')
        self.assertEqual(model._name, 'ir.attachment')
        with self.assertRaises(ModelAccessError):
            check_model_access(self.env, 'ir.attachment', 'write')

    def test_transient_write_blocked(self):
        with self.assertRaises(ModelAccessError):
            check_model_access(self.env, 'res.config.settings', 'write')

    def test_transient_read_allowed(self):
        # su: `self.env` is the plain user's, and `new_test_user` does not sudo on 19.
        manager = new_test_user(self.env(su=True), login='ow_ai_settings_mgr', groups='base.group_system')
        model = check_model_access(self.env(user=manager), 'res.config.settings', 'read')
        self.assertEqual(model._name, 'res.config.settings')

    def test_access_error_surfaces_as_model_access_error(self):
        # res.partner.category is not blocklisted (exact or prefix) or in
        # WRITE_BLOCKLIST, so this exercises the ORM AccessError -> ModelAccessError
        # branch specifically. Per addons/base/security/ir.model.access.csv,
        # base.group_user only has read on res.partner.category (full access
        # requires base.group_partner_manager), so a plain internal user genuinely lacks
        # write access at the ACL level.
        user = new_test_user(self.env(su=True), login='ow_ai_category_user', groups='base.group_user')
        with self.assertRaises(ModelAccessError) as cm:
            check_model_access(self.env(user=user), 'res.partner.category', 'write')
        self.assertIn('access', str(cm.exception))

    def test_su_raises_runtime_error(self):
        # An explicit check, not an `assert`: it must also hold under `python -O`.
        with self.assertRaises(RuntimeError):
            check_model_access(self.env(su=True), 'res.partner')


class TestParseDomain(TransactionCase):

    def test_valid_list(self):
        domain = parse_domain(self.env, 'res.partner', [('name', '=', 'Foo')])
        self.assertEqual(domain, Domain('name', '=', 'Foo'))

    def test_valid_string(self):
        domain = parse_domain(self.env, 'res.partner', "[('name', '=', 'Foo')]")
        self.assertEqual(domain, Domain('name', '=', 'Foo'))

    def test_invalid_field_name_in_message(self):
        with self.assertRaises(ValueError) as cm:
            parse_domain(self.env, 'res.partner', [('does_not_exist', '=', 'x')])
        self.assertIn('does_not_exist', str(cm.exception))
        self.assertTrue(str(cm.exception).startswith('Invalid domain:'))

    def test_invalid_operator(self):
        with self.assertRaises(ValueError):
            parse_domain(self.env, 'res.partner', [('name', 'not_an_operator', 'x')])

    def test_too_long(self):
        long_domain = "[('name', '=', '" + ('x' * 6000) + "')]"
        with self.assertRaises(ValueError):
            parse_domain(self.env, 'res.partner', long_domain)

    def test_too_deeply_nested(self):
        nested = [('name', '=', 'x')]
        for _ in range(25):
            nested = [nested]
        with self.assertRaises(ValueError):
            parse_domain(self.env, 'res.partner', nested)


class TestSerializeRecord(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': "Test Partner",
            'comment': '<p>Hello <b>world</b></p>',
        })

    def test_many2one_serialised_as_dict(self):
        child = self.env['res.partner'].create({'name': "Child", 'parent_id': self.partner.id})
        data = serialize_record(self.env, child, ['parent_id'])
        self.assertEqual(data['parent_id'], {'id': self.partner.id, 'display_name': self.partner.display_name})

    def test_many2one_false_is_none(self):
        data = serialize_record(self.env, self.partner, ['parent_id'])
        self.assertIsNone(data['parent_id'])

    def test_selection_has_label(self):
        data = serialize_record(self.env, self.partner, ['type'])
        self.assertEqual(data['type']['value'], self.partner.type)
        self.assertTrue(data['type']['label'])

    def test_html_field_becomes_plain_text(self):
        data = serialize_record(self.env, self.partner, ['comment'])
        self.assertIn('Hello', data['comment'])
        self.assertNotIn('<p>', data['comment'])
        self.assertNotIn('<b>', data['comment'])

    def test_binary_field_excluded(self):
        data = serialize_record(self.env, self.partner, ['image_1920'])
        self.assertNotIn('image_1920', data)

    def test_date_fields_are_iso(self):
        data = serialize_record(self.env, self.partner, ['create_date'])
        self.assertIsInstance(data['create_date'], str)
        self.assertTrue(data['create_date'][:4].isdigit())
        # ISO 8601 with a UTC offset, e.g. '2026-01-15T10:00:00+00:00'.
        self.assertIn('T', data['create_date'])
        self.assertTrue(data['create_date'][-6] in ('+', '-') or data['create_date'].endswith('Z'))

    def test_x2many_capped_with_sentinel(self):
        for i in range(25):
            self.env['res.partner'].create({'name': f"Child {i}", 'parent_id': self.partner.id})
        data = serialize_record(self.env, self.partner, ['child_ids'])
        self.assertEqual(len(data['child_ids']), 21)  # 20 items + sentinel
        self.assertEqual(data['child_ids'][-1], '…(+5 more)')

    def test_unknown_field_silently_skipped(self):
        data = serialize_record(self.env, self.partner, ['does_not_exist'])
        self.assertNotIn('does_not_exist', data)

    def test_result_is_json_serialisable(self):
        data = serialize_record(self.env, self.partner)
        json.dumps(data)  # must not raise

    def test_serialize_records_multiple(self):
        other = self.env['res.partner'].create({'name': "Other"})
        records = self.partner | other
        data = serialize_records(self.env, records, ['name'])
        self.assertEqual(len(data), 2)
        self.assertEqual({row['name'] for row in data}, {"Test Partner", "Other"})


class TestDescribeFields(TransactionCase):

    def test_excludes_message_and_binary_fields(self):
        described = describe_fields(self.env['res.partner'])
        names = {entry['name'] for entry in described}
        self.assertNotIn('message_ids', names)
        self.assertNotIn('image_1920', names)
        self.assertNotIn('activity_ids', names)

    def test_includes_selection_options(self):
        described = describe_fields(self.env['res.partner'])
        by_name = {entry['name']: entry for entry in described}
        self.assertIn('selection', by_name['type'])
        self.assertTrue(by_name['type']['selection'])

    def test_sorted_order(self):
        described = describe_fields(self.env['res.partner'], only=['id', 'display_name', 'name', 'email'])
        names = [entry['name'] for entry in described]
        self.assertEqual(names, ['id', 'display_name', 'name', 'email'])

    def test_include_help_toggle(self):
        without_help = describe_fields(self.env['res.partner'], only=['name'])
        with_help = describe_fields(self.env['res.partner'], only=['name'], include_help=True)
        self.assertNotIn('help', without_help[0])
        # help is only present in the dict if the field actually has one;
        # just check the include_help path doesn't error and returns a dict.
        self.assertIsInstance(with_help[0], dict)


class TestCompactJsonAndTruncate(TransactionCase):

    def test_compact_json_is_compact(self):
        self.assertEqual(compact_json({'a': 1, 'b': 2}), '{"a":1,"b":2}')

    def test_truncate_text_appends_note(self):
        result = truncate_text('x' * 10, 5)
        self.assertTrue(result.startswith('xxxxx'))
        self.assertIn('truncated 5 characters', result)

    def test_truncate_text_noop_under_limit(self):
        self.assertEqual(truncate_text('short', 100), 'short')


class TestOwAiDefaultFields(TransactionCase):

    def test_res_partner_default_fields(self):
        partner = self.env['res.partner'].create({'name': "Test"})
        names = partner._ow_ai_default_fields()
        self.assertLessEqual(len(names), 25)
        self.assertIn('name', names)
        self.assertIn('email', names)


class TestOwAiRecordContext(TransactionCase):

    def test_record_context_with_chatter(self):
        partner = self.env['res.partner'].create({'name': "Chatty"})
        partner.message_post(body="First comment", message_type='comment')
        partner.message_post(body="Second comment", message_type='comment')
        context = partner._ow_ai_record_context()
        self.assertEqual(context['model'], 'res.partner')
        self.assertEqual(context['id'], partner.id)
        self.assertTrue(context['url'].endswith(f'/odoo/res.partner/{partner.id}'))
        self.assertIsNotNone(context['chatter'])
        self.assertEqual(len(context['chatter']), 2)
        self.assertIn('Second comment', context['chatter'][0]['body'])
        self.assertIn('First comment', context['chatter'][1]['body'])


class TestAttachmentParts(TransactionCase):

    def test_png_resized(self):
        from PIL import Image
        data = _png_bytes(3000, 2000)
        attachment = self.env['ir.attachment'].create({
            'name': 'big.png',
            'raw': data,
            'mimetype': 'image/png',
        })
        parts = attachment._ow_ai_to_parts()
        self.assertEqual(len(parts), 1)
        part = parts[0]
        self.assertEqual(part['type'], 'inline_data')
        self.assertEqual(part['mimetype'], 'image/png')
        decoded = base64.b64decode(part['data'])
        img = Image.open(io.BytesIO(decoded))
        self.assertLessEqual(max(img.size), 1568)

    def test_pdf_returns_inline_data(self):
        pdf_bytes = b'%PDF-1.4\n%mock pdf content\n%%EOF'
        attachment = self.env['ir.attachment'].create({
            'name': 'doc.pdf',
            'raw': pdf_bytes,
            'mimetype': 'application/pdf',
        })
        parts = attachment._ow_ai_to_parts()
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]['type'], 'inline_data')
        self.assertEqual(parts[0]['mimetype'], 'application/pdf')
        self.assertEqual(base64.b64decode(parts[0]['data']), pdf_bytes)

    def test_text_attachment_with_index_content(self):
        content = b'Hello from a text file, this is searchable content.'
        attachment = self.env['ir.attachment'].create({
            'name': 'notes.txt',
            'raw': content,
            'mimetype': 'text/plain',
        })
        parts = attachment._ow_ai_to_parts()
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]['type'], 'text')
        self.assertIn('searchable content', parts[0]['text'])

    def test_unknown_binary_without_index_has_no_readable_text(self):
        attachment = self.env['ir.attachment'].create({
            'name': 'blob.bin',
            'raw': b'\x00\x01\x02\x03binarydata',
            'mimetype': 'application/octet-stream',
        })
        parts = attachment._ow_ai_to_parts()
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]['type'], 'text')
        self.assertIn('no readable text', parts[0]['text'])

    def test_access_denied_for_unrelated_user(self):
        attachment = self.env['ir.attachment'].create({
            'name': 'private.txt',
            'raw': b'secret',
            'mimetype': 'text/plain',
        })
        portal = new_test_user(self.env, login='ow_ai_attach_portal', groups='base.group_portal')
        with self.assertRaises(AccessError):
            attachment.with_user(portal)._ow_ai_to_parts()
