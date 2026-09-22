from datetime import datetime, timezone

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

ALLOWED_CONTACT_SCHEMES = ('mailto:', 'https://', 'http://', 'tel:')

# (security.txt field label, ir.config_parameter key) for the optional fields
OPTIONAL_FIELDS = [
    ('Encryption', 'ow_security_txt.encryption'),
    ('Acknowledgments', 'ow_security_txt.acknowledgments'),
    ('Preferred-Languages', 'ow_security_txt.preferred_languages'),
    ('Canonical', 'ow_security_txt.canonical'),
    ('Policy', 'ow_security_txt.policy'),
    ('Hiring', 'ow_security_txt.hiring'),
    ('CSAF', 'ow_security_txt.csaf'),
]


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Text is not supported by config_parameter=, so this field is
    # persisted manually in get_values()/set_values().
    ow_security_txt_contact = fields.Text(
        string='Contact',
        help='One contact per line: an email address, an https:// URL or a '
             'tel: number. Bare email addresses are turned into mailto: URIs '
             'automatically. Required for the file to be served.',
    )
    ow_security_txt_expires_months = fields.Integer(
        string='Expires After (Months)',
        default=6,
        config_parameter='ow_security_txt.expires_months',
        help='The Expires field is computed on every request as now plus '
             'this many months (RFC 9116 recommends less than a year).',
    )
    ow_security_txt_encryption = fields.Char(
        string='Encryption', config_parameter='ow_security_txt.encryption',
        help='URL of your PGP key, e.g. https://example.com/pgp-key.txt')
    ow_security_txt_acknowledgments = fields.Char(
        string='Acknowledgments', config_parameter='ow_security_txt.acknowledgments',
        help='URL of the page where security researchers are recognized.')
    ow_security_txt_preferred_languages = fields.Char(
        string='Preferred Languages', config_parameter='ow_security_txt.preferred_languages',
        help='Comma-separated language tags, e.g. "en, nl"')
    ow_security_txt_canonical = fields.Char(
        string='Canonical URL', config_parameter='ow_security_txt.canonical',
        help='Canonical URI of this security.txt file.')
    ow_security_txt_policy = fields.Char(
        string='Policy', config_parameter='ow_security_txt.policy',
        help='URL of your vulnerability disclosure policy.')
    ow_security_txt_hiring = fields.Char(
        string='Hiring', config_parameter='ow_security_txt.hiring',
        help='URL of your security-related job openings.')
    ow_security_txt_csaf = fields.Char(
        string='CSAF', config_parameter='ow_security_txt.csaf',
        help='URL of your CSAF provider-metadata.json.')

    def get_values(self):
        res = super().get_values()
        res['ow_security_txt_contact'] = self.env['ir.config_parameter'].sudo(
            ).get_param('ow_security_txt.contact', '')
        return res

    def set_values(self):
        super().set_values()
        self.env['ir.config_parameter'].sudo().set_param(
            'ow_security_txt.contact', self.ow_security_txt_contact or '')

    @api.model
    def _ow_security_txt_parse_contacts(self, raw):
        """Split the multiline contact value; prefix bare emails with mailto:."""
        contacts = []
        for line in (raw or '').splitlines():
            line = line.strip()
            if not line:
                continue
            if '@' in line and ':' not in line:
                line = 'mailto:%s' % line
            contacts.append(line)
        return contacts

    @api.constrains('ow_security_txt_contact')
    def _check_ow_security_txt_contact(self):
        for settings in self:
            for contact in self._ow_security_txt_parse_contacts(settings.ow_security_txt_contact):
                if not contact.startswith(ALLOWED_CONTACT_SCHEMES):
                    raise ValidationError(_(
                        'Invalid security.txt contact "%s": each line must be '
                        'an email address or a mailto:, https:// or tel: URI.',
                        contact))

    @api.constrains('ow_security_txt_expires_months')
    def _check_ow_security_txt_expires_months(self):
        for settings in self:
            if not 1 <= settings.ow_security_txt_expires_months <= 12:
                raise ValidationError(_(
                    'Expires After (Months) must be between 1 and 12: '
                    'RFC 9116 recommends an expiry less than a year away.'))

    @api.model
    def _render_ow_security_txt(self):
        """Build the security.txt body, or return False when unconfigured."""
        icp = self.env['ir.config_parameter'].sudo()
        contacts = self._ow_security_txt_parse_contacts(
            icp.get_param('ow_security_txt.contact'))
        if not contacts:
            return False
        months = int(icp.get_param('ow_security_txt.expires_months') or 6)
        expires = datetime.now(timezone.utc) + relativedelta(months=months)
        lines = ['Contact: %s' % contact for contact in contacts]
        lines.append('Expires: %s' % expires.strftime('%Y-%m-%dT%H:%M:%SZ'))
        for label, param in OPTIONAL_FIELDS:
            value = (icp.get_param(param) or '').strip()
            if value:
                lines.append('%s: %s' % (label, value))
        return '\n'.join(lines) + '\n'
