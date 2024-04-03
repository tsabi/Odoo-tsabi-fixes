from odoo import models

from odoo.addons.account.models.chart_template import template
from odoo.addons.l10n_hu_edi.models.account_tax import _DEFAULT_TAX_REASONS

class AccountChartTemplate(models.AbstractModel):
    _inherit = 'account.chart.template'

    def _load(self, template_code, company, install_demo):
        res = super()._load(template_code, company, install_demo)
        if template_code == 'hu':
            company._l10n_hu_edi_configure_company()
        return res

    @template('hu', 'account.tax')
    def _get_hu_account_tax(self):
        data = self._parse_csv('hu', 'account.tax', module='l10n_hu_edi')
        for vals in data.values():
            vals['l10n_hu_tax_reason'] = _DEFAULT_TAX_REASONS.get(vals['l10n_hu_tax_type'], False)
        return data
