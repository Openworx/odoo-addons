from odoo import http
from odoo.http import request


class SecurityTxtController(http.Controller):

    @http.route('/.well-known/security.txt', type='http', auth='public',
                methods=['GET'])
    def ow_security_txt(self, **kwargs):
        content = request.env['res.config.settings'].sudo()._render_ow_security_txt()
        if not content:
            return request.not_found()
        return request.make_response(content, headers=[
            ('Content-Type', 'text/plain; charset=utf-8'),
            ('Cache-Control', 'public, max-age=3600'),
        ])

    @http.route('/security.txt', type='http', auth='public', methods=['GET'])
    def ow_security_txt_legacy(self, **kwargs):
        return request.redirect('/.well-known/security.txt', code=301)
