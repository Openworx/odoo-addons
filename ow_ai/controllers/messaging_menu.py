# -*- coding: utf-8 -*-
"""Adds the ``ow-ai-chat`` tab to the messaging menu (mail's systray bell).

Mirrors ``im_livechat``'s own tab (``controllers/messaging_menu.py``):
override ``_get_menu_tab_domain``/``_get_menu_tab_filter_domain``, matching
this module's tab id and falling back to ``super()`` otherwise.
"""
from __future__ import annotations

from odoo.addons.mail.controllers.discuss.messaging_menu import DiscussMessagingMenuController
from odoo.fields import Domain


class OwAiMessagingMenuController(DiscussMessagingMenuController):

    def _get_menu_tab_domain(self, tab_id):
        if tab_id != 'ow-ai-chat':
            return super()._get_menu_tab_domain(tab_id)
        return Domain([
            ('channel_type', '=', 'ow_ai_chat'),
            ('self_member_id.is_pinned', '=', True),
        ])

    def _get_menu_tab_filter_domain(self, tab_id, filter_id):
        if tab_id == 'ow-ai-chat' and filter_id == 'ow-ai-chat-unread':
            return Domain('self_member_id.is_unread', '=', True)
        return super()._get_menu_tab_filter_domain(tab_id, filter_id)
