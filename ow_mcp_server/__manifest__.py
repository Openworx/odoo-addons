{
    'name': 'OW MCP Server',
    'version': '18.0.1.0.2',
    'summary': 'Native MCP (Model Context Protocol) server for Odoo — Claude, Cursor, Copilot.',
    'description': """
Native Model Context Protocol (MCP) server for Odoo 18.0.
===========================================================

Serves MCP JSON-RPC 2.0 directly from Odoo on /mcp, so MCP-compatible clients
(Claude Code, Claude Desktop, Cursor, Copilot) can connect without any external
Python dependency or bridge library.

Tools (11): list_models, search_records, get_record, create_record,
update_record, delete_record, search_count, get_model_schema, read_group,
get_user_context, list_modules. Multi-company switching via per-call
company_id argument. Authentication via Odoo's built-in User API Keys
(scope: mcp).
""",
    'category': 'Technical',
    'license': 'LGPL-3',
    'author': 'Openworx',
    'website': 'https://www.openworx.nl',
    'depends': ['base', 'base_setup'],
    'data': [
        'security/mcp_security.xml',
        'security/ir.model.access.csv',
        'data/mcp_config_data.xml',
        'data/ir_cron.xml',
        'views/res_config_settings_views.xml',
        'views/mcp_model_access_views.xml',
        'views/mcp_audit_log_views.xml',
        'views/mcp_menus.xml',
    ],
    'installable': True,
    'application': True,
}
