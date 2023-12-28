# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, models, fields, _
from odoo.exceptions import UserError

import base64
import tempfile
import zipfile
import contextlib

class L10nHuEdiTaxAuditExport(models.TransientModel):
    _name = 'l10n_hu_edi.tax_audit_export'
    _description = 'Tax Audit Export Wizard'

    date_from = fields.Date(
        string='Date From'
    )
    date_to = fields.Date(
        string='Date To'
    )
    filename = fields.Char(
        string='File name',
        compute='_compute_filename'
    )
    export_file = fields.Binary(
        string='Generated File',
        readonly=True
    )

    @api.depends('date_from', 'date_to')
    def _compute_filename(self):
        date_from = self.date_from
        if not date_from:
            first_invoice = self.env['account.move'].search(
                [
                    ('move_type', 'in', ('out_invoice', 'out_refund')),
                    ('state', '=', 'posted'),
                    ('country_code', '=', 'HU'),
                ],
                order='date',
                limit=1,
            )
            date_from = first_invoice.date
        date_to = self.date_to or fields.Date.today()
        self.filename = f'export_{date_from}_{date_to}.zip'

    def action_export(self):
        self.ensure_one()
        domain = [
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('state', '=', 'posted'),
            ('country_code', '=', 'HU'),
        ]
        if self.date_from:
            domain.append(('date', '>=', self.date_from))
        if self.date_to:
            domain.append(('date', '<=', self.date_to))

        invoices = self.env['account.move'].search(domain)
        if not invoices:
            raise UserError(_('No invoice to export!'))

        with tempfile.NamedTemporaryFile(mode='w+b', delete=True) as buf:
            with zipfile.ZipFile(buf, mode='w', compression=zipfile.ZIP_DEFLATED, allowZip64=False) as zf:
                # We might need to set the chain index and line numbers on invoices created before l10n_hu_edi was installed.
                # We do this temporarily for the purposes of the data export.
                with contextlib.closing(self.env.cr.savepoint(flush=False)):
                    for invoice in invoices.sorted(lambda i: i.create_date):
                        if not invoice.l10n_hu_invoice_chain_index:
                            invoice._l10n_hu_edi_set_chain_index_and_line_number()
                        filename = f'{invoice.name.replace("/", "_")}.xml'
                        zf.writestr(filename, invoice._l10n_hu_edi_generate_xml())
            buf.seek(0)
            self.export_file = base64.b64encode(buf.read())

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'view_mode': 'form',
            'res_id': self.id,
            'views': [(False, 'form')],
            'target': 'new',
        }
