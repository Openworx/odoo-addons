odoo.define('ow_remove_ent_settings.hide_ent', function(require) {
	"use strict";
	var FormRenderer = require('web.FormRenderer');
	FormRenderer.include({
		_renderTagForm: function(node) {
			var $result = this._super(node);
			if (this.state && this.state.model === "res.config.settings") {
				$result.find('.o_enterprise_label').parent().parent().parent().hide();
			}
			return $result;
		},
	});
});
