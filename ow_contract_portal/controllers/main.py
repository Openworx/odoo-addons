# -*- coding: utf-8 -*-
# Copyright 2019 Openworx
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).


import datetime

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal


class CustomerPortal(CustomerPortal):

    def _prepare_portal_layout_values(self):

        values = super(CustomerPortal, self)._prepare_portal_layout_values()
        contract_count = request.env['account.analytic.account'].search_count([])
        values['contract_count'] = contract_count
        return values

    @http.route(['/my/contract', '/my/contract/page/<int:page>'], type='http', auth="user", website=True)
    def portal_my_home_contract(self, page=1):
        values = self._prepare_portal_layout_values()
        partner = request.env.user.partner_id
        commercial_partner = request.env.user.commercial_partner_id.id
        ResContracts = request.env['account.analytic.account']

        domain = [
            ('partner_id', '=', commercial_partner)
        ]

        contract_count = ResContracts.search_count(domain)

        pager = request.website.pager(
            url="/my/contract",
            total=contract_count,
            page=page,
            step=self._items_per_page
        )

        contracts = ResContracts.search(domain, limit=self._items_per_page, offset=pager['offset'])

        values.update({
            'contracts': contracts,
            'page_name': 'contract',
            'pager': pager,
            'default_url': '/my/contract',
        })
        return request.render("ow_contract_portal.contracts", values)

    @http.route(['/my/contract/<int:contract>'], type='http', auth="user", website=True)
    def contract_page(self, contract=None):
        contract = request.env['account.analytic.account'].browse([contract])
        try:
            contract.check_access_rights('read')
            contract.check_access_rule('read')
        except AccessError:
                return request.website.render("website.403")



        return request.render("ow_contract_portal.contract_page",{
            'contract': contract.sudo(),
        })
