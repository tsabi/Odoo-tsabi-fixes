# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models, fields


class ResCompany(models.Model):
    _inherit = 'res.company'

    l10n_hu_group_vat = fields.Char(
        related='partner_id.l10n_hu_group_vat',
        readonly=False,
    )
    l10n_hu_tax_regime = fields.Selection(
        selection=[
            ('ie', 'Individual Exemption'),
            ('ca', 'Cash Accounting'),
            ('sb', 'Small Business'),
        ],
        string='Hungarian Tax Regime',
    )
    l10n_hu_edi_credentials_ids = fields.One2many(
        comodel_name='l10n_hu_edi.credentials',
        inverse_name='company_id',
        string='NAV Credentials',
    )
    l10n_hu_edi_primary_credentials_id = fields.Many2one(
        comodel_name='l10n_hu_edi.credentials',
        string='Primary NAV Credentials',
    )

    def _l10n_hu_edi_configure_company(self):
        """ Single-time configuration for companies, to be applied when l10n_hu_edi is installed
        or a new company is created.
        """
        for company in self:
            # Set profit/loss accounts on cash rounding method
            profit_account = self.env['account.chart.template'].with_company(company).ref('l10n_hu_969', raise_if_not_found=False)
            loss_account = self.env['account.chart.template'].with_company(company).ref('l10n_hu_869', raise_if_not_found=False)
            rounding_method = self.env.ref('l10n_hu_edi.cash_rounding_1_huf', raise_if_not_found=False)
            if profit_account and loss_account and rounding_method:
                rounding_method.with_company(company).write({
                    'profit_account_id': profit_account.id,
                    'loss_account_id': loss_account.id,
                })

            # Activate cash rounding on the company
            res_config_id = self.env['res.config.settings'].create({
                'company_id': company.id,
                'group_cash_rounding': True,
            })
            res_config_id.execute()

    def write(self, vals):
        # If we change the company VAT, we should deactivate any credentials with a different VAT number
        if 'vat' in vals:
            self.l10n_hu_edi_credentials_ids.sudo().filtered(lambda c: c.vat != vals['vat']).is_active = False
        return super().write(vals)
