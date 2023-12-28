# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.addons.l10n_hu_edi.models.l10n_hu_edi_connection import L10nHuEdiConnectionError


_PROTECTED_FIELDS = {'mode', 'vat', 'username', 'password', 'signature_key', 'replacement_key'}


class L10nHuEdiCredentials(models.Model):
    _name = 'l10n_hu_edi.credentials'
    _description = 'Hungarian TAX Authority Login Credentials'
    _rec_name = 'username'
    _order = 'is_active desc'

    # === User interface fields === #
    is_active = fields.Boolean(
        string='Active',
        default=True,
    )
    is_primary = fields.Boolean(
        string='Is Primary',
        compute='_compute_is_primary',
    )

    # === Data fields === #
    mode = fields.Selection(
        selection=[
            ('production', 'Production'),
            ('test', 'Test'),
        ],
        required=True,
        string='Server Mode',
    )
    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
        ondelete='cascade',
    )
    vat = fields.Char(
        string='VAT Number',
        default=lambda self: self.env.company.vat,
        store=True,
        required=True,
    )
    username = fields.Char(
        string='Username',
        required=True,
    )
    password = fields.Char(
        string='Password',
        required=True,
    )
    signature_key = fields.Char(
        string='Signature Key',
        required=True,
    )
    replacement_key = fields.Char(
        string='Replacement Key',
        required=True
    )
    transaction_ids = fields.One2many(
        comodel_name='l10n_hu_edi.transaction',
        inverse_name='credentials_id',
        string='Transactions',
    )

    @api.depends('company_id', 'company_id.l10n_hu_edi_primary_credentials_id')
    def _compute_is_primary(self):
        for credentials in self:
            credentials.is_primary = credentials == credentials.company_id.l10n_hu_edi_primary_credentials_id

    def button_set_primary(self):
        self.ensure_one()
        if not self.is_active:
            raise UserError(_("You cannot set non-active credentials as a company's primary credentials!"))
        self.company_id.l10n_hu_edi_primary_credentials_id = self

    @api.model_create_multi
    def create(self, vals_list):
        credentials = super().create(vals_list)
        credentials.test()
        for cred in credentials:
            if not cred.company_id.l10n_hu_edi_primary_credentials_id:
                cred.company_id.l10n_hu_edi_primary_credentials_id = cred
        return credentials

    def write(self, vals):
        if set(vals.keys()) & _PROTECTED_FIELDS and self.transaction_ids:
            raise UserError(_('You cannot modify credentials already used in a transaction. Deactivate them instead.'))
        if vals.get('is_active'):
            self.test()
        if vals.get('is_active') is False:
            self.filtered(lambda c: c.is_primary).mapped('company_id').l10n_hu_edi_primary_credentials_id = False
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_used(self):
        if self.mode == 'production' and self.transaction_ids:
            raise UserError(_('You cannot delete production credentials already used in a transaction. Deactivate them instead.'))

    def test(self):
        for credentials in self:
            if not credentials.vat:
                raise UserError(_('NAV Credentials: Please set the hungarian vat number on the company first!'))
            try:
                self.env['l10n_hu_edi.connection'].do_token_exchange(credentials)
            except L10nHuEdiConnectionError as e:
                raise UserError(_('NAV Credentials: Error from NAV: %s', e))
