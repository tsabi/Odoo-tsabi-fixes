# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models, fields, _
from odoo.exceptions import UserError
from odoo.addons.l10n_hu_edi.models.l10n_hu_edi_connection import L10nHuEdiConnectionError


L10N_HU_EDI_SERVER_MODE_SELECTION = [
    ('production', 'Production'),
    ('test', 'Test'),
]


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
    l10n_hu_edi_server_mode = fields.Selection(
        selection=L10N_HU_EDI_SERVER_MODE_SELECTION,
        string='Server Mode',
    )
    l10n_hu_edi_username = fields.Char(
        string='Username',
        groups='base.group_system',
    )
    l10n_hu_edi_password = fields.Char(
        string='Password',
        groups='base.group_system',
    )
    l10n_hu_edi_signature_key = fields.Char(
        string='Signature Key',
        groups='base.group_system',
    )
    l10n_hu_edi_replacement_key = fields.Char(
        string='Replacement Key',
        groups='base.group_system',
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

    def _l10n_hu_edi_get_credentials_dict(self):
        self.ensure_one()
        credentials_dict = {
            'vat': self.vat,
            'mode': self.l10n_hu_edi_server_mode,
            'username': self.l10n_hu_edi_username,
            'password': self.l10n_hu_edi_password,
            'signature_key': self.l10n_hu_edi_signature_key,
            'replacement_key': self.l10n_hu_edi_replacement_key,
        }
        if any(not v for v in credentials_dict.values()):
            raise UserError(_('Missing NAV credentials for company %s', self.name))
        return credentials_dict

    def _l10n_hu_edi_test_credentials(self):
        for company in self:
            if not company.vat:
                raise UserError(_('NAV Credentials: Please set the hungarian vat number on the company first!'))
            try:
                self.env['l10n_hu_edi.connection']._do_token_exchange(company._l10n_hu_edi_get_credentials_dict())
            except L10nHuEdiConnectionError as e:
                raise UserError(
                    _('Incorrect NAV Credentials!') + '\n'
                    + _('Check that your company VAT number is set correctly.') + '\n\n'
                    + str(e)
                )
