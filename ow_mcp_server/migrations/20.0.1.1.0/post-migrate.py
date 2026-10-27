"""Grant "MCP User" to the internal users who already use MCP.

From this version on /mcp and the OAuth endpoints require an active internal
user in group_mcp_user; before, the group was defined but never checked. So
existing integrations keep working, every active internal user who left a
trace of MCP use (an `mcp` API key, an OAuth token or consent, an audit row)
is added to the group. Portal and public users are not.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# (table, user column, extra condition)
_SOURCES = [
    ('res_users_apikeys', 'user_id', "scope = 'mcp'"),
    ('ow_mcp_audit_log', 'user_id', None),
    ('ow_mcp_oauth_access_token', 'user_id', None),
    ('ow_mcp_oauth_refresh_token', 'user_id', None),
    ('ow_mcp_oauth_consent', 'user_id', None),
]


def migrate(cr, version):
    if not version:
        return
    user_ids = set()
    for table, column, condition in _SOURCES:
        cr.execute('SELECT to_regclass(%s)', (table,))
        if not cr.fetchone()[0]:
            continue
        where = f'WHERE {column} IS NOT NULL'
        if condition:
            where += f' AND {condition}'
        cr.execute(f'SELECT DISTINCT {column} FROM {table} {where}')
        user_ids.update(row[0] for row in cr.fetchall())
    if not user_ids:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    users = env['res.users'].browse(sorted(user_ids)).exists().filtered(
        lambda u: u.active and not u.share
    )
    group = env.ref('ow_mcp_server.group_mcp_user')
    field = 'user_ids' if 'user_ids' in group._fields else 'users'
    users -= group[field]
    if not users:
        return
    group.write({field: [(4, uid) for uid in users.ids]})
    _logger.info(
        'ow_mcp_server: granted "MCP User" to %d existing MCP user(s): %s',
        len(users), ', '.join(users.mapped('login')),
    )
