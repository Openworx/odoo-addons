"""Changing a user's password or archiving
the user revokes their OAuth tokens (the old code left them valid: refresh
tokens rotated forever after a password reset)."""
from odoo import models


class ResUsers(models.Model):
    _inherit = 'res.users'

    def write(self, vals):
        res = super().write(vals)
        if 'password' in vals or ('active' in vals and not vals['active']):
            self.env['ow.mcp.oauth.refresh_token'].sudo().revoke_for_users(
                self.ids,
            )
        return res
